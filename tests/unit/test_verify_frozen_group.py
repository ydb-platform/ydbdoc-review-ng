"""doc_verify missing targets and full frozen-group scope (§4.1 / §5.2 / §1.2)."""

from __future__ import annotations

import inspect

from ydbdoc_review_ng import runtime_content as content_mod
from ydbdoc_review_ng import translation_plan as plan_mod


def test_verify_missing_target_stores_null_instead_of_raising() -> None:
    source = inspect.getsource(content_mod.RuntimeContent.select_source)
    assert "missing verify target is JSON null" in source
    assert 'files[path.value] = None' in source


def test_verify_does_not_complete_pair_from_translation_pr_diff() -> None:
    source = inspect.getsource(content_mod.RuntimeContent.select_source)
    assert "target_side.path not in verify_targets" not in source
    assert "pair.key.relative_path in complete" not in source


def test_mirror_does_not_auto_complete_both_locale_pairs() -> None:
    source = inspect.getsource(plan_mod.mirror_classified_files)
    assert "in complete" not in source
    assert '"page"' in source


def test_resource_plan_actions_include_copy_delete_rename() -> None:
    source = inspect.getsource(plan_mod.build_translation_plan)
    assert "PlanAction.COPY_TARGET" in source
    assert "translation_plan_localized_file_unsupported" in source
