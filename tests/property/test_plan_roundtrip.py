from __future__ import annotations

import os
import random
import subprocess
import sys
from collections import Counter

import pytest

from ydbdoc_review_ng.domain import GitSha, RepoPath, RepositoryId, SnapshotRef
from ydbdoc_review_ng.parser.markdown import build_markdown_plan
from ydbdoc_review_ng.plan import (
    BlockKind,
    FieldKind,
    InvalidPlanInput,
    PlanInputReason,
    ProtectedKind,
    fields_of,
    plan_from_wire,
    plan_to_wire,
    render_identity,
)

SEED = 7_007_2026
SNAPSHOT = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a" * 40))
PATH = RepoPath("ydb/docs/ru/property.md")


BASE_CASES = (
    b"\xef\xbb\xbf# Heading {#id}\n",
    "# Русский 😀 漢字\r\n".encode(),
    b"Paragraph with `code`, [link](a.md), ![alt](i.png), https://e.test/x.\r",
    b"First\n Second\n",
    b"- item\n  continuation\n  - child\n",
    b"| A | B |\n| --- | --- |\n| C | D |\n",
    b"---\ntitle: X\n---\n",
    b"{% note x %}\nbody\n{% endnote %}\n",
    b"```python\n# comment\n```\n",
    b"<base>\n",
    b"<div data='/>'>\nAfter\n",
    b"<script>\nalert(1)\n</script>\n",
    b"<style>\nx{}\n</style>\n",
    b"<pre>\nx\n</pre>\n",
    b"<textarea>\nx\n</textarea>\n",
    b"<widget>\nbody\n</widget>\n",
    b"<div>\n<div>\nInner\n</div>\nOuter\n</div>\nAfter\n",
    b"<widget>\n<widget data='</widget>'>\nInner\n</widget>\nOuter\n</widget>\nAfter\n",
    b"Use `a\nb` now.\n",
    b"Use ``a`b`` and *word*.\n",
    b"See <user@example.com> and <span>x</span>.\n",
    b"***word***\n",
    b"___word___\n",
    b"****word****\n",
    b"Visit <https://e.test> <!--x--> \\* **bold** ~~old~~.\n",
    b"[ref]: a.md\n",
    b"    code\n",
    b"***\n",
    b" \t\n",
    b"{% tabs %}\n{% tab A %}\nx\n{% endtabs %}\n",
    b"---\ntitle: X\nbody\n",
    b"text\x00more\n",
)

MALFORMED_QUOTA_CASES = tuple(
    [f"---\ntitle: X\nbody\nunique {index}\n".encode() for index in range(3)]
    + [
        f"{{% tabs %}}\n{{% tab A %}}\nText {index}\n{{% endtabs %}}\n".encode()
        for index in range(3)
    ]
    + [f"text\x00more {index}\n".encode() for index in range(3)]
)


def _case(index: int) -> bytes:
    base = BASE_CASES[index % len(BASE_CASES)]
    prefix = b"\xef\xbb\xbf" if index % 5 == 0 else b""
    if index % 4 == 0:
        prefix += b"---\ntitle: Property\n---\n\n"
    separator = (b"\n", b"\r\n", b"\r")[index % 3]
    bundle = (
        b"\n# Bundle heading\n\nTitle\n=====\n\n***\n\n- bundle item\n\n"
        b"    code\n\n[ref]: a.md\n\n"
        b"| A | B |\n| --- | --- |\n| C | D |\n\n"
        b"{% include x.md %}\n\n```\nx\n```\n\n<base>\n\n"
        b"Use foo::bar, docs/a.md, and {{ product.name }}.\n"
    )
    suffix = separator + f"Unique property prose {index} Ж 漢 😀.".encode() + separator
    source = prefix + base + bundle + suffix
    if index % 4 == 1:
        source = source.rstrip(b"\r\n")
    return source


def _reference_line(source: bytes, offset: int) -> int:
    line = 1
    cursor = 0
    while cursor < len(source):
        if source[cursor : cursor + 2] == b"\r\n":
            cursor += 2
            if offset < cursor:
                return line
            line += 1
        elif source[cursor : cursor + 1] in {b"\n", b"\r"}:
            cursor += 1
            if offset < cursor:
                return line
            line += 1
        else:
            cursor += 1
    return line


def _assert_plan_properties(source: bytes) -> tuple[Counter[str], Counter[str]]:
    plan = build_markdown_plan(SNAPSHOT, PATH, source)
    assert render_identity(source, plan) == source
    assert plan_from_wire(plan_to_wire(plan)) == plan
    assert build_markdown_plan(SNAPSHOT, PATH, source) == plan
    assert b"".join(source[b.span.start : b.span.end] for b in plan.blocks) == source
    cursor = 0
    categories: Counter[str] = Counter()
    for block in plan.blocks:
        assert block.span.start == cursor
        cursor = block.span.end
        assert block.lines.start == _reference_line(source, block.span.start)
        assert block.lines.end == _reference_line(source, block.span.end - 1)
        categories[f"block:{block.kind.value}"] += 1
        field_cursor = block.span.start
        for field in block.fields:
            assert field_cursor <= field.span.start < field.span.end <= block.span.end
            field_cursor = field.span.end
            assert field.lines.start == _reference_line(source, field.span.start)
            assert field.lines.end == _reference_line(source, field.span.end - 1)
            categories[f"field:{field.kind.value}"] += 1
            protected_cursor = field.span.start
            for region in field.protected_regions:
                assert protected_cursor <= region.span.start < region.span.end <= field.span.end
                protected_cursor = region.span.end
                categories[f"protected:{region.kind.value}"] += 1
    assert cursor == len(source)
    assert len({field.field_id for field in fields_of(plan)}) == len(fields_of(plan))
    unknowns = [block for block in plan.blocks if block.kind is BlockKind.UNKNOWN]
    assert len(unknowns) == len(plan.diagnostics)
    assert [item.span for item in plan.diagnostics] == [block.span for block in unknowns]
    flags: Counter[str] = Counter()
    flags["bom_present" if source.startswith(b"\xef\xbb\xbf") else "bom_absent"] += 1
    flags["final_separator" if source.endswith((b"\n", b"\r")) else "no_final_separator"] += 1
    if b"\r\n" in source:
        flags["crlf"] += 1
    if b"\n" in source.replace(b"\r\n", b""):
        flags["lf"] += 1
    if b"\r" in source.replace(b"\r\n", b""):
        flags["cr"] += 1
    field_bytes = b" ".join(source[item.span.start : item.span.end] for item in fields_of(plan))
    field_text = field_bytes.decode("utf-8")
    if any("a" <= char.lower() <= "z" for char in field_text):
        flags["unicode_ascii"] += 1
    if any("\u0400" <= char <= "\u04ff" for char in field_text):
        flags["unicode_ru"] += 1
    if "漢" in field_text:
        flags["unicode_three_byte"] += 1
    if "😀" in field_text:
        flags["unicode_four_byte"] += 1
    observed = [(block.kind, source[block.span.start : block.span.end]) for block in plan.blocks]
    if any(kind is BlockKind.T008_HTML and data.startswith(b"<base>") for kind, data in observed):
        flags["html_void"] += 1
    if any(kind is BlockKind.T008_HTML and data.startswith(b"<div data='/>'>") for kind, data in observed):
        flags["html_quoted_normal"] += 1
    for raw_name in ("script", "style", "pre", "textarea"):
        prefix = f"<{raw_name}>".encode()
        if any(kind is BlockKind.T008_HTML and data.startswith(prefix) for kind, data in observed):
            flags[f"html_raw_{raw_name}"] += 1
    if any(kind is BlockKind.UNKNOWN and data.startswith(b"<widget>") for kind, data in observed):
        flags["html_unlisted"] += 1
    if b"<div>\n<div>\n" in source:
        flags["html_nested_listed"] += 1
    if b"<widget>\n<widget" in source:
        flags["html_nested_unlisted"] += 1
        if b"data='</widget>'" in source:
            flags["html_nested_quote_lookalike"] += 1
    for name, witness in (
        ("inline_one_backtick", b"Use `a\nb` now."),
        ("inline_two_backtick", b"Use ``a`b``"),
        ("inline_uri", b"<https://e.test>"),
        ("inline_email", b"<user@example.com>"),
        ("inline_comment", b"<!--x-->"),
        ("inline_escape", b"\\*"),
        ("inline_emphasis", b"*word*"),
        ("inline_strong", b"**bold**"),
        ("inline_strike", b"~~old~~"),
        ("inline_triple_star", b"***word***"),
        ("inline_triple_underscore", b"___word___"),
        ("inline_long_delimiter", b"****word****"),
    ):
        if witness in source:
            flags[name] += 1
    if any(
        kind is BlockKind.UNKNOWN and data.startswith(b"---\ntitle: X\nbody\n")
        for kind, data in observed
    ):
        flags["malformed_front_matter"] += 1
    if any(
        kind is BlockKind.UNKNOWN and data.startswith(b"{% tabs %}\n{% tab A %}")
        for kind, data in observed
    ):
        flags["malformed_yfm_mismatch"] += 1
    if any(
        kind is BlockKind.UNKNOWN and data.startswith(b"text\x00more")
        for kind, data in observed
    ):
        flags["malformed_control"] += 1
    return categories, flags


def _shrink(source: bytes, predicate: object) -> bytes:
    """Deterministically retain the first smaller input that still fails."""
    check = predicate
    current = source
    while True:
        candidates: list[bytes] = []
        try:
            plan = build_markdown_plan(SNAPSHOT, PATH, current)
        except InvalidPlanInput:
            plan = None
        if plan is not None:
            for block in plan.blocks:
                candidates.append(current[: block.span.start] + current[block.span.end :])
        physical = current.splitlines(keepends=True)
        candidates.extend(b"".join(physical[:index] + physical[index + 1 :]) for index in range(len(physical)))
        candidates.extend((current[: max(1, len(current) // 2)], current.replace(b"\r\n", b"\n")))
        replacement = next(
            (char.encode() for char in current.decode("utf-8", "ignore") if char.isalnum()),
            b"a",
        )
        candidates.append(replacement)
        smaller = sorted({item for item in candidates if len(item) < len(current)}, key=lambda item: (len(item), item))
        retained = next((item for item in smaller if check(item)), None)  # type: ignore[operator]
        if retained is None:
            return current
        current = retained


@pytest.mark.parametrize("seed", [SEED])
def test_deterministic_shrinkable_roundtrip_corpus(seed: int) -> None:
    random.Random(seed)
    sources = {_case(index) for index in range(500)} | set(MALFORMED_QUOTA_CASES)
    assert len(sources) == 509
    categories: Counter[str] = Counter()
    flags: Counter[str] = Counter()
    for source in sorted(sources):
        try:
            observed, observed_flags = _assert_plan_properties(source)
        except AssertionError:
            minimized = _shrink(
                source,
                lambda candidate: _fails_plan_properties(candidate),
            )
            pytest.fail(f"seed={seed} minimized={minimized!r}")
        categories.update(observed)
        flags.update(observed_flags)
    assert all(categories[f"protected:{kind.value}"] >= 12 for kind in ProtectedKind)
    assert all(categories[f"block:{kind.value}"] >= 20 for kind in BlockKind)
    assert all(categories[f"field:{kind.value}"] >= 40 for kind in FieldKind)
    assert categories[f"block:{BlockKind.UNKNOWN.value}"] >= 60
    assert all(flags[name] >= 75 for name in ("lf", "crlf", "cr"))
    assert all(
        flags[name] >= 75
        for name in ("bom_present", "bom_absent", "final_separator", "no_final_separator")
    )
    assert all(
        flags[name] >= 40
        for name in ("unicode_ascii", "unicode_ru", "unicode_three_byte", "unicode_four_byte")
    )
    assert all(
        flags[name] >= 12
        for name in (
            "html_void",
            "html_quoted_normal",
            "html_raw_script",
            "html_raw_style",
            "html_raw_pre",
            "html_raw_textarea",
            "html_unlisted",
            "html_nested_listed",
            "html_nested_unlisted",
            "html_nested_quote_lookalike",
            "inline_one_backtick",
            "inline_two_backtick",
            "inline_uri",
            "inline_email",
            "inline_comment",
            "inline_escape",
            "inline_emphasis",
            "inline_strong",
            "inline_strike",
            "inline_triple_star",
            "inline_triple_underscore",
            "inline_long_delimiter",
        )
    )
    assert all(
        flags[name] >= 15
        for name in (
            "malformed_front_matter",
            "malformed_yfm_mismatch",
            "malformed_control",
        )
    )
    print(f"seed={seed} successful={len(sources)} distinct={len(sources)}")
    print(f"categories={dict(sorted(categories.items()))}")
    print(f"flags={dict(sorted(flags.items()))}")


def _fails_plan_properties(source: bytes) -> bool:
    try:
        _assert_plan_properties(source)
    except (AssertionError, InvalidPlanInput):
        return True
    return False


def test_malformed_and_invalid_corpus_is_distinct_and_typed() -> None:
    invalid_bases = (b"\xff", b"\x80", b"\xc0\xaf", b"\xe2\x82", b"\xed\xa0\x80")
    invalid = {base + bytes([index % 128]) for index in range(25) for base in invalid_bases}
    unknown = {f"{{% bad {index} %}}\n".encode() for index in range(75)}
    assert len(invalid | unknown) >= 100
    invalid_count = 0
    unknown_count = 0
    for source in invalid:
        with pytest.raises(InvalidPlanInput) as caught:
            build_markdown_plan(SNAPSHOT, PATH, source)
        assert caught.value.reason is PlanInputReason.INVALID_UTF8_SOURCE
        invalid_count += 1
    for source in unknown:
        plan = build_markdown_plan(SNAPSHOT, PATH, source)
        assert any(block.kind is BlockKind.UNKNOWN for block in plan.blocks)
        unknown_count += 1
    assert invalid_count >= 25
    assert unknown_count >= 60
    print(f"seed={SEED} malformed={invalid_count + unknown_count} invalid={invalid_count} unknown={unknown_count}")


WIRE_SCRIPT = """
import json
from ydbdoc_review_ng.domain import GitSha, RepoPath, RepositoryId, SnapshotRef
from ydbdoc_review_ng.parser.markdown import build_markdown_plan
from ydbdoc_review_ng.plan import plan_to_wire
p = build_markdown_plan(SnapshotRef(RepositoryId('ydb-platform/ydb'), GitSha('a'*40)), RepoPath('x.md'), '# Ж 😀\\r\\n'.encode())
print(json.dumps(plan_to_wire(p), ensure_ascii=False, sort_keys=True, separators=(',', ':')))
"""


def _wire_output(seed: str, locale_name: str) -> bytes:
    env = dict(os.environ, PYTHONHASHSEED=seed, LC_ALL=locale_name)
    return subprocess.check_output([sys.executable, "-c", WIRE_SCRIPT], env=env)


def _available_utf8_locale() -> str | None:
    try:
        output = subprocess.check_output(["locale", "-a"], text=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    available = output.splitlines()
    if "en_US.UTF-8" in available:
        return "en_US.UTF-8"
    return next(
        (name for name in available if "utf-8" in name.lower() or "utf8" in name.lower()),
        None,
    )


def test_wire_is_hash_seed_independent_under_c_locale() -> None:
    seeds = ("1", "777")
    outputs = [_wire_output(seed, "C") for seed in seeds]
    assert outputs[0] == outputs[1]
    print(f"determinism locales=C seeds={','.join(seeds)}")


def test_wire_is_utf8_locale_independent() -> None:
    utf8_locale = _available_utf8_locale()
    if utf8_locale is None:
        pytest.skip("no UTF-8 locale is available; only the locale comparison is skipped")
    assert _wire_output("1", "C") == _wire_output("1", utf8_locale)
    print(f"determinism locales=C,{utf8_locale} seed=1")
