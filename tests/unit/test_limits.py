from __future__ import annotations

import pytest

from ydbdoc_review_ng.direction import Direction
from ydbdoc_review_ng.domain import GitSha, RepositoryId, SnapshotRef
from ydbdoc_review_ng.runtime_content import Limits
from ydbdoc_review_ng.runtime_github import RuntimeBoundaryError
from ydbdoc_review_ng.scope import ScopeMeasurement, ScopePreflightRequest

SNAPSHOT = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a" * 40))


def _request(*, group_files: tuple[int, ...], source_files: tuple[int, ...]) -> ScopePreflightRequest:
    measurement = ScopeMeasurement(
        Direction.RU_TO_EN,
        0,
        sum(source_files),
        (),
        group_files,
        source_files,
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
        limits.check(_request(group_files=(3,), source_files=(10,)))


def test_limits_reports_source_character_overflow_separately() -> None:
    limits = Limits(
        {
            "YDBDOC_MAX_DEPENDENCY_FILES_PER_ARTICLE": "20",
            "YDBDOC_MAX_SOURCE_CHARACTERS": "100",
        }
    )

    with pytest.raises(RuntimeBoundaryError, match="source_character_limit_exceeded"):
        limits.check(_request(group_files=(1,), source_files=(101,)))
