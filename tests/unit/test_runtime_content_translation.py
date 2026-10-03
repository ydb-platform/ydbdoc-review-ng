from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import cast

import pytest
import yaml

from ydbdoc_review_ng import dependencies, runtime_content
from ydbdoc_review_ng.application import ImmutableRunSnapshot, WorkflowCandidate
from ydbdoc_review_ng.continuation import AcceptedDocument, SourceChange, SourceChangeInventory
from ydbdoc_review_ng.direction import InventoryFile, inventory_request
from ydbdoc_review_ng.domain import (
    FilePair,
    GitSha,
    Locale,
    RepoPath,
    RepositoryId,
    SnapshotRef,
)
from ydbdoc_review_ng.locales import PairKey
from ydbdoc_review_ng.models import (
    AttemptError,
    ModelCallResult,
    ModelRequest,
    YandexCredentials,
    YandexOpenAIClient,
)
from ydbdoc_review_ng.parser.markdown import build_markdown_plan
from ydbdoc_review_ng.publication import FileChange, PublicationPlan
from ydbdoc_review_ng.quality import review_pr
from ydbdoc_review_ng.runtime import RecordedModels, RuntimeSource
from ydbdoc_review_ng.runtime_content import (
    DirectionClient,
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
    build_translation_request,
    prepare_document,
)
from ydbdoc_review_ng.translation_plan import TranslationPlan
from tests.support.scripted_models import ScriptedModels as _SupportScripted

SOURCE_PATH = RepoPath("ydb/docs/ru/core/page.md")
TARGET_PATH = RepoPath("ydb/docs/en/core/page.md")
SNAPSHOT = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a" * 40))


class ScriptedModels:
    def __init__(self, responses: list[str | ModelCallResult]) -> None:
        self.responses = list(responses)
        self.calls: list[ModelRequest] = []
        self._tool = _SupportScripted(list(responses))

    def invoke(self, request: ModelRequest, /) -> ModelCallResult:
        from ydbdoc_review_ng.domain import ModelRole

        if request.role is ModelRole.CRITIC or (
            request.tools is not None
            or (request.schema is None and request.messages is not None)
        ):
            # Tool-using critic: reuse the shared expander (legacy files JSON → tools).
            result = self._tool.invoke(request)
            self.calls = self._tool.calls
            return result
        response = self.responses[len(self.calls)]
        self.calls.append(request)
        if type(response) is ModelCallResult:
            return response
        text = cast(str, response)
        # Production rejects raw Markdown (#22). Convert scripted chunk/prose into a
        # segment ID map when the response is not already JSON.
        if (
            request.schema is not None
            and "\nSegments: " in request.prompt
            and not _looks_like_json_object(text)
        ):
            converted = _scripted_text_to_segment_json(request, text)
            if converted is not None:
                text = converted
        return ModelCallResult(text, None, ())


def _looks_like_json_object(text: str) -> bool:
    stripped = text.lstrip()
    if not stripped.startswith("{"):
        return False
    try:
        return isinstance(json.loads(text), dict)
    except json.JSONDecodeError:
        return False


_SCRIPT_PLACEHOLDER = re.compile(r"\[\[YDBDOC_[A-Z]+_\d+\]\]")


def _scripted_text_to_segment_json(request: ModelRequest, text: str) -> str | None:
    encoded = request.prompt.split("\nSegments: ", 1)[1].split("\n\n", 1)[0]
    identity = json.loads(encoded)
    keys = list(identity.keys())
    # Leave unknown/malformed YDBDOC spellings raw so production rejects them.
    if "[[YDBDOC_" in text:
        tokens = re.findall(r"\[\[YDBDOC_[^\n\]]*(?:\]\])?", text)
        if not tokens or any(_SCRIPT_PLACEHOLDER.fullmatch(token) is None for token in tokens):
            return None
        parts = _SCRIPT_PLACEHOLDER.split(text)
        if len(parts) == len(keys) + 1 and parts[-1] == "":
            parts = parts[:-1]
        if len(parts) == len(keys):
            return json.dumps(dict(zip(keys, parts, strict=True)), ensure_ascii=False)
        return None
    if len(keys) == 1:
        return json.dumps({keys[0]: text}, ensure_ascii=False)
    source_prose = "".join(identity.values())
    if text == source_prose or text == _source_from_prompt(request.prompt):
        return json.dumps(identity, ensure_ascii=False)
    return None


def _source_from_prompt(prompt: str) -> str:
    if "\nSegments: " in prompt:
        encoded = prompt.split("\nSegments: ", 1)[1].split("\n\n", 1)[0]
        return "".join(json.loads(encoded).values())
    marker = "<AUTHORITATIVE_SOURCE_"
    if marker not in prompt:
        return prompt.split("\n\n", 1)[1]
    start = prompt.index("\n", prompt.index(marker)) + 1
    end = prompt.index("</AUTHORITATIVE_SOURCE_", start)
    return prompt[start:end]


def _echo_response(request: ModelRequest) -> str:
    if request.schema is not None and "\nSegments: " in request.prompt:
        encoded = request.prompt.split("\nSegments: ", 1)[1].split("\n\n", 1)[0]
        return json.dumps(json.loads(encoded), ensure_ascii=False)
    return _source_from_prompt(request.prompt)


class EchoChunkModels:
    def __init__(self) -> None:
        self.calls: list[ModelRequest] = []

    def invoke(self, request: ModelRequest, /) -> ModelCallResult:
        self.calls.append(request)
        return ModelCallResult(_echo_response(request), None, ())


class WireAccountingEchoModels(EchoChunkModels):
    def __init__(self) -> None:
        super().__init__()
        self.provider = YandexOpenAIClient(
            YandexCredentials("secret", "folder"), lambda wire: None, lambda attempt: None
        )
        self.budgets = []

    def invoke(self, request: ModelRequest, /) -> ModelCallResult:
        self.budgets.append(self.provider.prepare_request(request))
        return super().invoke(request)


class TranslatorThenCriticModels:
    def __init__(self, draft: str, corrected: str) -> None:
        self.draft = draft
        self.corrected = corrected
        self.calls: list[ModelRequest] = []

    def invoke(self, request: ModelRequest, /) -> ModelCallResult:
        self.calls.append(request)
        text = self.draft if len(self.calls) == 1 else self.corrected
        if (
            request.schema is not None
            and "\nSegments: " in request.prompt
            and not _looks_like_json_object(text)
        ):
            converted = _scripted_text_to_segment_json(request, text)
            if converted is not None:
                text = converted
        return ModelCallResult(text, None, ())


class InvalidTwiceThenEchoModels:
    def __init__(self, invalid: str) -> None:
        self.invalid = invalid
        self.calls: list[ModelRequest] = []

    def invoke(self, request: ModelRequest, /) -> ModelCallResult:
        self.calls.append(request)
        text = self.invalid if len(self.calls) <= 2 else _echo_response(request)
        if (
            len(self.calls) <= 2
            and request.schema is not None
            and "\nSegments: " in request.prompt
            and not _looks_like_json_object(text)
        ):
            converted = _scripted_text_to_segment_json(request, text)
            if converted is not None:
                text = converted
        return ModelCallResult(text, None, ())


class InvalidThenFilteredThenEchoModels:
    def __init__(self, invalid: str) -> None:
        self.invalid = invalid
        self.calls: list[ModelRequest] = []

    def invoke(self, request: ModelRequest, /) -> ModelCallResult:
        self.calls.append(request)
        if len(self.calls) == 1:
            return ModelCallResult(self.invalid, None, ())
        if len(self.calls) in {2, 3}:
            return ModelCallResult(None, AttemptError.CONTENT_FILTER, ())
        return ModelCallResult(_echo_response(request), None, ())


class FilterTwiceThenEchoModels:
    def __init__(self) -> None:
        self.calls: list[ModelRequest] = []

    def invoke(self, request: ModelRequest, /) -> ModelCallResult:
        self.calls.append(request)
        # Any recursive recovery would reach a later successful response.
        if len(self.calls) <= 4:
            return ModelCallResult(None, AttemptError.CONTENT_FILTER, ())
        return ModelCallResult(_echo_response(request), None, ())


def _heading_block(number: int, length: int) -> str:
    prefix = f"## Block {number:03d} "
    body_length = length - len(prefix) - 1
    return prefix + ("x " * body_length)[:body_length] + "\n"


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
    test_environment: dict[str, str] = {}
    if environment is not None:
        test_environment.update(environment)
    return RuntimeContent(
        cast(RuntimeSource, object()),
        cast(RecordedModels, models),
        test_environment,
    )


def test_translation_failure_cannot_invoke_yandex_fallback() -> None:
    document = document_for(b"# Use `CPUTime` now.\n")
    prepared = prepare_document(document.source, document.plan)
    valid = prepared.chunks[0].text
    models = ScriptedModels(
        [ModelCallResult(None, AttemptError.CONTENT_FILTER, ()), valid]
    )

    with pytest.raises(RuntimeBoundaryError, match="translation_model_failed"):
        content_with(models).translate_document(document)

    assert [call.model for call in models.calls] == ["deepseek-v4-flash"]


@pytest.mark.parametrize("role", ["translator", "classifier", "critic", "arbiter"])
def test_production_models_ignore_legacy_model_overrides(role) -> None:
    models = EchoChunkModels()
    explicit = content_with(
        models,
        {
            "YDBDOC_MODEL": "yandexgpt-5.1",
            "YDBDOC_MODEL_FALLBACK": "yandexgpt-5.1",
            "YDBDOC_MODEL_CRITIC": "yandexgpt-5.1",
            "YDBDOC_MODEL_CRITIC_FALLBACK": "yandexgpt-5.1",
            "YDBDOC_MODEL_ARBITER": "yandexgpt-5.1",
        },
    )

    if role == "translator":
        explicit.translate_document(document_for(b"# Complete document.\n"))
        calls = models.calls
    elif role == "classifier":
        before = SnapshotRef(SNAPSHOT.repository, GitSha("b" * 40))
        inventory = SourceChangeInventory(
            (SourceChange(SOURCE_PATH, "modified", None, None),),
            before.commit_sha,
            SNAPSHOT.commit_sha,
        )
        direction_models = ScriptedModels(
            [
                json.dumps(
                    {
                        "translation_required": True,
                        "direction": "ru_to_en",
                        "reason": "Translate the modified source page.",
                    }
                )
            ]
        )
        request = inventory_request(
            (InventoryFile(inventory.files[0], b"Before", b"Source", TARGET_PATH, None),),
            before,
            SNAPSHOT,
            (),
            explicit.model,
        )
        DirectionClient(direction_models, explicit.model).invoke(request, inventory)
        calls = direction_models.calls
    else:
        review_models = ScriptedModels(
            [
                json.dumps({"files": {TARGET_PATH.value: "# Target\n"}}),
                '{"verdict":"GREEN","findings":[]}',
            ]
        )
        review_pr(
            review_models,
            critic_model=explicit.critic_model,
            arbiter_model=explicit.arbiter_model,
            source_files={SOURCE_PATH.value: b"# Source\n"},
            translated_files={TARGET_PATH.value: b"# Target\n"},
            glossary_files={},
            validate_files=lambda files: None,
        )
        calls = [call for call in review_models.calls if call.role.value == role]
    assert calls
    assert {call.model for call in calls} == {"deepseek-v4-flash"}
    provider = YandexOpenAIClient(
        YandexCredentials("secret", "folder"), lambda wire: None, lambda attempt: None
    )
    for call in calls:
        budget = provider.prepare_request(call)
        payload = json.loads(budget.body)
        if role in {"critic", "arbiter"}:
            assert call.max_output_tokens is not None
            assert payload["max_tokens"] == call.max_output_tokens
            assert payload["max_tokens"] < 1_048_576 - len(budget.body)
        else:
            assert payload["max_tokens"] == 1_048_576 - len(budget.body)


def test_translation_context_overflow_stops_before_transport_without_splitting() -> None:
    sent = []
    provider = YandexOpenAIClient(
        YandexCredentials("secret", "folder"), sent.append, lambda attempt: None
    )
    runtime = content_with(provider)
    with pytest.raises(ValueError, match="context"):
        runtime.translate_document(
            document_for(b"# Complete document.\n"), operator_context="x" * 1_048_576
        )
    assert sent == []


def test_document_assembly_trace_contains_failing_stage_and_reason(monkeypatch) -> None:
    document = document_for(b"# Complete document.\n")
    events: list[tuple[str, str, str, dict[str, object]]] = []

    def record(component: str, operation: str, status: str, /, **details: object) -> None:
        events.append((component, operation, status, details))

    def fail_restore(*args, **kwargs):
        raise DocumentTranslationError("document_response:placeholder_mismatch")

    monkeypatch.setattr(runtime_content, "write_trace", record)
    monkeypatch.setattr(runtime_content, "restore_document", fail_restore)

    with pytest.raises(InvalidTranslationResponse, match="translation_response_invalid"):
        content_with(EchoChunkModels()).translate_document(document)

    assert (
        "translation",
        "document_assembly",
        "fail",
        {
            "article": TARGET_PATH.value,
            "stage": "restore_document",
            "code": "document_response:placeholder_mismatch",
            "error_type": "DocumentTranslationError",
        },
    ) in events


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
    [(Locale.RU, "ru to en"), (Locale.EN, "en to ru")],
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
        "[guide]([[YDBDOC_URL_0001]]).\n\n- One\n- Two\n"
    )
    models = ScriptedModels([response])

    accepted = content_with(models).translate_document(document)

    assert len(models.calls) == 1
    call = models.calls[0]
    assert (
        f"from {source_locale.value} to "
        f"{(Locale.EN if source_locale is Locale.RU else Locale.RU).value}"
        in call.prompt
    )
    assert "# Исходный заголовок" in call.prompt
    assert json.dumps("- Один\n- Два", ensure_ascii=False)[1:-1] in call.prompt
    prose = call.prompt.split("<PRESENTATION_REFERENCE_", 1)[0]
    assert "guide.md" not in prose
    assert "[руководством]" in call.prompt
    assert "YDBDOC_URL" not in call.prompt
    assert "<PRESENTATION_REFERENCE_" in call.prompt
    assert "# Old target" in call.prompt
    assert "formatting/presentation reference only" in call.prompt
    assert call.schema is not None
    assert (
        assemble_candidate(document.source, document.plan, document.request, accepted.as_dict())
        == b"# Translated heading\n\nText with [guide](guide.md).\n\n- One\n- Two\n"
    )


def test_translator_draft_gets_one_technical_correction() -> None:
    document = document_for(
        "# Исходный заголовок\n\nТекст со [ссылкой](guide.md).\n".encode(),
        target=None,
    )
    draft = (
        "# Translated heading\n\nText with [link](guide.md).\n"
    )
    corrected = "# Translated heading\n\nText with [link]([[YDBDOC_URL_0001]]).\n"
    models = TranslatorThenCriticModels(draft, corrected)

    _accepted, accepted_document = content_with(models)._translate_document(document)

    assert [call.role.value for call in models.calls] == ["translate", "translate"]
    assert "Segments:" in models.calls[1].prompt
    assert draft in models.calls[1].prompt
    assert "<PREVIOUS_RESPONSE>" in models.calls[1].prompt
    assert accepted_document.translated_markdown == (
        "# Translated heading\n\nText with [link](guide.md).\n"
    )


def test_correction_prompt_has_one_previous_response() -> None:
    document = document_for(
        "# Заголовок\n\nТекст со [ссылкой](guide.md).\n".encode(), target=None
    )
    models = TranslatorThenCriticModels(
        "# Heading\n\nText with [link](guide.md).\n",
        "# Heading\n\nText with [link]([[YDBDOC_URL_0001]]).\n",
    )

    content_with(models)._translate_document(document)

    critic_prompt = models.calls[1].prompt
    assert critic_prompt.count("<PREVIOUS_RESPONSE>") == 1
    assert critic_prompt.count("# Heading\n\nText with [link](guide.md).\n") == 1
    assert "Boundary contract:" in critic_prompt


def test_translation_uses_glossary_context_for_whole_document_request(monkeypatch) -> None:
    source = (
        "## FIRST\n\n" + ("first " * 180) + "\n\n"
        "## SECOND\n\n" + ("second " * 180) + "\n"
    ).encode()
    document = document_for(source, target=None)
    models = EchoChunkModels()
    content = content_with(models)
    contexts: list[str | None] = []

    def context_for_chunk(document, /, *, source_text=None):
        contexts.append(source_text)
        return "WHOLE_DOCUMENT_GLOSSARY"

    monkeypatch.setattr(content, "_terminology_context", context_for_chunk)

    content.translate_document(document)

    assert len(models.calls) == 1
    assert len(contexts) == 1
    assert contexts[0] is not None
    assert "FIRST" in contexts[0]
    assert "SECOND" in contexts[0]
    assert "WHOLE_DOCUMENT_GLOSSARY" in models.calls[0].prompt


def test_translation_sends_complete_oversized_glossary_section_to_model() -> None:
    source_text = "# Aardvark\n\n" + "aardvark prose. " * 700 + "aardvark prose.\n"
    source_section = (
        "#### Aardvark {#aardvark}\n\n**aardvark**.\n\n"
        + "source detail. " * 700
        + "source detail."
    )
    target_section = (
        "#### Target aardvark {#aardvark}\n\n**target aardvark**.\n\n"
        + "target detail. " * 700
        + "target detail."
    )

    class Github:
        def read_bytes(self, _snapshot: SnapshotRef, path: RepoPath, /) -> bytes | None:
            if path.value == "ydb/docs/ru/core/concepts/glossary.md":
                return source_section.encode()
            if path.value == "ydb/docs/en/core/concepts/glossary.md":
                return target_section.encode()
            return None

    models = WireAccountingEchoModels()
    content = content_with(models)
    content.source = cast(RuntimeSource, SimpleNamespace(github=Github()))
    content.plans = cast(
        FrozenSourcePlans,
        SimpleNamespace(
            preparation=SimpleNamespace(
                snapshots=SimpleNamespace(source_snapshot=SNAPSHOT),
                metadata_snapshot=SNAPSHOT,
            )
        ),
    )

    content.translate_document(document_for(source_text.encode(), target=None))

    assert 8_000 < len(source_text) < 200_000
    assert len(source_section) > 8_000
    assert len(target_section) > 8_000
    assert len(models.calls) == 1
    assert _source_from_prompt(models.calls[0].prompt) == source_text.rstrip("\n")
    assert source_section in models.calls[0].prompt
    assert target_section in models.calls[0].prompt
    assert len(models.budgets) == 1
    wire_body = json.loads(models.budgets[0].body)
    wire_prompt = wire_body["messages"][0]["content"]
    assert _source_from_prompt(wire_prompt) == source_text.rstrip("\n")
    assert source_section in wire_prompt
    assert target_section in wire_prompt
    assert wire_body["max_tokens"] == 1_048_576 - len(models.budgets[0].body)


def test_legacy_model_request_limit_environment_is_ignored() -> None:
    models = EchoChunkModels()

    content_with(
        models, {"YDBDOC_MAX_MODEL_REQUEST_CHARACTERS": "not-an-integer"}
    ).translate_document(document_for(b"# Complete document.\n", target=None))

    assert len(models.calls) == 1


def test_translate_without_existing_target_uses_full_translation_prompt() -> None:
    document = document_for(b"# New source page\n", target=None)
    models = ScriptedModels(["# New target page\n"])

    content_with(models).translate_document(document)

    assert "Translate the complete Markdown prose from ru to en" in models.calls[0].prompt
    assert "<PRESENTATION_REFERENCE_EN>" not in models.calls[0].prompt


def test_translate_sends_existing_target_as_presentation_reference_only() -> None:
    models = EchoChunkModels()
    content = content_with(models)

    content.translate_document(document_for(b"# Source heading\n", target=b"# Old target wording\n"))

    assert models.calls
    assert all("<PRESENTATION_REFERENCE_EN>" in call.prompt for call in models.calls)
    assert all("formatting/presentation reference only" in call.prompt for call in models.calls)
    assert all("Old target wording" in call.prompt for call in models.calls)
    assert all("<EXISTING_TARGET_EN>" not in call.prompt for call in models.calls)


def test_multiblock_document_uses_one_request_without_workflow_override() -> None:
    source = ("## First\n\n" + ("first " * 700) + "\n\n" + "## Second\n\n" + ("second " * 700) + "\n").encode()
    models = EchoChunkModels()

    RuntimeContent(
        cast(RuntimeSource, object()),
        cast(RecordedModels, models),
        {},
    ).translate_document(document_for(source, target=None))

    assert len(models.calls) == 1
    assert "First" in models.calls[0].prompt
    assert "Second" in models.calls[0].prompt


def test_large_source_document_uses_one_translator_request() -> None:
    source = "".join(
        f"## Entry {index}\n\nDefinition {index} with [a link](guide-{index}.md).\n\n"
        + ("Details. " * 90)
        + "\n"
        for index in range(1, 121)
    ).encode()
    models = EchoChunkModels()
    content = RuntimeContent(cast(RuntimeSource, object()), cast(RecordedModels, models), {})

    _accepted, translated = content._translate_document(document_for(source, target=None))

    assert 100_000 < len(source) < 200_000
    assert len(models.calls) == 1
    request_prose = _source_from_prompt(models.calls[0].prompt)
    assert request_prose.count("## Entry ") == 120
    assert "Definition 1 with [a link]()." in request_prose
    assert "Definition 120 with [a link]()." in request_prose
    assert translated.translated_markdown.encode() == source


def test_existing_target_cannot_override_symmetric_source_link_destination() -> None:
    document = document_for(
        b"See [query hints](./dev/optimization/hints.md).\n",
        target=b"See [query hints](./dev/query-execution-optimization/query-hints.md).\n",
    )
    models = ScriptedModels(
        [
            (
                "See [query hints]([[YDBDOC_URL_0001]]).\n"
            )
        ]
    )

    _accepted, accepted_document = content_with(models)._translate_document(document)

    prompt = models.calls[0].prompt
    prose = prompt.split("<PRESENTATION_REFERENCE_", 1)[0]
    assert "(./dev/optimization/hints.md)" not in prose
    assert "[query hints]" in prompt
    assert "YDBDOC_URL" not in prompt
    # Old destination may appear only inside the presentation-reference block.
    assert "(./dev/query-execution-optimization/query-hints.md)" not in prose
    assert "(./dev/query-execution-optimization/query-hints.md)" in prompt
    assert "(./dev/optimization/hints.md)" in accepted_document.translated_markdown
    assert accepted_document.translated_markdown == (
        "See [query hints](./dev/optimization/hints.md).\n"
    )


def test_whole_document_request_keeps_existing_target_as_presentation_reference() -> None:
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
    )
    assert len(prepared.chunks) == 1
    models = ScriptedModels([prepared.chunks[0].text])

    content_with(models).translate_document(document)

    assert len(models.calls) == 1
    prompt = models.calls[0].prompt
    assert "Source one" in prompt
    assert "Source three" in prompt
    assert "<PRESENTATION_REFERENCE_EN>" in prompt
    assert "formatting/presentation reference only" in prompt
    assert all(label in prompt for label in ("Target one", "Target two", "Target three"))


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
    """Grammar reorder around protected code is expressible via segment values.

    Placeholder tokens stay in source assembly order (§2 ID-map contract). The
    translated prose around them may change word order between locales.
    """
    document = document_for(source, source_locale=source_locale)
    prepared = prepare_document(document.source, document.plan)
    response = translated.decode()
    for placeholder in prepared.placeholders:
        response = response.replace(placeholder.source_bytes.decode(), placeholder.token)
    models = ScriptedModels([response])

    _accepted, accepted_document = content_with(models)._translate_document(document)

    assert len(models.calls) == 1
    assert "exactly once" in models.calls[0].prompt
    assert "runtime restores" in models.calls[0].prompt
    # Soft-publish must accept the assembled candidate; exact placeholder
    # adjacency follows source segment assembly rather than raw Markdown order.
    assert "TraceId" in accepted_document.translated_markdown
    assert ".sys/query_sessions" in accepted_document.translated_markdown
    assert "`" in accepted_document.translated_markdown


def test_structured_translator_cannot_delete_or_duplicate_protected_fragments() -> None:
    source = "Запустите `ydb` и откройте [руководство](guide.md).\n".encode()

    class StructuredTranslator:
        def __init__(self) -> None:
            self.calls: list[ModelRequest] = []

        def invoke(self, request: ModelRequest, /) -> ModelCallResult:
            self.calls.append(request)
            assert "`ydb`" not in request.prompt
            assert "guide.md" not in request.prompt
            encoded = request.prompt.split("\nSegments: ", 1)[1].split("\n\n", 1)[0]
            segments = json.loads(encoded)
            translated = {
                key: value.replace("Запустите", "Run").replace("и откройте", "and open").replace(
                    "руководство", "the guide"
                )
                for key, value in segments.items()
            }
            return ModelCallResult(json.dumps(translated, ensure_ascii=False), None, ())

    models = StructuredTranslator()
    _, accepted = content_with(models)._translate_document(document_for(source, target=None))

    assert len(models.calls) == 1
    assert models.calls[0].schema is not None
    assert accepted.translated_markdown == "Run `ydb` and open [the guide](guide.md).\n"


def test_continuation_revalidates_field_local_inline_code_grammar_order() -> None:
    source = (
        "* В системные представления `.sys/top_queries_*` и `.sys/query_sessions` "
        "добавлена колонка `TraceId`.\n"
    ).encode()
    translated = (
        b"* The `TraceId` column was added to `.sys/top_queries_*` and `.sys/query_sessions`.\n"
    )
    document = document_for(source)
    plans = FrozenSourcePlans(
        cast(FrozenPreparation, object()),
        None,
        (document,),
        (),
        cast(TranslationPlan, object()),
    )

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


def test_malformed_frontmatter_response_still_publishes_assembled_utf8() -> None:
    """REQUIREMENTS §2: frontmatter diagnostics must not block soft-publish."""
    source = b"---\ntitle: Source title\ndescription: Source value\n---\n"
    document = document_for(source)
    malformed = '---\ntitle: "Broken title\ndescription: Invalid value\n---\n'
    models = ScriptedModels([malformed])

    _accepted, accepted = content_with(models)._translate_document(document)

    assert len(models.calls) == 1
    assert accepted.translated_markdown == malformed


@pytest.mark.parametrize("source_locale", [Locale.RU, Locale.EN])
def test_large_document_uses_one_complete_request(
    source_locale: Locale,
) -> None:
    source = (
        "\n\n".join(f"Paragraph {number:03d} " + ("x " * 388)[:775] for number in range(140)).encode()
        + b"\n"
    )
    assert 110_000 < len(source.decode()) < 112_000
    document = document_for(source, source_locale=source_locale)
    models = EchoChunkModels()

    accepted = content_with(models).translate_document(document)

    assert len(models.calls) == 1
    request_prose = _source_from_prompt(models.calls[0].prompt)
    assert request_prose.count("Paragraph ") == 140
    assert all(call.schema is not None for call in models.calls)
    assert (
        assemble_candidate(document.source, document.plan, document.request, accepted.as_dict())
        == source
    )


@pytest.mark.parametrize("source_locale", [Locale.RU, Locale.EN])
def test_exhausted_content_filter_stops_original_chunk_without_split(
    source_locale: Locale,
) -> None:
    source = content_filter_witness(with_leading_chunk=True)
    document = document_for(source, source_locale=source_locale)
    prepared = prepare_document(
        source,
        document.plan,
    )
    assert len(prepared.chunks) == 1
    parent = prepared.chunks[0]
    models = ScriptedModels(
        [
            ModelCallResult(None, AttemptError.CONTENT_FILTER, ()),
        ]
    )

    with pytest.raises(RuntimeBoundaryError, match="translation_model_failed"):
        content_with(models).translate_document(document)

    assert len(models.calls) == 1
    assert len(_source_from_prompt(models.calls[0].prompt)) == len(parent.text.rstrip("\n"))


def test_content_filter_does_not_split_at_available_boundary() -> None:
    source = (_heading_block(1, 9_000) + _heading_block(2, 1_000)).encode()
    document = document_for(source)
    prepared = prepare_document(
        source,
        document.plan,
    )
    assert [
        (len(chunk.text), chunk.block_end - chunk.block_start) for chunk in prepared.chunks
    ] == [(10_000, 2)]
    models = ScriptedModels(
        [
            ModelCallResult(None, AttemptError.CONTENT_FILTER, ()),
            ModelCallResult(None, AttemptError.CONTENT_FILTER, ()),
            prepared.chunks[0].text[:9_000],
            prepared.chunks[0].text[9_000:],
        ]
    )

    with pytest.raises(RuntimeBoundaryError, match="translation_model_failed"):
        content_with(models).translate_document(document)

    raw_requests = tuple(_source_from_prompt(call.prompt) for call in models.calls)
    assert tuple(map(len, raw_requests)) == (9_998,)


def test_content_filter_with_existing_target_is_terminal() -> None:
    source = content_filter_witness()
    document = document_for(source, target=b"# Existing target reference\n")
    prepared = prepare_document(
        source,
        document.plan,
    )
    parent = prepared.chunks[0]
    models = ScriptedModels(
        [
            ModelCallResult(None, AttemptError.CONTENT_FILTER, ()),
            parent.text[:7_870],
            parent.text[7_870:],
        ]
    )

    with pytest.raises(RuntimeBoundaryError, match="translation_model_failed"):
        content_with(models).translate_document(document)

    assert len(models.calls) == 1
    assert all("<PRESENTATION_REFERENCE_EN>" in call.prompt for call in models.calls)
    assert all("# Existing target reference" in call.prompt for call in models.calls)
    assert all("formatting/presentation reference only" in call.prompt for call in models.calls)


def test_protected_only_chunk_bypasses_model() -> None:
    source = b"```text\nopaque technical content\n```\n"
    document = document_for(source)
    models = ScriptedModels([])

    _accepted, translated = content_with(models)._translate_document(
        document,
        operator_context="Continue the saved translation.",
    )

    assert translated.translated_markdown.encode() == source
    assert models.calls == []


def test_operator_context_is_not_part_of_authoritative_markdown() -> None:
    source = b"# Source heading\n\nSource paragraph.\n"
    document = document_for(source, target=None)
    models = EchoChunkModels()

    _accepted, translated = content_with(models)._translate_document(
        document,
        operator_context="Keep every link pair separate.",
    )

    assert translated.translated_markdown.encode() == source
    assert "Segments:" in models.calls[0].prompt
    assert "<OPERATOR_GUIDANCE>" in models.calls[0].prompt
    assert "never include or translate it in the output" in models.calls[0].prompt


def test_same_page_target_fragment_must_exist_in_candidate() -> None:
    document = document_for(b"## Source {#source}\n\nSee [section](#source).\n")
    content = content_with(ScriptedModels([]))
    valid = b"## Target {#target}\n\nSee [section](#target).\n"
    dangling = b"## Other {#other}\n\nSee [section](#target).\n"

    assert content._link_resolver(document, valid).allows("#source", "#target")
    assert not content._link_resolver(document, dangling).allows("#source", "#target")
    unchanged_dangling = b"## Other {#other}\n\nSee [section](#source).\n"
    assert not content._link_resolver(document, unchanged_dangling).allows(
        "#source", "#source"
    )


def test_absolute_internal_link_fragment_is_checked_against_target_page() -> None:
    destination = (
        "https://ydb.tech/docs/ru/concepts/table#row-oriented-table"
    )
    document = document_for(f"See [table]({destination}).\n".encode())
    target_page = RepoPath("ydb/docs/en/core/concepts/table.md")

    class Github:
        def read_bytes(self, _snapshot: SnapshotRef, path: RepoPath, /) -> bytes | None:
            if path == target_page:
                return b"## Row tables {#row-oriented-tables}\n"
            return None

    content = content_with(ScriptedModels([]))
    content.source = cast(RuntimeSource, SimpleNamespace(github=Github()))
    content.plans = cast(
        FrozenSourcePlans,
        SimpleNamespace(
            preparation=SimpleNamespace(
                snapshots=SimpleNamespace(source_snapshot=SNAPSHOT),
                metadata_snapshot=SNAPSHOT,
            )
        ),
    )

    assert content._link_resolver(document, None)(destination) == (
        "https://ydb.tech/docs/en/concepts/table#row-oriented-tables"
    )


def test_content_filter_does_not_create_recursive_child_calls() -> None:
    source = content_filter_witness()
    document = document_for(source)
    models = FilterTwiceThenEchoModels()

    with pytest.raises(RuntimeBoundaryError, match="translation_model_failed"):
        content_with(models)._translate_document(document)

    assert len(models.calls) == 1
    assert len(_source_from_prompt(models.calls[0].prompt)) == 15_800


def test_content_filter_without_top_level_boundary_is_terminal() -> None:
    document = document_for(_heading_block(1, 10_000).encode())
    models = ScriptedModels([
        ModelCallResult(None, AttemptError.CONTENT_FILTER, ()),
        ModelCallResult(None, AttemptError.CONTENT_FILTER, ()),
    ])

    with pytest.raises(RuntimeBoundaryError, match="translation_model_failed"):
        content_with(models).translate_document(document)

    assert len(models.calls) == 1


def test_non_final_parent_does_not_trigger_adaptive_split() -> None:
    document = document_for(content_filter_witness())
    models = ScriptedModels([
        ModelCallResult(None, AttemptError.NON_FINAL, ()),
        ModelCallResult(None, AttemptError.NON_FINAL, ()),
    ])

    with pytest.raises(RuntimeBoundaryError, match="translation_model_failed"):
        content_with(models).translate_document(document)

    assert len(models.calls) == 1


def test_content_filter_on_technical_correction_does_not_publish_invalid_response() -> None:
    document = document_for(b"# See [guide](guide.md).\n")
    prepared = prepare_document(document.source, document.plan)
    missing_placeholder = prepared.placeholders[0]
    invalid = prepared.chunks[0].text.replace(missing_placeholder.token, "", 1)
    models = ScriptedModels([
        invalid,
        ModelCallResult(None, AttemptError.CONTENT_FILTER, ()),
        ModelCallResult(None, AttemptError.CONTENT_FILTER, ()),
    ])
    content = content_with(models)

    with pytest.raises(RuntimeBoundaryError, match="translation_model_failed"):
        content._translate_document(document)

    assert len(models.calls) == 2
    assert "Important correction" in models.calls[1].prompt


def test_complete_markdown_response_gets_exactly_one_technical_correction() -> None:
    document = document_for(b"# Use `CPUTime` now.\n")
    prepared = prepare_document(document.source, document.plan)
    valid = prepared.chunks[0].text
    placeholder = next(item for item in prepared.placeholders if item.source_bytes == b"`CPUTime`")
    invalid = valid.replace(placeholder.token, "", 1)
    models = ScriptedModels([invalid, invalid])
    content = content_with(models)

    with pytest.raises(InvalidTranslationResponse, match="translation_response_invalid"):
        content._translate_document(document)

    assert len(models.calls) == 2
    assert "Important correction" not in models.calls[0].prompt
    assert "<PREVIOUS_RESPONSE>" not in models.calls[0].prompt
    correction = models.calls[1].prompt
    assert "Important correction" in correction
    assert f"<PREVIOUS_RESPONSE>\n{invalid}\n</PREVIOUS_RESPONSE>" in correction
    assert "Rejected translation:" not in correction
    assert placeholder.token not in correction
    assert "runtime restores" in correction


def test_invalid_correction_does_not_fall_back_to_primary_invalid_response() -> None:
    document = document_for(b"# Use `CPUTime` now.\n")
    prepared = prepare_document(document.source, document.plan)
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
    prepared = prepare_document(document.source, document.plan)
    first_url, _first_code, second_url, second_code = (
        item.token for item in prepared.placeholders
    )
    malformed = (
        f"* [First]({first_url}) uses . [\n"
        f"* Second]({second_url}) uses {second_code}.\n"
    )
    models = ScriptedModels([malformed, malformed])
    content = content_with(models)

    with pytest.raises(InvalidTranslationResponse, match="translation_response_invalid"):
        content._translate_document(document)

    assert len(models.calls) == 2


def test_missing_placeholder_does_not_bypass_malformed_frontmatter() -> None:
    source = b'---\ntitle: "Use `CPUTime`"\n---\n'
    document = document_for(source)
    prepared = prepare_document(document.source, document.plan)
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


def test_validate_plan_soft_publishes_malformed_yaml_frontmatter() -> None:
    """REQUIREMENTS §2/§7: YAML/parser diagnostics must not block publication (#5)."""
    from ydbdoc_review_ng.continuation import SourceChangeInventory
    from ydbdoc_review_ng.direction import Direction
    from ydbdoc_review_ng.domain import Mode
    from ydbdoc_review_ng.repository import BaseBranch, PullRequestState, ResolvedRepositorySnapshots
    from ydbdoc_review_ng.runtime_github import GitHubBackend
    from ydbdoc_review_ng.scope import PotentialScopeSet, ScopeManifest

    document = document_for(b"# Source\n")
    target_path = document.entry.pair.target_path
    invalid = b'---\ntitle: "unterminated\n---\n'
    snap = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a" * 40))
    source = RuntimeSource({}, cast(GitHubBackend, object()))
    source.snapshots = ResolvedRepositorySnapshots(
        PullRequestState.MERGED,
        BaseBranch("main"),
        snap,
        snap,
        snap,
        snap,
        snap,
        snap,
        snap,
    )
    content = RuntimeContent(source, cast(RecordedModels, ScriptedModels([])), {})
    preparation = FrozenPreparation(
        ImmutableRunSnapshot(Mode.DOC_TRANSLATE, snap.commit_sha, None, "translation/pr-1", None),
        source.snapshots,
        SourceChangeInventory(()),
        snap,
        (),
        PotentialScopeSet(snap, content.roots, (), None),
        True,
    )
    content.plans = FrozenSourcePlans(
        preparation,
        ScopeManifest(Direction.RU_TO_EN, snap, content.roots, (), (), 0, 0),
        (document,),
        (),
        TranslationPlan(Direction.RU_TO_EN, (), ()),
    )
    content.documents = (document,)
    candidate = WorkflowCandidate(pack({target_path.value: invalid}), None)
    plan = PublicationPlan((FileChange(target_path, None, invalid),), ())

    content.validate_plan(cast(ImmutableRunSnapshot, object()), candidate, plan)


def test_validate_plan_soft_publishes_nonsymmetric_existing_target_link() -> None:
    """REQUIREMENTS §2: link-shape diagnostics must not block soft-publish."""
    from ydbdoc_review_ng.continuation import SourceChangeInventory
    from ydbdoc_review_ng.direction import Direction
    from ydbdoc_review_ng.domain import Mode
    from ydbdoc_review_ng.repository import BaseBranch, PullRequestState, ResolvedRepositorySnapshots
    from ydbdoc_review_ng.runtime_github import GitHubBackend
    from ydbdoc_review_ng.scope import PotentialScopeSet, ScopeManifest

    document = document_for(
        b"See [query hints](./dev/optimization/hints.md).\n",
        target=b"See [query hints](./dev/query-execution-optimization/query-hints.md).\n",
    )
    target_path = document.entry.pair.target_path
    localized = b"See [query hints](./dev/query-execution-optimization/query-hints.md).\n"
    snap = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a" * 40))
    source = RuntimeSource({}, cast(GitHubBackend, object()))
    source.snapshots = ResolvedRepositorySnapshots(
        PullRequestState.MERGED,
        BaseBranch("main"),
        snap,
        snap,
        snap,
        snap,
        snap,
        snap,
        snap,
    )
    content = RuntimeContent(source, cast(RecordedModels, ScriptedModels([])), {})
    preparation = FrozenPreparation(
        ImmutableRunSnapshot(Mode.DOC_TRANSLATE, snap.commit_sha, None, "translation/pr-1", None),
        source.snapshots,
        SourceChangeInventory(()),
        snap,
        (),
        PotentialScopeSet(snap, content.roots, (), None),
        True,
    )
    content.plans = FrozenSourcePlans(
        preparation,
        ScopeManifest(Direction.RU_TO_EN, snap, content.roots, (), (), 0, 0),
        (document,),
        (),
        TranslationPlan(Direction.RU_TO_EN, (), ()),
    )
    content.documents = (document,)
    candidate = WorkflowCandidate(pack({target_path.value: localized}), None)
    plan = PublicationPlan((FileChange(target_path, document.entry.target_content, localized),), ())

    content.validate_plan(cast(ImmutableRunSnapshot, object()), candidate, plan)


def test_lost_placeholder_candidate_is_not_created() -> None:
    document = document_for(b"# Use `CPUTime` now.\n")
    prepared = prepare_document(document.source, document.plan)
    placeholder = next(item for item in prepared.placeholders if item.source_bytes == b"`CPUTime`")
    invalid = prepared.chunks[0].text.replace(placeholder.token, "", 1)
    models = ScriptedModels([invalid, invalid])
    content = content_with(models)
    with pytest.raises(InvalidTranslationResponse, match="translation_response_invalid"):
        content._translate_document(document)

    assert len(models.calls) == 2


def test_reordered_link_pairs_still_publish_with_structure_diagnostic() -> None:
    """REQUIREMENTS §2: link diagnostics must not block assembled UTF-8 publish."""
    document = document_for(b"Read [one](one.md), then [two](two.md).\n")
    prepared = prepare_document(document.source, document.plan)
    first_url, second_url = (
        item.token for item in prepared.placeholders
    )
    reordered = f"Read [two]({second_url}), after [one]({first_url}).\n"
    models = ScriptedModels([reordered])
    _accepted, accepted = content_with(models)._translate_document(document)

    assert len(models.calls) == 1
    assert "two" in accepted.translated_markdown


def test_live_nested_link_reorder_witness_still_publishes() -> None:
    """REQUIREMENTS §2: nested link reorder is a diagnostic, not a hard gate."""
    document = document_for("* [Добавлена](issue) поддержка [репликации](guide).\n".encode())
    prepared = prepare_document(document.source, document.plan)
    outer_url, inner_url = (
        item.token for item in prepared.placeholders
    )
    reordered = (
        f"* [Support for replication]({inner_url}) "
        f"[has been added]({outer_url}).\n"
    )
    models = ScriptedModels([reordered])
    _accepted, accepted = content_with(models)._translate_document(document)

    assert len(models.calls) == 1
    assert accepted.translated_markdown.startswith("* ")


def test_exhausted_invalid_large_document_stops_after_one_correction() -> None:
    source = content_filter_witness() + b"## Use `CPUTime` now.\n"
    document = document_for(source)
    prepared = prepare_document(
        source,
        document.plan,
    )
    assert len(prepared.chunks) == 1
    parent = prepared.chunks[0]
    placeholder = next(item for item in prepared.placeholders if item.source_bytes == b"`CPUTime`")
    invalid = parent.text.replace(placeholder.token, "", 1)
    models = InvalidTwiceThenEchoModels(invalid)
    content = content_with(models)

    with pytest.raises(InvalidTranslationResponse, match="translation_response_invalid"):
        content._translate_document(document)

    assert len(models.calls) == 2
    assert "Important correction" in models.calls[1].prompt


def test_large_chunk_stops_when_technical_correction_is_filtered() -> None:
    source = content_filter_witness() + b"## Use `CPUTime` now.\n"
    document = document_for(source)
    prepared = prepare_document(
        source,
        document.plan,
    )
    parent = prepared.chunks[0]
    placeholder = next(item for item in prepared.placeholders if item.source_bytes == b"`CPUTime`")
    invalid = parent.text.replace(placeholder.token, "", 1)
    models = InvalidThenFilteredThenEchoModels(invalid)

    with pytest.raises(RuntimeBoundaryError, match="translation_model_failed"):
        content_with(models)._translate_document(document)

    assert len(models.calls) == 2
    assert "Important correction" in models.calls[1].prompt


def test_provider_failure_is_not_semantically_retried() -> None:
    document = document_for(b"# Source heading\n")
    models = ScriptedModels([
        ModelCallResult(None, AttemptError.TRANSPORT, ()),
        ModelCallResult(None, AttemptError.TRANSPORT, ()),
    ])

    with pytest.raises(RuntimeBoundaryError, match="translation_model_failed"):
        content_with(models).translate_document(document)

    assert len(models.calls) == 1


def test_source_larger_than_legacy_response_cap_reaches_model() -> None:
    document = document_for(("One indivisible paragraph " + "x" * 16_001 + "\n").encode())
    models = EchoChunkModels()

    content_with(models).translate_document(document)

    assert len(models.calls) == 1
    assert "One indivisible paragraph" in models.calls[0].prompt


def test_correction_dialog_preserves_full_previous_response() -> None:
    document = document_for(b"# See [guide](guide.md).\n")
    prepared = prepare_document(document.source, document.plan)
    invalid = prepared.chunks[0].text.replace(prepared.placeholders[0].token, "", 1) + "x" * 10_000
    models = ScriptedModels([invalid, prepared.chunks[0].text])
    operator_context = "Reviewer context"

    accepted = content_with(models).translate_document(document, operator_context=operator_context)

    assert accepted
    assert len(models.calls) == 2
    assert "<PREVIOUS_RESPONSE>" in models.calls[1].prompt


def test_long_protected_fragment_does_not_inflate_model_prompt() -> None:
    source = b"```text\n" + b"x" * 5_000 + b"\n```\n\nVisible prose.\n"
    document = document_for(source, target=None)
    prepared = prepare_document(document.source, document.plan)
    models = ScriptedModels([prepared.chunks[0].text])

    content_with(models).translate_document(
        document
    )

    assert len(models.calls) == 1


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

    content_with(models).translate_document(
        document, operator_context=operator_context
    )

    assert len(models.calls) == 1


def test_nested_yfm_fence_code_mutation_gets_one_technical_correction() -> None:
    source = (
        b'{% note info "Title" %}\n```python\nprint("OPAQUE_CODE")\n'
        b"# Translatable comment\n```\n{% endnote %}\n"
    )
    document = document_for(source)
    prepared = prepare_document(document.source, document.plan)
    valid = prepared.chunks[0].text
    invalid = valid.replace("OPAQUE_CODE", "MUTATED_CODE").replace(
        "[[YDBDOC_PROTECTED_0001]]", 'print("MUTATED_CODE")'
    )
    models = ScriptedModels([invalid, valid])

    content_with(models).translate_document(document)

    assert len(models.calls) == 2
    assert "OPAQUE_CODE" not in models.calls[0].prompt
    assert "Important correction" in models.calls[1].prompt
    assert "runtime restores" in models.calls[1].prompt


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


def test_small_multiblock_invalid_document_stops_after_one_correction() -> None:
    source = b"# First `one`\n\n# Second `two`\n"
    document = document_for(source, target=None)
    prepared = prepare_document(source, document.plan)
    invalid = prepared.chunks[0].text.replace(prepared.placeholders[0].token, "")
    models = InvalidTwiceThenEchoModels(invalid)

    with pytest.raises(InvalidTranslationResponse, match="translation_response_invalid"):
        content_with(models)._translate_document(document)

    assert len(models.calls) == 2
    assert all(call.role.value == "translate" for call in models.calls)
    assert "<PREVIOUS_RESPONSE>" in models.calls[1].prompt
    assert "`one`" not in models.calls[1].prompt


def test_invalid_indivisible_chunk_has_two_calls_and_safe_diagnostic(capsys) -> None:
    document = document_for(b"# Private-prose `one`\n", target=None)
    models = ScriptedModels(["# bad\n", "# bad\n"])

    with pytest.raises(InvalidTranslationResponse):
        content_with(models)._translate_document(document)

    assert len(models.calls) == 2
    events = [json.loads(line.removeprefix("YDBDOC_TRACE "))
              for line in capsys.readouterr().err.splitlines()]
    failures = [event for event in events if event['operation'] == 'chunk_validation']
    assert len(failures) == 2
    assert all(event['code'] == 'document_response:placeholder_mismatch' for event in failures)
    assert all('Private-prose' not in json.dumps(event) for event in events)


def test_copied_russian_paragraph_gets_one_echo_correction() -> None:
    """REQUIREMENTS §2.2: one translator correction for residual Russian prose."""
    source = "Русский абзац о выполнении запросов должен быть полностью переведён.\n"
    translated = "The Russian paragraph about query execution must be fully translated.\n"
    models = ScriptedModels([source, translated])
    _, result = content_with(models)._translate_document(document_for(source.encode()))
    assert result.translated_markdown == translated
    assert len(models.calls) == 2
    assert "untranslated_source_prose" in models.calls[1].prompt


def test_repeated_source_echo_publishes_with_diagnostic_after_one_retry() -> None:
    """REQUIREMENTS §2.2: after one failed correction, publish and diagnose."""
    source = "Русский абзац о выполнении запросов должен быть полностью переведён.\n"
    models = ScriptedModels([source, source])
    content = content_with(models)
    _, result = content._translate_document(document_for(source.encode()))
    assert result.translated_markdown == source
    assert len(models.calls) == 2
    assert content._source_echo_diagnostics


def test_cyrillic_inside_protected_code_does_not_require_translation() -> None:
    source = '# Heading\n\n```text\nРусский абзац внутри кода сохраняется полностью без изменений.\n```\n'
    _, result = content_with(EchoChunkModels())._translate_document(document_for(source.encode()))
    assert result.translated_markdown == source


def test_decimal_comma_localization_does_not_invent_protected_paths() -> None:
    document = document_for("Время [задачи](task.md) — 1,79 с и 0,81 с.\n".encode())
    prepared = prepare_document(document.source, document.plan)
    response = prepared.chunks[0].text.replace("Время", "Time").replace("задачи", "task")
    response = response.replace("1,79 с и 0,81 с", "1.79 s and 0.81 s")
    models = ScriptedModels([response])
    _, result = content_with(models)._translate_document(document)
    assert result.translated_markdown == "Time [task](task.md) — 1.79 s and 0.81 s.\n"
    assert len(models.calls) == 1


def test_duplicate_translated_glossary_alias_is_left_to_critic_editor() -> None:
    source = (
        "**Семейство колонок**, **группа колонок** или **колоночная группа** — "
        "способ хранения данных.\n"
    )
    draft = (
        "**Column family**, **column group**, or **column group** is a way to "
        "store data.\n"
    )
    models = ScriptedModels([draft])

    _, result = content_with(models)._translate_document(document_for(source.encode()))

    assert result.translated_markdown == draft
    assert len(models.calls) == 1
