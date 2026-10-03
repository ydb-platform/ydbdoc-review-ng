"""Offline model stub that understands tool critic turns and legacy files JSON."""

from __future__ import annotations

import json
from typing import Any

from tests.support.tool_critic_scripts import finish_only, patch_read_finish
from ydbdoc_review_ng.domain import ModelRole
from ydbdoc_review_ng.models import AttemptError, ModelCallResult, ModelRequest


def _draft_from_request(request: ModelRequest) -> dict[str, bytes]:
    prompt = request.prompt
    if "<translation-pr-files>" not in prompt:
        # messages-based tool requests store prompt separately; recover from messages.
        if request.messages is None:
            return {}
        for message in request.messages:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if isinstance(content, str) and "<translation-pr-files>" in content:
                prompt = content
                break
        else:
            return {}
    block = prompt.split("<translation-pr-files>\n", 1)[1].split(
        "\n</translation-pr-files>", 1
    )[0]
    rendered = json.loads(block)
    out: dict[str, bytes] = {}
    for path, text in rendered.items():
        if text is None:
            out[path] = b""
        elif isinstance(text, str):
            out[path] = text.encode("utf-8")
    return out


def _expand_files_json(raw: str, request: ModelRequest) -> list[ModelCallResult]:
    data = json.loads(raw)
    if type(data) is not dict or "files" not in data or "verdict" in data:
        raise ValueError("not legacy critic files json")
    files = data["files"]
    if type(files) is not dict:
        raise ValueError("files not object")
    drafts = _draft_from_request(request)
    turns: list[ModelCallResult] = []
    # One path per chunk in production packing.
    for path, text in files.items():
        reviewed = text.encode("utf-8") if isinstance(text, str) else b""
        draft = drafts.get(path, b"")
        turns.extend(patch_read_finish(path, draft, reviewed))
    return turns or [finish_only()]


class ScriptedModels:
    """Queue of ModelCallResult | legacy JSON strings for offline review_pr tests."""

    def __init__(self, payloads: list[Any]) -> None:
        self.payloads: list[Any] = list(payloads)
        self.calls: list[ModelRequest] = []

    def invoke(self, request: ModelRequest) -> ModelCallResult:
        self.calls.append(request)
        if not self.payloads:
            raise AssertionError("scripted model out of payloads")
        item = self.payloads.pop(0)
        if isinstance(item, ModelCallResult):
            return item
        if isinstance(item, AttemptError):
            return ModelCallResult(None, item, ())
        if isinstance(item, str):
            if request.role is ModelRole.CRITIC:
                try:
                    expanded = _expand_files_json(item, request)
                except (ValueError, json.JSONDecodeError):
                    return ModelCallResult(item, None, ())
                # Current turn is the first expanded tool result; queue the rest.
                head, *tail = expanded
                self.payloads[0:0] = tail
                return head
            return ModelCallResult(item, None, ())
        raise TypeError(f"unsupported scripted payload: {type(item)!r}")
