from __future__ import annotations

import pytest

from ydbdoc_review_ng.domain import GitSha, RepoPath, RepositoryId, SnapshotRef
from ydbdoc_review_ng.direction import Direction
from ydbdoc_review_ng.runtime_content import Limits
from ydbdoc_review_ng.runtime_github import RuntimeBoundaryError
from ydbdoc_review_ng.scope import DependencyWitness, ScopeMeasurement, ScopePreflightRequest


SNAPSHOT = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a" * 40))


def _request(*, dependency_files: int, source_characters: int) -> ScopePreflightRequest:
    witnesses = tuple(
        DependencyWitness(
            RepoPath(f"ydb/docs/ru/dep-{index}.md"),
            RepoPath("ydb/docs/ru/article.md"),
        )
        for index in range(dependency_files)
    )
    measurement = ScopeMeasurement(
        Direction.RU_TO_EN,
        dependency_files,
        source_characters,
        witnesses,
    )
    return ScopePreflightRequest(SNAPSHOT, (measurement,))


def test_limits_reports_dependency_file_overflow_separately() -> None:
    limits = Limits(
        {
            "YDBDOC_MAX_DEPENDENCY_FILES_PER_ARTICLE": "2",
            "YDBDOC_MAX_SOURCE_CHARACTERS": "100",
        }
    )

    with pytest.raises(RuntimeBoundaryError, match="dependency_file_limit_exceeded"):
        limits.check(_request(dependency_files=3, source_characters=10))


def test_limits_reports_source_character_overflow_separately() -> None:
    limits = Limits(
        {
            "YDBDOC_MAX_DEPENDENCY_FILES_PER_ARTICLE": "20",
            "YDBDOC_MAX_SOURCE_CHARACTERS": "100",
        }
    )

    with pytest.raises(RuntimeBoundaryError, match="source_character_limit_exceeded"):
        limits.check(_request(dependency_files=1, source_characters=101))
