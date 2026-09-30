from __future__ import annotations

import pytest

from ydbdoc_review_ng.continuation import SourceChange, SourceChangeInventory
from ydbdoc_review_ng.direction import Direction
from ydbdoc_review_ng.domain import FilePair, GitSha, Locale, RepoPath, RepositoryId, SnapshotRef
from ydbdoc_review_ng.errors import SafeDiagnosticError
from ydbdoc_review_ng.locales import LocaleRoots, PairKey
from ydbdoc_review_ng.scope import (
    FileOperation,
    InitialPairDisposition,
    InitialPairOutcome,
    ScopeEntry,
    ScopeManifest,
    ScopeOrigin,
)
from ydbdoc_review_ng.translation_plan import (
    PathKind,
    PlanAction,
    TranslationPlanError,
    build_translation_plan,
    classify_path,
    preflight_inventory,
    reconcile_candidate_outputs,
    reconcile_fixed_outputs,
    translation_plan_sha256,
)

ROOTS = LocaleRoots(RepoPath("ydb/docs/ru/core"), RepoPath("ydb/docs/en/core"))
SNAPSHOT = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a" * 40))


def change(
    path: str,
    status: str = "modified",
    *,
    previous: str | None = None,
    rename_changed: bool | None = None,
) -> SourceChange:
    return SourceChange(
        RepoPath(path),
        status,
        None if previous is None else RepoPath(previous),
        rename_changed,
    )


def inventory(*changes: SourceChange) -> SourceChangeInventory:
    return SourceChangeInventory(tuple(sorted(changes, key=lambda item: item.path.value)))


def status_for(operation: FileOperation) -> tuple[str, str | None, bool | None]:
    if operation in {
        FileOperation.DELETE_TARGET,
        FileOperation.NOOP_TARGET_ABSENT,
        FileOperation.SKIP_SOURCE_TOMBSTONE,
        FileOperation.SKIP_TARGET_TOMBSTONE,
    }:
        return "removed", None, None
    if operation in {
        FileOperation.RENAME_TARGET,
        FileOperation.RENAME_TARGET_AND_TRANSLATE,
        FileOperation.NOOP_TARGET_ALREADY_RENAMED,
    }:
        return "renamed", "ydb/docs/ru/core/old.md", operation is not FileOperation.RENAME_TARGET
    return "modified", None, None


def entry(relative: str, operation: FileOperation = FileOperation.TRANSLATE) -> ScopeEntry:
    key = PairKey(RepoPath(relative))
    source = RepoPath(ROOTS.ru.value + "/" + relative)
    target = RepoPath(ROOTS.en.value + "/" + relative)
    source_content = None if operation in {
        FileOperation.DELETE_TARGET,
        FileOperation.NOOP_TARGET_ABSENT,
        FileOperation.RENAME_TARGET,
        FileOperation.NOOP_TARGET_ALREADY_RENAMED,
        FileOperation.SKIP_SOURCE_TOMBSTONE,
        FileOperation.SKIP_TARGET_TOMBSTONE,
    } else b"# Source\n"
    target_content = b"# Target\n" if operation in {
        FileOperation.DELETE_TARGET,
        FileOperation.NOOP_TARGET_ALREADY_RENAMED,
    } else None
    rename_path = RepoPath(ROOTS.en.value + "/old.md") if operation in {
        FileOperation.RENAME_TARGET,
        FileOperation.RENAME_TARGET_AND_TRANSLATE,
    } else None
    rename_content = b"# Old\n" if rename_path is not None else None
    return ScopeEntry(
        FilePair(Locale.RU, Locale.EN, source, target),
        source_content,
        target_content,
        ScopeOrigin.INITIAL,
        operation,
        (key,),
        rename_path,
        rename_content,
    )


def manifest(*entries: ScopeEntry, complete: tuple[str, ...] = ()) -> ScopeManifest:
    selected = {item.initial_keys[0]: item for item in entries}
    keys = sorted(
        set(selected) | {PairKey(RepoPath(value)) for value in complete},
        key=lambda key: key.relative_path.value,
    )
    outcomes = tuple(
        InitialPairOutcome(
            key,
            InitialPairDisposition.SELECTED
            if key in selected
            else InitialPairDisposition.COMPLETE_PAIR,
            selected.get(key),
        )
        for key in keys
    )
    ordered = tuple(sorted(entries, key=lambda item: item.pair.target_path.value))
    characters = sum(len(item.source_content.decode()) for item in ordered if item.source_content)
    return ScopeManifest(Direction.RU_TO_EN, SNAPSHOT, ROOTS, outcomes, ordered, 0, characters)


def toc_postcondition(relative: str, content: bytes = b"items:\n") -> dict[RepoPath, bytes]:
    return {RepoPath(ROOTS.en.value + "/" + relative): content}


def toc_source_snapshots(
    relative: str,
    *added: tuple[str, str],
    new: bool = False,
) -> dict[RepoPath, tuple[bytes | None, bytes]]:
    before = b"items:\n"
    after = b"items:\n" + b"".join(
        f"- name: {name}\n  href: {href}\n".encode() for name, href in added
    )
    return {
        RepoPath(ROOTS.ru.value + "/" + relative): (None if new else before, after)
    }


@pytest.mark.parametrize(
    "path,kind,locale,relative",
    [
        ("ydb/docs/ru/core/a.md", PathKind.MARKDOWN, "ru", "a.md"),
        ("ydb/docs/en/core/a.md", PathKind.MARKDOWN, "en", "a.md"),
        ("ydb/docs/ru/core/a/toc_i.yaml", PathKind.TOC, "ru", "a/toc_i.yaml"),
        ("ydb/docs/en/redirects.yaml", PathKind.REDIRECTS, "en", "redirects.yaml"),
        ("ydb/docs/ru/_assets/a.png", PathKind.ASSET, "ru", "_assets/a.png"),
        ("ydb/docs/ru/core/a.json", PathKind.LOCALIZED_OTHER, "ru", "core/a.json"),
        ("README.md", PathKind.OUTSIDE_LOCALES, None, None),
    ],
)
def test_path_classification_is_exhaustive(path, kind, locale, relative) -> None:
    result = classify_path(ROOTS, RepoPath(path))
    assert (result.kind, result.locale, result.relative) == (kind, locale, relative)


@pytest.mark.parametrize(
    "operation,action",
    [
        (FileOperation.TRANSLATE, PlanAction.TRANSLATE_DOCUMENT),
        (FileOperation.DELETE_TARGET, PlanAction.DELETE_TARGET),
        (FileOperation.RENAME_TARGET, PlanAction.RENAME_TARGET),
        (FileOperation.RENAME_TARGET_AND_TRANSLATE, PlanAction.RENAME_AND_TRANSLATE),
        (FileOperation.NOOP_TARGET_ABSENT, PlanAction.TARGET_ALREADY_ABSENT),
        (FileOperation.NOOP_TARGET_ALREADY_RENAMED, PlanAction.TARGET_ALREADY_RENAMED),
    ],
)
def test_markdown_status_operation_matrix(operation: FileOperation, action: PlanAction) -> None:
    document = entry("page.md", operation)
    status, previous, changed = status_for(operation)
    plan = build_translation_plan(
        inventory(
            change(
                document.pair.source_path.value,
                status,
                previous=previous,
                rename_changed=changed,
            )
        ),
        ROOTS,
        manifest(document),
    )
    assert plan.inputs[0].action is action


@pytest.mark.parametrize(
    "operation",
    [
        FileOperation.SKIP_SOURCE_TOMBSTONE,
        FileOperation.SKIP_TARGET_TOMBSTONE,
    ],
)
def test_markdown_removal_fails_until_toc_cleanup_is_proven(
    operation: FileOperation,
) -> None:
    document = entry("page.md", operation)
    with pytest.raises(TranslationPlanError, match="markdown_delete_unsupported"):
        build_translation_plan(
            inventory(change(document.pair.source_path.value, "removed")),
            ROOTS,
            manifest(document),
        )


@pytest.mark.parametrize(
    "status,operation",
    [
        ("removed", FileOperation.TRANSLATE),
        ("modified", FileOperation.DELETE_TARGET),
        ("added", FileOperation.RENAME_TARGET_AND_TRANSLATE),
        ("copied", FileOperation.TRANSLATE),
        ("changed", FileOperation.TRANSLATE),
        ("unchanged", FileOperation.TRANSLATE),
    ],
)
def test_incompatible_markdown_status_and_scope_operation_fail_closed(
    status: str, operation: FileOperation
) -> None:
    document = entry("page.md", operation)
    kwargs = (
        {"previous": "ydb/docs/ru/core/old.md", "rename_changed": True}
        if status == "renamed"
        else {}
    )
    with pytest.raises(TranslationPlanError, match="status_operation_mismatch"):
        build_translation_plan(
            inventory(change(document.pair.source_path.value, status, **kwargs)),
            ROOTS,
            manifest(document),
        )


@pytest.mark.parametrize("operation", [FileOperation.RENAME_TARGET, FileOperation.RENAME_TARGET_AND_TRANSLATE])
def test_rename_claims_old_delete_and_new_write(operation: FileOperation) -> None:
    document = entry("page.md", operation)
    status, previous, changed = status_for(operation)
    plan = build_translation_plan(
        inventory(change(document.pair.source_path.value, status, previous=previous, rename_changed=changed)),
        ROOTS,
        manifest(document),
    )
    assert plan.inputs[0].outputs == (
        RepoPath("ydb/docs/en/core/old.md"),
        RepoPath("ydb/docs/en/core/page.md"),
    )


@pytest.mark.parametrize(
    "previous,current",
    [
        ("ydb/docs/ru/core/old.md", "README.md"),
        ("README.md", "ydb/docs/ru/core/new.md"),
        ("ydb/docs/ru/core/old.md", "ydb/docs/en/core/new.md"),
        ("ydb/docs/ru/core/old.md", "ydb/docs/ru/core/toc_i.yaml"),
    ],
)
def test_rename_crossing_policy_boundary_is_rejected(previous: str, current: str) -> None:
    renamed = change(current, "renamed", previous=previous, rename_changed=True)
    with pytest.raises(TranslationPlanError, match="rename_crosses_policy_boundary"):
        build_translation_plan(inventory(renamed), ROOTS, None)


def test_complete_pair_target_change_and_outside_file_are_explicit() -> None:
    plan = build_translation_plan(
        inventory(
            change("README.md"),
            change("ydb/docs/en/core/complete.md"),
            change("ydb/docs/ru/core/complete.md"),
        ),
        ROOTS,
        manifest(complete=("complete.md",)),
    )
    assert {item.change.path.value: item.action for item in plan.inputs} == {
        "README.md": PlanAction.IGNORE_OUTSIDE_LOCALES,
        "ydb/docs/en/core/complete.md": PlanAction.TARGET_SIDE_CHANGE,
        "ydb/docs/ru/core/complete.md": PlanAction.COMPLETE_PAIR,
    }


@pytest.mark.parametrize(
    "name",
    [
        "toc.yaml",
        "toc.yml",
        "toc_i.yaml",
        "toc_p.yaml",
        "toc_m.yaml",
        "toc_x.yaml",
        "toc_changelog.yaml",
        "toc_custom-2.yml",
    ],
)
@pytest.mark.parametrize("status", ["added", "modified"])
def test_supported_toc_write_is_an_independent_planned_input(name: str, status: str) -> None:
    document = entry("manual/page.md")
    source_toc = change(f"ydb/docs/ru/core/manual/{name}", status)
    plan = build_translation_plan(
        inventory(change(document.pair.source_path.value), source_toc),
        ROOTS,
        manifest(document),
        toc_postconditions=toc_postcondition(f"manual/{name}"),
        toc_source_snapshots=toc_source_snapshots(
            f"manual/{name}", ("Page", "page.md"), new=status == "added"
        ),
    )
    planned = {item.change.path: item for item in plan.inputs}
    toc = planned[source_toc.path]
    assert toc.kind is PathKind.TOC
    assert toc.action is PlanAction.SYNC_TOC
    assert toc.outputs == (RepoPath(f"ydb/docs/en/core/manual/{name}"),)


@pytest.mark.parametrize("status", ["removed", "copied", "changed", "unchanged"])
def test_unsupported_toc_status_fails_closed(status: str) -> None:
    document = entry("manual/page.md")
    with pytest.raises(TranslationPlanError, match="toc_operation_unsupported"):
        build_translation_plan(
            inventory(
                change(document.pair.source_path.value),
                change("ydb/docs/ru/core/manual/toc_i.yaml", status),
            ),
            ROOTS,
            manifest(document),
        )


def test_toc_rename_inside_locale_is_rejected_until_executor_supports_it() -> None:
    document = entry("manual/page.md")
    with pytest.raises(TranslationPlanError, match="toc_operation_unsupported"):
        build_translation_plan(
            inventory(
                change(document.pair.source_path.value),
                change(
                    "ydb/docs/ru/core/manual/toc_i.yaml",
                    "renamed",
                    previous="ydb/docs/ru/core/manual/toc_old.yaml",
                    rename_changed=True,
                ),
            ),
            ROOTS,
            manifest(document),
        )


@pytest.mark.parametrize(
    "path",
    [
        "ydb/docs/ru/redirects.yaml",
        "ydb/docs/ru/core/image.png",
        "ydb/docs/ru/core/config.json",
    ],
)
def test_unsupported_source_localized_kind_fails_instead_of_filtering(path: str) -> None:
    document = entry("page.md")
    with pytest.raises(TranslationPlanError, match="localized_file_unsupported"):
        build_translation_plan(
            inventory(change(document.pair.source_path.value), change(path)),
            ROOTS,
            manifest(document),
        )


def test_metadata_only_pr_fails_before_models_instead_of_being_no_translation() -> None:
    with pytest.raises(TranslationPlanError, match="translation_plan_direction_missing"):
        build_translation_plan(
            inventory(change("ydb/docs/ru/core/manual/toc_i.yaml")), ROOTS, None
        )


@pytest.mark.parametrize(
    "changed",
    [
        change("ydb/docs/ru/core/image.png"),
        change("ydb/docs/ru/redirects.yaml"),
        change("ydb/docs/ru/core/page.md", "copied"),
        change("ydb/docs/ru/core/toc_i.yaml", "removed"),
    ],
)
def test_inventory_preflight_rejects_unsupported_rows_before_direction_model(
    changed: SourceChange,
) -> None:
    with pytest.raises(TranslationPlanError):
        preflight_inventory(inventory(changed), ROOTS)


def test_inventory_preflight_accepts_pr50839_shape() -> None:
    preflight_inventory(
        inventory(
            change("ydb/docs/ru/core/maintenance/manual/blobdepot.md"),
            change("ydb/docs/ru/core/maintenance/manual/blobdepot_decommit.md"),
            change("ydb/docs/ru/core/maintenance/manual/index.md"),
            change("ydb/docs/ru/core/maintenance/manual/toc_i.yaml"),
        ),
        ROOTS,
    )


def test_pr50839_plan_contains_three_documents_and_its_toc() -> None:
    documents = tuple(
        entry(f"maintenance/manual/{name}.md")
        for name in ("blobdepot", "blobdepot_decommit", "index")
    )
    expected_toc = (
        b"items:\n- name: BlobDepot\n  href: blobdepot.md\n"
        b'- name: "Group Decommissioning"\n  href: blobdepot_decommit.md\n'
    )
    plan = build_translation_plan(
        inventory(
            *(change(item.pair.source_path.value) for item in documents),
            change("ydb/docs/ru/core/maintenance/manual/toc_i.yaml"),
        ),
        ROOTS,
        manifest(*documents),
        toc_postconditions=toc_postcondition(
            "maintenance/manual/toc_i.yaml", expected_toc
        ),
        toc_source_snapshots={
            RepoPath("ydb/docs/ru/core/maintenance/manual/toc_i.yaml"): (
                b"items:\n",
                (
                    b"items:\n- name: BlobDepot\n  href: blobdepot.md\n"
                    b"- name: Decommission\n  href: blobdepot_decommit.md\n"
                ),
            )
        },
    )
    assert {item.action for item in plan.inputs} == {
        PlanAction.TRANSLATE_DOCUMENT,
        PlanAction.SYNC_TOC,
    }
    assert RepoPath("ydb/docs/en/core/maintenance/manual/toc_i.yaml") in plan.outputs
    with pytest.raises(TranslationPlanError, match="translation_plan_toc_uncovered"):
        reconcile_fixed_outputs(plan, ())
    with pytest.raises(TranslationPlanError, match="translation_plan_toc_uncovered"):
        reconcile_fixed_outputs(
            plan,
            (("ydb/docs/en/core/maintenance/manual/toc_i.yaml", b"items:\n"),),
        )
    reconcile_fixed_outputs(
        plan,
        (("ydb/docs/en/core/maintenance/manual/toc_i.yaml", expected_toc),),
    )


def test_toc_delta_rejects_an_unplanned_second_entry_even_when_target_file_exists() -> None:
    document = entry("manual/page.md")
    source_toc = "manual/toc_i.yaml"
    with pytest.raises(TranslationPlanError, match="toc_delta_uncovered"):
        build_translation_plan(
            inventory(
                change(document.pair.source_path.value),
                change("ydb/docs/ru/core/manual/toc_i.yaml"),
            ),
            ROOTS,
            manifest(document),
            toc_postconditions=toc_postcondition(
                source_toc,
                b"items:\n- name: Page\n  href: page.md\n"
                b"- name: Forgotten\n  href: forgotten.md\n",
            ),
            toc_source_snapshots=toc_source_snapshots(
                source_toc,
                ("Page", "page.md"),
                ("Forgotten", "forgotten.md"),
            ),
        )


@pytest.mark.parametrize(
    "before,after",
    [
        (
            b"items:\n- name: Old\n  href: page.md\n",
            b"items:\n- name: New\n  href: page.md\n",
        ),
        (
            b"items:\n- name: Page\n  href: page.md\n",
            b"items: []\n",
        ),
        (
            b"items:\n- name: A\n  href: a.md\n- name: B\n  href: b.md\n",
            b"items:\n- name: B\n  href: b.md\n- name: A\n  href: a.md\n",
        ),
        (b"items: []\n", b"items: [] # changed comment\n"),
        (
            b"items:\n- name: Existing\n  href: existing.md\n",
            (
                b"items:\n# changed comment\n- name: Existing\n  href: existing.md\n"
                b"- name: Page\n  href: page.md\n"
            ),
        ),
    ],
)
def test_toc_edits_removals_moves_and_comment_only_changes_fail_closed(
    before: bytes, after: bytes
) -> None:
    document = entry("manual/page.md")
    source_path = RepoPath("ydb/docs/ru/core/manual/toc_i.yaml")
    with pytest.raises(TranslationPlanError, match="toc_delta_unsupported"):
        build_translation_plan(
            inventory(change(document.pair.source_path.value), change(source_path.value)),
            ROOTS,
            manifest(document),
            toc_postconditions=toc_postcondition("manual/toc_i.yaml"),
            toc_source_snapshots={source_path: (before, after)},
        )


def test_translation_plan_hash_binds_source_toc_head() -> None:
    document = entry("manual/page.md")
    source_path = RepoPath("ydb/docs/ru/core/manual/toc_i.yaml")

    def planned(name: bytes):
        return build_translation_plan(
            inventory(change(document.pair.source_path.value), change(source_path.value)),
            ROOTS,
            manifest(document),
            toc_postconditions=toc_postcondition("manual/toc_i.yaml"),
            toc_source_snapshots={
                source_path: (
                    b"items:\n",
                    b"items:\n- name: " + name + b"\n  href: page.md\n",
                )
            },
        )

    left = planned(b"Page")
    right = planned(b"Other page")
    assert translation_plan_sha256(left) != translation_plan_sha256(right)


def test_same_counterpart_changed_in_both_locales_is_a_collision() -> None:
    document = entry("page.md")
    with pytest.raises(TranslationPlanError, match="target_collision"):
        build_translation_plan(
            inventory(
                change(document.pair.source_path.value),
                change(document.pair.target_path.value),
            ),
            ROOTS,
            manifest(document),
        )


def test_inventory_markdown_missing_from_manifest_is_a_plan_error() -> None:
    selected = entry("selected.md")
    with pytest.raises(TranslationPlanError, match="translation_plan_markdown_missing"):
        build_translation_plan(
            inventory(change("ydb/docs/ru/core/missing.md")), ROOTS, manifest(selected)
        )


def test_reconciliation_requires_changed_toc_result_not_unrelated_output() -> None:
    document = entry("manual/page.md")
    expected_toc = b"items:\n- name: Page\n  href: page.md\n"
    plan = build_translation_plan(
        inventory(
            change(document.pair.source_path.value),
            change("ydb/docs/ru/core/manual/toc_i.yaml"),
        ),
        ROOTS,
        manifest(document),
        toc_postconditions=toc_postcondition("manual/toc_i.yaml", expected_toc),
        toc_source_snapshots=toc_source_snapshots(
            "manual/toc_i.yaml", ("Page", "page.md")
        ),
    )
    with pytest.raises(TranslationPlanError, match="translation_plan_toc_uncovered"):
        reconcile_fixed_outputs(plan, ((document.pair.target_path.value, b"translated"),))
    with pytest.raises(TranslationPlanError, match="translation_plan_toc_uncovered"):
        reconcile_fixed_outputs(
            plan,
            (("ydb/docs/en/core/manual/toc_i.yaml", b"items:\n"),),
        )
    reconcile_fixed_outputs(
        plan,
        (("ydb/docs/en/core/manual/toc_i.yaml", expected_toc),),
    )


def test_reconciliation_requires_delete_tombstone() -> None:
    document = entry("page.md", FileOperation.DELETE_TARGET)
    plan = build_translation_plan(
        inventory(change(document.pair.source_path.value, "removed")),
        ROOTS,
        manifest(document),
    )
    with pytest.raises(TranslationPlanError, match="translation_plan_delete_uncovered"):
        reconcile_fixed_outputs(plan, ())
    reconcile_fixed_outputs(plan, ((document.pair.target_path.value, None),))


@pytest.mark.parametrize(
    "operation,status,files,error",
    [
        (FileOperation.TRANSLATE, "modified", (), "candidate_output_missing"),
        (
            FileOperation.DELETE_TARGET,
            "removed",
            (("ydb/docs/en/core/page.md", b"orphan"),),
            "candidate_delete_missing",
        ),
        (
            FileOperation.RENAME_TARGET,
            "renamed",
            (("ydb/docs/en/core/page.md", b"moved"),),
            "candidate_rename_missing",
        ),
    ],
)
def test_final_candidate_reconciliation_blocks_missing_terminal_results(
    operation: FileOperation,
    status: str,
    files: tuple[tuple[str, bytes | None], ...],
    error: str,
) -> None:
    document = entry("page.md", operation)
    kwargs = (
        {"previous": "ydb/docs/ru/core/old.md", "rename_changed": False}
        if status == "renamed"
        else {}
    )
    plan = build_translation_plan(
        inventory(change(document.pair.source_path.value, status, **kwargs)),
        ROOTS,
        manifest(document),
    )
    with pytest.raises(TranslationPlanError, match=error):
        reconcile_candidate_outputs(plan, files)


def test_final_candidate_reconciliation_accepts_complete_rename_result() -> None:
    document = entry("page.md", FileOperation.RENAME_TARGET)
    plan = build_translation_plan(
        inventory(
            change(
                document.pair.source_path.value,
                "renamed",
                previous="ydb/docs/ru/core/old.md",
                rename_changed=False,
            )
        ),
        ROOTS,
        manifest(document),
    )
    reconcile_candidate_outputs(
        plan,
        (
            ("ydb/docs/en/core/old.md", None),
            ("ydb/docs/en/core/page.md", b"# Moved\n"),
        ),
    )


def test_rename_and_translate_requires_old_tombstone_and_new_content() -> None:
    document = entry("page.md", FileOperation.RENAME_TARGET_AND_TRANSLATE)
    plan = build_translation_plan(
        inventory(
            change(
                document.pair.source_path.value,
                "renamed",
                previous="ydb/docs/ru/core/old.md",
                rename_changed=True,
            )
        ),
        ROOTS,
        manifest(document),
    )
    with pytest.raises(TranslationPlanError, match="candidate_rename_missing"):
        reconcile_candidate_outputs(
            plan,
            (("ydb/docs/en/core/page.md", b"# Translated\n"),),
        )
    reconcile_candidate_outputs(
        plan,
        (
            ("ydb/docs/en/core/old.md", None),
            ("ydb/docs/en/core/page.md", b"# Translated\n"),
        ),
    )


def test_plan_is_immutable_canonical_and_safe_diagnostic() -> None:
    document = entry("page.md")
    plan = build_translation_plan(
        inventory(change(document.pair.source_path.value)), ROOTS, manifest(document)
    )
    with pytest.raises((AttributeError, TypeError)):
        plan.inputs = ()  # type: ignore[misc]
    assert plan.outputs == tuple(sorted(plan.outputs, key=lambda path: path.value))
    error = TranslationPlanError("translation_plan_localized_file_unsupported")
    assert isinstance(error, SafeDiagnosticError)
    assert str(error) == "translation_plan_localized_file_unsupported"


def test_inventory_permutation_is_canonicalized_by_inventory_boundary() -> None:
    first = change("README.md")
    second = change("ydb/docs/ru/core/page.md")
    document = entry("page.md")
    left = build_translation_plan(inventory(first, second), ROOTS, manifest(document))
    right = build_translation_plan(inventory(second, first), ROOTS, manifest(document))
    assert left == right
