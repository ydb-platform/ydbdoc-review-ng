"""Regression for PR 55293: URL labels must not consume link destinations."""

import pytest

from ydbdoc_review_ng.domain import GitSha, RepoPath, RepositoryId, SnapshotRef
from ydbdoc_review_ng.parser.markdown import build_markdown_plan
from ydbdoc_review_ng.plan import ProtectedKind, fields_of, render_identity


@pytest.mark.parametrize("prefix", ["Go to ", "Перейдите по адресу "])
@pytest.mark.parametrize("suffix", [".", ":"])
@pytest.mark.parametrize("label", ["http://localhost:8765/x", "UI"])
def test_link_label_and_destination_remain_separate(prefix, suffix, label):
    source = f"{prefix}[{label}](http://localhost:8765/x){suffix}\n".encode()
    snapshot = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a" * 40))
    plan = build_markdown_plan(snapshot, RepoPath("sample.md"), source)
    regions = [region for field in fields_of(plan) for region in field.protected_regions]
    urls = [source[r.span.start : r.span.end] for r in regions if r.kind is ProtectedKind.URL]
    assert urls == ([b"http://localhost:8765/x"] if label.startswith("http") else [])
    assert [
        source[r.span.start : r.span.end] for r in regions if r.kind is ProtectedKind.LINK_CLOSE
    ] == [b"](http://localhost:8765/x)"]
    assert render_identity(source, plan) == source
