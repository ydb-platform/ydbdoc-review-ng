"""Live, non-document probe for the alternate critic-editor contract."""

from __future__ import annotations

import json
import os
from typing import cast

from ydbdoc_review_ng.domain import Locale, RepoPath
from ydbdoc_review_ng.models import (
    AttemptResult,
    ModelRequest,
    NativeYandexClient,
    UrllibTransport,
    YandexCredentials,
    YandexOpenAIClient,
)
from ydbdoc_review_ng.models.types import FrozenJson, mutable_json
from ydbdoc_review_ng.quality.critic import build_critic_request, parse_critic_response
from ydbdoc_review_ng.quality.repair import _fallback_editor_request


def _targeted_probe_request(primary: ModelRequest) -> ModelRequest:
    targeted = _fallback_editor_request(primary, primary.model)
    return ModelRequest(
        targeted.role,
        targeted.model,
        targeted.prompt
        + "\n\nMandatory unresolved edits:\n"
        + json.dumps(
            [
                {
                    "reason": "The alias storage group is duplicated.",
                    "searchable_snippet": "Storage group, distributed storage group, "
                    "storage group, or Blob storage group",
                    "expected_correction": "Remove the duplicate storage group alias.",
                }
            ]
        )
        + "\nApply every expected_correction and return changed corrected_markdown.",
        None
        if targeted.schema is None
        else cast(FrozenJson, mutable_json(targeted.schema)),
        targeted.max_tokens,
        targeted.target_path,
    )


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
        requested_ids=(),
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
    attempts: list[AttemptResult] = []
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
    diagnostic_source = (
        "**Группа хранения**, **группа распределённого хранилища** или "
        "**группа Blob Storage** — место надёжного хранения данных."
    ).encode()
    diagnostic_target = (
        b"**Storage group**, **distributed storage group**, **storage group**, or "
        b"**Blob storage group** is a place for reliable data storage."
    )
    diagnostic_primary = build_critic_request(
        model=os.environ.get("YDBDOC_MODEL_CRITIC") or "yandexgpt-5.1",
        source=diagnostic_source,
        target=diagnostic_target,
        target_path=RepoPath("ydb/docs/en/probe.md"),
        source_locale=Locale.RU,
        target_locale=Locale.EN,
        requested_ids=("document",),
        source_is_excerpt=True,
        target_is_excerpt=True,
        editable=True,
    )
    primary_attempts: list[AttemptResult] = []
    primary_result = NativeYandexClient(
        YandexCredentials(api_key, folder_id),
        UrllibTransport(),
        primary_attempts.append,
    ).invoke(diagnostic_primary)
    if not primary_result.success or primary_result.text is None:
        print("structured critic probe failed: model call")
        return 1
    try:
        parse_critic_response(
            primary_result.text,
            target_path=RepoPath("ydb/docs/en/probe.md"),
            requested_ids=(),
            editable=True,
            current_target=diagnostic_target.decode(),
        )
    except ValueError:
        print("structured critic probe failed: response contract")
        return 1
    primary_usage = primary_result.attempts[-1].usage
    targeted = _targeted_probe_request(diagnostic_primary)
    targeted_attempts: list[AttemptResult] = []
    targeted_result = NativeYandexClient(
        YandexCredentials(api_key, folder_id),
        UrllibTransport(),
        targeted_attempts.append,
    ).invoke(targeted)
    if not targeted_result.success or targeted_result.text is None:
        print("targeted editor probe failed: model call")
        return 1
    try:
        targeted_payload = json.loads(targeted_result.text)
    except json.JSONDecodeError:
        print("targeted editor probe failed: malformed JSON")
        return 1
    corrected = targeted_payload.get("corrected_markdown")
    if type(corrected) is not str or corrected == diagnostic_target.decode():
        print("targeted editor probe failed: unchanged correction")
        return 1
    targeted_usage = targeted_result.attempts[-1].usage
    print(
        "critic fallback and targeted editor probes passed;",
        f"prompt_characters={len(request.prompt)};",
        f"input_tokens={usage.input_tokens};",
        f"output_tokens={usage.output_tokens};",
        f"primary_input_tokens={primary_usage.input_tokens};",
        f"primary_output_tokens={primary_usage.output_tokens};",
        f"targeted_input_tokens={targeted_usage.input_tokens};",
        f"targeted_output_tokens={targeted_usage.output_tokens}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
