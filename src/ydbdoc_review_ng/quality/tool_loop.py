"""Tool-using critic session FSM: turn budget, pending re-read, retry."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ydbdoc_review_ng.quality.workspace import (
    CriticWorkspace,
    LineRange,
    ToolError,
)


class LoopFailureReason(str, Enum):
    PROTOCOL = "protocol"
    TURN_BUDGET = "turn_budget"
    INVALID_TOOL = "invalid_tool"
    TOOL_ERROR = "tool_error"
    NO_FINISH = "no_finish"


class ProtocolError(RuntimeError):
    def __init__(self, reason: LoopFailureReason, detail: str = "", /) -> None:
        self.reason = reason
        self.detail = detail
        suffix = f": {detail}" if detail else ""
        super().__init__(f"critic_protocol:{reason.value}{suffix}")


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class ToolResult:
    tool_call_id: str
    name: str
    content: str


@dataclass(frozen=True, slots=True)
class ToolLoopResult:
    ok: bool
    reviewed_bytes: bytes | None
    failure_reason: LoopFailureReason | None = None
    detail: str = ""


CRITIC_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read",
            "description": "Read a 1-based inclusive line window from a workspace path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
                "required": ["path", "start_line", "end_line"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search for a literal pattern in workspace path(s).",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": (
                "Apply a unified-diff hunk patch to the writable draft target. "
                "One apply_patch per assistant turn; covering read required after."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "patch": {"type": "string"},
                },
                "required": ["path", "patch"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": (
                "Finish the critic session after all pending re-reads are covered. "
                "Reviewed bytes are the workspace writable file."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
]


def _parse_args(raw: str) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ProtocolError(LoopFailureReason.INVALID_TOOL, "bad json args") from error
    if type(value) is not dict:
        raise ProtocolError(LoopFailureReason.INVALID_TOOL, "args not object")
    return value


def _ranges_covered(pending: list[LineRange], reads: list[LineRange]) -> list[LineRange]:
    remaining: list[LineRange] = []
    for need in pending:
        if any(window.covers(need) for window in reads):
            continue
        remaining.append(need)
    return remaining


def _tool_error_result(call: ToolCall, error: ToolError) -> ToolResult:
    """Return recoverable workspace failures to the model; do not abort the session.

    FSM protocol violations stay as ProtocolError. ToolError from read/grep/patch
    (bad bounds, bad hunk, RO mount, etc.) is a normal tool payload so the model
    can correct itself within the turn budget.
    """
    return ToolResult(
        call.id,
        call.name,
        json.dumps(
            {"ok": False, "error": error.reason.value, "detail": error.detail},
            ensure_ascii=False,
        ),
    )


@dataclass
class CriticToolLoop:
    workspace: CriticWorkspace
    max_tool_turns: int = 32
    _pending: list[LineRange] = field(default_factory=list)
    _turns_used: int = 0
    finished: bool = False
    had_successful_patch: bool = False

    def handle_turn(self, tool_calls: list[ToolCall]) -> list[ToolResult]:
        if self.finished:
            raise ProtocolError(LoopFailureReason.PROTOCOL, "already finished")
        if self._turns_used >= self.max_tool_turns:
            raise ProtocolError(LoopFailureReason.TURN_BUDGET, "max tool turns")
        if not tool_calls:
            raise ProtocolError(LoopFailureReason.PROTOCOL, "empty tool_calls")
        self._turns_used += 1

        names = [call.name for call in tool_calls]
        if "apply_patch" in names and len(tool_calls) != 1:
            raise ProtocolError(
                LoopFailureReason.PROTOCOL, "apply_patch must be alone in a turn"
            )
        if "finish" in names and len(tool_calls) != 1:
            raise ProtocolError(LoopFailureReason.PROTOCOL, "finish must be alone")

        if self._pending and any(name != "read" for name in names):
            raise ProtocolError(
                LoopFailureReason.PROTOCOL,
                "only read allowed while pending_reread",
            )

        results: list[ToolResult] = []
        read_windows: list[LineRange] = []
        for call in tool_calls:
            results.append(self._dispatch(call, read_windows))

        if self._pending and read_windows:
            self._pending = _ranges_covered(self._pending, read_windows)
        return results

    def _dispatch(self, call: ToolCall, read_windows: list[LineRange]) -> ToolResult:
        if call.name == "read":
            args = _parse_args(call.arguments)
            path = args.get("path")
            start = args.get("start_line")
            end = args.get("end_line")
            if type(path) is not str or type(start) is not int or type(end) is not int:
                raise ProtocolError(LoopFailureReason.INVALID_TOOL, "read args")
            try:
                content = self.workspace.read(path, start_line=start, end_line=end)
            except ToolError as error:
                return _tool_error_result(call, error)
            read_windows.append(LineRange(start, end))
            return ToolResult(call.id, "read", content)

        if call.name == "grep":
            args = _parse_args(call.arguments)
            pattern = args.get("pattern")
            path = args.get("path")
            if type(pattern) is not str:
                raise ProtocolError(LoopFailureReason.INVALID_TOOL, "grep args")
            if path is not None and type(path) is not str:
                raise ProtocolError(LoopFailureReason.INVALID_TOOL, "grep path")
            try:
                hits = self.workspace.grep(pattern, path=path if type(path) is str else None)
            except ToolError as error:
                return _tool_error_result(call, error)
            payload = [{"path": p, "line": n, "snippet": s} for p, n, s in hits]
            return ToolResult(call.id, "grep", json.dumps(payload, ensure_ascii=False))

        if call.name == "apply_patch":
            if self._pending:
                raise ProtocolError(
                    LoopFailureReason.PROTOCOL, "apply_patch while pending_reread"
                )
            args = _parse_args(call.arguments)
            path = args.get("path")
            patch = args.get("patch")
            if type(path) is not str or type(patch) is not str:
                raise ProtocolError(LoopFailureReason.INVALID_TOOL, "apply_patch args")
            try:
                touched = self.workspace.apply_patch(path, patch)
            except ToolError as error:
                return _tool_error_result(call, error)
            self._pending.extend(touched)
            self.had_successful_patch = True
            return ToolResult(
                call.id,
                "apply_patch",
                json.dumps(
                    {
                        "ok": True,
                        "touched": [{"start": r.start, "end": r.end} for r in touched],
                    },
                    ensure_ascii=False,
                ),
            )

        if call.name == "finish":
            if self._pending:
                raise ProtocolError(
                    LoopFailureReason.PROTOCOL, "finish with pending_reread"
                )
            self.finished = True
            return ToolResult(call.id, "finish", json.dumps({"ok": True}))

        raise ProtocolError(LoopFailureReason.INVALID_TOOL, call.name)

    @staticmethod
    def run_with_retry(
        *,
        workspace_factory: Callable[[], CriticWorkspace],
        complete: Callable[[list[dict[str, object]], list[dict[str, object]]], Any],
        initial_messages: list[dict[str, object]] | None = None,
        max_tool_turns: int = 32,
        on_session_retry: Callable[[], None] | None = None,
    ) -> ToolLoopResult:
        """Run up to two full sessions (initial + one retry) from a fresh workspace."""
        last_failure = LoopFailureReason.NO_FINISH
        last_detail = ""
        for attempt in range(2):
            if attempt > 0 and on_session_retry is not None:
                on_session_retry()
            workspace = workspace_factory()
            loop = CriticToolLoop(workspace, max_tool_turns=max_tool_turns)
            messages: list[dict[str, object]] = list(initial_messages or [])
            try:
                while not loop.finished:
                    turn = complete(messages, CRITIC_TOOLS)
                    tool_calls: list[ToolCall] = list(turn.tool_calls)
                    assistant: dict[str, object] = {
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
                    messages.append(assistant)
                    results = loop.handle_turn(tool_calls)
                    for result in results:
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": result.tool_call_id,
                                "content": result.content,
                            }
                        )
                return ToolLoopResult(
                    ok=True,
                    reviewed_bytes=workspace.writable_bytes(),
                )
            except ProtocolError as error:
                last_failure = error.reason
                last_detail = error.detail
                continue
        return ToolLoopResult(
            ok=False,
            reviewed_bytes=None,
            failure_reason=last_failure,
            detail=last_detail,
        )
