"""Coverage matrix for REQUIREMENTS_RU.md §3 TOC Python-delta applicator."""

from __future__ import annotations

import pytest
import yaml

from ydbdoc_review_ng.domain import RepoPath
from ydbdoc_review_ng.toc_delta import (
    TocDeltaError,
    apply_toc_delta,
    planned_toc_markdown_additions,
)


TOC = RepoPath("ydb/docs/ru/core/manual/toc_i.yaml")


def _items(content: bytes | None) -> list[object]:
    assert content is not None
    loaded = yaml.safe_load(content.decode("utf-8"))
    assert isinstance(loaded, dict)
    items = loaded.get("items") or []
    assert isinstance(items, list)
    return items


def test_section3_append_of_simple_markdown_entries_is_supported() -> None:
    before = b"items:\n- name: Existing\n  href: existing.md\n"
    after = before + b"- name: Page\n  href: page.md\n"
    assert planned_toc_markdown_additions(TOC, before, after) == (
        RepoPath("ydb/docs/ru/core/manual/page.md"),
    )
    target = b"items:\n- name: Existing EN\n  href: existing.md\n- name: Extra\n  href: extra.md\n"
    result = apply_toc_delta(before, after, target, toc_path=TOC)
    assert result.content is not None
    items = _items(result.content)
    assert items[0] == {"name": "Existing EN", "href": "existing.md"}
    assert items[1] == {"name": "Page", "href": "page.md"}
    assert items[2] == {"name": "Extra", "href": "extra.md"}


def test_section3_label_change_updates_visible_string() -> None:
    before = b"items:\n- name: Old\n  href: page.md\n"
    after = b"items:\n- name: New\n  href: page.md\n"
    target = b"items:\n- name: Old EN\n  href: page.md\n- name: Keep\n  href: keep.md\n"
    result = apply_toc_delta(
        before,
        after,
        target,
        toc_path=TOC,
        translations={"items/0/name": "New EN"},
    )
    assert _items(result.content) == [
        {"name": "New EN", "href": "page.md"},
        {"name": "Keep", "href": "keep.md"},
    ]
    assert result.string_changes[0].text == "New"
    assert planned_toc_markdown_additions(TOC, before, after) == ()


def test_section3_delete_removes_matching_target_entry() -> None:
    before = b"items:\n- name: Page\n  href: page.md\n"
    after = b"items: []\n"
    target = b"items:\n- name: Page EN\n  href: page.md\n- name: Keep\n  href: keep.md\n"
    result = apply_toc_delta(before, after, target, toc_path=TOC)
    assert _items(result.content) == [{"name": "Keep", "href": "keep.md"}]


def test_section3_delete_missing_target_entry_is_not_an_error() -> None:
    before = b"items:\n- name: Page\n  href: page.md\n"
    after = b"items: []\n"
    target = b"items:\n- name: Keep\n  href: keep.md\n"
    result = apply_toc_delta(before, after, target, toc_path=TOC)
    assert _items(result.content) == [{"name": "Keep", "href": "keep.md"}]


def test_section3_reorder_matches_source_order_and_keeps_target_only() -> None:
    before = b"items:\n- name: A\n  href: a.md\n- name: B\n  href: b.md\n"
    after = b"items:\n- name: B\n  href: b.md\n- name: A\n  href: a.md\n"
    target = (
        b"items:\n- name: A EN\n  href: a.md\n- name: Extra\n  href: extra.md\n"
        b"- name: B EN\n  href: b.md\n"
    )
    result = apply_toc_delta(before, after, target, toc_path=TOC)
    assert _items(result.content) == [
        {"name": "B EN", "href": "b.md"},
        {"name": "A EN", "href": "a.md"},
        {"name": "Extra", "href": "extra.md"},
    ]


def test_section3_href_rename_updates_without_duplicating() -> None:
    before = b"items:\n- name: Page\n  href: old.md\n"
    after = b"items:\n- name: Page\n  href: new.md\n"
    target = b"items:\n- name: Page EN\n  href: old.md\n"
    result = apply_toc_delta(before, after, target, toc_path=TOC)
    assert _items(result.content) == [{"name": "Page EN", "href": "new.md"}]
    assert planned_toc_markdown_additions(TOC, before, after) == ()


def test_section3_hierarchy_adds_nested_entry() -> None:
    before = b"items:\n- name: Parent\n  items:\n  - name: Child\n    href: child.md\n"
    after = (
        b"items:\n- name: Parent\n  items:\n  - name: Child\n    href: child.md\n"
        b"  - name: Extra\n    href: extra.md\n"
    )
    target = (
        b"items:\n- name: Parent EN\n  items:\n  - name: Child EN\n    href: child.md\n"
    )
    result = apply_toc_delta(before, after, target, toc_path=TOC)
    assert planned_toc_markdown_additions(TOC, before, after) == (
        RepoPath("ydb/docs/ru/core/manual/extra.md"),
    )
    items = _items(result.content)
    assert items == [
        {
            "name": "Parent EN",
            "items": [
                {"name": "Child EN", "href": "child.md"},
                {"name": "Extra", "href": "extra.md"},
            ],
        }
    ]


def test_section3_include_change_is_applied() -> None:
    before = b"items:\n- include: old/toc.yaml\n"
    after = b"items:\n- include: new/toc.yaml\n"
    target = b"items:\n- include: old/toc.yaml\n- name: Keep\n  href: keep.md\n"
    result = apply_toc_delta(before, after, target, toc_path=TOC)
    assert _items(result.content) == [
        {"include": "new/toc.yaml"},
        {"name": "Keep", "href": "keep.md"},
    ]


def test_section3_condition_field_follows_source_change() -> None:
    before = b"items:\n- name: Page\n  href: page.md\n  when: old\n"
    after = b"items:\n- name: Page\n  href: page.md\n  when: new\n"
    target = b"items:\n- name: Page EN\n  href: page.md\n  when: old\n"
    result = apply_toc_delta(before, after, target, toc_path=TOC)
    assert _items(result.content) == [
        {"name": "Page EN", "href": "page.md", "when": "new"},
    ]


def test_section3_new_target_toc_from_additions_only() -> None:
    after = b"items:\n- name: Page\n  href: page.md\n- name: Keep\n  href: keep.md\n"
    # before=None means the whole after file is new; only its entries are created.
    result = apply_toc_delta(None, after, None, toc_path=TOC)
    assert result.added_markdown == (
        RepoPath("ydb/docs/ru/core/manual/keep.md"),
        RepoPath("ydb/docs/ru/core/manual/page.md"),
    )
    assert _items(result.content) == [
        {"name": "Page", "href": "page.md"},
        {"name": "Keep", "href": "keep.md"},
    ]


def test_section3_new_target_toc_contains_only_changed_entries_from_pr() -> None:
    before = b"items:\n- name: Keep\n  href: keep.md\n"
    after = b"items:\n- name: Keep\n  href: keep.md\n- name: Page\n  href: page.md\n"
    result = apply_toc_delta(before, after, None, toc_path=TOC)
    assert _items(result.content) == [{"name": "Page", "href": "page.md"}]


def test_section3_delete_only_without_target_toc_creates_no_file() -> None:
    before = b"items:\n- name: Page\n  href: page.md\n"
    after = b"items: []\n"
    result = apply_toc_delta(before, after, None, toc_path=TOC)
    assert result.content is None
    assert result.added_markdown == ()


def test_section3_comment_only_rewrite_preserves_target() -> None:
    """§3.1: zero structural delta must not abort; keep the current target."""
    before = b"items: []\n"
    after = b"items: [] # changed comment\n"
    target = b"items:\n- name: Keep\n  href: keep.md\n"
    result = apply_toc_delta(before, after, target, toc_path=TOC)
    assert result.content == target
    assert result.string_changes == ()
