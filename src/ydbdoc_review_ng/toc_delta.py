"""Python-owned TOC structural delta (REQUIREMENTS_RU.md §3).

Parses source TOC before/after, applies add/delete/rename/change (including
href, hierarchy, includes, and opaque non-visible fields) to the current target
TOC while preserving unrelated target entries. Visible strings
(`name` / `title` / `label`) use an optional translation map; without a mapping
the source after-text is the provisional value for changed fields.
"""

from __future__ import annotations

import json
import posixpath
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

import yaml  # type: ignore[import-untyped]

from ydbdoc_review_ng.domain import ModelRole, RepoPath
from ydbdoc_review_ng.errors import SafeDiagnosticError
from ydbdoc_review_ng.models import ModelRequest
from ydbdoc_review_ng.models.types import FrozenJson

_VISIBLE = ("name", "title", "label")


class TocDeltaError(SafeDiagnosticError):
    """Source TOC delta cannot be represented by the structural applicator."""

    def __init__(self, code: str = "toc_delta_unsupported", /) -> None:
        super().__init__(code)


class TocStringTranslationError(SafeDiagnosticError):
    """TOC visible-string JSON map could not be extracted after retries."""

    def __init__(self, code: str = "toc_string_translation_failed", /) -> None:
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class TocStringChange:
    string_id: str
    text: str
    field: str


@dataclass(frozen=True, slots=True)
class TocDeltaResult:
    content: bytes | None
    added_markdown: tuple[RepoPath, ...]
    string_changes: tuple[TocStringChange, ...]


def _mapping(node: Any) -> dict[str, Any]:
    if type(node) is not dict:
        raise TocDeltaError()
    return node


def _load_root(content: bytes) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(content.decode("utf-8"))
    except (UnicodeError, yaml.YAMLError) as error:
        raise TocDeltaError() from error
    root = _mapping(loaded)
    items = root.get("items")
    if items is None:
        result = dict(root)
        result["items"] = []
        return result
    if type(items) is not list:
        raise TocDeltaError()
    return root


def _dump_root(root: Mapping[str, Any]) -> bytes:
    payload = dict(root)
    if not payload.get("items"):
        payload["items"] = []
    try:
        text = yaml.safe_dump(
            payload,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        )
    except yaml.YAMLError as error:
        raise TocDeltaError() from error
    return text.encode("utf-8")


def _include_path(entry: Mapping[str, Any]) -> str | None:
    include = entry.get("include")
    if type(include) is str and include.strip():
        return include
    if type(include) is dict:
        path = include.get("path")
        if type(path) is str and path.strip():
            return path
    return None


def target_only_toc_references(source_after: bytes, target: bytes) -> frozenset[str]:
    """Return target navigation identities absent from the current source TOC."""

    def references(content: bytes) -> frozenset[str]:
        found: set[str] = set()

        def visit(entry: Mapping[str, Any]) -> None:
            href = entry.get("href")
            if type(href) is str and href.strip():
                found.add(f"href:{href}")
            include = _include_path(entry)
            if include is not None:
                found.add(f"include:{include}")
            children = entry.get("items")
            if children is None:
                return
            if type(children) is not list:
                raise TocDeltaError()
            for child in children:
                visit(_mapping(child))

        visit(_load_root(content))
        return frozenset(found)

    return references(target) - references(source_after)


def validate_target_only_toc_references(
    source_after: bytes, target: bytes, corrected: bytes
) -> None:
    """Reject critic output that removes navigation outside the source PR delta."""
    protected = target_only_toc_references(source_after, target)
    remaining = target_only_toc_references(source_after, corrected)
    if not protected.issubset(remaining):
        raise TocDeltaError("toc_target_only_reference_removed")


def _identity(entry: Mapping[str, Any]) -> str:
    href = entry.get("href")
    if type(href) is str and href.strip():
        return f"href:{href}"
    include = _include_path(entry)
    if include is not None:
        return f"include:{include}"
    name = entry.get("name")
    items = entry.get("items")
    if type(name) is str and name.strip() and type(items) is list:
        return f"group:{name}"
    raise TocDeltaError()


def _index(items: list[Any]) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for item in items:
        entry = _mapping(item)
        key = _identity(entry)
        if key in indexed:
            raise TocDeltaError()
        indexed[key] = entry
    return indexed


def _copy_structure(value: Any) -> Any:
    return yaml.safe_load(yaml.safe_dump(value, allow_unicode=True))


def _match_rename(
    before_index: Mapping[str, dict[str, Any]],
    after_index: Mapping[str, dict[str, Any]],
) -> dict[str, str]:
    """Map before-identity → after-identity for href renames with identical bodies."""
    deleted = [key for key in before_index if key not in after_index]
    added = [key for key in after_index if key not in before_index]
    mapping: dict[str, str] = {}
    used_added: set[str] = set()
    for before_key in deleted:
        if not before_key.startswith("href:"):
            continue
        before_entry = before_index[before_key]
        before_body = {key: value for key, value in before_entry.items() if key != "href"}
        for after_key in added:
            if after_key in used_added or not after_key.startswith("href:"):
                continue
            after_entry = after_index[after_key]
            after_body = {key: value for key, value in after_entry.items() if key != "href"}
            if before_body != after_body:
                continue
            mapping[before_key] = after_key
            used_added.add(after_key)
            break
    return mapping


def _collect_string_changes(
    before_entry: Mapping[str, Any] | None,
    after_entry: Mapping[str, Any],
    path_prefix: str,
    out: list[TocStringChange],
) -> None:
    for field in _VISIBLE:
        after_value = after_entry.get(field)
        if type(after_value) is not str or not after_value.strip():
            continue
        before_value = None if before_entry is None else before_entry.get(field)
        if before_value == after_value:
            continue
        out.append(TocStringChange(f"{path_prefix}/{field}", after_value, field))


def _collect_new_target_strings(
    entry: Mapping[str, Any],
    path_prefix: str,
    out: list[TocStringChange],
) -> None:
    """Collect visible labels for scaffolding that appears for the first time in target."""
    for field in _VISIBLE:
        value = entry.get(field)
        if type(value) is not str or not value.strip():
            continue
        string_id = f"{path_prefix}/{field}"
        if any(item.string_id == string_id for item in out):
            continue
        out.append(TocStringChange(string_id, value, field))


def _subtree_keys(entry: Mapping[str, Any]) -> frozenset[str]:
    keys: set[str] = set()
    href = entry.get("href")
    if type(href) is str and href.strip():
        keys.add(f"href:{href}")
    include = _include_path(entry)
    if include is not None:
        keys.add(f"include:{include}")
    children = entry.get("items")
    if type(children) is list:
        for child in children:
            keys |= _subtree_keys(_mapping(child))
    return frozenset(keys)


def _prune_deleted_descendants(
    entry: Mapping[str, Any], deleted_keys: frozenset[str], /
) -> dict[str, Any] | None:
    """Drop descendants covered by a source deletion; keep target-only siblings (§3.1)."""
    node = dict(entry)
    children = entry.get("items")
    if type(children) is not list:
        return None if _subtree_keys(entry) <= deleted_keys else dict(entry)
    kept: list[Any] = []
    for child in children:
        child_entry = _mapping(child)
        child_keys = _subtree_keys(child_entry)
        if child_keys and child_keys <= deleted_keys:
            continue
        if child_keys & deleted_keys:
            pruned = _prune_deleted_descendants(child_entry, deleted_keys)
            if pruned is not None:
                kept.append(pruned)
        else:
            kept.append(child)
    if not kept:
        return None
    node["items"] = kept
    return node


def _find_target_entry(
    after_entry: Mapping[str, Any],
    before_entry: Mapping[str, Any] | None,
    target_items: list[Any],
    consumed: set[int],
) -> tuple[int, dict[str, Any]] | None:
    """Match a source entry to a target entry by href/include or nested fingerprint."""
    after_key = _identity(after_entry)
    if after_key.startswith("href:") or after_key.startswith("include:"):
        for index, raw in enumerate(target_items):
            if index in consumed:
                continue
            candidate = _mapping(raw)
            if _identity(candidate) == after_key:
                return index, candidate
        if before_entry is not None:
            before_key = _identity(before_entry)
            if before_key != after_key:
                for index, raw in enumerate(target_items):
                    if index in consumed:
                        continue
                    candidate = _mapping(raw)
                    if _identity(candidate) == before_key:
                        return index, candidate
        return None

    fingerprint = _subtree_keys(before_entry if before_entry is not None else after_entry)
    best: tuple[int, dict[str, Any], int] | None = None
    for index, raw in enumerate(target_items):
        if index in consumed:
            continue
        candidate = _mapping(raw)
        if type(candidate.get("items")) is not list:
            continue
        overlap = len(fingerprint & _subtree_keys(candidate))
        if overlap == 0:
            continue
        if best is None or overlap > best[2]:
            best = (index, candidate, overlap)
    if best is None:
        return None
    return best[0], best[1]


def _apply_items(
    before_items: list[Any],
    after_items: list[Any],
    target_items: list[Any],
    *,
    path_prefix: str,
    translations: Mapping[str, str],
    strings: list[TocStringChange],
) -> list[dict[str, Any]]:
    before_index = _index(list(before_items))
    after_index = _index(list(after_items))
    renames = _match_rename(before_index, after_index)
    reverse_rename = {after_key: before_key for before_key, after_key in renames.items()}

    def _canonical(key: str) -> str:
        return reverse_rename.get(key, key)

    before_order = [_identity(_mapping(item)) for item in before_items]
    after_shared_order = [
        _canonical(key)
        for key in (_identity(_mapping(item)) for item in after_items)
        if _canonical(key) in before_index
    ]
    reorder = after_shared_order != before_order

    # Start from target items; annotate with identity for edits.
    working: list[dict[str, Any] | None] = [
        _copy_structure(_mapping(item)) for item in target_items
    ]
    assert all(type(item) is dict for item in working)

    def _target_identity(entry: Mapping[str, Any]) -> str:
        return _identity(entry)

    # Deletes (and identities renamed away). Localized group names match by nested
    # href/include fingerprint when the visible group title differs (§3.1).
    deleted = {
        key for key in before_index if key not in after_index and key not in renames
    }
    deleted_entries = [before_index[key] for key in deleted]
    after_subtree = frozenset().union(
        *(_subtree_keys(_mapping(item)) for item in after_items)
    ) if after_items else frozenset()
    for index, entry in enumerate(list(working)):
        assert entry is not None
        key = _target_identity(entry)
        if key in deleted:
            deleted_entry = before_index[key]
            deleted_keys = _subtree_keys(deleted_entry)
            # Same-name RU/EN groups hit exact identity; still keep target-only kids (§3.1).
            if deleted_keys and not (_subtree_keys(entry) <= deleted_keys):
                working[index] = _prune_deleted_descendants(entry, deleted_keys)
            else:
                working[index] = None
            continue
        if key in renames:
            continue
        for deleted_entry in deleted_entries:
            deleted_keys = _subtree_keys(deleted_entry)
            if not deleted_keys:
                continue
            overlap = _subtree_keys(entry) & deleted_keys
            if overlap and not (deleted_keys & after_subtree):
                # Preserve unrelated target-only descendants inside a matched group.
                if _subtree_keys(entry) <= deleted_keys:
                    working[index] = None
                else:
                    working[index] = _prune_deleted_descendants(entry, deleted_keys)
                break

    # Apply after entries: updates, renames, and additions.
    consumed_target_indexes: set[int] = set()
    for position, after_raw in enumerate(after_items):
        after_entry = _mapping(after_raw)
        after_key = _identity(after_entry)
        before_key = reverse_rename.get(after_key, after_key)
        before_entry = before_index.get(before_key)
        prefix = f"{path_prefix}/{position}"
        _collect_string_changes(before_entry, after_entry, prefix, strings)

        matched = _find_target_entry(
            after_entry,
            before_entry,
            [item for item in working if item is not None],
            set(),
        )
        # Remap matched entry to working index (skipping Nones carefully).
        match_index: int | None = None
        if matched is not None:
            _, matched_entry = matched
            for index, entry in enumerate(working):
                if entry is None or index in consumed_target_indexes:
                    continue
                if entry is matched_entry or _target_identity(entry) in {
                    _identity(matched_entry),
                    before_key,
                    after_key,
                }:
                    # Prefer exact object or identity match on live working slot.
                    if _target_identity(entry) in {
                        _identity(matched_entry),
                        before_key,
                        after_key,
                    } or (
                        before_entry is not None
                        and _subtree_keys(entry) & _subtree_keys(before_entry)
                    ):
                        match_index = index
                        break
            if match_index is None:
                # Search by after/before href identity directly.
                for index, entry in enumerate(working):
                    if entry is None or index in consumed_target_indexes:
                        continue
                    identity = _target_identity(entry)
                    if identity == after_key or identity == before_key:
                        match_index = index
                        break

        if match_index is None:
            # Unchanged source peers that the target never had stay absent (§3.1).
            if (
                before_entry is not None
                and before_entry == after_entry
                and before_key == after_key
            ):
                continue
            # First appearance of this scaffolding in an existing target TOC still
            # needs visible-string IDs even when the source label did not change (§3.3).
            _collect_new_target_strings(after_entry, prefix, strings)
            node = _copy_structure(after_entry)
            assert type(node) is dict
            for field in _VISIBLE:
                string_id = f"{prefix}/{field}"
                if field in node and string_id in translations:
                    node[field] = translations[string_id]
            if type(node.get("items")) is list:
                child_before = (
                    []
                    if before_entry is None or type(before_entry.get("items")) is not list
                    else list(before_entry["items"])
                )
                node["items"] = _apply_items(
                    child_before,
                    list(after_entry.get("items") or []),
                    [],
                    path_prefix=prefix,
                    translations=translations,
                    strings=strings,
                )
            # Insert relative to previous after-neighbor that exists in working.
            insert_at = len(working)
            for prior in range(position - 1, -1, -1):
                prior_key = _identity(_mapping(after_items[prior]))
                prior_before = reverse_rename.get(prior_key, prior_key)
                for index, entry in enumerate(working):
                    if entry is None:
                        continue
                    identity = _target_identity(entry)
                    if identity == prior_key or identity == prior_before:
                        insert_at = index + 1
                        break
                else:
                    continue
                break
            working.insert(insert_at, node)
            consumed_target_indexes.add(insert_at)
            # Shift consumed indexes after insertion point.
            consumed_target_indexes = {
                index + 1 if index >= insert_at and index != insert_at else index
                for index in consumed_target_indexes
            }
            consumed_target_indexes.add(insert_at)
            continue

        consumed_target_indexes.add(match_index)
        target_entry = working[match_index]
        assert target_entry is not None
        node = _copy_structure(target_entry)
        assert type(node) is dict
        for key, value in after_entry.items():
            if key in _VISIBLE or key == "items":
                continue
            node[key] = _copy_structure(value)
        # Drop non-visible keys that the source delta removed (§3.1 conditions).
        if before_entry is not None:
            for key in list(node):
                if key in _VISIBLE or key in {"items", "href", "include"}:
                    continue
                if key in before_entry and key not in after_entry:
                    node.pop(key, None)
        if "href" in after_entry:
            node["href"] = after_entry["href"]
        for field in _VISIBLE:
            if field not in after_entry:
                node.pop(field, None)
                continue
            string_id = f"{prefix}/{field}"
            if before_entry is not None and before_entry.get(field) == after_entry.get(field):
                if field in target_entry:
                    node[field] = target_entry[field]
                else:
                    node[field] = after_entry[field]
            elif string_id in translations:
                node[field] = translations[string_id]
            else:
                node[field] = after_entry[field]
        if type(after_entry.get("items")) is list:
            child_before = (
                []
                if before_entry is None or type(before_entry.get("items")) is not list
                else list(before_entry["items"])
            )
            child_target = (
                list(target_entry["items"])
                if type(target_entry.get("items")) is list
                else []
            )
            node["items"] = _apply_items(
                child_before,
                list(after_entry["items"]),
                child_target,
                path_prefix=prefix,
                translations=translations,
                strings=strings,
            )
        elif "items" in node and "items" not in after_entry:
            node.pop("items", None)
        working[match_index] = node

    result = [entry for entry in working if entry is not None]

    if reorder:
        # Rebuild following source after order, then append remaining target-only.
        ordered: list[dict[str, Any]] = []
        used: set[int] = set()
        by_identity = { _identity(entry): index for index, entry in enumerate(result) }
        for after_raw in after_items:
            after_entry = _mapping(after_raw)
            after_key = _identity(after_entry)
            before_key = reverse_rename.get(after_key, after_key)
            index = by_identity.get(after_key, by_identity.get(before_key))
            if index is None or index in used:
                # Fall back to fingerprint match for groups.
                matched = _find_target_entry(after_entry, before_index.get(before_key), result, used)
                if matched is None:
                    continue
                index, _entry = matched
            used.add(index)
            ordered.append(result[index])
        for index, entry in enumerate(result):
            if index not in used:
                ordered.append(entry)
        return ordered

    return result


def _added_markdown_paths(
    toc_path: RepoPath,
    before_items: list[Any],
    after_items: list[Any],
) -> tuple[RepoPath, ...]:
    before_index = _index(list(before_items)) if before_items else {}
    after_index = _index(list(after_items))
    renames = _match_rename(before_index, after_index)
    directory = toc_path.value.rsplit("/", 1)[0]
    paths: list[RepoPath] = []
    for key in after_index:
        if key in before_index or key in renames.values():
            continue
        if not key.startswith("href:"):
            continue
        href = key.removeprefix("href:")
        if href.endswith(".md"):
            paths.append(RepoPath(posixpath.normpath(posixpath.join(directory, href))))
    # Nested additions
    for after_raw in after_items:
        after_entry = _mapping(after_raw)
        after_key = _identity(after_entry)
        before_key = next((b for b, a in renames.items() if a == after_key), after_key)
        before_entry = before_index.get(before_key)
        if type(after_entry.get("items")) is not list:
            continue
        child_before = (
            []
            if before_entry is None or type(before_entry.get("items")) is not list
            else list(before_entry["items"])
        )
        paths.extend(
            _added_markdown_paths(toc_path, child_before, list(after_entry["items"]))
        )
    return tuple(sorted(set(paths), key=lambda item: item.value))


def _root_visible_changes(
    before_root: Mapping[str, Any],
    after_root: Mapping[str, Any],
    translations: Mapping[str, str],
    strings: list[TocStringChange],
) -> dict[str, Any]:
    """Apply / collect root-level visible string delta (title/name/label)."""
    updates: dict[str, Any] = {}
    for field in _VISIBLE:
        after_value = after_root.get(field)
        before_value = before_root.get(field)
        if after_value == before_value:
            continue
        if type(after_value) is str and after_value.strip():
            strings.append(TocStringChange(field, after_value, field))
            updates[field] = translations.get(field, after_value)
        elif after_value is None and before_value is not None:
            updates[field] = None
        elif after_value is not None:
            updates[field] = after_value
    return updates


def _prune_new_target_items(
    before_items: list[Any],
    after_items: list[Any],
    *,
    path_prefix: str,
    translations: Mapping[str, str],
    strings: list[TocStringChange],
) -> list[dict[str, Any]]:
    """Build a new target TOC from PR-changed entries only (§3.4)."""
    before_index = _index(list(before_items)) if before_items else {}
    after_index = _index(list(after_items))
    renames = _match_rename(before_index, after_index)
    created: list[dict[str, Any]] = []
    for position, after_raw in enumerate(after_items):
        after_entry = _mapping(after_raw)
        key = _identity(after_entry)
        before_key = next((b for b, a in renames.items() if a == key), key)
        before_entry = before_index.get(before_key)
        if before_entry is not None and before_entry == after_entry and before_key == key:
            continue
        prefix = f"{path_prefix}/{position}"
        # New target TOC materializes ancestor scaffolding for the first time in
        # the target locale: every visible string in the pruned tree needs an ID
        # even when unchanged in the source PR (§3.3 / §3.4).
        _collect_new_target_strings(after_entry, prefix, strings)
        node = _copy_structure(after_entry)
        assert type(node) is dict
        for field in _VISIBLE:
            string_id = f"{prefix}/{field}"
            if field in node and string_id in translations:
                node[field] = translations[string_id]
        if type(after_entry.get("items")) is list:
            child_before = (
                []
                if before_entry is None or type(before_entry.get("items")) is not list
                else list(before_entry["items"])
            )
            children = _prune_new_target_items(
                child_before,
                list(after_entry["items"]),
                path_prefix=prefix,
                translations=translations,
                strings=strings,
            )
            if before_entry is not None and type(before_entry.get("items")) is list:
                if not children:
                    # Nested PR change produced no new-target children; skip shell.
                    continue
                node["items"] = children
            else:
                node["items"] = children
        created.append(node)
    return created


def apply_toc_delta(
    source_before: bytes | None,
    source_after: bytes,
    target: bytes | None,
    /,
    *,
    toc_path: RepoPath,
    translations: Mapping[str, str] | None = None,
) -> TocDeltaResult:
    """Apply source before→after structural delta onto the current target TOC."""
    after_root = _load_root(source_after)
    after_items = list(after_root.get("items") or [])
    if source_before is None:
        before_items: list[Any] = []
        before_root: dict[str, Any] = {"items": []}
    else:
        before_root = _load_root(source_before)
        before_items = list(before_root.get("items") or [])

    before_root_keys = {key: value for key, value in before_root.items() if key != "items"}
    after_root_keys = {key: value for key, value in after_root.items() if key != "items"}
    root_changed = before_root_keys != after_root_keys

    if source_before is not None and before_root == after_root:
        # Comment/format-only source edit: no structural delta → keep target (§3.1).
        return TocDeltaResult(target, (), ())

    before_index = _index(before_items) if before_items else {}
    after_index = _index(after_items)
    renames = _match_rename(before_index, after_index)
    added_keys = {
        key for key in after_index if key not in before_index and key not in set(renames.values())
    }
    deleted_keys = {key for key in before_index if key not in after_index and key not in renames}
    order_changed = [_identity(_mapping(item)) for item in before_items] != [
        _identity(_mapping(item)) for item in after_items
    ]
    body_changed = any(
        before_index.get(next((b for b, a in renames.items() if a == key), key)) != after_entry
        or next((b for b, a in renames.items() if a == key), key) != key
        for key, after_entry in after_index.items()
        if next((b for b, a in renames.items() if a == key), key) in before_index
    )
    if (
        not added_keys
        and not deleted_keys
        and not renames
        and not order_changed
        and not body_changed
        and not root_changed
    ):
        # Visible-string-only / non-structural noise after parse → keep target.
        return TocDeltaResult(target, (), ())

    strings: list[TocStringChange] = []
    mapping = {} if translations is None else dict(translations)
    added = _added_markdown_paths(toc_path, before_items, after_items)
    root_updates = _root_visible_changes(before_root_keys, after_root_keys, mapping, strings)

    if target is None:
        if not added_keys and not renames and not body_changed and not root_changed:
            # Deletes only (or empty after) and no target TOC → do not create.
            return TocDeltaResult(None, (), ())
        created = _prune_new_target_items(
            before_items,
            after_items,
            path_prefix="items",
            translations=mapping,
            strings=strings,
        )
        if not created and not root_updates:
            return TocDeltaResult(None, (), tuple(strings))
        new_root: dict[str, Any] = {"items": created}
        for key, value in after_root_keys.items():
            if key in root_updates:
                if root_updates[key] is not None:
                    new_root[key] = root_updates[key]
            elif before_root_keys.get(key) != value:
                new_root[key] = value
        return TocDeltaResult(_dump_root(new_root), added, tuple(strings))

    target_root = _load_root(target)
    target_items = list(target_root.get("items") or [])
    applied_items = _apply_items(
        before_items,
        after_items,
        target_items,
        path_prefix="items",
        translations=mapping,
        strings=strings,
    )
    new_root = dict(target_root)
    new_root["items"] = applied_items
    for key, value in after_root_keys.items():
        if key in root_updates:
            if root_updates[key] is None:
                new_root.pop(key, None)
            else:
                new_root[key] = root_updates[key]
        elif before_root_keys.get(key) != value:
            new_root[key] = value
    for key in list(new_root):
        if key == "items":
            continue
        if key in before_root_keys and key not in after_root_keys:
            new_root.pop(key, None)
    return TocDeltaResult(_dump_root(new_root), added, tuple(strings))


def planned_toc_markdown_additions(
    toc_path: RepoPath, before: bytes | None, after: bytes, /
) -> tuple[RepoPath, ...]:
    """Markdown paths newly introduced by the source TOC delta."""
    # Apply against an empty target solely to validate the delta and collect adds.
    result = apply_toc_delta(before, after, b"items: []\n", toc_path=toc_path)
    return result.added_markdown


_TOC_STRING_PROMPT = """You translate only the listed YDB documentation TOC visible strings.

Return exactly one JSON object with a "strings" map. Keys must be the provided
string IDs. Values must be non-empty translations into the target locale.
Do not invent IDs. Do not translate hrefs, includes, or structural keys.
"""


def build_toc_string_request(
    changes: tuple[TocStringChange, ...],
    /,
    *,
    source_locale: str,
    target_locale: str,
    model: str,
    target_path: RepoPath,
) -> ModelRequest:
    if not changes:
        raise TocStringTranslationError("toc_string_translation_empty")
    payload = {
        "source_locale": source_locale,
        "target_locale": target_locale,
        "strings": [
            {"id": item.string_id, "field": item.field, "text": item.text} for item in changes
        ],
    }
    properties = {
        item.string_id: {"type": "string", "minLength": 1} for item in changes
    }
    return ModelRequest(
        ModelRole.TRANSLATE,
        model,
        _TOC_STRING_PROMPT + "\nInput:\n" + json.dumps(payload, ensure_ascii=False),
        cast(
            FrozenJson,
            {
                "type": "object",
                "properties": {
                    "strings": {
                        "type": "object",
                        "properties": properties,
                        "required": list(properties),
                        "additionalProperties": False,
                    }
                },
                "required": ["strings"],
                "additionalProperties": False,
            },
        ),
        target_path,
    )


def parse_toc_string_response(
    raw: str, changes: tuple[TocStringChange, ...], /
) -> dict[str, str]:
    expected = {item.string_id for item in changes}

    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError
        return result

    try:
        value = json.loads(raw, object_pairs_hook=object_pairs)
        if type(value) is not dict or set(value) != {"strings"}:
            raise ValueError
        strings = value["strings"]
        if type(strings) is not dict or set(strings) != expected:
            raise ValueError
        parsed: dict[str, str] = {}
        for key in expected:
            text = strings[key]
            if type(text) is not str or not text.strip():
                raise ValueError
            parsed[key] = text
        return parsed
    except (ValueError, TypeError, KeyError, RecursionError, json.JSONDecodeError):
        raise TocStringTranslationError() from None
