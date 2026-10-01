"""Pure, fail-closed planning for every source pull-request inventory row.

The planner performs no repository reads and no mutations. It turns the frozen
GitHub inventory plus the frozen Markdown scope into an immutable intent plan.
Execution is separate and reconciles concrete results before publication.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from typing import Any

import yaml  # type: ignore[import-untyped]

from ydbdoc_review_ng.continuation import SourceChange, SourceChangeInventory
from ydbdoc_review_ng.direction import ClassifiedFile, Direction, InventoryClassification
from ydbdoc_review_ng.domain import ContentHash, RepoPath
from ydbdoc_review_ng.errors import SafeDiagnosticError
from ydbdoc_review_ng.locales import LocaleRoots, PairKey, paired_markdown_path
from ydbdoc_review_ng.runtime_metadata import _toc
from ydbdoc_review_ng.scope import (
    FileOperation,
    InitialPairDisposition,
    ScopeManifest,
    ScopeOrigin,
)
from ydbdoc_review_ng.toc_delta import TocDeltaError, planned_toc_markdown_additions

_TOC_NAME = re.compile(r"^toc(?:_[A-Za-z0-9-]+)?\.ya?ml$")
_ASSET_EXTENSIONS = frozenset(
    {".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".ico", ".pdf"}
)
_ORDINARY_STATUSES = frozenset({"added", "modified"})


class TranslationPlanError(SafeDiagnosticError):
    """The complete inventory cannot be represented by the supported executor."""


class PathKind(str, Enum):
    MARKDOWN = "markdown"
    TOC = "toc"
    REDIRECTS = "redirects"
    ASSET = "asset"
    LOCALIZED_OTHER = "localized_other"
    OUTSIDE_LOCALES = "outside_locales"


class PlanAction(str, Enum):
    NO_ACTION = "no_action"
    TRANSLATE_DOCUMENT = "translate_document"
    DELETE_TARGET = "delete_target"
    COPY_TARGET = "copy_target"
    RENAME_TARGET = "rename_target"
    RENAME_AND_TRANSLATE = "rename_and_translate"
    TARGET_ALREADY_ABSENT = "target_already_absent"
    TARGET_ALREADY_RENAMED = "target_already_renamed"
    SOURCE_TOMBSTONE = "source_tombstone"
    TARGET_TOMBSTONE = "target_tombstone"
    COMPLETE_PAIR = "complete_pair"
    TARGET_SIDE_CHANGE = "target_side_change"
    SYNC_TOC = "sync_toc"
    IGNORE_OUTSIDE_LOCALES = "ignore_outside_locales"


@dataclass(frozen=True, slots=True)
class ClassifiedPath:
    path: RepoPath
    locale: str | None
    relative: str | None
    kind: PathKind


@dataclass(frozen=True, slots=True)
class PlannedInput:
    change: SourceChange
    kind: PathKind
    action: PlanAction
    target_path: RepoPath | None
    outputs: tuple[RepoPath, ...]
    expected_sha256: str | None = None
    source_before_sha256: str | None = None
    source_after_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class TranslationPlan:
    direction: Direction | None
    inputs: tuple[PlannedInput, ...]
    outputs: tuple[RepoPath, ...]

    def __post_init__(self) -> None:
        paths = tuple(item.change.path.value for item in self.inputs)
        if paths != tuple(sorted(set(paths))):
            raise TranslationPlanError("translation_plan_inputs_not_canonical")
        if self.outputs != tuple(sorted(set(self.outputs), key=lambda path: path.value)):
            raise TranslationPlanError("translation_plan_outputs_not_canonical")
        claimed = tuple(path for item in self.inputs for path in item.outputs)
        if not set(claimed).issubset(self.outputs):
            raise TranslationPlanError("translation_plan_outputs_incomplete")


def _yaml_mapping(node: Any) -> dict[str, Any]:
    if not isinstance(node, yaml.MappingNode) or node.tag != "tag:yaml.org,2002:map":
        raise TranslationPlanError("translation_plan_toc_delta_unsupported")
    values: dict[str, Any] = {}
    for key, value in node.value:
        if (
            not isinstance(key, yaml.ScalarNode)
            or key.tag != "tag:yaml.org,2002:str"
            or key.value in values
        ):
            raise TranslationPlanError("translation_plan_toc_delta_unsupported")
        values[key.value] = value
    return values


def _yaml_fingerprint(node: Any, active: set[int] | None = None) -> tuple[object, ...]:
    """Canonicalize a safe YAML node while rejecting aliases and exotic tags."""
    active = set() if active is None else active
    if id(node) in active:
        raise TranslationPlanError("translation_plan_toc_delta_unsupported")
    active.add(id(node))
    try:
        if isinstance(node, yaml.ScalarNode):
            if node.tag not in {
                "tag:yaml.org,2002:" + suffix
                for suffix in ("str", "null", "bool", "int", "float", "timestamp")
            }:
                raise TranslationPlanError("translation_plan_toc_delta_unsupported")
            return ("scalar", node.tag, node.value)
        if isinstance(node, yaml.SequenceNode):
            if node.tag != "tag:yaml.org,2002:seq":
                raise TranslationPlanError("translation_plan_toc_delta_unsupported")
            return ("sequence", tuple(_yaml_fingerprint(item, active) for item in node.value))
        values = _yaml_mapping(node)
        return (
            "mapping",
            tuple(
                (key, _yaml_fingerprint(value, active))
                for key, value in sorted(values.items())
            ),
        )
    finally:
        active.remove(id(node))


def _compose_toc(content: bytes) -> tuple[Any, tuple[Any, ...]]:
    try:
        root = yaml.compose(content.decode("utf-8"), Loader=yaml.SafeLoader)
        values = _yaml_mapping(root)
        items = values.get("items")
        if isinstance(items, yaml.ScalarNode) and items.tag == "tag:yaml.org,2002:null":
            return root, ()
        if not isinstance(items, yaml.SequenceNode) or items.tag != "tag:yaml.org,2002:seq":
            raise TranslationPlanError("translation_plan_toc_delta_unsupported")
        return root, tuple(items.value)
    except TranslationPlanError:
        raise
    except (UnicodeError, yaml.YAMLError, TypeError, ValueError, RecursionError):
        raise TranslationPlanError("translation_plan_toc_delta_unsupported") from None


def validate_toc_correction(expected: bytes, corrected: bytes, /) -> tuple[tuple[str, str], ...]:
    """Validate deterministic YAML structure and return matching prose labels."""

    def structure(content: bytes) -> tuple[tuple[object, ...], tuple[str, ...]]:
        _toc(content, "translation_plan_toc_correction_invalid")
        root, _items = _compose_toc(content)
        labels: list[str] = []
        stack = [root]
        while stack:
            node = stack.pop()
            if isinstance(node, yaml.MappingNode):
                for key, value in sorted(_yaml_mapping(node).items()):
                    if key in {"name", "title"} and isinstance(value, yaml.ScalarNode):
                        if value.tag != "tag:yaml.org,2002:str":
                            raise TranslationPlanError("translation_plan_toc_correction_invalid")
                        labels.append(value.value)
                        value.value = ""
                    else:
                        stack.append(value)
            elif isinstance(node, yaml.SequenceNode):
                stack.extend(node.value)
        return _yaml_fingerprint(root), tuple(labels)

    expected_structure, expected_labels = structure(expected)
    corrected_structure, corrected_labels = structure(corrected)
    if expected_structure != corrected_structure:
        raise TranslationPlanError("translation_plan_toc_correction_invalid")
    return tuple(zip(expected_labels, corrected_labels, strict=True))


def _planned_toc_additions(
    path: RepoPath, before: bytes | None, after: bytes, /
) -> tuple[RepoPath, ...]:
    """Return Markdown paths newly introduced by the source TOC structural delta."""
    try:
        return planned_toc_markdown_additions(path, before, after)
    except TocDeltaError as error:
        raise TranslationPlanError(str(error)) from None



def _locale_root(core_root: RepoPath) -> RepoPath:
    marker = "/core"
    if not core_root.value.endswith(marker):
        raise TranslationPlanError("translation_plan_locale_root_invalid")
    return RepoPath(core_root.value[: -len(marker)])


def _relative(root: RepoPath, path: RepoPath) -> str | None:
    prefix = root.value + "/"
    return path.value.removeprefix(prefix) if path.value.startswith(prefix) else None


def _paired(root: RepoPath, relative: str) -> RepoPath:
    return RepoPath(root.value + "/" + relative)


def classify_path(roots: LocaleRoots, path: RepoPath, /) -> ClassifiedPath:
    """Classify one path without hiding localized metadata as external."""
    locale_roots = (("ru", _locale_root(roots.ru)), ("en", _locale_root(roots.en)))
    for locale, locale_root in locale_roots:
        locale_relative = _relative(locale_root, path)
        if locale_relative is None:
            continue
        core_root = roots.ru if locale == "ru" else roots.en
        core_relative = _relative(core_root, path)
        basename = posixpath.basename(path.value)
        if core_relative is not None and basename.endswith(".md"):
            return ClassifiedPath(path, locale, core_relative, PathKind.MARKDOWN)
        if core_relative is not None and _TOC_NAME.fullmatch(basename) is not None:
            return ClassifiedPath(path, locale, core_relative, PathKind.TOC)
        if path == RepoPath(locale_root.value + "/redirects.yaml"):
            return ClassifiedPath(path, locale, "redirects.yaml", PathKind.REDIRECTS)
        if posixpath.splitext(basename)[1].lower() in _ASSET_EXTENSIONS:
            return ClassifiedPath(path, locale, locale_relative, PathKind.ASSET)
        return ClassifiedPath(path, locale, locale_relative, PathKind.LOCALIZED_OTHER)
    return ClassifiedPath(path, None, None, PathKind.OUTSIDE_LOCALES)


_ACTIONS = {
    FileOperation.TRANSLATE: PlanAction.TRANSLATE_DOCUMENT,
    FileOperation.DELETE_TARGET: PlanAction.DELETE_TARGET,
    FileOperation.RENAME_TARGET: PlanAction.RENAME_TARGET,
    FileOperation.RENAME_TARGET_AND_TRANSLATE: PlanAction.RENAME_AND_TRANSLATE,
    FileOperation.NOOP_TARGET_ABSENT: PlanAction.TARGET_ALREADY_ABSENT,
    FileOperation.NOOP_TARGET_ALREADY_RENAMED: PlanAction.TARGET_ALREADY_RENAMED,
    FileOperation.SKIP_SOURCE_TOMBSTONE: PlanAction.SOURCE_TOMBSTONE,
    FileOperation.SKIP_TARGET_TOMBSTONE: PlanAction.TARGET_TOMBSTONE,
}

_STATUS_OPERATIONS = {
    "added": frozenset({FileOperation.TRANSLATE}),
    "modified": frozenset({FileOperation.TRANSLATE}),
    "removed": frozenset(
        {
            FileOperation.DELETE_TARGET,
            FileOperation.NOOP_TARGET_ABSENT,
            FileOperation.SKIP_SOURCE_TOMBSTONE,
            FileOperation.SKIP_TARGET_TOMBSTONE,
        }
    ),
    "renamed": frozenset(
        {
            FileOperation.RENAME_TARGET,
            FileOperation.RENAME_TARGET_AND_TRANSLATE,
            FileOperation.NOOP_TARGET_ALREADY_RENAMED,
        }
    ),
}


def _source_and_target_roots(
    roots: LocaleRoots, direction: Direction
) -> tuple[str, RepoPath, RepoPath]:
    if direction is Direction.RU_TO_EN:
        return "ru", roots.ru, roots.en
    return "en", roots.en, roots.ru


def _complete_pairs(
    inventory: SourceChangeInventory, roots: LocaleRoots
) -> frozenset[tuple[PathKind, str]]:
    localized = [
        (classify_path(roots, change.path), change.status)
        for change in inventory.files
        if change.status in {"added", "modified"}
    ]
    seen = {
        (item.locale, item.kind, item.relative)
        for item, _status in localized
        if item.locale is not None
    }
    return frozenset(
        (kind, relative)
        for locale, kind, relative in seen
        if relative is not None
        and ("en" if locale == "ru" else "ru", kind, relative) in seen
    )


def _validate_rename_shape(
    change: SourceChange,
    current: ClassifiedPath,
    roots: LocaleRoots,
) -> None:
    if change.status != "renamed":
        return
    assert change.previous_path is not None
    previous = classify_path(roots, change.previous_path)
    if (
        previous.locale != current.locale
        or previous.kind is not current.kind
        or previous.kind is PathKind.OUTSIDE_LOCALES
    ):
        raise TranslationPlanError("translation_plan_rename_crosses_policy_boundary")


def preflight_inventory(inventory: SourceChangeInventory, roots: LocaleRoots, /) -> None:
    """Validate Git facts without deciding whether a file needs translation."""
    for change in inventory.files:
        if change.status not in {"added", "modified", "removed", "renamed"}:
            raise TranslationPlanError("translation_plan_status_operation_mismatch")


def _markdown_input(
    change: SourceChange,
    current: ClassifiedPath,
    target_root: RepoPath,
    manifest: ScopeManifest,
) -> PlannedInput:
    assert current.relative is not None
    key = PairKey(RepoPath(current.relative))
    outcome = next((item for item in manifest.initial_outcomes if item.key == key), None)
    target_path = _paired(target_root, current.relative)
    if outcome is None:
        raise TranslationPlanError("translation_plan_markdown_missing")
    if outcome.disposition is InitialPairDisposition.COMPLETE_PAIR:
        return PlannedInput(change, current.kind, PlanAction.COMPLETE_PAIR, target_path, ())
    entry = next(
        (
            item
            for item in manifest.entries
            if item.origin is ScopeOrigin.INITIAL and key in item.initial_keys
        ),
        None,
    )
    if entry is None or entry.pair.source_path != change.path:
        raise TranslationPlanError("translation_plan_markdown_missing")
    allowed = _STATUS_OPERATIONS.get(change.status)
    if allowed is None or entry.operation not in allowed:
        raise TranslationPlanError("translation_plan_status_operation_mismatch")
    action = _ACTIONS[entry.operation]
    if action in {
        PlanAction.SOURCE_TOMBSTONE,
        PlanAction.TARGET_TOMBSTONE,
    }:
        raise TranslationPlanError("translation_plan_markdown_delete_unsupported")
    if action in {PlanAction.RENAME_TARGET, PlanAction.RENAME_AND_TRANSLATE}:
        if entry.rename_from_target_path is None:
            raise TranslationPlanError("translation_plan_rename_preimage_missing")
        outputs = tuple(
            sorted(
                (entry.rename_from_target_path, entry.pair.target_path),
                key=lambda path: path.value,
            )
        )
    elif action in {
        PlanAction.TARGET_ALREADY_ABSENT,
        PlanAction.TARGET_ALREADY_RENAMED,
        PlanAction.SOURCE_TOMBSTONE,
        PlanAction.TARGET_TOMBSTONE,
    }:
        outputs = ()
    else:
        outputs = (entry.pair.target_path,)
    return PlannedInput(change, current.kind, action, entry.pair.target_path, outputs)


def mirror_classified_files(
    inventory: SourceChangeInventory,
    roots: LocaleRoots,
    direction: Direction | None,
    /,
    *,
    only_targets: frozenset[RepoPath] | None = None,
) -> tuple[ClassifiedFile, ...]:
    """Derive per-file mirror actions from Git facts. Models never choose these."""
    if direction is None:
        return tuple(ClassifiedFile(change, "none", None) for change in inventory.files)
    source_locale, _source_root, target_root = _source_and_target_roots(roots, direction)
    mirrored: list[ClassifiedFile] = []
    for change in inventory.files:
        current = classify_path(roots, change.path)
        if current.locale != source_locale or current.relative is None:
            mirrored.append(ClassifiedFile(change, "none", None))
            continue
        target = RepoPath(target_root.value + "/" + current.relative)
        previous_target = None
        if current.kind is PathKind.MARKDOWN and change.previous_path is not None:
            previous_target = paired_markdown_path(roots, change.previous_path)
        if only_targets is not None and target not in only_targets and previous_target not in only_targets:
            mirrored.append(ClassifiedFile(change, "none", None))
            continue
        if current.kind is PathKind.MARKDOWN:
            mirrored.append(ClassifiedFile(change, "page", None))
        elif current.kind is PathKind.TOC:
            mirrored.append(
                ClassifiedFile(change, "toc_delta", "Apply navigation delta from source PR.")
            )
        elif current.kind in {PathKind.ASSET, PathKind.REDIRECTS, PathKind.LOCALIZED_OTHER}:
            mirrored.append(ClassifiedFile(change, "resource", None))
        else:
            mirrored.append(ClassifiedFile(change, "none", None))
    return tuple(mirrored)


def build_translation_plan(
    inventory: SourceChangeInventory,
    roots: LocaleRoots,
    manifest: ScopeManifest | None,
    /,
    *,
    toc_postconditions: Mapping[RepoPath, bytes | None] | None = None,
    toc_source_snapshots: Mapping[RepoPath, tuple[bytes | None, bytes]] | None = None,
    classification: InventoryClassification | None = None,
) -> TranslationPlan:
    """Build intent before metadata/model execution and cover every inventory row."""
    if classification is not None and not classification.translation_required:
        return TranslationPlan(
            None,
            tuple(
                PlannedInput(
                    change,
                    classify_path(roots, change.path).kind,
                    PlanAction.NO_ACTION,
                    None,
                    (),
                )
                for change in inventory.files
            ),
            (),
        )
    complete = _complete_pairs(inventory, roots)
    no_action = {
        item.path for item in inventory.semantic_actions if item.action == "none"
    }
    direction = None if manifest is None else manifest.direction
    if direction is None:
        inputs: list[PlannedInput] = []
        for change in inventory.files:
            current = classify_path(roots, change.path)
            if change.path in no_action:
                inputs.append(PlannedInput(change, current.kind, PlanAction.NO_ACTION, None, ()))
                continue
            _validate_rename_shape(change, current, roots)
            if current.kind is PathKind.OUTSIDE_LOCALES:
                inputs.append(
                    PlannedInput(
                        change, current.kind, PlanAction.IGNORE_OUTSIDE_LOCALES, None, ()
                    )
                )
            elif current.relative is not None and (current.kind, current.relative) in complete:
                inputs.append(
                    PlannedInput(change, current.kind, PlanAction.COMPLETE_PAIR, None, ())
                )
            else:
                raise TranslationPlanError("translation_plan_direction_missing")
        return TranslationPlan(None, tuple(inputs), ())

    assert manifest is not None
    source_locale, _source_root, target_root = _source_and_target_roots(roots, direction)
    target_locale = "en" if source_locale == "ru" else "ru"
    planned: list[PlannedInput] = []
    generated: set[RepoPath] = set()
    target_changes: set[RepoPath] = set()

    for change in inventory.files:
        current = classify_path(roots, change.path)
        if change.path in no_action:
            planned.append(PlannedInput(change, current.kind, PlanAction.NO_ACTION, None, ()))
            continue
        _validate_rename_shape(change, current, roots)
        if current.kind is PathKind.OUTSIDE_LOCALES:
            planned.append(
                PlannedInput(change, current.kind, PlanAction.IGNORE_OUTSIDE_LOCALES, None, ())
            )
            continue
        if current.locale == target_locale:
            target_changes.add(change.path)
            planned.append(
                PlannedInput(change, current.kind, PlanAction.TARGET_SIDE_CHANGE, change.path, ())
            )
            continue
        if current.locale != source_locale or current.relative is None:
            raise TranslationPlanError("translation_plan_locale_classification_invalid")
        if current.kind is PathKind.MARKDOWN:
            item = _markdown_input(change, current, target_root, manifest)
        elif current.kind is PathKind.TOC:
            if change.status not in _ORDINARY_STATUSES:
                raise TranslationPlanError("translation_plan_toc_operation_unsupported")
            target = _paired(target_root, current.relative)
            # Key present with None = soft-pending TOC (string map failed or no file).
            if toc_postconditions is None or target not in toc_postconditions:
                raise TranslationPlanError("translation_plan_toc_postcondition_missing")
            expected = toc_postconditions[target]
            snapshots = (
                None if toc_source_snapshots is None else toc_source_snapshots.get(change.path)
            )
            if snapshots is None:
                raise TranslationPlanError("translation_plan_toc_source_snapshot_missing")
            before, after = snapshots
            if change.status == "added" and before is not None:
                raise TranslationPlanError("translation_plan_toc_delta_unsupported")
            if change.status == "modified" and before is None:
                raise TranslationPlanError("translation_plan_toc_delta_unsupported")
            additions = _planned_toc_additions(change.path, before, after)
            planned_sources = {
                entry.pair.source_path
                for entry in manifest.entries
                if entry.operation
                in {
                    FileOperation.TRANSLATE,
                    FileOperation.RENAME_TARGET,
                    FileOperation.RENAME_TARGET_AND_TRANSLATE,
                }
            }
            if not set(additions).issubset(planned_sources):
                raise TranslationPlanError("translation_plan_toc_delta_uncovered")
            item = PlannedInput(
                change,
                current.kind,
                PlanAction.SYNC_TOC,
                target,
                (target,),
                None if expected is None else sha256(expected).hexdigest(),
                None if before is None else sha256(before).hexdigest(),
                sha256(after).hexdigest(),
            )
        elif current.kind in {PathKind.ASSET, PathKind.REDIRECTS, PathKind.LOCALIZED_OTHER}:
            # §1.2: locale resources are deterministic copy/delete/rename.
            target = _paired(target_root, current.relative)
            if change.status == "removed":
                item = PlannedInput(
                    change, current.kind, PlanAction.DELETE_TARGET, target, (target,)
                )
            elif change.status == "renamed":
                assert change.previous_path is not None
                previous = classify_path(roots, change.previous_path)
                if previous.relative is None:
                    raise TranslationPlanError("translation_plan_rename_preimage_missing")
                previous_target = _paired(target_root, previous.relative)
                item = PlannedInput(
                    change,
                    current.kind,
                    PlanAction.RENAME_TARGET,
                    target,
                    tuple(sorted((previous_target, target), key=lambda path: path.value)),
                )
            elif change.status in {"added", "modified"}:
                item = PlannedInput(
                    change, current.kind, PlanAction.COPY_TARGET, target, (target,)
                )
            else:
                raise TranslationPlanError("translation_plan_localized_file_unsupported")
        else:
            raise TranslationPlanError("translation_plan_localized_file_unsupported")
        planned.append(item)
        generated.update(item.outputs)

    if generated & target_changes:
        raise TranslationPlanError("translation_plan_target_collision")
    scope_outputs = {
        entry.pair.target_path
        for entry in manifest.entries
        if entry.operation
        not in {
            FileOperation.NOOP_TARGET_ABSENT,
            FileOperation.NOOP_TARGET_ALREADY_RENAMED,
            FileOperation.SKIP_SOURCE_TOMBSTONE,
            FileOperation.SKIP_TARGET_TOMBSTONE,
        }
    }
    scope_outputs.update(
        entry.rename_from_target_path
        for entry in manifest.entries
        if entry.rename_from_target_path is not None
    )
    outputs = tuple(sorted(scope_outputs | generated, key=lambda path: path.value))
    return TranslationPlan(direction, tuple(planned), outputs)


def translation_plan_sha256(plan: TranslationPlan, /) -> ContentHash:
    """Hash the canonical cross-file plan for continuation replay."""
    import json

    payload = {
        "direction": None if plan.direction is None else plan.direction.value,
        "inputs": [
            {
                "path": item.change.path.value,
                "status": item.change.status,
                "previous_path": (
                    None
                    if item.change.previous_path is None
                    else item.change.previous_path.value
                ),
                "rename_changed": item.change.rename_changed,
                "kind": item.kind.value,
                "action": item.action.value,
                "target_path": None if item.target_path is None else item.target_path.value,
                "outputs": [path.value for path in item.outputs],
                "expected_sha256": (
                    None
                    if item.action is PlanAction.SYNC_TOC
                    else item.expected_sha256
                ),
                "source_before_sha256": item.source_before_sha256,
                "source_after_sha256": item.source_after_sha256,
            }
            for item in plan.inputs
        ],
        "outputs": [path.value for path in plan.outputs],
    }
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return ContentHash(sha256(canonical.encode("utf-8")).hexdigest())


def reconcile_fixed_outputs(
    plan: TranslationPlan,
    fixed_files: tuple[tuple[str, bytes | None], ...],
    /,
) -> None:
    """Prove that non-model operations produced their exact planned terminal state."""
    fixed = {RepoPath(path): content for path, content in fixed_files}
    for item in plan.inputs:
        if item.action is PlanAction.SYNC_TOC:
            if item.target_path is None or item.target_path not in fixed:
                raise TranslationPlanError("translation_plan_toc_uncovered")
            content = fixed[item.target_path]
            if item.expected_sha256 is None:
                # Soft-pending: null TOC is allowed; critic must create/fix.
                if content is not None:
                    raise TranslationPlanError("translation_plan_toc_uncovered")
            elif content is None or sha256(content).hexdigest() != item.expected_sha256:
                raise TranslationPlanError("translation_plan_toc_uncovered")
        elif item.action is PlanAction.DELETE_TARGET:
            if item.target_path not in fixed or fixed[item.target_path] is not None:
                raise TranslationPlanError("translation_plan_delete_uncovered")
        elif item.action is PlanAction.COPY_TARGET:
            if item.target_path not in fixed or fixed[item.target_path] is None:
                raise TranslationPlanError("translation_plan_localized_file_unsupported")
        elif item.action is PlanAction.RENAME_TARGET and any(
            path not in fixed for path in item.outputs
        ):
            raise TranslationPlanError("translation_plan_rename_uncovered")


def reconcile_candidate_outputs(
    plan: TranslationPlan,
    candidate_files: tuple[tuple[str, bytes | None], ...],
    /,
    *,
    toc_postconditions: Mapping[RepoPath, bytes | None] | None = None,
) -> None:
    """Require every inventory-owned mutation in the final candidate."""
    candidate = {RepoPath(path): content for path, content in candidate_files}
    for item in plan.inputs:
        target = item.target_path
        if item.action in {
            PlanAction.TRANSLATE_DOCUMENT,
        }:
            # Soft-publish may leave a failed translator target as null for critic.
            if target not in candidate:
                raise TranslationPlanError("translation_plan_candidate_output_missing")
        elif item.action is PlanAction.SYNC_TOC:
            if target is None or target not in candidate:
                raise TranslationPlanError("translation_plan_candidate_output_missing")
            content = candidate[target]
            expected = None if toc_postconditions is None else toc_postconditions.get(target)
            if item.expected_sha256 is None:
                # Soft-pending TOC: null candidate until critic creates the file.
                # Critic may supply a complete TOC (§3.6 / §4.1).
                if content is None:
                    continue
                _toc(content, "translation_plan_toc_correction_invalid")
                continue
            if (
                content is None
                or sha256(content if expected is None else expected).hexdigest()
                != item.expected_sha256
            ):
                raise TranslationPlanError("translation_plan_candidate_output_missing")
            if expected is not None:
                # Critic may change href/hierarchy/conditions; only require parseable TOC.
                _toc(content, "translation_plan_toc_correction_invalid")
        elif item.action is PlanAction.DELETE_TARGET:
            if target not in candidate or candidate[target] is not None:
                raise TranslationPlanError("translation_plan_candidate_delete_missing")
        elif item.action in {PlanAction.RENAME_TARGET, PlanAction.RENAME_AND_TRANSLATE} and (
            len(item.outputs) != 2
            or item.target_path not in item.outputs
            or candidate.get(item.target_path) is None
            or any(
                candidate.get(path, b"missing") is not None
                for path in item.outputs
                if path != item.target_path
            )
        ):
            raise TranslationPlanError("translation_plan_candidate_rename_missing")
