"""Run one tool-using critic chunk against a ModelExecutor."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Any

from ydbdoc_review_ng.domain import ModelRole
from ydbdoc_review_ng.models import ModelCallResult, ModelRequest, ModelToolCall
from ydbdoc_review_ng.quality.tool_loop import (
    CRITIC_TOOLS,
    CriticToolLoop,
    LoopFailureReason,
    ProtocolError,
    ToolCall,
    ToolLoopResult,
)
from ydbdoc_review_ng.quality.workspace import CriticWorkspace, ToolError, ToolErrorReason


def _max_tool_turns(environment: Mapping[str, str] | None) -> int:
    raw = "" if environment is None else environment.get("YDBDOC_CRITIC_MAX_TOOL_TURNS", "")
    raw = raw.strip() if raw else ""
    if not raw:
        return 12
    try:
        value = int(raw)
    except ValueError:
        return 12
    return value if value >= 1 else 12


def _workspace_for_chunk(
    *,
    source_path: str,
    target_path: str,
    source_bytes: bytes,
    draft_bytes: bytes | None,
    glossary_files: Mapping[str, bytes],
    presentation_reference: bytes | None,
    toc_snapshots: Mapping[str, Mapping[str, str | None]] | None,
) -> CriticWorkspace:
    read_only: dict[str, bytes] = {source_path: source_bytes}
    for path, content in glossary_files.items():
        read_only[path] = content
    if presentation_reference is not None:
        read_only[f"presentation-reference/{target_path}"] = presentation_reference
    if toc_snapshots:
        for path, snapshot in toc_snapshots.items():
            before = snapshot.get("before")
            after = snapshot.get("after")
            if before is not None:
                read_only[f"toc-snapshot/{path}/before"] = before.encode("utf-8")
            if after is not None:
                read_only[f"toc-snapshot/{path}/after"] = after.encode("utf-8")
    return CriticWorkspace(
        writable_path=target_path,
        writable_bytes=b"" if draft_bytes is None else draft_bytes,
        read_only=read_only,
    )


def build_critic_tool_request(
    *,
    model: str,
    messages: list[dict[str, Any]],
    max_output_tokens: int = 4096,
    tool_choice: str | None = "auto",
    prompt: str = "critic-tool-turn",
    developer_prompt: str | None = None,
) -> ModelRequest:
    return ModelRequest(
        ModelRole.CRITIC,
        model,
        prompt,
        None,
        developer_prompt=developer_prompt,
        max_output_tokens=max_output_tokens,
        tools=CRITIC_TOOLS,
        messages=messages,
        tool_choice=tool_choice,
    )


def initial_critic_messages(
    *,
    developer_prompt: str,
    user_prompt: str,
) -> list[dict[str, Any]]:
    return [
        {"role": "developer", "content": developer_prompt},
        {"role": "user", "content": user_prompt},
    ]


def _to_tool_calls(result: ModelCallResult) -> list[ToolCall]:
    if result.failure is not None:
        raise ProtocolError(LoopFailureReason.TOOL_ERROR, result.failure.value)
    if not result.tool_calls:
        raise ProtocolError(LoopFailureReason.PROTOCOL, "missing tool_calls")
    return [
        ToolCall(call.id, call.name, call.arguments)
        for call in result.tool_calls
    ]


def run_tool_critic_chunk(
    invoke: Callable[[ModelRequest], ModelCallResult],
    *,
    model: str,
    source_path: str,
    target_path: str,
    source_bytes: bytes,
    draft_bytes: bytes | None,
    glossary_files: Mapping[str, bytes],
    developer_prompt: str,
    user_prompt: str,
    validate_files: Callable[[Mapping[str, bytes]], None],
    presentation_reference: bytes | None = None,
    toc_snapshots: Mapping[str, Mapping[str, str | None]] | None = None,
    after_patch: Callable[[bytes], None] | None = None,
    environment: Mapping[str, str] | None = None,
    before_model_call: Callable[[], None] | None = None,
) -> ToolLoopResult:
    """Run up to two full tool sessions; publish workspace bytes on success."""

    max_turns = _max_tool_turns(environment)

    def workspace_factory() -> CriticWorkspace:
        return _workspace_for_chunk(
            source_path=source_path,
            target_path=target_path,
            source_bytes=source_bytes,
            draft_bytes=draft_bytes,
            glossary_files=glossary_files,
            presentation_reference=presentation_reference,
            toc_snapshots=toc_snapshots,
        )

    initial = initial_critic_messages(
        developer_prompt=developer_prompt, user_prompt=user_prompt
    )

    def complete(
        messages: list[dict[str, object]], tools: list[dict[str, object]]
    ) -> Any:
        del tools
        if before_model_call is not None:
            before_model_call()
        request = build_critic_tool_request(
            model=model,
            messages=messages,
            prompt=user_prompt,
            developer_prompt=developer_prompt,
        )
        result = invoke(request)
        # Attach tool_calls onto a tiny namespace object for CriticToolLoop.
        class _Turn:
            def __init__(self, calls: list[ToolCall]) -> None:
                self.tool_calls = calls

        return _Turn(_to_tool_calls(result))

    # Wrap CriticToolLoop to inject after_patch validation on apply_patch.
    class _ValidatingLoop(CriticToolLoop):
        def _dispatch(self, call: ToolCall, read_windows: list[Any]) -> Any:
            result = super()._dispatch(call, read_windows)
            if call.name == "apply_patch" and after_patch is not None:
                try:
                    after_patch(self.workspace.writable_bytes())
                except ToolError as error:
                    raise ProtocolError(
                        LoopFailureReason.TOOL_ERROR,
                        error.detail or error.reason.value,
                    ) from error
            return result

    last_failure = LoopFailureReason.NO_FINISH
    last_detail = ""
    for _attempt in range(2):
        workspace = workspace_factory()
        loop = _ValidatingLoop(workspace, max_tool_turns=max_turns)
        messages: list[dict[str, object]] = list(initial)
        try:
            while not loop.finished:
                turn = complete(messages, CRITIC_TOOLS)
                tool_calls = list(turn.tool_calls)
                messages.append(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": call.id,
                                "type": "function",
                                "function": {
                                    "name": call.name,
                                    "arguments": call.arguments,
                                },
                            }
                            for call in tool_calls
                        ],
                    }
                )
                results = loop.handle_turn(tool_calls)
                for item in results:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": item.tool_call_id,
                            "content": item.content,
                        }
                    )
            reviewed = {target_path: workspace.writable_bytes()}
            validate_files(reviewed)
            return ToolLoopResult(ok=True, reviewed_bytes=workspace.writable_bytes())
        except ProtocolError as error:
            last_failure = error.reason
            last_detail = error.detail
            continue
        except (ToolError, ValueError, TypeError, UnicodeError) as error:
            # Do not catch RuntimeError: PersistenceError and other infra failures
            # must abort the job, not convert into an unreviewed RED retry.
            last_failure = LoopFailureReason.TOOL_ERROR
            last_detail = str(error)
            continue
    return ToolLoopResult(
        ok=False,
        reviewed_bytes=None,
        failure_reason=last_failure,
        detail=last_detail,
    )


def toc_target_only_guard(
    *,
    source_after: bytes,
    protected_refs: frozenset[str],
) -> Callable[[bytes], None]:
    """Reject patches that drop target-only TOC refs (REQUIREMENTS §3.6)."""

    def _check(candidate: bytes) -> None:
        from ydbdoc_review_ng.toc_delta import (
            TocDeltaError,
            target_only_toc_references,
        )

        try:
            remaining = set(target_only_toc_references(source_after, candidate))
        except (TocDeltaError, UnicodeDecodeError) as error:
            raise ToolError(ToolErrorReason.INVALID_PATCH, "toc parse") from error
        missing = protected_refs - remaining
        if missing:
            raise ToolError(
                ToolErrorReason.INVALID_PATCH,
                f"toc target-only removed: {sorted(missing)}",
            )

    return _check


def model_tool_call_result(*calls: ModelToolCall) -> ModelCallResult:
    return ModelCallResult(None, None, (), calls)


def env_max_tool_turns() -> int:
    return _max_tool_turns(os.environ)
