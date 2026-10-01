"""Deferred §3 TOC delta witnesses from external review (#16/#17)."""

from __future__ import annotations

import yaml

from ydbdoc_review_ng.domain import RepoPath
from ydbdoc_review_ng.toc_delta import apply_toc_delta


TOC = RepoPath("ydb/docs/ru/core/manual/toc_i.yaml")


def _items(content: bytes | None) -> list[object]:
    assert content is not None
    loaded = yaml.safe_load(content.decode("utf-8"))
    assert isinstance(loaded, dict)
    return list(loaded.get("items") or [])


def test_unchanged_source_entry_missing_from_target_is_not_added() -> None:
    """§3.1: delta must not invent missing peers that the PR did not touch."""
    before = b"items:\n- name: Old\n  href: old.md\n- name: A\n  href: a.md\n"
    after = b"items:\n- name: Old\n  href: old.md\n- name: A2\n  href: a.md\n"
    target = b"items:\n- name: A EN\n  href: a.md\n"
    result = apply_toc_delta(before, after, target, toc_path=TOC)
    assert _items(result.content) == [{"name": "A2", "href": "a.md"}]


def test_deleted_group_matches_target_peer_by_nested_hrefs() -> None:
    """§3.1: localized group names still delete the matching nested target peer."""
    before = (
        b"items:\n- name: Source group\n  items:\n  - name: A\n    href: a.md\n"
        b"- name: Keep\n  href: keep.md\n"
    )
    after = b"items:\n- name: Keep\n  href: keep.md\n"
    target = (
        b"items:\n- name: Target group\n  items:\n  - name: A EN\n    href: a.md\n"
        b"- name: Keep EN\n  href: keep.md\n"
    )
    result = apply_toc_delta(before, after, target, toc_path=TOC)
    assert _items(result.content) == [{"name": "Keep EN", "href": "keep.md"}]


def test_deleted_group_preserves_unrelated_target_only_child() -> None:
    """§3.1: overlap delete must not drop target-only siblings inside the group."""
    before = (
        b"items:\n- name: Source group\n  items:\n  - name: A\n    href: a.md\n"
        b"- name: Keep\n  href: keep.md\n"
    )
    after = b"items:\n- name: Keep\n  href: keep.md\n"
    target = (
        b"items:\n- name: Target group\n  items:\n"
        b"  - name: A EN\n    href: a.md\n"
        b"  - name: Target only\n    href: target-only.md\n"
        b"- name: Keep EN\n  href: keep.md\n"
    )
    result = apply_toc_delta(before, after, target, toc_path=TOC)
    items = _items(result.content)
    assert {"name": "Keep EN", "href": "keep.md"} in items
    group = next(item for item in items if isinstance(item, dict) and item.get("name") == "Target group")
    assert group["items"] == [{"name": "Target only", "href": "target-only.md"}]


def test_deleted_same_name_group_preserves_unrelated_target_only_child() -> None:
    """§3.1: identical RU/EN group titles still keep target-only children (#7)."""
    before = (
        b"items:\n- name: API\n  items:\n  - name: A\n    href: a.md\n"
        b"- name: Keep\n  href: keep.md\n"
    )
    after = b"items:\n- name: Keep\n  href: keep.md\n"
    target = (
        b"items:\n- name: API\n  items:\n"
        b"  - name: A EN\n    href: a.md\n"
        b"  - name: Extra\n    href: extra.md\n"
        b"- name: Keep EN\n  href: keep.md\n"
    )
    result = apply_toc_delta(before, after, target, toc_path=TOC)
    items = _items(result.content)
    assert {"name": "Keep EN", "href": "keep.md"} in items
    group = next(item for item in items if isinstance(item, dict) and item.get("name") == "API")
    assert group["items"] == [{"name": "Extra", "href": "extra.md"}]


def test_removed_source_condition_is_cleared_on_target() -> None:
    """§3.1: non-visible keys deleted in source are deleted on the matched target."""
    before = b"items:\n- name: A\n  href: a.md\n  when: old\n"
    after = b"items:\n- name: A\n  href: a.md\n"
    target = b"items:\n- name: A EN\n  href: a.md\n  when: old\n"
    result = apply_toc_delta(before, after, target, toc_path=TOC)
    assert _items(result.content) == [{"name": "A EN", "href": "a.md"}]


def test_new_target_toc_prunes_unchanged_nested_siblings() -> None:
    """§3.4: new target TOC keeps only PR-changed nested entries, not Keep."""
    before = b"items:\n- name: Parent\n  items:\n  - name: Keep\n    href: keep.md\n"
    after = (
        b"items:\n- name: Parent\n  items:\n  - name: Keep\n    href: keep.md\n"
        b"  - name: New\n    href: new.md\n"
    )
    result = apply_toc_delta(before, after, None, toc_path=TOC)
    assert _items(result.content) == [
        {"name": "Parent", "items": [{"name": "New", "href": "new.md"}]}
    ]
    # Ancestor scaffolding first appears in the target locale and needs a string ID.
    assert {item.text for item in result.string_changes} == {"Parent", "New"}
    assert any(item.string_id.endswith("/name") and item.text == "New" for item in result.string_changes)


def test_existing_empty_target_toc_assigns_ids_for_new_ancestor() -> None:
    """§3.3: scaffolding inserted into an empty target TOC still gets string IDs (#8)."""
    before = b"items:\n- name: Parent\n  items:\n  - name: Keep\n    href: keep.md\n"
    after = (
        b"items:\n- name: Parent\n  items:\n  - name: Keep\n    href: keep.md\n"
        b"  - name: New\n    href: new.md\n"
    )
    result = apply_toc_delta(before, after, b"items: []\n", toc_path=TOC)
    assert _items(result.content) == [
        {"name": "Parent", "items": [{"name": "New", "href": "new.md"}]}
    ]
    assert {item.text for item in result.string_changes} == {"Parent", "New"}


def test_root_title_change_updates_target_and_string_map() -> None:
    """§3.3: root visible strings (title) are part of the delta, not fail-closed."""
    before = b"title: Old\nitems:\n- name: A\n  href: a.md\n"
    after = b"title: New\nitems:\n- name: A\n  href: a.md\n"
    target = b"title: Old EN\nitems:\n- name: A EN\n  href: a.md\n"
    result = apply_toc_delta(
        before,
        after,
        target,
        toc_path=TOC,
        translations={"title": "New EN"},
    )
    loaded = yaml.safe_load(result.content.decode("utf-8"))
    assert loaded["title"] == "New EN"
    assert loaded["items"] == [{"name": "A EN", "href": "a.md"}]
    assert result.string_changes[0].string_id == "title"
    assert result.string_changes[0].text == "New"
