from __future__ import annotations

import pytest

from ydbdoc_review_ng.quality.delta_scope import (
    build_pair_delta_scope,
    delta_touched_guard,
)
from ydbdoc_review_ng.quality.workspace import ToolError


def test_unique_dest_scope_marks_new_dest_line() -> None:
    source_before = b"* [cfg](./old.md).\n* Keep.\n"
    source_after = b"* [cfg](./new.md).\n* Keep.\n"
    draft = b"* [cfg](./new.md).\n* Keep.\n"
    scope = build_pair_delta_scope(
        source_before,
        source_after,
        draft,
        source_path="ru/a.md",
        target_path="en/a.md",
    )
    assert scope.change_class == "unique_dest"
    assert 1 in scope.touched_lines
    assert 2 not in scope.touched_lines


def test_delta_guard_rejects_patch_outside_touched_lines() -> None:
    original = b"* [cfg](./new.md).\n* Keep this historical line.\n"
    guard = delta_touched_guard(original, (1,))
    guard(b"* [cfg](./newer.md).\n* Keep this historical line.\n")
    with pytest.raises(ToolError):
        guard(b"* [cfg](./new.md).\n* Keep this historical line changed.\n")
