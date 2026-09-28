import pytest

from ydbdoc_review_ng.domain import GitSha, RepoPath, RepositoryId, SnapshotRef
from ydbdoc_review_ng.parser.markdown import build_markdown_plan
from ydbdoc_review_ng.runtime_assets import missing_assets
from ydbdoc_review_ng.runtime_github import RuntimeBoundaryError

SOURCE = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a" * 40))
BASE = SnapshotRef(SOURCE.repository, GitSha("b" * 40))
PATH = RepoPath("ydb/docs/ru/core/dev/optimization/layout.md")
TARGET = RepoPath("ydb/docs/en/core/dev/optimization/layout.md")
ASSET = "ydb/docs/ru/_assets/chart.svg"
TARGET_ASSET = "ydb/docs/en/_assets/chart.svg"


class Reader:
    def __init__(self, target=None):
        self.values = {(SOURCE, ASSET): b"<svg>source</svg>"}
        if target is not None:
            self.values[BASE, TARGET_ASSET] = target
        self.calls = []

    def read_bytes(self, snapshot, path):
        self.calls.append((snapshot, path.value))
        return self.values.get((snapshot, path.value))


def collect(source, reader, pending=None):
    plan = build_markdown_plan(SOURCE, PATH, source)
    return missing_assets(reader, SOURCE, BASE, PATH, TARGET, source, plan, pending or {})


def test_missing_image_is_copied_byte_exact_from_pinned_source():
    reader = Reader()
    changes = collect(b"![Chart](../../../_assets/chart.svg?width=100#plot)\n", reader)
    assert len(changes) == 1
    assert changes[0].path.value == TARGET_ASSET
    assert changes[0].before is None
    assert changes[0].after == b"<svg>source</svg>"
    assert reader.calls == [(BASE, TARGET_ASSET), (SOURCE, ASSET)]


def test_existing_localized_image_is_not_overwritten():
    reader = Reader(b"<svg>English labels</svg>")
    assert collect(b"![Chart](../../../_assets/chart.svg)\n", reader) == ()
    assert reader.calls == [(BASE, TARGET_ASSET)]


def test_shared_pending_image_and_code_examples_do_not_add_copies():
    reader = Reader()
    source = b"![Chart](../../../_assets/chart.svg)\n\n```md\n![Example](fake.png)\n```\n"
    assert collect(source, reader, {TARGET_ASSET: b"<svg>source</svg>"}) == ()
    assert reader.calls == []


@pytest.mark.parametrize(
    "url",
    [
        "https://example.org/a.svg",
        "//example.org/a.svg",
        "../../../../outside.svg",
        "../../../../../outside.svg",
        "guide.md",
        "data:image/svg+xml,svg",
    ],
)
def test_remote_or_outside_locale_destinations_are_not_read(url):
    reader = Reader()
    assert collect(f"![Image]({url})\n".encode(), reader) == ()
    assert reader.calls == []


def test_missing_source_asset_fails_before_publication():
    reader = Reader()
    reader.values.clear()
    with pytest.raises(RuntimeBoundaryError, match="source_asset_missing"):
        collect(b"![Chart](../../../_assets/chart.svg)\n", reader)


def test_binary_and_escaped_filename_are_supported():
    reader = Reader()
    reader.values[SOURCE, "ydb/docs/ru/_assets/chart image.png"] = b"\x89PNG\x00\xff"
    changes = collect(b'[Download](../../../_assets/chart%20image.png "Image")\n', reader)
    assert changes[0].path.value == "ydb/docs/en/_assets/chart image.png"
    assert changes[0].after == b"\x89PNG\x00\xff"


@pytest.mark.parametrize('limit', ['_MAX_ASSETS', '_MAX_ASSET_BYTES'])
def test_asset_budget_is_checked_before_candidate_assembly(monkeypatch, limit):
    from ydbdoc_review_ng import runtime_assets
    monkeypatch.setattr(runtime_assets, limit, 0)
    with pytest.raises(RuntimeBoundaryError, match='asset_scope_limit_exceeded'):
        collect(b'![Chart](../../../_assets/chart.svg)\n', Reader())
