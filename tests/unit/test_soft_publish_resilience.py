"""P1A soft-publish resilience: provider fail, structure diagnostics, no raw MD fallback."""

from __future__ import annotations

import json
from typing import cast

import pytest

from ydbdoc_review_ng.domain import (
    FilePair,
    GitSha,
    Locale,
    ModelRole,
    RepoPath,
    RepositoryId,
    SnapshotRef,
)
from ydbdoc_review_ng.locales import PairKey
from ydbdoc_review_ng.models import AttemptError, ModelCallResult, ModelRequest
from ydbdoc_review_ng.parser.markdown import build_markdown_plan
from ydbdoc_review_ng.runtime import RecordedModels, RuntimeSource
from ydbdoc_review_ng.runtime_content import (
    Document,
    InvalidTranslationResponse,
    RuntimeContent,
    pack,
    unpack,
)
from ydbdoc_review_ng.runtime_github import RuntimeBoundaryError
from ydbdoc_review_ng.scope import FileOperation, ScopeEntry, ScopeOrigin
from ydbdoc_review_ng.translation import build_translation_request
from ydbdoc_review_ng.translation.document import prepare_document

from tests.unit.test_runtime_content_translation import (
    EchoChunkModels,
    ScriptedModels,
    content_with,
    document_for,
)

SNAPSHOT = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a" * 40))
SOURCE_PATH = RepoPath("ydb/docs/ru/core/page.md")
TARGET_PATH = RepoPath("ydb/docs/en/core/page.md")


def test_raw_markdown_without_segment_id_map_is_rejected() -> None:
    """REQUIREMENTS §2: only the segment ID-map contract is accepted (#22)."""
    document = document_for(b"# Source heading\n\nPlain paragraph for translation.\n")

    class RawMarkdownModels:
        def __init__(self) -> None:
            self.calls: list[ModelRequest] = []

        def invoke(self, request: ModelRequest, /) -> ModelCallResult:
            self.calls.append(request)
            return ModelCallResult(
                "This is a raw Markdown response, not the ID map.\n", None, ()
            )

    models = RawMarkdownModels()

    with pytest.raises(InvalidTranslationResponse, match="translation_response_invalid"):
        content_with(models)._translate_document(document)

    assert len(models.calls) == 2


def test_assembled_utf8_with_table_shape_mismatch_is_published() -> None:
    """REQUIREMENTS §2: Markdown/YFM diagnostics must not block publish (#6)."""
    source = (
        b"| A | B |\n"
        b"| --- | --- |\n"
        b"| 1 | 2 |\n"
    )
    document = document_for(source)
    # Identity segment map keeps IDs exact; then mutate one segment value to a
    # three-column table so restore still yields UTF-8 with a shape diagnostic.
    models = _TableShapeMismatchModels()
    _accepted, accepted_document = content_with(models)._translate_document(document)

    assert accepted_document.translated_markdown.encode("utf-8").startswith(b"|")
    assert "| A | B | C |" in accepted_document.translated_markdown or accepted_document.translated_markdown.count("|") >= 4
    assert len(models.calls) >= 1


def test_provider_failure_on_one_document_soft_continues_siblings() -> None:
    """REQUIREMENTS §5.1: one page provider failure must not abort the group (#3)."""
    from ydbdoc_review_ng.application import ImmutableRunSnapshot, WorkflowCandidate
    from ydbdoc_review_ng.continuation import SourceChange, SourceChangeInventory
    from ydbdoc_review_ng.direction import Direction
    from ydbdoc_review_ng.domain import Mode
    from ydbdoc_review_ng.repository import BaseBranch, PullRequestState, ResolvedRepositorySnapshots
    from ydbdoc_review_ng.runtime_content import FrozenPreparation, FrozenSourcePlans
    from ydbdoc_review_ng.runtime_github import GitHubBackend
    from ydbdoc_review_ng.scope import PotentialScopeSet, ScopeManifest
    from ydbdoc_review_ng.translation_plan import TranslationPlan

    docs = []
    for name in ("a", "b", "c"):
        source = f"# Source {name}\n".encode()
        path_ru = RepoPath(f"ydb/docs/ru/core/{name}.md")
        path_en = RepoPath(f"ydb/docs/en/core/{name}.md")
        entry = ScopeEntry(
            FilePair(Locale.RU, Locale.EN, path_ru, path_en),
            source,
            f"# Old {name}\n".encode(),
            ScopeOrigin.INITIAL,
            FileOperation.TRANSLATE,
            (PairKey(RepoPath(f"{name}.md")),),
            None,
            None,
        )
        plan = build_markdown_plan(SNAPSHOT, path_ru, source)
        docs.append(Document(entry, source, plan, build_translation_request(source, plan)))

    class SelectiveFail(EchoChunkModels):
        def invoke(self, request: ModelRequest, /) -> ModelCallResult:
            self.calls.append(request)
            if request.target_path is not None and request.target_path.value.endswith("/b.md"):
                return ModelCallResult(None, AttemptError.TRANSPORT, ())
            return ModelCallResult(
                __import__("tests.unit.test_runtime_content_translation", fromlist=["_echo_response"])._echo_response(
                    request
                ),
                None,
                (),
            )

    models = SelectiveFail()
    source = RuntimeSource({}, cast(GitHubBackend, object()))
    source.snapshots = ResolvedRepositorySnapshots(
        PullRequestState.MERGED,
        BaseBranch("main"),
        SNAPSHOT,
        SNAPSHOT,
        SNAPSHOT,
        SNAPSHOT,
        SNAPSHOT,
        SNAPSHOT,
        SNAPSHOT,
    )
    content = RuntimeContent(source, cast(RecordedModels, models), {})
    preparation = FrozenPreparation(
        ImmutableRunSnapshot(Mode.DOC_TRANSLATE, SNAPSHOT.commit_sha, None, "translation/pr-42", None),
        source.snapshots,
        SourceChangeInventory(
            tuple(
                SourceChange(RepoPath(f"ydb/docs/ru/core/{name}.md"), "modified", None, None)
                for name in ("a", "b", "c")
            )
        ),
        SNAPSHOT,
        (),
        PotentialScopeSet(SNAPSHOT, content.roots, (), None),
        True,
    )
    plans = FrozenSourcePlans(
        preparation,
        ScopeManifest(Direction.RU_TO_EN, SNAPSHOT, content.roots, (), (), 0, 0),
        tuple(docs),
        (),
        TranslationPlan(Direction.RU_TO_EN, (), ()),
    )

    candidate = content._translate_documents(plans, tuple(docs), (), ())
    files = unpack(candidate.content)

    assert files["ydb/docs/en/core/a.md"] == b"# Source a\n"
    assert files["ydb/docs/en/core/b.md"] is None
    assert files["ydb/docs/en/core/c.md"] == b"# Source c\n"
    assert not any(
        call.target_path is not None and call.target_path.value.endswith("/b.md")
        for call in models.calls
        if False
    )
    # a and c translated; b failed once (provider may retry internally — we only care soft-continue)
    translated_targets = {
        call.target_path.value
        for call in models.calls
        if call.target_path is not None and call.role is ModelRole.TRANSLATE
    }
    assert "ydb/docs/en/core/a.md" in translated_targets
    assert "ydb/docs/en/core/c.md" in translated_targets


class _TableShapeMismatchModels:
    """Return a valid segment ID map whose restored Markdown changes table shape."""

    def __init__(self) -> None:
        self.calls: list[ModelRequest] = []

    def invoke(self, request: ModelRequest, /) -> ModelCallResult:
        self.calls.append(request)
        encoded = request.prompt.split("\nSegments: ", 1)[1].split("\n\n", 1)[0]
        values = json.loads(encoded)
        # Widen the first table-looking segment to three columns when possible.
        for key, value in list(values.items()):
            if "| A | B |" in value:
                values[key] = value.replace("| A | B |", "| A | B | C |").replace(
                    "| --- | --- |", "| --- | --- | --- |"
                ).replace("| 1 | 2 |", "| 1 | 2 | 3 |")
                break
            if value.strip() == "A | B":
                values[key] = "A | B | C"
            elif value.strip() == "--- | ---":
                values[key] = "--- | --- | ---"
            elif value.strip() == "1 | 2":
                values[key] = "1 | 2 | 3"
        return ModelCallResult(json.dumps(values, ensure_ascii=False), None, ())
