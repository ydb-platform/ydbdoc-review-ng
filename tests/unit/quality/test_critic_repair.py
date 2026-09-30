"""Checkpoint field recovery retained after retiring per-document semantic repair."""

import pytest

from ydbdoc_review_ng.domain import GitSha, RepoPath, RepositoryId, SnapshotRef
from ydbdoc_review_ng.parser.markdown import build_markdown_plan
from ydbdoc_review_ng.quality import QualityInputError
from ydbdoc_review_ng.quality.repair import _derive_target_translations
from ydbdoc_review_ng.translation import assemble_candidate, build_translation_request

SNAPSHOT = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a" * 40))
SOURCE_PATH = RepoPath("ydb/docs/en/example.md")
TARGET_PATH = RepoPath("ydb/docs/ru/example.md")


def test_checkpoint_recovers_current_target_prose_and_protected_links() -> None:
    source = b"# Install YDB\n\nRead [the guide](/docs/guide) before starting.\n"
    target = "# Установка YDB\n\nПрочитайте [руководство](/docs/guide) до начала.\n".encode()
    plan = build_markdown_plan(SNAPSHOT, SOURCE_PATH, source)
    request = build_translation_request(source, plan)

    values = _derive_target_translations(source, plan, request, target, TARGET_PATH)

    assert values[request.requested_ids[0]] == "Установка YDB"
    assert "Прочитайте" in values[request.requested_ids[1]]
    assert "руководство" in values[request.requested_ids[1]]
    assert "Read" not in values[request.requested_ids[1]]
    assert assemble_candidate(source, plan, request, values) == target


def test_checkpoint_preserves_logical_escaped_frontmatter_values() -> None:
    source = b'---\ntitle: "An \\"escaped\\" title"\ndescription: "Old description"\n---\n'
    target = b'---\ntitle: "A \\"quoted\\" title"\ndescription: "Corrected description"\n---\n'
    plan = build_markdown_plan(SNAPSHOT, SOURCE_PATH, source)
    request = build_translation_request(source, plan)

    values = _derive_target_translations(source, plan, request, target, TARGET_PATH)

    title_id, description_id = request.requested_ids
    assert values == {title_id: 'A "quoted" title', description_id: "Corrected description"}
    assert assemble_candidate(source, plan, request, values) == target


@pytest.mark.parametrize(
    "target",
    [
        b"Run `other` and open [the guide](/guide).\n",
        b"Run `ydb` and open [the guide](/wrong).\n",
        b"Run `ydb` and open [the guide](/guide).\n\nExtra paragraph.\n",
    ],
)
def test_checkpoint_rejects_changed_protected_content_or_structure(target: bytes) -> None:
    source = b"Run `ydb` and open [the guide](/guide).\n"
    plan = build_markdown_plan(SNAPSHOT, SOURCE_PATH, source)
    request = build_translation_request(source, plan)

    with pytest.raises(QualityInputError):
        _derive_target_translations(source, plan, request, target, TARGET_PATH)
