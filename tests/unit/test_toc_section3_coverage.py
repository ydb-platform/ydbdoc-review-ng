"""Coverage matrix for REQUIREMENTS_RU.md §3 TOC Python-delta.

Current executor is append-only for simple `{name, href}` Markdown entries.
Structural delete/rename/reorder/hierarchy/includes remain fail-closed until a
dedicated delta applicator lands.
"""

from __future__ import annotations

import pytest

from ydbdoc_review_ng.domain import RepoPath
from ydbdoc_review_ng.translation_plan import (
    TranslationPlanError,
    _planned_toc_additions,
)


TOC = RepoPath("ydb/docs/ru/core/manual/toc_i.yaml")


def test_section3_append_of_simple_markdown_entries_is_supported() -> None:
    before = b"items:\n- name: Existing\n  href: existing.md\n"
    after = before + b"- name: Page\n  href: page.md\n"
    assert _planned_toc_additions(TOC, before, after) == (
        RepoPath("ydb/docs/ru/core/manual/page.md"),
    )


@pytest.mark.parametrize(
    "before,after,gap",
    [
        (
            b"items:\n- name: Old\n  href: page.md\n",
            b"items:\n- name: New\n  href: page.md\n",
            "label_change",
        ),
        (
            b"items:\n- name: Page\n  href: page.md\n",
            b"items: []\n",
            "delete",
        ),
        (
            b"items:\n- name: A\n  href: a.md\n- name: B\n  href: b.md\n",
            b"items:\n- name: B\n  href: b.md\n- name: A\n  href: a.md\n",
            "reorder",
        ),
        (
            b"items:\n- name: Page\n  href: old.md\n",
            b"items:\n- name: Page\n  href: new.md\n",
            "href_rename",
        ),
        (
            b"items:\n- name: Parent\n  items:\n  - name: Child\n    href: child.md\n",
            b"items:\n- name: Parent\n  items:\n  - name: Child\n    href: child.md\n"
            b"  - name: Extra\n    href: extra.md\n",
            "hierarchy",
        ),
    ],
)
def test_section3_structural_gaps_fail_closed(before: bytes, after: bytes, gap: str) -> None:
    del gap
    with pytest.raises(TranslationPlanError, match="toc_delta_unsupported"):
        _planned_toc_additions(TOC, before, after)


def test_section3_new_target_toc_from_additions_only_when_source_added() -> None:
    after = b"items:\n- name: Page\n  href: page.md\n"
    assert _planned_toc_additions(TOC, None, after) == (
        RepoPath("ydb/docs/ru/core/manual/page.md"),
    )
