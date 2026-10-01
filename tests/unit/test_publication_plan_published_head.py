"""P0: publication_plan must compare against the published translation head.

REQUIREMENTS §0/§4.1/§7: after soft-publish, critic corrections that match the
original base still differ from the translator draft already on the branch.
The plan must read before-bytes from snapshot.context.current_head (published),
not from the immutable source-run context that still points at the base.
"""

from __future__ import annotations

from dataclasses import replace
from typing import cast

from ydbdoc_review_ng.application import ImmutableRunSnapshot, WorkflowCandidate
from ydbdoc_review_ng.continuation import SourceChange, SourceChangeInventory
from ydbdoc_review_ng.direction import Direction
from ydbdoc_review_ng.domain import GitSha, Mode, RepoPath, RepositoryId, SnapshotRef
from ydbdoc_review_ng.publication import PublicationContext
from ydbdoc_review_ng.repository import BaseBranch, PullRequestState, ResolvedRepositorySnapshots
from ydbdoc_review_ng.runtime import RecordedModels, RuntimeSource
from ydbdoc_review_ng.runtime_content import (
    FrozenPreparation,
    FrozenSourcePlans,
    RuntimeContent,
    pack,
)
from ydbdoc_review_ng.runtime_github import GitHubBackend
from ydbdoc_review_ng.scope import PotentialScopeSet, ScopeManifest
from ydbdoc_review_ng.translation_plan import TranslationPlan

REPOSITORY = RepositoryId("ydb-platform/ydb")
BASE = GitSha("a" * 40)
PUBLISHED = GitSha("b" * 40)
SOURCE = SnapshotRef(REPOSITORY, GitSha("c" * 40))
EN = "ydb/docs/en/core/a.md"
OLD = b"# Old a\n"
DRAFT = b"# Translated\n"


class _Pinned:
    def __init__(self, files: dict[tuple[GitSha, str], bytes]) -> None:
        self.files = files
        self.reads: list[GitSha] = []

    def read_bytes(self, snapshot: SnapshotRef, path: RepoPath) -> bytes | None:
        self.reads.append(snapshot.commit_sha)
        return self.files.get((snapshot.commit_sha, path.value))


def _content(reader: _Pinned) -> RuntimeContent:
    source = RuntimeSource({}, cast(GitHubBackend, reader))
    source.snapshots = ResolvedRepositorySnapshots(
        PullRequestState.MERGED,
        BaseBranch("main"),
        SOURCE,
        SnapshotRef(REPOSITORY, BASE),
        SnapshotRef(REPOSITORY, BASE),
        SnapshotRef(REPOSITORY, BASE),
        SnapshotRef(REPOSITORY, BASE),
        SnapshotRef(REPOSITORY, BASE),
        SOURCE,
    )
    # Immutable source-run context still points at the pre-publish base head.
    source.context = PublicationContext(
        "ydb-platform/ydb", "translation/pr-42", "main", "main", BASE
    )
    source.inventory = SourceChangeInventory(
        (SourceChange(RepoPath("ydb/docs/ru/core/a.md"), "modified", None, None),)
    )
    content = RuntimeContent(source, cast(RecordedModels, object()), {})
    preparation = FrozenPreparation(
        ImmutableRunSnapshot(Mode.DOC_TRANSLATE, SOURCE.commit_sha, None, "translation/pr-42", None),
        source.snapshots,
        source.inventory,
        SnapshotRef(REPOSITORY, BASE),
        (),
        PotentialScopeSet(SOURCE, content.roots, (), None),
        True,
    )
    content.plans = FrozenSourcePlans(
        preparation,
        ScopeManifest(Direction.RU_TO_EN, SOURCE, content.roots, (), (), 0, 0),
        (),
        (),
        TranslationPlan(Direction.RU_TO_EN, (), ()),
    )
    return content


def test_publication_plan_compares_against_published_head_after_soft_publish() -> None:
    """Critic returning base bytes must still differ from the published draft."""
    reader = _Pinned({(BASE, EN): OLD, (PUBLISHED, EN): DRAFT})
    content = _content(reader)
    # After soft-publish, GitPublicationAdapter replaces snapshot.context with
    # the updated publication head that already contains the translator draft.
    snapshot = ImmutableRunSnapshot(
        Mode.DOC_TRANSLATE,
        SOURCE.commit_sha,
        None,
        "translation/pr-42",
        PublicationContext(
            "ydb-platform/ydb", "translation/pr-42", "main", "main", PUBLISHED
        ),
    )
    candidate = WorkflowCandidate(pack({EN: OLD}), None)

    plan = content.publication_plan(snapshot, candidate)

    assert reader.reads == [PUBLISHED]
    assert len(plan.files) == 1
    change = plan.files[0]
    assert change.path.value == EN
    assert change.before == DRAFT
    assert change.after == OLD
    assert plan.changed


def test_publication_plan_falls_back_to_source_context_when_snapshot_lacks_publication() -> None:
    reader = _Pinned({(BASE, EN): OLD})
    content = _content(reader)
    snapshot = replace(
        content.plans.preparation.snapshot,
        context=None,
    )
    candidate = WorkflowCandidate(pack({EN: DRAFT}), None)

    plan = content.publication_plan(snapshot, candidate)

    assert reader.reads == [BASE]
    assert plan.files[0].before == OLD
    assert plan.changed
