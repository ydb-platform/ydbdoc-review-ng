"""Helpers to script offline tool-using critic model turns."""

from __future__ import annotations

import json

from ydbdoc_review_ng.models import ModelCallResult, ModelToolCall


def _lines(data: bytes) -> list[str]:
    text = data.decode("utf-8")
    if text == "":
        return []
    if text.endswith("\n"):
        return text[:-1].split("\n")
    return text.split("\n")


def unified_replace_patch(old: bytes, new: bytes) -> str:
    """Build a context-free unified hunk replacing the whole file."""
    old_lines = _lines(old)
    new_lines = _lines(new)
    body = "".join(f"-{line}\n" for line in old_lines) + "".join(
        f"+{line}\n" for line in new_lines
    )
    if not body:
        body = "+\n"
    return f"@@\n{body}"


def tool_result(*calls: tuple[str, dict[str, object]]) -> ModelCallResult:
    tool_calls = tuple(
        ModelToolCall(f"call_{index}", name, json.dumps(arguments, ensure_ascii=False))
        for index, (name, arguments) in enumerate(calls, start=1)
    )
    return ModelCallResult(None, None, (), tool_calls)


def finish_only() -> ModelCallResult:
    return tool_result(("finish", {}))


def patch_read_finish(
    path: str, draft: bytes, reviewed: bytes
) -> list[ModelCallResult]:
    """Three-turn script: apply_patch → covering read → finish."""
    if draft == reviewed:
        return [finish_only()]
    patch = unified_replace_patch(draft, reviewed)
    # Touched lines: all new lines (1..N) or line 1 for empty.
    new_lines = _lines(reviewed)
    end = max(1, len(new_lines))
    return [
        tool_result(("apply_patch", {"path": path, "patch": patch})),
        tool_result(
            ("read", {"path": path, "start_line": 1, "end_line": end})
        ),
        finish_only(),
    ]
