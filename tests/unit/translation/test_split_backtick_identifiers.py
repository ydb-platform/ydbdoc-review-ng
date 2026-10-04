"""Normalize model-emitted `seg`_`seg` identifier mangling after restore."""

from __future__ import annotations

from ydbdoc_review_ng.translation.split_backtick import (
    count_split_backtick_identifiers,
    normalize_split_backtick_identifiers,
)


def test_collapses_two_and_many_segment_runs() -> None:
    draft = b"See `log`_`config` and `default`_`sampling`_`level` now.\n"
    assert normalize_split_backtick_identifiers(draft) == (
        b"See `log_config` and `default_sampling_level` now.\n"
    )


def test_collapses_bare_words_joined_by_backticked_underscore() -> None:
    """Include/path form: log`_`components and `_`includes (only `_` is backticked)."""
    draft = (
        b"{% include [log`_`components](../../contributor/`_`includes/log`_`components.md) %}\n"
        b'component: "TABLET`_`MAIN"\n'
    )
    assert normalize_split_backtick_identifiers(draft) == (
        b"{% include [log_components](../../contributor/_includes/log_components.md) %}\n"
        b'component: "TABLET_MAIN"\n'
    )


def test_collapses_inside_yaml_examples() -> None:
    draft = (
        b"```yaml\n"
        b"`log`_`config`:\n"
        b"  `default`_`level`: 5  # `NOTICE`\n"
        b"  `sys`_`log`: true\n"
        b"```\n"
    )
    assert normalize_split_backtick_identifiers(draft) == (
        b"```yaml\n"
        b"`log_config`:\n"
        b"  `default_level`: 5  # `NOTICE`\n"
        b"  `sys_log`: true\n"
        b"```\n"
    )


def test_leaves_healthy_inline_code_and_bare_ids_alone() -> None:
    draft = b"Use `log_config` and bare default_level with `NOTICE`.\n"
    assert normalize_split_backtick_identifiers(draft) == draft
    assert count_split_backtick_identifiers(draft) == 0


def test_count_reports_mangling_and_clears_after_normalize() -> None:
    draft = b"`a`_`b` and `c`_`d`_`e` and `ok`\n"
    assert count_split_backtick_identifiers(draft) >= 2
    assert count_split_backtick_identifiers(normalize_split_backtick_identifiers(draft)) == 0
