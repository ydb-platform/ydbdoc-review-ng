from __future__ import annotations

import json
from itertools import pairwise
from types import SimpleNamespace
from typing import cast

import pytest
import yaml

from ydbdoc_review_ng import dependencies, runtime_content
from ydbdoc_review_ng.application import ImmutableRunSnapshot, WorkflowCandidate
from ydbdoc_review_ng.continuation import AcceptedDocument
from ydbdoc_review_ng.domain import (
    FilePair,
    GitSha,
    Locale,
    RepoPath,
    RepositoryId,
    SnapshotRef,
)
from ydbdoc_review_ng.locales import PairKey
from ydbdoc_review_ng.models import AttemptError, ModelCallResult, ModelRequest
from ydbdoc_review_ng.parser.markdown import build_markdown_plan
from ydbdoc_review_ng.publication import FileChange, PublicationPlan
from ydbdoc_review_ng.runtime import RecordedModels, RuntimeSource
from ydbdoc_review_ng.runtime_content import (
    Document,
    FrozenPreparation,
    FrozenSourcePlans,
    InvalidTranslationResponse,
    RuntimeContent,
    pack,
    unpack,
)
from ydbdoc_review_ng.runtime_github import RuntimeBoundaryError
from ydbdoc_review_ng.scope import FileOperation, ScopeEntry, ScopeOrigin
from ydbdoc_review_ng.translation import (
    DocumentTranslationError,
    assemble_candidate,
    build_document_prompt,
    build_translation_request,
    prepare_document,
)

SOURCE_PATH = RepoPath("ydb/docs/ru/core/page.md")
TARGET_PATH = RepoPath("ydb/docs/en/core/page.md")
SNAPSHOT = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a" * 40))


class ScriptedModels:
    def __init__(self, responses: list[str | ModelCallResult]) -> None:
        self.responses = responses
        self.calls: list[ModelRequest] = []

    def invoke(self, request: ModelRequest, /) -> ModelCallResult:
        response = self.responses[len(self.calls)]
        self.calls.append(request)
        if type(response) is ModelCallResult:
            return response
        return ModelCallResult(cast(str, response), None, ())


def _source_from_prompt(prompt: str) -> str:
    marker = "<AUTHORITATIVE_SOURCE_"
    if marker not in prompt:
        return prompt.split("\n\n", 1)[1]
    start = prompt.index("\n", prompt.index(marker)) + 1
    end = prompt.index("</AUTHORITATIVE_SOURCE_", start)
    return prompt[start:end]


class EchoChunkModels:
    def __init__(self) -> None:
        self.calls: list[ModelRequest] = []

    def invoke(self, request: ModelRequest, /) -> ModelCallResult:
        self.calls.append(request)
        return ModelCallResult(_source_from_prompt(request.prompt), None, ())


class InvalidTwiceThenEchoModels:
    def __init__(self, invalid: str) -> None:
        self.invalid = invalid
        self.calls: list[ModelRequest] = []

    def invoke(self, request: ModelRequest, /) -> ModelCallResult:
        self.calls.append(request)
        response = self.invalid if len(self.calls) <= 2 else _source_from_prompt(request.prompt)
        return ModelCallResult(response, None, ())


class InvalidThenFilteredThenEchoModels:
    def __init__(self, invalid: str) -> None:
        self.invalid = invalid
        self.calls: list[ModelRequest] = []

    def invoke(self, request: ModelRequest, /) -> ModelCallResult:
        self.calls.append(request)
        if len(self.calls) == 1:
            return ModelCallResult(self.invalid, None, ())
        if len(self.calls) == 2:
            return ModelCallResult(None, AttemptError.CONTENT_FILTER, ())
        return ModelCallResult(_source_from_prompt(request.prompt), None, ())


class FilterTwiceThenEchoModels:
    def __init__(self) -> None:
        self.calls: list[ModelRequest] = []

    def invoke(self, request: ModelRequest, /) -> ModelCallResult:
        self.calls.append(request)
        if len(self.calls) <= 2:
            return ModelCallResult(None, AttemptError.CONTENT_FILTER, ())
        return ModelCallResult(_source_from_prompt(request.prompt), None, ())


class NestedInvalidThenEchoModels:
    def __init__(self, parent_invalid: str) -> None:
        self.parent_invalid = parent_invalid
        self.calls: list[ModelRequest] = []

    def invoke(self, request: ModelRequest, /) -> ModelCallResult:
        self.calls.append(request)
        call_number = len(self.calls)
        if call_number <= 2:
            return ModelCallResult(self.parent_invalid, None, ())
        source = _source_from_prompt(request.prompt)
        if call_number in {4, 5, 7, 8}:
            marker_start = source.index("[[YDBDOC_PROTECTED_")
            marker_end = source.index("]]", marker_start) + 2
            source = source[:marker_start] + source[marker_end:]
        return ModelCallResult(source, None, ())


def _heading_block(number: int, length: int) -> str:
    prefix = f"## Block {number:03d} "
    return prefix + "x" * (length - len(prefix) - 1) + "\n"


def content_filter_witness(*, with_leading_chunk: bool = False) -> bytes:
    lengths = [157] * 49 + [177] + [130] * 60 + [131]
    witness = "".join(_heading_block(number, length) for number, length in enumerate(lengths))
    if not with_leading_chunk:
        return witness.encode()
    return (_heading_block(999, 15_900) + witness).encode()


def document_for(
    source: bytes,
    *,
    source_locale: Locale = Locale.RU,
    target: bytes | None = b"# Old target\n",
) -> Document:
    target_locale = Locale.EN if source_locale is Locale.RU else Locale.RU
    source_path, target_path = (
        (SOURCE_PATH, TARGET_PATH) if source_locale is Locale.RU else (TARGET_PATH, SOURCE_PATH)
    )
    key = PairKey(RepoPath("page.md"))
    entry = ScopeEntry(
        FilePair(source_locale, target_locale, source_path, target_path),
        source,
        target,
        ScopeOrigin.INITIAL,
        FileOperation.TRANSLATE,
        (key,),
        None,
        None,
    )
    plan = build_markdown_plan(SNAPSHOT, source_path, source)
    return Document(entry, source, plan, build_translation_request(source, plan))


def content_with(models: object, environment: dict[str, str] | None = None) -> RuntimeContent:
    return RuntimeContent(
        cast(RuntimeSource, object()),
        cast(RecordedModels, models),
        environment or {},
    )


def test_probable_duplicate_pairs_missing_symmetric_link_with_existing_target_link() -> None:
    source = b"See [query hints](optimization/hints.md).\n"
    target = b"See [query hints](query-execution-optimization/query-hints.md).\n"
    entry = document_for(source, target=target).entry
    missing_source = RepoPath("ydb/docs/ru/core/optimization/hints.md")
    missing_target = RepoPath("ydb/docs/en/core/optimization/hints.md")
    old_target = RepoPath(
        "ydb/docs/en/core/query-execution-optimization/query-hints.md"
    )
    resolved = dependencies.ResolvedDependency(
        dependencies.DependencyLink(SOURCE_PATH, missing_source),
        missing_source,
        missing_target,
        dependencies.DependencyResolutionState.TARGET_MISSING_SOURCE_EXISTS,
    )

    class Reader:
        def read_bytes(self, snapshot: SnapshotRef, path: RepoPath, /) -> bytes | None:
            return b"# Existing article\n" if path == old_target else None

    warnings = runtime_content._probable_duplicate_warnings(
        Reader(), SNAPSHOT, (entry,), (resolved,)
    )

    assert tuple((item.new_target_path, item.existing_target_path) for item in warnings) == (
        (missing_target, old_target),
    )


def test_dependency_article_is_added_to_symmetric_target_toc() -> None:
    target_snapshot = SnapshotRef(SNAPSHOT.repository, GitSha("b" * 40))
    source_path = RepoPath("ydb/docs/ru/core/optimization/hints.md")
    target_path = RepoPath("ydb/docs/en/core/optimization/hints.md")
    values = {
        (SNAPSHOT, RepoPath("ydb/docs/ru/core/toc.yaml")): (
            b"items:\n  - name: Hints\n    href: optimization/hints.md\n"
        ),
        (target_snapshot, RepoPath("ydb/docs/en/core/toc.yaml")): b"items:\n",
    }

    class Reader:
        def read_bytes(self, snapshot: SnapshotRef, path: RepoPath, /) -> bytes | None:
            return values.get((snapshot, path))

    entry = ScopeEntry(
        FilePair(Locale.RU, Locale.EN, source_path, target_path),
        b"# Hints\n",
        None,
        ScopeOrigin.DEPENDENCY,
        FileOperation.TRANSLATE,
        (PairKey(RepoPath("changelog.md")),),
        None,
        None,
    )
    source = cast(RuntimeSource, SimpleNamespace(github=Reader()))
    content = RuntimeContent(source, cast(RecordedModels, object()), {})
    preparation = cast(
        FrozenPreparation,
        SimpleNamespace(
            snapshots=SimpleNamespace(source_snapshot=SNAPSHOT),
            metadata_snapshot=target_snapshot,
            inventory=SimpleNamespace(files=()),
        ),
    )
    files: dict[str, bytes | None] = {}

    content._metadata(preparation, entry, files)

    target_toc = files["ydb/docs/en/core/toc.yaml"]
    assert target_toc is not None
    assert b"href: optimization/hints.md" in target_toc


@pytest.mark.parametrize(
    ("source_locale", "direction"),
    [(Locale.RU, "authoritative ru Markdown"), (Locale.EN, "authoritative en Markdown")],
)
def test_translate_document_uses_complete_markdown_and_selected_direction(
    source_locale: Locale, direction: str
) -> None:
    document = document_for(
        "# Исходный заголовок\n\nТекст с [руководством](guide.md).\n\n- Один\n- Два\n".encode(),
        source_locale=source_locale,
    )
    response = (
        "# Translated heading\n\nText with "
        "[[YDBDOC_PROTECTED_LINK_0001_OPEN]]guide"
        "[[YDBDOC_PROTECTED_LINK_0001_CLOSE]].\n\n- One\n- Two\n"
    )
    models = ScriptedModels([response])

    accepted = content_with(models).translate_document(document)

    assert len(models.calls) == 1
    call = models.calls[0]
    assert direction in call.prompt
    assert "Synchronize the existing" in call.prompt
    assert "# Исходный заголовок" in call.prompt
    assert "- Один\n- Два" in call.prompt
    assert "guide.md" not in call.prompt
    assert (
        "[[YDBDOC_PROTECTED_LINK_0001_OPEN]]руководством"
        "[[YDBDOC_PROTECTED_LINK_0001_CLOSE]]"
        in call.prompt
    )
    assert "# Old target" in call.prompt
    assert "JSON" not in call.prompt
    assert (
        assemble_candidate(document.source, document.plan, document.request, accepted.as_dict())
        == b"# Translated heading\n\nText with [guide](guide.md).\n\n- One\n- Two\n"
    )


def test_translate_without_existing_target_uses_full_translation_prompt() -> None:
    document = document_for(b"# New source page\n", target=None)
    models = ScriptedModels(["# New target page\n"])

    content_with(models).translate_document(document)

    assert "Translate the complete Markdown below from ru to en" in models.calls[0].prompt
    assert "<EXISTING_TARGET_EN>" not in models.calls[0].prompt


def test_existing_target_cannot_override_symmetric_source_link_destination() -> None:
    document = document_for(
        b"See [query hints](./dev/optimization/hints.md).\n",
        target=b"See [query hints](./dev/query-execution-optimization/query-hints.md).\n",
    )
    models = ScriptedModels(
        [
            (
                "See [[YDBDOC_PROTECTED_LINK_0001_OPEN]]query hints"
                "[[YDBDOC_PROTECTED_LINK_0001_CLOSE]].\n"
            )
        ]
    )

    _accepted, accepted_document = content_with(models)._translate_document(document)

    assert "(./dev/optimization/hints.md)" not in models.calls[0].prompt
    assert (
        "[[YDBDOC_PROTECTED_LINK_0001_OPEN]]query hints"
        "[[YDBDOC_PROTECTED_LINK_0001_CLOSE]]"
        in models.calls[0].prompt
    )
    assert "(./dev/query-execution-optimization/query-hints.md)" in models.calls[0].prompt
    assert accepted_document.translated_markdown == (
        "See [query hints](./dev/optimization/hints.md).\n"
    )


def test_chunked_sync_uses_ordered_non_overlapping_target_excerpts() -> None:
    source = (
        "## Source one\n" + "a" * 900 + "\n\n"
        "## Source two\n" + "b" * 900 + "\n\n"
        "## Source three\n" + "c" * 900 + "\n"
    ).encode()
    target = (
        "## Target one\n" + "x" * 90 + "\n\n"
        "## Target two\n" + "y" * 90 + "\n\n"
        "## Target three\n" + "z" * 90 + "\n"
    ).encode()
    document = document_for(source, target=target)
    prepared = prepare_document(
        document.source,
        document.plan,
        max_characters=2400,
        source_locale="ru",
        target_locale="en",
    )
    assert len(prepared.chunks) == 3
    models = ScriptedModels([chunk.text for chunk in prepared.chunks])

    content_with(models, {"YDBDOC_MAX_MODEL_REQUEST_CHARACTERS": "2400"}).translate_document(
        document
    )

    assert len(models.calls) == 3
    for index, label in enumerate(("Target one", "Target two", "Target three")):
        prompt = models.calls[index].prompt
        assert label in prompt
        assert all(
            other not in prompt
            for other in ("Target one", "Target two", "Target three")
            if other != label
        )
        assert len(prompt) <= 2400


def test_translate_restores_source_final_lf_without_technical_correction() -> None:
    document = document_for(b"# Source heading\n")
    models = ScriptedModels(["# Translated heading", "unused correction"])

    accepted = content_with(models).translate_document(document)

    assert len(models.calls) == 1
    assert (
        assemble_candidate(document.source, document.plan, document.request, accepted.as_dict())
        == b"# Translated heading\n"
    )


def test_translate_accepts_list_indentation_drift_and_preserves_model_markdown() -> None:
    source = b"* Parent\n  * Nested source item\n"
    translated = "* Parent translated\n* Nested translated item\n"
    document = document_for(source)
    models = ScriptedModels([translated, "unused correction"])
    content = content_with(models)
    plans = cast(
        FrozenSourcePlans,
        SimpleNamespace(
            preparation=SimpleNamespace(for_translation=True),
            manifest=None,
            documents=(document,),
            fixed_files=(),
        ),
    )

    candidate = content._translate_documents(plans, (document,), (), ())

    assert len(models.calls) == 1
    assert unpack(candidate.content)[TARGET_PATH.value] == translated.encode()


@pytest.mark.parametrize(
    ("source_locale", "source", "translated"),
    [
        (
            Locale.RU,
            (
                "* В системные представления `.sys/top_queries_*` и "
                "`.sys/query_sessions` добавлена колонка `TraceId`.\n"
            ).encode(),
            (
                b"* The `TraceId` column was added to `.sys/top_queries_*` and "
                b"`.sys/query_sessions`.\n"
            ),
        ),
        (
            Locale.EN,
            (
                b"* The `TraceId` column was added to `.sys/top_queries_*` and "
                b"`.sys/query_sessions`.\n"
            ),
            (
                "* В системные представления `.sys/top_queries_*` и "
                "`.sys/query_sessions` добавлена колонка `TraceId`.\n"
            ).encode(),
        ),
    ],
    ids=["ru-to-en-live-order", "en-to-ru-inverse-order"],
)
def test_translate_accepts_field_local_inline_code_grammar_order(
    source_locale: Locale, source: bytes, translated: bytes
) -> None:
    document = document_for(source, source_locale=source_locale)
    prepared = prepare_document(document.source, document.plan, max_characters=100_000)
    response = translated.decode()
    for placeholder in prepared.placeholders:
        response = response.replace(placeholder.source_bytes.decode(), placeholder.token)
    models = ScriptedModels([response, response])

    accepted = content_with(models).translate_document(document)

    assert len(models.calls) == 1
    assert "exactly once" in models.calls[0].prompt
    assert "top-level source block" in models.calls[0].prompt
    assert (
        assemble_candidate(document.source, document.plan, document.request, accepted.as_dict())
        == translated
    )


def test_continuation_revalidates_field_local_inline_code_grammar_order() -> None:
    source = (
        "* В системные представления `.sys/top_queries_*` и `.sys/query_sessions` "
        "добавлена колонка `TraceId`.\n"
    ).encode()
    translated = (
        b"* The `TraceId` column was added to `.sys/top_queries_*` and `.sys/query_sessions`.\n"
    )
    document = document_for(source)
    plans = FrozenSourcePlans(cast(FrozenPreparation, object()), None, (document,), ())

    restored = content_with(ScriptedModels([])).restore_accepted_documents(
        plans,
        (AcceptedDocument(document.entry.pair.target_path, translated.decode()),),
    )

    assert len(restored) == 1
    assert (
        assemble_candidate(document.source, document.plan, document.request, restored[0].as_dict())
        == translated
    )


@pytest.mark.parametrize("source_locale", [Locale.RU, Locale.EN])
def test_translate_rejects_link_groups_that_exchange_source_endpoints(
    source_locale: Locale,
) -> None:
    document = document_for(
        b"Read [one](one.md), then [two](two.md).\n",
        source_locale=source_locale,
    )
    swapped = (
        "Read [[YDBDOC_PROTECTED_0001]]one[[YDBDOC_PROTECTED_0004]], then "
        "[[YDBDOC_PROTECTED_0003]]two[[YDBDOC_PROTECTED_0002]].\n"
    )
    models = ScriptedModels([swapped, swapped])

    with pytest.raises(InvalidTranslationResponse, match="translation_response_invalid"):
        content_with(models).translate_document(document)

    assert len(models.calls) == 2


@pytest.mark.parametrize("source_locale", [Locale.RU, Locale.EN])
def test_translate_rejects_crossed_link_group_intervals(source_locale: Locale) -> None:
    document = document_for(
        b"Read [one](one.md), then [two](two.md).\n",
        source_locale=source_locale,
    )
    crossed = (
        "Read [[YDBDOC_PROTECTED_0001]]one[[YDBDOC_PROTECTED_0003]], then "
        "[[YDBDOC_PROTECTED_0002]]two[[YDBDOC_PROTECTED_0004]].\n"
    )
    models = ScriptedModels([crossed, crossed])

    with pytest.raises(InvalidTranslationResponse, match="translation_response_invalid"):
        content_with(models).translate_document(document)

    assert len(models.calls) == 2


@pytest.mark.parametrize(
    ("second_response", "succeeds"),
    [
        (
            "---\ntitle: Fixed title\ndescription: Valid value\n---\nTranslated body.\n",
            True,
        ),
        (
            '---\ntitle: "Still broken\ndescription: Invalid again\n---\nTranslated body.\n',
            False,
        ),
    ],
    ids=["valid-correction", "invalid-correction"],
)
def test_malformed_frontmatter_response_uses_one_technical_correction(
    second_response: str, succeeds: bool
) -> None:
    source = b"---\ntitle: Source title\ndescription: Source value\n---\nSource body.\n"
    document = document_for(source)
    malformed = '---\ntitle: "Broken title\ndescription: Invalid value\n---\nTranslated body.\n'
    models = ScriptedModels([malformed, second_response])

    if succeeds:
        accepted = content_with(models).translate_document(document)
        assert (
            assemble_candidate(
                document.source,
                document.plan,
                document.request,
                accepted.as_dict(),
            )
            == second_response.encode()
        )
    else:
        with pytest.raises(InvalidTranslationResponse, match="translation_response_invalid"):
            content_with(models).translate_document(document)

    assert len(models.calls) == 2
    assert "document_response:structure_mismatch" in models.calls[1].prompt


@pytest.mark.parametrize("source_locale", [Locale.RU, Locale.EN])
def test_large_document_uses_minimum_response_safe_raw_chunks(
    source_locale: Locale,
) -> None:
    source = (
        "\n\n".join(f"Paragraph {number:03d} " + "x" * 775 for number in range(140)).encode()
        + b"\n"
    )
    assert 110_000 < len(source.decode()) < 112_000
    document = document_for(source, source_locale=source_locale)
    models = EchoChunkModels()

    accepted = content_with(
        models, {"YDBDOC_MAX_MODEL_REQUEST_CHARACTERS": "250000"}
    ).translate_document(document)

    raw_chunks = tuple(_source_from_prompt(call.prompt) for call in models.calls)
    assert len(raw_chunks) > 1
    assert all(len(chunk) <= 16_000 for chunk in raw_chunks)
    assert all(len(left + right) > 16_000 for left, right in pairwise(raw_chunks))
    assert all(call.max_tokens == 8_000 and call.schema is None for call in models.calls)
    assert "".join(raw_chunks).encode() == source
    assert (
        assemble_candidate(document.source, document.plan, document.request, accepted.as_dict())
        == source
    )


@pytest.mark.parametrize("source_locale", [Locale.RU, Locale.EN])
def test_exhausted_content_filter_splits_only_original_chunk_nearest_midpoint(
    source_locale: Locale,
) -> None:
    source = content_filter_witness(with_leading_chunk=True)
    document = document_for(source, source_locale=source_locale)
    prepared = prepare_document(
        source,
        document.plan,
        max_characters=250_000,
        source_locale=source_locale.value,
        target_locale=(Locale.EN if source_locale is Locale.RU else Locale.RU).value,
    )
    assert [
        (len(chunk.text), chunk.block_end - chunk.block_start) for chunk in prepared.chunks
    ] == [
        (15_900, 1),
        (15_801, 111),
    ]
    first, filtered = prepared.chunks
    models = ScriptedModels(
        [
            first.text,
            ModelCallResult(None, AttemptError.CONTENT_FILTER, ()),
            filtered.text[:7_870],
            filtered.text[7_870:],
        ]
    )

    accepted = content_with(
        models, {"YDBDOC_MAX_MODEL_REQUEST_CHARACTERS": "250000"}
    ).translate_document(document)

    raw_requests = tuple(_source_from_prompt(call.prompt) for call in models.calls)
    assert tuple(map(len, raw_requests)) == (15_900, 15_801, 7_870, 7_931)
    assert raw_requests.count(first.text) == 1
    assert raw_requests[2] + raw_requests[3] == filtered.text
    assert (
        assemble_candidate(document.source, document.plan, document.request, accepted.as_dict())
        == source
    )


def test_content_filter_uses_only_boundary_even_when_one_child_exceeds_half_cap() -> None:
    source = (_heading_block(1, 9_000) + _heading_block(2, 1_000)).encode()
    document = document_for(source)
    prepared = prepare_document(
        source,
        document.plan,
        max_characters=250_000,
        source_locale="ru",
        target_locale="en",
    )
    assert [
        (len(chunk.text), chunk.block_end - chunk.block_start) for chunk in prepared.chunks
    ] == [(10_000, 2)]
    models = ScriptedModels(
        [
            ModelCallResult(None, AttemptError.CONTENT_FILTER, ()),
            prepared.chunks[0].text[:9_000],
            prepared.chunks[0].text[9_000:],
        ]
    )

    accepted = content_with(models).translate_document(document)

    raw_requests = tuple(_source_from_prompt(call.prompt) for call in models.calls)
    assert tuple(map(len, raw_requests)) == (10_000, 9_000, 1_000)
    assert raw_requests[1] + raw_requests[2] == raw_requests[0]
    assert (
        assemble_candidate(document.source, document.plan, document.request, accepted.as_dict())
        == source
    )


def test_content_filter_children_do_not_repeat_filtered_target_reference() -> None:
    source = content_filter_witness()
    document = document_for(source, target=b"# Existing target reference\n")
    prepared = prepare_document(
        source,
        document.plan,
        max_characters=250_000,
        source_locale="ru",
        target_locale="en",
    )
    parent = prepared.chunks[0]
    models = ScriptedModels(
        [
            ModelCallResult(None, AttemptError.CONTENT_FILTER, ()),
            parent.text[:7_870],
            parent.text[7_870:],
        ]
    )

    content_with(models).translate_document(document)

    assert "<EXISTING_TARGET_EN>" in models.calls[0].prompt
    assert all("<EXISTING_TARGET_EN>" not in call.prompt for call in models.calls[1:])


def test_content_filter_in_child_recursively_splits_and_preserves_document() -> None:
    source = content_filter_witness()
    document = document_for(source)
    models = FilterTwiceThenEchoModels()

    _accepted, translated = content_with(
        models, {"YDBDOC_MAX_MODEL_REQUEST_CHARACTERS": "250000"}
    )._translate_document(document)

    assert translated.translated_markdown.encode() == source
    assert len(models.calls) == 5
    assert len(_source_from_prompt(models.calls[0].prompt)) == 15_801
    assert len(_source_from_prompt(models.calls[1].prompt)) == 7_870


def test_content_filter_without_top_level_boundary_is_terminal() -> None:
    document = document_for(_heading_block(1, 10_000).encode())
    models = ScriptedModels([ModelCallResult(None, AttemptError.CONTENT_FILTER, ())])

    with pytest.raises(RuntimeBoundaryError, match="translation_model_failed"):
        content_with(models).translate_document(document)

    assert len(models.calls) == 1


def test_non_final_parent_does_not_trigger_adaptive_split() -> None:
    document = document_for(content_filter_witness())
    models = ScriptedModels([ModelCallResult(None, AttemptError.NON_FINAL, ())])

    with pytest.raises(RuntimeBoundaryError, match="translation_model_failed"):
        content_with(models).translate_document(document)

    assert len(models.calls) == 1


def test_content_filter_on_technical_correction_does_not_publish_invalid_response() -> None:
    document = document_for(b"# See [guide](guide.md).\n# Next heading\n")
    prepared = prepare_document(document.source, document.plan, max_characters=100_000)
    missing_placeholder = prepared.placeholders[0]
    invalid = prepared.chunks[0].text.replace(missing_placeholder.token, "", 1)
    models = ScriptedModels([invalid, ModelCallResult(None, AttemptError.CONTENT_FILTER, ())])
    content = content_with(models)

    with pytest.raises(RuntimeBoundaryError, match="translation_model_failed"):
        content._translate_document(document)

    assert len(models.calls) == 2
    assert "Important correction" in models.calls[1].prompt


def test_complete_markdown_response_gets_exactly_one_technical_correction() -> None:
    document = document_for(b"# Use `CPUTime` now.\n")
    prepared = prepare_document(document.source, document.plan, max_characters=100_000)
    valid = prepared.chunks[0].text
    placeholder = next(item for item in prepared.placeholders if item.source_bytes == b"`CPUTime`")
    invalid = valid.replace(placeholder.token, "", 1)
    models = ScriptedModels([invalid, invalid])
    content = content_with(models)

    with pytest.raises(InvalidTranslationResponse, match="translation_response_invalid"):
        content._translate_document(document)

    assert len(models.calls) == 2
    assert "Important correction" not in models.calls[0].prompt
    correction = models.calls[1].prompt
    assert "Important correction" in correction
    assert prepared.chunks[0].text in correction
    assert "Rejected translation:" not in correction
    assert placeholder.token in correction
    assert "`CPUTime`" in correction
    assert "reorder" in correction


def test_invalid_correction_does_not_fall_back_to_primary_invalid_response() -> None:
    document = document_for(b"# Use `CPUTime` now.\n")
    prepared = prepare_document(document.source, document.plan, max_characters=100_000)
    placeholder = prepared.placeholders[0]
    primary = prepared.chunks[0].text.replace(placeholder.token, "", 1)
    duplicated_correction = prepared.chunks[0].text.replace(
        placeholder.token, placeholder.token + placeholder.token, 1
    )
    models = ScriptedModels([primary, duplicated_correction])
    content = content_with(models)

    with pytest.raises(InvalidTranslationResponse, match="translation_response_invalid"):
        content._translate_document(document)

    assert len(models.calls) == 2


def test_exhausted_missing_placeholder_rejects_malformed_markdown() -> None:
    source = (
        b"* [First](a.md) uses `CPUTime`.\n"
        b"* [Second](b.md) uses `REPLACE INTO`.\n"
    )
    document = document_for(source)
    prepared = prepare_document(document.source, document.plan, max_characters=100_000)
    first_open, first_close, _missing_code, second_open, second_close, second_code = (
        item.token for item in prepared.placeholders
    )
    malformed = (
        f"* {first_open}First{first_close} uses . [\n"
        f"* Second]{second_open}{second_close} uses {second_code}.\n"
    )
    models = ScriptedModels([malformed, malformed])
    content = content_with(models)

    with pytest.raises(InvalidTranslationResponse, match="translation_response_invalid"):
        content._translate_document(document)

    assert len(models.calls) == 2


def test_missing_placeholder_does_not_bypass_malformed_frontmatter() -> None:
    source = b'---\ntitle: "Use `CPUTime`"\n---\n'
    document = document_for(source)
    prepared = prepare_document(document.source, document.plan, max_characters=100_000)
    placeholder = prepared.placeholders[0]
    invalid = prepared.chunks[0].text.replace(placeholder.token, "", 1).replace(
        '"\n---\n', "\n---\n"
    )
    models = ScriptedModels([invalid, invalid])
    content = content_with(models)

    with pytest.raises(InvalidTranslationResponse, match="translation_response_invalid"):
        content._translate_document(document)

    assert len(models.calls) == 2


@pytest.mark.parametrize(
    "malformed",
    (
        "# Use [[YDBDOC_PROTECTED_X]].\n",
        "# Use [[YDBDOC_PROTECTED_X.\n",
        "# Use [[YDBDOC_PROTECTED_0001.\n",
        "# Use [[YDBDOC_PROTECTED_\n0001]].\n",
    ),
)
def test_malformed_unknown_placeholder_remains_terminal_after_correction(
    malformed: str,
) -> None:
    document = document_for(b"# Use `CPUTime`.\n")
    models = ScriptedModels([malformed, malformed])
    content = content_with(models)

    with pytest.raises(InvalidTranslationResponse, match="translation_response_invalid"):
        content._translate_document(document)

    assert len(models.calls) == 2


def test_validate_plan_never_allows_unvalidated_translation_bytes() -> None:
    document = document_for(b"# Source\n")
    target_path = document.entry.pair.target_path
    invalid = b'---\ntitle: "unterminated\n---\n'
    content = content_with(ScriptedModels([]))
    content.documents = (document,)
    candidate = WorkflowCandidate(pack({target_path.value: invalid}), None)
    plan = PublicationPlan((FileChange(target_path, None, invalid),), ())

    with pytest.raises(yaml.YAMLError):
        content.validate_plan(cast(ImmutableRunSnapshot, object()), candidate, plan)


def test_validate_plan_rejects_nonsymmetric_existing_target_link() -> None:
    document = document_for(
        b"See [query hints](./dev/optimization/hints.md).\n",
        target=b"See [query hints](./dev/query-execution-optimization/query-hints.md).\n",
    )
    target_path = document.entry.pair.target_path
    localized = b"See [query hints](./dev/query-execution-optimization/query-hints.md).\n"
    content = content_with(ScriptedModels([]))
    content.documents = (document,)
    candidate = WorkflowCandidate(pack({target_path.value: localized}), None)
    plan = PublicationPlan((FileChange(target_path, document.entry.target_content, localized),), ())

    with pytest.raises(DocumentTranslationError, match="structure_mismatch"):
        content.validate_plan(cast(ImmutableRunSnapshot, object()), candidate, plan)


def test_lost_placeholder_candidate_is_not_created() -> None:
    document = document_for(b"# Use `CPUTime` now.\n")
    prepared = prepare_document(document.source, document.plan, max_characters=100_000)
    placeholder = next(item for item in prepared.placeholders if item.source_bytes == b"`CPUTime`")
    invalid = prepared.chunks[0].text.replace(placeholder.token, "", 1)
    models = ScriptedModels([invalid, invalid])
    content = content_with(models)
    with pytest.raises(InvalidTranslationResponse, match="translation_response_invalid"):
        content._translate_document(document)

    assert len(models.calls) == 2


def test_reordered_link_pairs_are_not_published() -> None:
    document = document_for(b"Read [one](one.md), then [two](two.md).\n")
    prepared = prepare_document(document.source, document.plan, max_characters=100_000)
    first_open, first_close, second_open, second_close = (
        item.token for item in prepared.placeholders
    )
    reordered = f"Read {second_open}two{second_close}, after {first_open}one{first_close}.\n"
    models = ScriptedModels([reordered, reordered])
    content = content_with(models)

    with pytest.raises(InvalidTranslationResponse, match="translation_response_invalid"):
        content._translate_document(document)

    assert len(models.calls) == 2


def test_live_nested_link_reorder_witness_is_not_published() -> None:
    document = document_for("* [Добавлена](issue) поддержка [репликации](guide).\n".encode())
    prepared = prepare_document(document.source, document.plan, max_characters=100_000)
    outer_open, outer_close, inner_open, inner_close = (
        item.token for item in prepared.placeholders
    )
    reordered = (
        f"* {inner_open}Support for replication{inner_close} "
        f"{outer_open}has been added{outer_close}.\n"
    )
    models = ScriptedModels([reordered, reordered])
    content = content_with(models)

    with pytest.raises(InvalidTranslationResponse, match="translation_response_invalid"):
        content._translate_document(document)


def test_exhausted_invalid_large_chunk_is_split_once_and_validated() -> None:
    source = content_filter_witness() + b"## Use `CPUTime` now.\n"
    document = document_for(source)
    prepared = prepare_document(
        source,
        document.plan,
        max_characters=250_000,
        source_locale="ru",
        target_locale="en",
    )
    assert len(prepared.chunks) == 1
    parent = prepared.chunks[0]
    placeholder = next(item for item in prepared.placeholders if item.source_bytes == b"`CPUTime`")
    invalid = parent.text.replace(placeholder.token, "", 1)
    models = InvalidTwiceThenEchoModels(invalid)
    content = content_with(models, {"YDBDOC_MAX_MODEL_REQUEST_CHARACTERS": "250000"})

    _accepted, accepted_document = content._translate_document(document)

    assert len(models.calls) == 4
    assert "Important correction" in models.calls[1].prompt
    assert accepted_document.translated_markdown.encode() == source


def test_invalid_adaptive_child_is_split_again_until_valid() -> None:
    source = content_filter_witness() + b"## Use `CPUTime` now.\n"
    document = document_for(source)
    prepared = prepare_document(
        source,
        document.plan,
        max_characters=250_000,
        source_locale="ru",
        target_locale="en",
    )
    parent = prepared.chunks[0]
    placeholder = next(item for item in prepared.placeholders if item.source_bytes == b"`CPUTime`")
    parent_invalid = parent.text.replace(placeholder.token, "", 1)
    models = NestedInvalidThenEchoModels(parent_invalid)

    _accepted, accepted_document = content_with(
        models, {"YDBDOC_MAX_MODEL_REQUEST_CHARACTERS": "250000"}
    )._translate_document(document)

    assert len(models.calls) == 10
    assert accepted_document.translated_markdown.encode() == source


def test_large_chunk_splits_when_technical_correction_is_filtered() -> None:
    source = content_filter_witness() + b"## Use `CPUTime` now.\n"
    document = document_for(source)
    prepared = prepare_document(
        source,
        document.plan,
        max_characters=250_000,
        source_locale="ru",
        target_locale="en",
    )
    parent = prepared.chunks[0]
    placeholder = next(item for item in prepared.placeholders if item.source_bytes == b"`CPUTime`")
    invalid = parent.text.replace(placeholder.token, "", 1)
    models = InvalidThenFilteredThenEchoModels(invalid)

    _accepted, accepted_document = content_with(
        models, {"YDBDOC_MAX_MODEL_REQUEST_CHARACTERS": "250000"}
    )._translate_document(document)

    assert len(models.calls) == 4
    assert "Important correction" in models.calls[1].prompt
    assert accepted_document.translated_markdown.encode() == source


def test_provider_failure_is_not_semantically_retried() -> None:
    document = document_for(b"# Source heading\n")
    models = ScriptedModels([ModelCallResult(None, AttemptError.TRANSPORT, ())])

    with pytest.raises(RuntimeBoundaryError, match="translation_model_failed"):
        content_with(models).translate_document(document)

    assert len(models.calls) == 1


def test_prompt_limit_failure_happens_before_model_call() -> None:
    document = document_for(b"# One complete top-level block that cannot fit.\n")
    models = ScriptedModels([])

    with pytest.raises(DocumentTranslationError, match="top_level_block_exceeds_limit"):
        content_with(models, {"YDBDOC_MAX_MODEL_REQUEST_CHARACTERS": "520"}).translate_document(
            document
        )

    assert models.calls == []


def test_response_cap_rejects_one_oversized_top_level_block_before_model_call() -> None:
    document = document_for(("One indivisible paragraph " + "x" * 16_001 + "\n").encode())
    models = ScriptedModels([])

    with pytest.raises(DocumentTranslationError, match="top_level_block_exceeds_limit"):
        content_with(models, {"YDBDOC_MAX_MODEL_REQUEST_CHARACTERS": "250000"}).translate_document(
            document
        )

    assert models.calls == []


def test_near_limit_correction_reservation_fails_before_model_call() -> None:
    document = document_for(b"# See [guide](guide.md).\n")
    prepared = prepare_document(document.source, document.plan, max_characters=100_000)
    invalid = prepared.chunks[0].text.replace(prepared.placeholders[0].token, "", 1)
    models = ScriptedModels([invalid])
    operator_context = "Reviewer context"
    initial_prompt = (
        build_document_prompt(prepared.chunks[0], "ru", "en")
        + "\n\nOperator context:\n"
        + operator_context
    )
    limit = len(initial_prompt)

    with pytest.raises(DocumentTranslationError, match="top_level_block_exceeds_limit"):
        content_with(
            models, {"YDBDOC_MAX_MODEL_REQUEST_CHARACTERS": str(limit)}
        ).translate_document(document, operator_context=operator_context)

    assert models.calls == []


def test_long_protected_fragment_correction_is_reserved_before_model_call() -> None:
    source = b"```text\n" + b"x" * 5_000 + b"\n```\n\nVisible prose.\n"
    document = document_for(source, target=None)
    models = ScriptedModels([])

    with pytest.raises(DocumentTranslationError, match="top_level_block_exceeds_limit"):
        content_with(models, {"YDBDOC_MAX_MODEL_REQUEST_CHARACTERS": "900"}).translate_document(
            document
        )

    assert models.calls == []


def test_multiblock_unit_accepts_cosmetic_blank_line_change_without_retry() -> None:
    source = (
        b"\n\n".join(
            (
                b"Paragraph one has thirty seven letters.",
                b"Paragraph two has thirty seven letters.",
                b"Paragraph three has thirty five chars.",
            )
        )
        + b"\n"
    )
    document = document_for(source, target=None)
    invalid = source.decode().replace("\n\n", "\n", 1)
    models = ScriptedModels([invalid])
    operator_context = "Reviewer context"

    content_with(models, {"YDBDOC_MAX_MODEL_REQUEST_CHARACTERS": "900"}).translate_document(
        document, operator_context=operator_context
    )

    assert len(models.calls) == 1
    assert all(len(call.prompt) <= 900 for call in models.calls)


def test_nested_yfm_fence_code_mutation_gets_one_technical_correction() -> None:
    source = (
        b'{% note info "Title" %}\n```python\nprint("OPAQUE_CODE")\n'
        b"# Translatable comment\n```\n{% endnote %}\n"
    )
    document = document_for(source)
    prepared = prepare_document(document.source, document.plan, max_characters=100_000)
    valid = prepared.chunks[0].text
    invalid = valid.replace("OPAQUE_CODE", "MUTATED_CODE").replace(
        "[[YDBDOC_PROTECTED_0001]]", 'print("MUTATED_CODE")'
    )
    models = ScriptedModels([invalid, valid])

    content_with(models).translate_document(document)

    assert len(models.calls) == 2
    assert "OPAQUE_CODE" not in models.calls[0].prompt
    assert "Important correction" in models.calls[1].prompt
    assert "[[YDBDOC_PROTECTED_0001]]" in models.calls[1].prompt
    assert "OPAQUE_CODE" in models.calls[1].prompt


def test_translation_trace_is_payload_free(capsys: pytest.CaptureFixture[str]) -> None:
    document = document_for(b"# PRIVATE SOURCE HEADING\n")
    models = ScriptedModels(["# PRIVATE TRANSLATED HEADING\n"])

    content_with(models).translate_document(document)

    output = capsys.readouterr().err
    events = [json.loads(line.removeprefix("YDBDOC_TRACE ")) for line in output.splitlines()]
    assert [(event["operation"], event["status"]) for event in events] == [
        ("chunk", "start"),
        ("chunk", "ok"),
    ]
    assert "PRIVATE SOURCE" not in output
    assert "PRIVATE TRANSLATED" not in output
