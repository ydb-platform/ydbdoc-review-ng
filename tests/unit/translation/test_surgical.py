from __future__ import annotations

from ydbdoc_review_ng.domain import (
    FilePair,
    GitSha,
    Locale,
    RepoPath,
    RepositoryId,
    SnapshotRef,
)
from ydbdoc_review_ng.locales import PairKey
from ydbdoc_review_ng.parser.markdown import build_markdown_plan
from ydbdoc_review_ng.runtime_content import Document, RuntimeContent
from ydbdoc_review_ng.scope import FileOperation, ScopeEntry, ScopeOrigin
from ydbdoc_review_ng.translation import build_translation_request
from ydbdoc_review_ng.translation.surgical import (
    SurgicalMode,
    apply_unique_replacements,
    plan_surgical_update,
)

OLD = b"./maintenance/manual/dynamic-config#anchor"
NEW = b"./devops/configuration-management/configuration-v1/dynamic-config#anchor"


def test_unique_url_replacements_patch_existing_english_and_skip_whole_file() -> None:
    source_before = (
        b"* Enable views in [dynamic configuration](" + OLD + b").\n"
        b"* Keep this sentence unchanged.\n"
    )
    source_after = (
        b"* Enable views in [dynamic configuration](" + NEW + b").\n"
        b"* Keep this sentence unchanged.\n"
    )
    existing_en = (
        b"* Enable views in [dynamic configuration](" + OLD + b").\n"
        b"* Keep this sentence unchanged.\n"
    )

    plan = plan_surgical_update(source_before, source_after, existing_en)

    assert plan.mode is SurgicalMode.UNIQUE_REPLACEMENTS
    assert plan.hunks == ()
    assert plan.patched_target == (
        b"* Enable views in [dynamic configuration](" + NEW + b").\n"
        b"* Keep this sentence unchanged.\n"
    )


def test_repeated_identical_url_replacements_update_every_english_copy() -> None:
    source_before = OLD.join((b"A ", b" B ", b" C\n"))
    source_after = NEW.join((b"A ", b" B ", b" C\n"))
    existing_en = OLD.join((b"A ", b" B ", b" C\n"))

    plan = plan_surgical_update(source_before, source_after, existing_en)

    assert plan.mode is SurgicalMode.UNIQUE_REPLACEMENTS
    assert plan.patched_target == NEW.join((b"A ", b" B ", b" C\n"))


def test_identical_source_versions_fall_back_to_whole_file() -> None:
    plan = plan_surgical_update(b"* Same\n", b"* Same\n", b"* Stale English\n")
    assert plan.mode is SurgicalMode.WHOLE_FILE
    assert plan.patched_target is None


def test_structural_insert_falls_back_to_whole_file() -> None:
    plan = plan_surgical_update(
        b"* One\n",
        b"* One\n* Two\n",
        b"* One\n",
    )
    assert plan.mode is SurgicalMode.WHOLE_FILE
    assert plan.patched_target is None


def test_prose_change_with_shared_url_yields_located_hunk() -> None:
    source_before = b"* Old wording [cfg](" + OLD + b").\n* Untouched.\n"
    source_after = b"* New wording [cfg](" + NEW + b").\n* Untouched.\n"
    # EN still has the old URL; wording differs from RU.
    existing_en = b"* Previous English [cfg](" + OLD + b").\n* Untouched.\n"

    plan = plan_surgical_update(source_before, source_after, existing_en)

    assert plan.mode is SurgicalMode.HUNKS
    assert len(plan.hunks) == 1
    hunk = plan.hunks[0]
    assert hunk.source_after.startswith(b"* New wording")
    assert hunk.existing_target_fragment.startswith(b"* Previous English")
    stitched = (
        existing_en[: hunk.target_span[0]]
        + b"* Translated [cfg](" + NEW + b").\n"
        + existing_en[hunk.target_span[1] :]
    )
    assert b"* Untouched.\n" in stitched
    assert b"* Previous English" not in stitched


def test_unique_url_replacements_do_not_rewrite_sibling_manual_paths() -> None:
    sibling = b"./maintenance/manual/virtual_storage_groups_decommit.md"
    source_before = (
        b"* [cfg](./maintenance/manual/dynamic-config).\n"
        b"* [other](" + sibling + b").\n"
    )
    source_after = (
        b"* [cfg](./devops/configuration-management/configuration-v1/dynamic-config).\n"
        b"* [other](" + sibling + b").\n"
    )
    existing_en = (
        b"* [cfg](./devops/configuration-management/configuration-v1/dynamic-config).\n"
        b"* [other](" + sibling + b").\n"
    )
    plan = plan_surgical_update(source_before, source_after, existing_en)
    assert plan.mode is SurgicalMode.UNIQUE_REPLACEMENTS
    assert plan.patched_target == existing_en
    assert sibling in plan.patched_target


def test_unique_path_replacements_keep_english_anchor_fragments() -> None:
    source_before = b"* [cfg](./maintenance/manual/dynamic-config#ru-anchor).\n"
    source_after = (
        b"* [cfg](./devops/configuration-management/configuration-v1/dynamic-config#ru-anchor).\n"
    )
    existing_en = (
        b"* [cfg](./devops/configuration-management/configuration-v1/dynamic-config#en-anchor).\n"
    )
    plan = plan_surgical_update(source_before, source_after, existing_en)
    assert plan.mode is SurgicalMode.UNIQUE_REPLACEMENTS
    assert plan.patched_target == existing_en


def test_runtime_unique_replacements_do_not_run_presentation_map() -> None:
    source_before = b"* Enable views in [cfg](" + OLD + b").\n"
    source_after = b"* Enable views in [cfg](" + NEW + b").\n"
    existing_en = (
        b"* Enable views in [cfg](" + OLD + b").\n"
        b"* Added `SELECT` support and also SELECT again.\n"
    )
    content = RuntimeContent(_SourceBefore(source_before, source_after), _BoomModels(), {})

    _accepted, translated = content._translate_document(_document(source_after, existing_en))

    assert "also SELECT again" in translated.translated_markdown
    assert "`SELECT` support" in translated.translated_markdown
    assert NEW.decode() in translated.translated_markdown


def test_already_applied_english_is_unique_replacements_noop() -> None:
    source_before = b"* See [cfg](" + OLD + b").\n"
    source_after = b"* See [cfg](" + NEW + b").\n"
    existing_en = b"* See [cfg](" + NEW + b").\n"
    plan = plan_surgical_update(source_before, source_after, existing_en)
    assert plan.mode is SurgicalMode.UNIQUE_REPLACEMENTS
    assert plan.patched_target == existing_en


def test_apply_unique_replacements_rejects_missing_old_string() -> None:
    assert apply_unique_replacements(b"no match\n", ((OLD, NEW),)) is None


class _BoomModels:
    def invoke(self, request) -> None:
        raise AssertionError(f"model must not run for unique replacements: {request.role}")


class _GithubBefore:
    def __init__(self, before: bytes, after: bytes | None = None) -> None:
        self.before = before
        self.after = after
        self.base = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("b" * 40))
        self.change = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("c" * 40))

    def read_bytes(self, snapshot, path):
        del path
        if snapshot == self.base:
            return self.before
        if snapshot == self.change:
            return self.after
        raise AssertionError(snapshot)


class _SourceBefore:
    def __init__(self, before: bytes, after: bytes | None = None) -> None:
        github = _GithubBefore(before, after)
        self.source_base_snapshot = github.base
        self.source_change_snapshot = github.change
        self.github = github


def _document(source: bytes, target: bytes):
    source_path = RepoPath("ydb/docs/ru/core/page.md")
    target_path = RepoPath("ydb/docs/en/core/page.md")
    entry = ScopeEntry(
        FilePair(Locale.RU, Locale.EN, source_path, target_path),
        source,
        target,
        ScopeOrigin.INITIAL,
        FileOperation.TRANSLATE,
        (PairKey(RepoPath("page.md")),),
        None,
        None,
    )
    plan = build_markdown_plan(
        SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a" * 40)),
        source_path,
        source,
    )
    return Document(entry, source, plan, build_translation_request(source, plan))


def test_runtime_skips_model_when_existing_english_needs_only_url_rewrites() -> None:
    source_before = b"* Enable views in [cfg](" + OLD + b").\n"
    source_after = b"* Enable views in [cfg](" + NEW + b").\n"
    existing_en = b"* Enable views in [cfg](" + OLD + b").\n"
    content = RuntimeContent(_SourceBefore(source_before), _BoomModels(), {})

    _accepted, translated = content._translate_document(
        _document(source_after, existing_en)
    )

    assert translated.translated_markdown == "* Enable views in [cfg](" + NEW.decode() + ").\n"


def test_runtime_surgical_uses_pr_change_snapshot_not_later_main_source() -> None:
    source_before = b"* Enable views in [cfg](" + OLD + b").\n"
    source_after = b"* Enable views in [cfg](" + NEW + b").\n"
    existing_en = b"* Enable views in [cfg](" + OLD + b").\n"
    later_main = source_after + b"* Unrelated later changelog line.\n"
    content = RuntimeContent(_SourceBefore(source_before, source_after), _BoomModels(), {})

    _accepted, translated = content._translate_document(_document(later_main, existing_en))

    assert translated.translated_markdown == "* Enable views in [cfg](" + NEW.decode() + ").\n"
