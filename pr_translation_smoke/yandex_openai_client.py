from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .contract import ContractError
from .yandex_client import normalize_model_uri


ENDPOINT = "https://ai.api.cloud.yandex.net/v1/chat/completions"


def build_request_payload(
    *, model_uri: str, prompt: str, schema: dict[str, Any], max_tokens: int
) -> dict[str, Any]:
    return {
        "model": model_uri,
        "stream": False,
        "temperature": 0,
        "max_tokens": max_tokens,
        "reasoning_effort": "none",
        "messages": [
            {
                "role": "system",
                "content": (
                    "Translate YDB technical documentation from Russian to English. "
                    "Return only the requested schema-bound JSON map."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "translation_field",
                "strict": True,
                "schema": schema,
            },
        },
    }


def extract_response_text(response: dict[str, Any]) -> str:
    try:
        choice = response["choices"][0]
        status = choice["finish_reason"]
        text = choice["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise ContractError("OpenAI-compatible response has an unexpected shape") from error
    if status != "stop":
        raise ContractError(f"OpenAI-compatible completion is not final: {status}")
    if not isinstance(text, str) or not text:
        raise ContractError("OpenAI-compatible completion text is empty")
    return text


def complete(
    *,
    api_key: str,
    folder_id: str,
    model_uri: str,
    prompt: str,
    schema: dict[str, Any],
    max_tokens: int = 2000,
    timeout_seconds: int = 60,
) -> tuple[str, dict[str, Any]]:
    payload = build_request_payload(
        model_uri=normalize_model_uri(model_uri, folder_id),
        prompt=prompt,
        schema=schema,
        max_tokens=max_tokens,
    )
    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Api-Key {api_key}",
            "Content-Type": "application/json",
            "OpenAI-Project": folder_id,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            decoded = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Yandex OpenAI API HTTP {error.code}: {body}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Yandex OpenAI API request failed: {error.reason}") from error
    return extract_response_text(decoded), decoded
