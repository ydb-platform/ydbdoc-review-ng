"""Live, non-document probe for the alternate critic-editor contract."""

from __future__ import annotations

import json
import os

from ydbdoc_review_ng.domain import Locale, RepoPath
from ydbdoc_review_ng.models import (
    UrllibTransport,
    YandexCredentials,
    YandexOpenAIClient,
)
from ydbdoc_review_ng.quality.critic import build_critic_request
from ydbdoc_review_ng.quality.repair import _fallback_editor_request


def main() -> int:
    api_key = os.environ.get("YANDEX_API_KEY", "")
    folder_id = os.environ.get("YANDEX_FOLDER_ID", "")
    if not api_key or not folder_id:
        print("critic fallback probe unavailable: model credentials are not configured")
        return 2
    # One probe unit matches a maximum translator chunk, not a combined critic
    # excerpt: filtered combined excerpts are replayed on these exact units.
    source = "".join(
        f"- Параметр {index}: стабильное тестовое значение {index}.\n"
        for index in range(80)
    ).encode()
    target = "".join(
        f"- Parameter {index}: stable test value {index}.\n" for index in range(80)
    ).encode()
    primary = build_critic_request(
        model="yandexgpt-5.1",
        source=source,
        target=target,
        target_path=RepoPath("ydb/docs/en/probe.md"),
        source_locale=Locale.RU,
        target_locale=Locale.EN,
        requested_ids=("document",),
        source_is_excerpt=True,
        target_is_excerpt=True,
        editable=True,
        terminology_context="term = stable term\n" * 300,
    )
    request = _fallback_editor_request(
        primary,
        os.environ.get("YDBDOC_MODEL_CRITIC_FALLBACK")
        or os.environ.get("YDBDOC_MODEL")
        or "deepseek-v4-flash",
    )
    attempts = []
    client = YandexOpenAIClient(
        YandexCredentials(
            api_key,
            folder_id,
        ),
        UrllibTransport(),
        attempts.append,
    )
    result = client.invoke(request)
    if not result.success or result.text is None:
        last = result.attempts[-1] if result.attempts else None
        print(
            "critic fallback probe failed:",
            result.failure.value if result.failure is not None else "unknown",
            None if last is None else last.http_status,
            None if last is None else last.response_status,
        )
        return 1
    try:
        payload = json.loads(result.text)
    except json.JSONDecodeError:
        print("critic fallback probe failed: malformed JSON")
        return 1
    if type(payload) is not dict or set(payload) != {"corrected_markdown"}:
        print("critic fallback probe failed: wrong JSON shape")
        return 1
    if type(payload["corrected_markdown"]) is not str:
        print("critic fallback probe failed: corrected_markdown is not text")
        return 1
    usage = result.attempts[-1].usage
    print(
        "critic fallback probe passed;",
        f"prompt_characters={len(request.prompt)};",
        f"input_tokens={usage.input_tokens};",
        f"output_tokens={usage.output_tokens}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
