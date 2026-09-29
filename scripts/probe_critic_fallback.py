"""Live, non-document probe for the alternate critic-editor contract."""

from __future__ import annotations

import json
import os

from ydbdoc_review_ng.domain import Locale, RepoPath
from ydbdoc_review_ng.models import (
    AttemptResult,
    ModelCallResult,
    ModelRequest,
    NativeYandexClient,
    UrllibTransport,
    YandexCredentials,
    YandexOpenAIClient,
)
from ydbdoc_review_ng.quality.critic import (
    CriticResponseError,
    build_critic_request,
    parse_critic_response,
)
from ydbdoc_review_ng.quality.repair import _fallback_editor_request
from ydbdoc_review_ng.quality.types import CriticResult

_DIAGNOSTIC_SOURCE = (
    "**Группа хранения**, **группа распределённого хранилища** или "
    "**группа Blob Storage** — место надёжного хранения данных."
).encode()
_DIAGNOSTIC_TARGET = (
    b"**Storage group**, **distributed storage group**, **storage group**, or "
    b"**Blob storage group** is a place for reliable data storage."
)


def _diagnostic_probe_request(model: str) -> ModelRequest:
    return build_critic_request(
        model=model,
        source=_DIAGNOSTIC_SOURCE,
        target=_DIAGNOSTIC_TARGET,
        target_path=RepoPath("ydb/docs/en/probe.md"),
        source_locale=Locale.RU,
        target_locale=Locale.EN,
        requested_ids=(),
        source_is_excerpt=True,
        target_is_excerpt=True,
        editable=True,
    )


def _parse_diagnostic_probe_response(raw: str) -> CriticResult:
    return parse_critic_response(
        raw,
        target_path=RepoPath("ydb/docs/en/probe.md"),
        requested_ids=(),
        editable=True,
        current_target=_DIAGNOSTIC_TARGET.decode(),
    )


def _arbiter_probe_request(model: str, corrected: str) -> ModelRequest:
    return build_critic_request(
        model=model,
        source=_DIAGNOSTIC_SOURCE,
        target=corrected.encode(),
        target_path=RepoPath("ydb/docs/en/probe.md"),
        source_locale=Locale.RU,
        target_locale=Locale.EN,
        requested_ids=(),
        final=True,
        source_is_excerpt=True,
        target_is_excerpt=True,
    )


def _client(
    model: str,
    credentials: YandexCredentials,
    attempts: list[AttemptResult],
) -> NativeYandexClient | YandexOpenAIClient:
    client_type = YandexOpenAIClient if "deepseek" in model.lower() else NativeYandexClient
    return client_type(credentials, UrllibTransport(), attempts.append)


def _failure_summary(result: ModelCallResult) -> str:
    last = result.attempts[-1] if result.attempts else None
    failure = result.failure.value if result.failure is not None else "unknown"
    http_status = None if last is None else last.http_status
    response_status = None if last is None else last.response_status
    return (
        f"{failure}; attempts={len(result.attempts)}; "
        f"http={http_status}; status={response_status}"
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
        print("critic fallback probe failed:", _failure_summary(result))
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
    print("critic fallback probe passed")
    usage = result.attempts[-1].usage
    diagnostic_primary = _diagnostic_probe_request(
        os.environ.get("YDBDOC_MODEL_CRITIC") or "yandexgpt-5.1"
    )
    primary_attempts: list[AttemptResult] = []
    primary_result = NativeYandexClient(
        YandexCredentials(api_key, folder_id),
        UrllibTransport(),
        primary_attempts.append,
    ).invoke(diagnostic_primary)
    if not primary_result.success or primary_result.text is None:
        print("structured critic probe failed: model call;", _failure_summary(primary_result))
        return 1
    try:
        editor_result = _parse_diagnostic_probe_response(primary_result.text)
    except CriticResponseError as error:
        print(f"structured critic probe failed: response contract ({error.reason.value})")
        return 1
    corrected = editor_result.corrected_markdown
    if (
        corrected is None
        or corrected == _DIAGNOSTIC_TARGET.decode()
        or corrected.lower().count("**storage group**") != 1
    ):
        print("structured critic probe failed: editor did not correct the duplicate alias")
        return 1
    print("structured critic-editor probe passed")
    primary_usage = primary_result.attempts[-1].usage
    arbiter_model = os.environ.get("YDBDOC_MODEL_ARBITER") or os.environ.get(
        "YDBDOC_MODEL"
    ) or "deepseek-v4-flash"
    arbiter = _arbiter_probe_request(arbiter_model, corrected)
    arbiter_attempts: list[AttemptResult] = []
    arbiter_result = _client(
        arbiter_model,
        YandexCredentials(api_key, folder_id),
        arbiter_attempts,
    ).invoke(arbiter)
    if not arbiter_result.success or arbiter_result.text is None:
        print("arbiter probe failed: model call;", _failure_summary(arbiter_result))
        return 1
    try:
        arbiter_review = parse_critic_response(
            arbiter_result.text,
            target_path=RepoPath("ydb/docs/en/probe.md"),
            requested_ids=(),
            editable=False,
            current_target=corrected,
        )
    except CriticResponseError as error:
        print(f"arbiter probe failed: response contract ({error.reason.value})")
        return 1
    if arbiter_review.verdict.value != "GREEN":
        print("arbiter probe failed: corrected translation was rejected")
        return 1
    arbiter_usage = arbiter_result.attempts[-1].usage
    print(
        "critic fallback, critic-editor, and independent arbiter probes passed;",
        f"prompt_characters={len(request.prompt)};",
        f"input_tokens={usage.input_tokens};",
        f"output_tokens={usage.output_tokens};",
        f"primary_input_tokens={primary_usage.input_tokens};",
        f"primary_output_tokens={primary_usage.output_tokens};",
        f"arbiter_input_tokens={arbiter_usage.input_tokens};",
        f"arbiter_output_tokens={arbiter_usage.output_tokens}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
