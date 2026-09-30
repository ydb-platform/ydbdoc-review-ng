"""Live synthetic probe for translator and whole-PR quality contracts."""

from __future__ import annotations

import os
from collections.abc import Mapping

from ydbdoc_review_ng.domain import (
    GitSha,
    ModelRole,
    RepoPath,
    RepositoryId,
    SnapshotRef,
)
from ydbdoc_review_ng.models import (
    AttemptResult,
    ModelCallResult,
    ModelRequest,
    NativeYandexClient,
    UrllibTransport,
    YandexCredentials,
    YandexOpenAIClient,
)
from ydbdoc_review_ng.parser.markdown import build_markdown_plan
from ydbdoc_review_ng.plan import SourcePlan
from ydbdoc_review_ng.quality.critic import (
    CriticResponseError,
    build_pr_arbiter_request,
    build_pr_critic_request,
    parse_pr_arbiter_response,
    parse_pr_critic_response,
)
from ydbdoc_review_ng.runtime_content import (
    _assemble_document_chunk_segments,
    _document_chunk_translation_request,
    _TranslationSegment,
)
from ydbdoc_review_ng.translation import (
    DocumentTranslationRequest,
    TranslationField,
    TranslationRequest,
    prepare_document,
    restore_document,
    validate_chunk_response,
)

_DIAGNOSTIC_SOURCE = (
    "**Группа хранения**, **группа распределённого хранилища** или "
    "**группа Blob Storage** — место надёжного хранения данных."
).encode()
_DIAGNOSTIC_TARGET = (
    b"**Storage group**, **distributed storage group**, **storage group**, or "
    b"**Blob storage group** is a place for reliable data storage."
)
_TRANSLATOR_SOURCE = "Запустите `ydb` и откройте [руководство](guide.md).\n".encode()
_SOURCE_PATH = "ydb/docs/ru/probe.md"
_TARGET_PATH = "ydb/docs/en/probe.md"
_GLOSSARY_FILES = {
    "ydb/docs/en/glossary.md": b"Storage group: a place for reliable data storage.\n",
}


def _translator_probe_contract(
    model: str,
) -> tuple[
    ModelRequest,
    TranslationField,
    TranslationRequest,
    tuple[_TranslationSegment, ...],
    DocumentTranslationRequest,
    SourcePlan,
]:
    path = RepoPath("ydb/docs/ru/probe.md")
    snapshot = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("0" * 40))
    plan = build_markdown_plan(snapshot, path, _TRANSLATOR_SOURCE)
    document = prepare_document(
        _TRANSLATOR_SOURCE,
        plan,
        max_characters=6000,
        source_locale="ru",
        target_locale="en",
    )
    chunk = document.chunks[0]
    request, field, contract, segments = _document_chunk_translation_request(
        ModelRequest(
            ModelRole.TRANSLATE,
            model,
            "translator probe",
            None,
            8000,
            RepoPath("ydb/docs/en/probe.md"),
        ),
        chunk,
        document.placeholders,
        "ru",
        "en",
    )
    return request, field, contract, segments, document, plan


def _diagnostic_probe_request(model: str) -> ModelRequest:
    return build_pr_critic_request(
        model=model,
        source_files={_SOURCE_PATH: _DIAGNOSTIC_SOURCE},
        translated_files={_TARGET_PATH: _DIAGNOSTIC_TARGET},
        glossary_files=_GLOSSARY_FILES,
    )


def _parse_diagnostic_probe_response(raw: str) -> dict[str, bytes]:
    return parse_pr_critic_response(raw, target_paths=(_TARGET_PATH,))


def _arbiter_probe_request(model: str, corrected: Mapping[str, bytes]) -> ModelRequest:
    return build_pr_arbiter_request(
        model=model,
        source_files={_SOURCE_PATH: _DIAGNOSTIC_SOURCE},
        translated_files=corrected,
        glossary_files=_GLOSSARY_FILES,
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
        print("quality probe unavailable: model credentials are not configured")
        return 2
    translator_model = os.environ.get("YDBDOC_MODEL") or "deepseek-v4-flash"
    translator_request, field, contract, segments, document, source_plan = (
        _translator_probe_contract(translator_model)
    )
    translator_attempts: list[AttemptResult] = []
    translator_result = _client(
        translator_model,
        YandexCredentials(api_key, folder_id),
        translator_attempts,
    ).invoke(translator_request)
    if not translator_result.success or translator_result.text is None:
        print("structured translator probe failed: model call;", _failure_summary(translator_result))
        return 1
    try:
        draft = _assemble_document_chunk_segments(
            field, contract, segments, translator_result.text
        )
        validate_chunk_response(document.chunks[0], document.placeholders, draft)
        restored = restore_document(
            _TRANSLATOR_SOURCE, source_plan, document, (draft,)
        ).decode()
    except (TypeError, ValueError):
        print("structured translator probe failed: response contract")
        return 1
    if restored.count("`ydb`") != 1 or restored.count("(guide.md)") != 1:
        print("structured translator probe failed: runtime-owned fragments were not restored")
        return 1
    print("structured translator probe passed")
    translator_usage = translator_result.attempts[-1].usage
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
        corrected_files = _parse_diagnostic_probe_response(primary_result.text)
    except CriticResponseError as error:
        print(f"structured critic probe failed: response contract ({error.reason.value})")
        return 1
    corrected = corrected_files[_TARGET_PATH].decode()
    if (
        corrected == _DIAGNOSTIC_TARGET.decode()
        or corrected.lower().count("**storage group**") != 1
    ):
        print("structured critic probe failed: editor did not correct the duplicate alias")
        return 1
    print("structured critic-editor probe passed")
    primary_usage = primary_result.attempts[-1].usage
    arbiter_model = os.environ.get("YDBDOC_MODEL_ARBITER") or os.environ.get(
        "YDBDOC_MODEL"
    ) or "deepseek-v4-flash"
    arbiter = _arbiter_probe_request(arbiter_model, corrected_files)
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
        arbiter_review = parse_pr_arbiter_response(
            arbiter_result.text,
            target_paths=(_TARGET_PATH,),
        )
    except CriticResponseError as error:
        print(f"arbiter probe failed: response contract ({error.reason.value})")
        return 1
    if arbiter_review.verdict.value != "GREEN":
        print("arbiter probe failed: corrected translation was rejected")
        return 1
    arbiter_usage = arbiter_result.attempts[-1].usage
    print(
        "translator, whole-PR critic, and independent arbiter probes passed;",
        f"translator_input_tokens={translator_usage.input_tokens};",
        f"translator_output_tokens={translator_usage.output_tokens};",
        f"primary_input_tokens={primary_usage.input_tokens};",
        f"primary_output_tokens={primary_usage.output_tokens};",
        f"arbiter_input_tokens={arbiter_usage.input_tokens};",
        f"arbiter_output_tokens={arbiter_usage.output_tokens}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
