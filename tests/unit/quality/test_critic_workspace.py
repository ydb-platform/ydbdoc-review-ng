"""Unit tests for critic tool workspace (P1a)."""

from __future__ import annotations

import pytest

from ydbdoc_review_ng.quality.workspace import (
    PATCH_ABSOLUTE_BYTE_CAP,
    CriticWorkspace,
    LineRange,
    ToolError,
    ToolErrorReason,
)


def _ws(
    *,
    target: bytes | None = b"alpha\nbeta\ngamma\n",
    source: bytes | None = None,
    seed_missing: bool = False,
) -> CriticWorkspace:
    source_bytes = "источник\n".encode() if source is None else source
    return CriticWorkspace(
        writable_path="docs/en/article.md",
        writable_bytes=b"" if seed_missing else (b"" if target is None else target),
        read_only={
            "docs/ru/article.md": source_bytes,
            "docs/ru/glossary.md": b"term: value\n",
            "docs/en/glossary.md": b"term: value\n",
        },
    )


def test_read_returns_1based_line_window() -> None:
    ws = _ws()
    result = ws.read("docs/en/article.md", start_line=2, end_line=3)
    assert result == "2|beta\n3|gamma\n"


def test_read_source_is_allowed() -> None:
    ws = _ws()
    assert "1|источник\n" in ws.read("docs/ru/article.md", start_line=1, end_line=1)


def test_grep_searches_workspace_bytes_not_disk(tmp_path) -> None:
    # Disk file would mismatch if grep accidentally opened the path.
    disk = tmp_path / "article.md"
    disk.write_text("disk-only\n", encoding="utf-8")
    ws = _ws(target=b"alpha\nneedle here\ngamma\n")
    hits = ws.grep("needle", path="docs/en/article.md")
    assert hits == [("docs/en/article.md", 2, "needle here")]


def test_apply_patch_updates_writable_and_reports_touched_range() -> None:
    ws = _ws()
    touched = ws.apply_patch(
        "docs/en/article.md",
        "@@\n alpha\n-beta\n+BETA\n gamma\n",
    )
    assert ws.writable_bytes() == b"alpha\nBETA\ngamma\n"
    assert touched == (LineRange(2, 2),)


def test_apply_patch_rejects_read_only_mount() -> None:
    ws = _ws()
    with pytest.raises(ToolError) as caught:
        ws.apply_patch("docs/ru/article.md", "@@\n-" + "источник" + "\n+source\n")
    assert caught.value.reason is ToolErrorReason.READ_ONLY


def test_apply_patch_rejects_unknown_and_escape_paths() -> None:
    ws = _ws()
    with pytest.raises(ToolError) as caught:
        ws.apply_patch("../etc/passwd", "@@\n+x\n")
    assert caught.value.reason is ToolErrorReason.UNKNOWN_PATH
    with pytest.raises(ToolError) as caught2:
        ws.read("docs/en/../ru/article.md", start_line=1, end_line=1)
    assert caught2.value.reason is ToolErrorReason.UNKNOWN_PATH


def test_missing_target_seeds_empty_writable_and_patch_can_create() -> None:
    ws = _ws(seed_missing=True)
    assert ws.writable_bytes() == b""
    ws.apply_patch("docs/en/article.md", "@@\n+# Title\n+\n")
    assert ws.writable_bytes() == b"# Title\n\n"


def test_apply_patch_rejects_invalid_hunk() -> None:
    ws = _ws()
    with pytest.raises(ToolError) as caught:
        ws.apply_patch("docs/en/article.md", "@@\n-not-a-real-line\n+x\n")
    assert caught.value.reason is ToolErrorReason.INVALID_PATCH


def test_apply_patch_rejects_oversized_span() -> None:
    big = ("line\n" * 3000).encode()
    ws = _ws(target=big)
    # Replace a huge contiguous span (> 8 KiB of changed text).
    removed = "".join("-line\n" for _ in range(2500))
    added = "".join("+LINE\n" for _ in range(2500))
    patch = f"@@\n{removed}{added}"
    with pytest.raises(ToolError) as caught:
        ws.apply_patch("docs/en/article.md", patch)
    assert caught.value.reason is ToolErrorReason.OVERSIZED_PATCH
    assert PATCH_ABSOLUTE_BYTE_CAP == 8 * 1024


def test_small_file_allows_large_relative_rewrite_under_absolute_cap() -> None:
    ws = _ws(target=b"ab\n")
    # File < 2 KiB: 40% rule does not block; absolute cap still applies.
    ws.apply_patch("docs/en/article.md", "@@\n-ab\n+xy\n")
    assert ws.writable_bytes() == b"xy\n"


def test_empty_seed_first_write_uses_absolute_cap_not_percent_of_zero() -> None:
    ws = _ws(seed_missing=True)
    # Empty seed: relative % of 0 must not block; absolute cap still applies.
    ws.apply_patch("docs/en/article.md", "@@\n+# hello\n")
    assert ws.writable_bytes() == b"# hello\n"
