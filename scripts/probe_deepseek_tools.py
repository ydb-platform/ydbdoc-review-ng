#!/usr/bin/env python3
"""P0 live probe: DeepSeek tool_calls multi-turn on YC OpenAI-compatible endpoint.

Smoke-scoped unless YANDEX_API_KEY + YANDEX_FOLDER_ID are both set.
Does not invent secrets; reads only existing env names.

Usage:
  YDBDOC_LIVE=1 python scripts/probe_deepseek_tools.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

ENDPOINT = "https://ai.api.cloud.yandex.net/v1/chat/completions"
DEFAULT_MODEL = "deepseek-v4-flash"


@dataclass(frozen=True)
class Creds:
    api_key: str
    folder_id: str
    source: str  # env-name pair label, never the secret


def _resolve_creds() -> Creds:
    pairs = (
        ("YANDEX_API_KEY", "YANDEX_FOLDER_ID", "production YANDEX_*"),
        (
            "YANDEX_CLOUD_API_KEY_DOC_REVIEW",
            "YANDEX_CLOUD_FOLDER_DOC_REVIEW",
            "shell YANDEX_CLOUD_*_DOC_REVIEW",
        ),
        ("YDBDOC_YC_API_KEY", "YDBDOC_YC_FOLDER_ID", "smoke YDBDOC_YC_*"),
        ("YC_API_KEY", "YC_FOLDER_ID", "smoke YC_*"),
    )
    for key_name, folder_name, label in pairs:
        api_key = os.environ.get(key_name, "").strip()
        folder_id = os.environ.get(folder_name, "").strip()
        if api_key and folder_id:
            return Creds(api_key, folder_id, label)
    raise SystemExit(
        "No usable credentials. Need one of: YANDEX_API_KEY+YANDEX_FOLDER_ID, "
        "YANDEX_CLOUD_API_KEY_DOC_REVIEW+YANDEX_CLOUD_FOLDER_DOC_REVIEW, "
        "YDBDOC_YC_API_KEY+YDBDOC_YC_FOLDER_ID, or YC_API_KEY+YC_FOLDER_ID."
    )


def _model_uri(model: str, folder_id: str) -> str:
    return model if model.startswith("gpt://") else f"gpt://{folder_id}/{model}"


def _post(creds: Creds, payload: dict[str, Any], *, timeout: float) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        ENDPOINT,
        data=body,
        headers={
            "Authorization": f"Api-Key {creds.api_key}",
            "Content-Type": "application/json",
            "OpenAI-Project": creds.folder_id,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")
        try:
            document = json.loads(raw)
        except json.JSONDecodeError:
            document = {"raw": raw}
        return error.code, document


def _choice0(document: dict[str, Any]) -> dict[str, Any]:
    choices = document.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError(f"no choices in response: {json.dumps(document)[:500]}")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise RuntimeError("choices[0] is not an object")
    return choice


def main() -> int:
    if os.environ.get("YDBDOC_LIVE", "").strip() != "1":
        print("Refusing: set YDBDOC_LIVE=1 for this paid probe.", file=sys.stderr)
        return 2

    creds = _resolve_creds()
    model = (
        os.environ.get("YDBDOC_MODEL_CRITIC")
        or os.environ.get("YDBDOC_MODEL")
        or os.environ.get("YDBDOC_MODEL_TRANSLATE")
        or DEFAULT_MODEL
    ).strip()
    timeout = float(os.environ.get("YDBDOC_MODEL_HTTP_TIMEOUT_SECONDS", "120"))
    model_uri = _model_uri(model, creds.folder_id)

    tools = [
        {
            "type": "function",
            "function": {
                "name": "echo",
                "description": "Echo a short token back to the caller.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "token": {
                            "type": "string",
                            "description": "Exact token to echo.",
                        }
                    },
                    "required": ["token"],
                    "additionalProperties": False,
                },
            },
        }
    ]

    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                "You are a tool-use probe. Call the echo tool exactly once with "
                "token=probe-ok. Do not answer in plain text before the tool call."
            ),
        },
        {
            "role": "user",
            "content": "Call echo with token probe-ok.",
        },
    ]

    turn1_payload = {
        "model": model_uri,
        "stream": False,
        "temperature": 0,
        "max_tokens": 256,
        "reasoning_effort": "none",
        "tools": tools,
        "tool_choice": "required",
        "messages": messages,
    }

    print(f"endpoint={ENDPOINT}")
    print(f"model={model!r} model_uri={model_uri!r}")
    print(f"creds_source={creds.source}")
    print(f"folder_id={creds.folder_id}")
    print("--- turn1: request tools ---")

    status1, doc1 = _post(creds, turn1_payload, timeout=timeout)
    print(f"http_status={status1}")
    if status1 != 200:
        print("FAIL: non-200 on tool request")
        print(json.dumps(doc1, ensure_ascii=False)[:2000])
        return 1

    choice1 = _choice0(doc1)
    message1 = choice1.get("message")
    if not isinstance(message1, dict):
        print("FAIL: message missing")
        print(json.dumps(doc1, ensure_ascii=False)[:2000])
        return 1

    finish1 = choice1.get("finish_reason")
    content1 = message1.get("content")
    tool_calls = message1.get("tool_calls")
    usage1 = doc1.get("usage")

    print(f"finish_reason={finish1!r}")
    print(f"content_is_null={content1 is None}")
    print(f"content_repr={content1!r}"[:200])
    print(f"tool_calls_present={isinstance(tool_calls, list) and bool(tool_calls)}")
    print(f"usage={usage1!r}")

    if not isinstance(tool_calls, list) or not tool_calls:
        print("FAIL: no tool_calls in turn1")
        print(json.dumps(doc1, ensure_ascii=False)[:3000])
        return 1

    call0 = tool_calls[0]
    if not isinstance(call0, dict):
        print("FAIL: tool_calls[0] not object")
        return 1
    call_id = call0.get("id")
    function = call0.get("function")
    print(f"tool_call_id={call_id!r}")
    print(f"tool_function={function!r}"[:500])

    # Landmine check for clients.py: finish_reason=tool_calls + null content
    # must not be treated as NON_FINAL/EMPTY_TEXT once P1c lands.
    if finish1 == "tool_calls" and content1 is None:
        print("OBS: finish_reason=tool_calls with content=null (clients.py landmine confirmed)")
    elif finish1 == "tool_calls":
        print(f"OBS: finish_reason=tool_calls with content={content1!r}")
    else:
        print(f"OBS: finish_reason={finish1!r} (not classic tool_calls; still has tool_calls[])")

    messages.append(
        {
            "role": "assistant",
            "content": content1,
            "tool_calls": tool_calls,
        }
    )
    messages.append(
        {
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps({"ok": True, "token": "probe-ok"}, ensure_ascii=False),
        }
    )

    turn2_payload = {
        "model": model_uri,
        "stream": False,
        "temperature": 0,
        "max_tokens": 128,
        "reasoning_effort": "none",
        "messages": messages,
    }

    print("--- turn2: role=tool result ---")
    status2, doc2 = _post(creds, turn2_payload, timeout=timeout)
    print(f"http_status={status2}")
    if status2 != 200:
        print("FAIL: non-200 on tool-result turn")
        print(json.dumps(doc2, ensure_ascii=False)[:2000])
        return 1

    choice2 = _choice0(doc2)
    message2 = choice2.get("message") if isinstance(choice2.get("message"), dict) else {}
    finish2 = choice2.get("finish_reason")
    content2 = message2.get("content") if isinstance(message2, dict) else None
    usage2 = doc2.get("usage")
    print(f"finish_reason={finish2!r}")
    print(f"content_repr={content2!r}"[:300])
    print(f"usage={usage2!r}")

    if finish2 not in {"stop", "length"} and not (
        isinstance(message2.get("tool_calls"), list) and message2.get("tool_calls")
    ):
        # Accept stop, or another tool call; reject hard provider errors already handled.
        print(f"WARN: unexpected finish_reason on turn2: {finish2!r}")

    print("PASS: tool_calls present on turn1; role=tool round-trip returned HTTP 200")
    summary = {
        "result": "PASS",
        "endpoint": ENDPOINT,
        "model": model,
        "model_uri": model_uri,
        "creds_source": creds.source,
        "folder_id": creds.folder_id,
        "turn1": {
            "http_status": status1,
            "finish_reason": finish1,
            "content_is_null": content1 is None,
            "tool_calls_count": len(tool_calls),
            "usage": usage1,
        },
        "turn2": {
            "http_status": status2,
            "finish_reason": finish2,
            "content_is_null": content2 is None,
            "usage": usage2,
        },
    }
    print("--- summary ---")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
