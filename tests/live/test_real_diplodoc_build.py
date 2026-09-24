from __future__ import annotations

import os
from pathlib import Path

import pytest

from ydbdoc_review_ng.diplodoc import DiplodocBuildError, DiplodocBuildValidator
from ydbdoc_review_ng.domain import RepoPath
from ydbdoc_review_ng.publication import FileChange, PublicationPlan

pytestmark = pytest.mark.live


def test_real_ydb_diplodoc_rejects_broken_candidate_and_restores_checkout() -> None:
    repository = Path(os.environ["YDBDOC_LIVE_YDB_REPOSITORY"])
    docs = repository / "ydb/docs"
    relative = RepoPath("ydb/docs/en/core/yql/reference/syntax/action.md")
    page = repository / relative.value
    before = page.read_bytes()
    broken = before + b"\n[deliberately broken link]()\n"
    validator = DiplodocBuildValidator(docs)

    with pytest.raises(DiplodocBuildError) as raised:
        validator(PublicationPlan((FileChange(relative, before, broken),), ()))

    assert any(
        "en/yql/reference/syntax/action.md" in issue and "MD042" in issue
        for issue in raised.value.issues
    )
    assert page.read_bytes() == before
