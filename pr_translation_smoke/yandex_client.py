from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .contract import ContractError


ENDPOINT = "https://llm.api.cloud.yandex.net/foundationModels/v1/completion"


def normalize_model_uri(model: str, folder_id: str) -> str:
    if model.startswith("gpt://"):
        return model
    return f"gpt://{folder_id}/{model}"


def build_request_payload(
    *, model_uri: str, prompt: str, schema: dict[str, Any], max_tokens: int
) -> dict[str, Any]:
    return {
        "modelUri": model_uri,
        "completionOptions": {
            "stream": False,
            "temperature": 0,
            "maxTokens": str(max_tokens),
            "reasoningOptions": {"mode": "DISABLED"},
        },
        "messages": [
            {
                "role": "system",
                "text": (
                    "Translate YDB technical documentation from Russian to English. "
                    "Translate every requested field completely and accurately. Preserve "
                    "every opaque <S...> token exactly once and in its original order. "
                    "Do not add explanations or fields."
                ),
            },
            {"role": "user", "text": prompt},
        ],
        "jsonSchema": {"schema": schema},
    }


def extract_response_text(response: dict[str, Any]) -> str:
    try:
        alternative = response["result"]["alternatives"][0]
        status = alternative["status"]
        text = alternative["message"]["text"]
    except (KeyError, IndexError, TypeError) as error:
        raise ContractError("Yandex response has an unexpected shape") from error
    if status != "ALTERNATIVE_STATUS_FINAL":
        raise ContractError(f"Yandex completion is not final: {status}")
    if not isinstance(text, str) or not text:
        raise ContractError("Yandex completion text is empty")
    return text


def complete(
    *,
    api_key: str,
    folder_id: str,
    model_uri: str,
    prompt: str,
    schema: dict[str, Any],
    max_tokens: int = 8000,
    timeout_seconds: int = 180,
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
            "x-folder-id": folder_id,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            decoded = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Yandex API HTTP {error.code}: {body}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Yandex API request failed: {error.reason}") from error
    return extract_response_text(decoded), decoded
