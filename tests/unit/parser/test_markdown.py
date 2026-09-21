from __future__ import annotations

import dataclasses
import hashlib
import inspect
import sys
from collections.abc import Mapping
from enum import Enum
from types import FrameType

import pytest

from ydbdoc_review_ng import plan as plan_module
from ydbdoc_review_ng.direction import Direction
from ydbdoc_review_ng.domain import (
    ContentHash,
    Diagnostic,
    FilePair,
    GitSha,
    Locale,
    RepoPath,
    RepositoryId,
    Severity,
    SnapshotRef,
)
from ydbdoc_review_ng.errors import InvariantViolation
from ydbdoc_review_ng.locales import LocaleRoots, PairKey
from ydbdoc_review_ng.parser import core
from ydbdoc_review_ng.parser import markdown as markdown_module
from ydbdoc_review_ng.parser.markdown import build_markdown_plan, build_scope_plans
from ydbdoc_review_ng.plan import (
    PLAN_VERSION,
    Block,
    BlockKind,
    ByteSpan,
    Field,
    FieldId,
    FieldKind,
    InvalidPlanInput,
    LineRange,
    MalformedPlanPayload,
    PlanDiagnostic,
    PlanError,
    PlanInputReason,
    PlanSerializationError,
    ProtectedKind,
    ProtectedRegion,
    SourcePlan,
    UnsupportedPlanVersion,
    fields_of,
    make_field_id,
    plan_from_wire,
    plan_to_wire,
    render_identity,
    validate_source_plan,
)
from ydbdoc_review_ng.scope import FileOperation, ScopeEntry, ScopeManifest, ScopeOrigin

SNAPSHOT = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a" * 40))
PATH = RepoPath("ydb/docs/en/test.md")
VOID_HTML_NAMES = [
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
]
NORMAL_HTML_NAMES = [
    "address",
    "article",
    "aside",
    "basefont",
    "blockquote",
    "body",
    "caption",
    "center",
    "colgroup",
    "dd",
    "details",
    "dialog",
    "dir",
    "div",
    "dl",
    "dt",
    "fieldset",
    "figcaption",
    "figure",
    "footer",
    "form",
    "frame",
    "frameset",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "head",
    "header",
    "html",
    "iframe",
    "legend",
    "li",
    "main",
    "menu",
    "menuitem",
    "nav",
    "noframes",
    "ol",
    "optgroup",
    "option",
    "p",
    "search",
    "section",
    "summary",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "title",
    "tr",
    "ul",
]
RAW_HTML_NAMES = ["script", "style", "pre", "textarea"]


def build(source: bytes) -> SourcePlan:
    return build_markdown_plan(SNAPSHOT, PATH, source)


def block_shape(source: bytes) -> list[tuple[BlockKind, int, int, int, int]]:
    return [
        (block.kind, block.span.start, block.span.end, block.lines.start, block.lines.end)
        for block in build(source).blocks
    ]


def region_shape(source: bytes) -> list[tuple[ProtectedKind, int, int, int | None]]:
    plan = build(source)
    return [
        (region.kind, region.span.start, region.span.end, region.group)
        for field in fields_of(plan)
        for region in field.protected_regions
    ]


def test_public_builder_work_growth_is_bounded() -> None:
    production_filenames = {
        core.__file__,
        markdown_module.__file__,
        plan_module.__file__,
    }
    assert None not in production_filenames

    def measure(source: bytes) -> tuple[SourcePlan, int]:
        events = 0

        def trace(frame: FrameType, event: str, _arg: object) -> object:
            nonlocal events
            if event == "line" and frame.f_code.co_filename in production_filenames:
                events += 1
            return trace

        sys.settrace(trace)
        try:
            measured_plan = build_markdown_plan(SNAPSHOT, PATH, source)
            validate_source_plan(source, measured_plan)
        finally:
            sys.settrace(None)
        return measured_plan, events

    def assert_bounded(family: str, counts: list[int]) -> None:
        ratios = [counts[index + 1] / counts[index] for index in range(2)]
        assert all(ratio <= 2.50 for ratio in ratios), (
            f"{family} work counts={counts}, ratios={ratios}"
        )

    for family, base in (("inline", 256), ("block", 64)):
        counts: list[int] = []
        for multiplier in (1, 2, 4):
            size = base * multiplier
            source = b"`x` " * size + b"x\n" if family == "inline" else b"# x\n\n" * size
            first_plan, first_count = measure(source)
            second_plan, second_count = measure(source)

            assert first_plan == second_plan
            assert render_identity(source, first_plan) == source
            if family == "inline":
                assert len(first_plan.blocks) == 1
                assert len(fields_of(first_plan)) == 1
                assert len(fields_of(first_plan)[0].protected_regions) == size
            else:
                assert len(first_plan.blocks) == 2 * size
                assert len(fields_of(first_plan)) == size
            assert first_count == second_count
            counts.append(first_count)
        assert_bounded(family, counts)


def test_public_contract_inventory_signatures_and_records() -> None:
    from ydbdoc_review_ng import plan
    from ydbdoc_review_ng.parser import markdown

    assert PLAN_VERSION == 1
    assert core.__all__ == ()
    assert set(markdown.__all__) == {"build_markdown_plan", "build_scope_plans"}
    assert len(plan.__all__) == 24
    assert set(plan.__all__) == {
        "PLAN_VERSION",
        "BlockKind",
        "FieldKind",
        "ProtectedKind",
        "PlanInputReason",
        "ByteSpan",
        "LineRange",
        "FieldId",
        "ProtectedRegion",
        "Field",
        "Block",
        "PlanDiagnostic",
        "SourcePlan",
        "PlanError",
        "InvalidPlanInput",
        "PlanSerializationError",
        "UnsupportedPlanVersion",
        "MalformedPlanPayload",
        "make_field_id",
        "fields_of",
        "validate_source_plan",
        "render_identity",
        "plan_to_wire",
        "plan_from_wire",
    }
    assert [
        [(item.name, item.value) for item in enum]
        for enum in (
            BlockKind,
            FieldKind,
            ProtectedKind,
            PlanInputReason,
        )
    ] == [
        [
            ("UTF8_BOM", "utf8_bom"),
            ("BLANK", "blank"),
            ("THEMATIC_BREAK", "thematic_break"),
            ("INDENTED_CODE", "indented_code"),
            ("REFERENCE_DEFINITION", "reference_definition"),
            ("ATX_HEADING", "atx_heading"),
            ("SETEXT_HEADING", "setext_heading"),
            ("PARAGRAPH", "paragraph"),
            ("LIST_ITEM", "list_item"),
            ("TABLE", "table"),
            ("T008_FRONT_MATTER", "t008_front_matter"),
            ("T008_YFM", "t008_yfm"),
            ("T008_FENCE", "t008_fence"),
            ("T008_HTML", "t008_html"),
            ("UNKNOWN", "unknown"),
        ],
        [
            ("HEADING", "heading"),
            ("PARAGRAPH", "paragraph"),
            ("LIST_ITEM", "list_item"),
            ("TABLE_CELL", "table_cell"),
        ],
        [
            ("MARKDOWN_SYNTAX", "markdown_syntax"),
            ("INLINE_CODE", "inline_code"),
            ("LINK_OPEN", "link_open"),
            ("LINK_CLOSE", "link_close"),
            ("IMAGE_OPEN", "image_open"),
            ("IMAGE_CLOSE", "image_close"),
            ("AUTOLINK", "autolink"),
            ("URL", "url"),
            ("PATH", "path"),
            ("IDENTIFIER", "identifier"),
            ("TEMPLATE", "template"),
            ("HTML_INLINE", "html_inline"),
            ("EXPLICIT_ANCHOR", "explicit_anchor"),
            ("ESCAPE", "escape"),
            ("LINE_BREAK", "line_break"),
            ("CONTINUATION_PREFIX", "continuation_prefix"),
        ],
        [
            ("INVALID_UTF8_SOURCE", "invalid_utf8_source"),
            ("SOURCE_SIZE_MISMATCH", "source_size_mismatch"),
            ("SOURCE_HASH_MISMATCH", "source_hash_mismatch"),
            ("FIELD_ID_COLLISION", "field_id_collision"),
        ],
    ]
    assert [
        [item.name for item in dataclasses.fields(record)]
        for record in (
            ByteSpan,
            LineRange,
            FieldId,
            ProtectedRegion,
            Field,
            Block,
            PlanDiagnostic,
            SourcePlan,
        )
    ] == [
        ["start", "end"],
        ["start", "end"],
        ["value"],
        ["kind", "span", "group"],
        ["field_id", "kind", "span", "lines", "protected_regions"],
        ["kind", "span", "lines", "fields"],
        ["diagnostic", "span"],
        [
            "plan_version",
            "source_snapshot",
            "source_path",
            "source_sha256",
            "byte_length",
            "line_count",
            "blocks",
            "diagnostics",
        ],
    ]
    assert {
        function.__name__: str(inspect.signature(function))
        for function in (
            make_field_id,
            fields_of,
            validate_source_plan,
            render_identity,
            plan_to_wire,
            plan_from_wire,
            build_markdown_plan,
            build_scope_plans,
        )
    } == {
        "make_field_id": "(source_sha256: 'ContentHash', ordinal: 'int', span: 'ByteSpan', kind: 'FieldKind', /) -> 'FieldId'",
        "fields_of": "(plan: 'SourcePlan', /) -> 'tuple[Field, ...]'",
        "validate_source_plan": "(source: 'bytes', plan: 'SourcePlan', /) -> 'None'",
        "render_identity": "(source: 'bytes', plan: 'SourcePlan', /) -> 'bytes'",
        "plan_to_wire": "(plan: 'SourcePlan', /) -> 'dict[str, JsonValue]'",
        "plan_from_wire": "(payload: 'Mapping[str, object]', /) -> 'SourcePlan'",
        "build_markdown_plan": "(source_snapshot: 'SnapshotRef', source_path: 'RepoPath', source: 'bytes', /) -> 'SourcePlan'",
        "build_scope_plans": "(manifest: 'ScopeManifest', /) -> 'tuple[SourcePlan, ...]'",
    }
    assert issubclass(InvalidPlanInput, PlanError)
    assert issubclass(UnsupportedPlanVersion, PlanSerializationError)
    assert issubclass(MalformedPlanPayload, PlanSerializationError)


def test_records_are_frozen_slotted_hashable_strict_and_repr_safe() -> None:
    span = ByteSpan(0, 1)
    assert hash(span) == hash(ByteSpan(0, 1))
    with pytest.raises(dataclasses.FrozenInstanceError):
        span.start = 1  # type: ignore[misc]
    assert not hasattr(span, "__dict__")
    with pytest.raises(InvariantViolation, match="ByteSpan.start: expected exact int"):
        ByteSpan(True, 1)  # type: ignore[arg-type]

    secret = "SECRET_SOURCE_CANARY"
    diagnostic = Diagnostic(Severity.RED, "x", "x", "fix", PATH, 1, 1, secret)
    wrapped = PlanDiagnostic(diagnostic, ByteSpan(0, 1))
    assert repr(wrapped) == "PlanDiagnostic(span=ByteSpan(start=0, end=1))"
    assert secret not in repr(wrapped)


def test_field_and_block_reject_peer_overlap() -> None:
    with pytest.raises(InvariantViolation, match="ordered non-overlapping contained regions"):
        Field(
            FieldId("f1-000001-" + "0" * 64),
            FieldKind.PARAGRAPH,
            ByteSpan(0, 4),
            LineRange(1, 1),
            (
                ProtectedRegion(ProtectedKind.INLINE_CODE, ByteSpan(0, 3), None),
                ProtectedRegion(ProtectedKind.ESCAPE, ByteSpan(2, 4), None),
            ),
        )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (b"", []),
        (b"\xef\xbb\xbf", [(BlockKind.UTF8_BOM, 0, 3, 1, 1)]),
        (
            b"\xef\xbb\xbf# Hi\n",
            [
                (BlockKind.UTF8_BOM, 0, 3, 1, 1),
                (BlockKind.ATX_HEADING, 3, 8, 1, 1),
            ],
        ),
        ("x\ufeffy".encode(), [(BlockKind.PARAGRAPH, 0, 5, 1, 1)]),
        (b" \t\n\t\r", [(BlockKind.BLANK, 0, 5, 1, 2)]),
        (b"***\n", [(BlockKind.THEMATIC_BREAK, 0, 4, 1, 1)]),
        (b"    code\n", [(BlockKind.INDENTED_CODE, 0, 9, 1, 1)]),
        (b"[x]: a.md\n", [(BlockKind.REFERENCE_DEFINITION, 0, 10, 1, 1)]),
    ],
)
def test_basic_blocks(source: bytes, expected: list[tuple[BlockKind, int, int, int, int]]) -> None:
    assert block_shape(source) == expected
    assert render_identity(source, build(source)) == source


@pytest.mark.parametrize(
    ("source", "offset"),
    [
        (b"\xff", 0),
        (b"x\x80", 1),
        (b"\xc0\xaf", 0),
        (b"\xe2\x82", 0),
        (b"\xed\xa0\x80", 0),
    ],
)
def test_invalid_utf8_is_typed_and_precise(source: bytes, offset: int) -> None:
    with pytest.raises(InvalidPlanInput) as caught:
        build(source)
    assert caught.value.reason is PlanInputReason.INVALID_UTF8_SOURCE
    assert caught.value.byte_offset == offset
    assert source.decode("latin1") not in str(caught.value)


@pytest.mark.parametrize("source", [b"a\n", b"a\r\n", b"a\r", b"a\r\nb\nc\rD", "Ж😀漢\n".encode()])
def test_newlines_multibyte_and_identity(source: bytes) -> None:
    plan = build(source)
    assert render_identity(source, plan) == source
    assert b"".join(source[b.span.start : b.span.end] for b in plan.blocks) == source


def test_exact_heading_ids_and_byte_coordinates() -> None:
    en = build(b"# Hello\n")
    ru = build("# Привет\r\n".encode())
    assert fields_of(en)[0] == Field(
        FieldId("f1-000001-212be759d189c7672c2e2ee0982e9048f0f3df8227324c4b9eb213d3280395ff"),
        FieldKind.HEADING,
        ByteSpan(2, 7),
        LineRange(1, 1),
        (),
    )
    assert fields_of(ru)[0].span == ByteSpan(2, 14)
    assert fields_of(ru)[0].field_id.value == (
        "f1-000001-556c56cecb577834a347b9714c92e1f9fbaf67adebcd4d22c9fae8cd4b3e11b8"
    )


@pytest.mark.parametrize(
    ("source", "kind", "span"),
    [
        (b"###### Header #### \n", FieldKind.HEADING, ByteSpan(7, 13)),
        (b"Title\r\n=====\r\n", FieldKind.HEADING, ByteSpan(0, 5)),
        (b"Title\n---\n", FieldKind.HEADING, ByteSpan(0, 5)),
        (b"First\nSecond\n", FieldKind.PARAGRAPH, ByteSpan(0, 12)),
        (b"  > First\n  > Second.\n", FieldKind.PARAGRAPH, ByteSpan(4, 21)),
        (b"- item\n", FieldKind.LIST_ITEM, ByteSpan(2, 6)),
        (b"+ item\n", FieldKind.LIST_ITEM, ByteSpan(2, 6)),
        (b"* item\n", FieldKind.LIST_ITEM, ByteSpan(2, 6)),
        (b"1. item\n", FieldKind.LIST_ITEM, ByteSpan(3, 7)),
        (b"1) item\n", FieldKind.LIST_ITEM, ByteSpan(3, 7)),
    ],
)
def test_whole_grammatical_fields(source: bytes, kind: FieldKind, span: ByteSpan) -> None:
    field = fields_of(build(source))[0]
    assert (field.kind, field.span) == (kind, span)


def test_list_nesting_and_prefix_regions_are_nonoverlapping() -> None:
    source = b"- parent\n  continuation\n  - child\n"
    plan = build(source)
    assert block_shape(source) == [
        (BlockKind.LIST_ITEM, 0, 24, 1, 2),
        (BlockKind.LIST_ITEM, 24, 34, 3, 3),
    ]
    assert fields_of(plan)[0].span == ByteSpan(2, 23)
    assert fields_of(plan)[0].protected_regions == (
        ProtectedRegion(ProtectedKind.LINE_BREAK, ByteSpan(8, 9), None),
        ProtectedRegion(ProtectedKind.CONTINUATION_PREFIX, ByteSpan(9, 11), None),
    )
    assert fields_of(plan)[1].span == ByteSpan(28, 33)


def test_list_marker_variants_are_complete_fields() -> None:
    for source, field_span in (
        (b"- item\n", ByteSpan(2, 6)),
        (b"+ item\n", ByteSpan(2, 6)),
        (b"* item\n", ByteSpan(2, 6)),
        (b"1. item\n", ByteSpan(3, 7)),
        (b"1) item\n", ByteSpan(3, 7)),
    ):
        plan = build(source)
        assert block_shape(source) == [(BlockKind.LIST_ITEM, 0, len(source), 1, 1)]
        assert len(fields_of(plan)) == 1
        field = fields_of(plan)[0]
        assert (field.kind, field.span, field.lines, field.protected_regions) == (
            FieldKind.LIST_ITEM,
            field_span,
            LineRange(1, 1),
            (),
        )


def test_table_fields_are_row_major_and_pipes_in_code_or_escapes_do_not_split() -> None:
    source = b"| Name | A\\|B | `x|y` |\n| :--- | ---: | --- |\n| YDB | Database | Value |\n"
    plan = build(source)
    assert [source[f.span.start : f.span.end] for f in fields_of(plan)] == [
        b"Name",
        b"A\\|B",
        b"YDB",
        b"Database",
        b"Value",
    ]
    assert all(f.kind is FieldKind.TABLE_CELL for f in fields_of(plan))


def test_links_images_and_reference_links_stay_in_owning_field() -> None:
    source = b"Read [guide](a.md), ![diagram][img], and [more][ref].\n"
    plan = build(source)
    assert len(fields_of(plan)) == 1
    regions = fields_of(plan)[0].protected_regions
    assert [(r.kind, r.group) for r in regions] == [
        (ProtectedKind.LINK_OPEN, 1),
        (ProtectedKind.LINK_CLOSE, 1),
        (ProtectedKind.IMAGE_OPEN, 2),
        (ProtectedKind.IMAGE_CLOSE, 2),
        (ProtectedKind.LINK_OPEN, 3),
        (ProtectedKind.LINK_CLOSE, 3),
    ]


def test_linked_image_emits_nested_container_regions_in_nesting_order() -> None:
    source = b"[![diagram](/good.png)](/outer)\n"
    regions = fields_of(build(source))[0].protected_regions

    assert tuple(
        (region.kind, region.group, source[region.span.start : region.span.end])
        for region in regions
    ) == (
        (ProtectedKind.LINK_OPEN, 1, b"["),
        (ProtectedKind.IMAGE_OPEN, 2, b"!["),
        (ProtectedKind.IMAGE_CLOSE, 2, b"](/good.png)"),
        (ProtectedKind.LINK_CLOSE, 1, b"](/outer)"),
    )


def test_link_label_balancing_skips_complete_code_spans() -> None:
    cases = (
        (
            b"Read [a `]` b](x.md).\n",
            ByteSpan(0, 21),
            (
                (ProtectedKind.LINK_OPEN, ByteSpan(5, 6), 1, b"["),
                (ProtectedKind.INLINE_CODE, ByteSpan(8, 11), None, b"`]`"),
                (ProtectedKind.LINK_CLOSE, ByteSpan(13, 20), 1, b"](x.md)"),
            ),
        ),
        (
            b"See ![a `]` b](i.png).\n",
            ByteSpan(0, 22),
            (
                (ProtectedKind.IMAGE_OPEN, ByteSpan(4, 6), 1, b"!["),
                (ProtectedKind.INLINE_CODE, ByteSpan(8, 11), None, b"`]`"),
                (ProtectedKind.IMAGE_CLOSE, ByteSpan(13, 21), 1, b"](i.png)"),
            ),
        ),
        (
            b"Read [a \\] b](x.md).\n",
            ByteSpan(0, 20),
            (
                (ProtectedKind.LINK_OPEN, ByteSpan(5, 6), 1, b"["),
                (ProtectedKind.ESCAPE, ByteSpan(8, 10), None, b"\\]"),
                (ProtectedKind.LINK_CLOSE, ByteSpan(12, 19), 1, b"](x.md)"),
            ),
        ),
    )
    for source, field_span, expected_regions in cases:
        plan = build(source)
        assert plan.blocks[0].kind is BlockKind.PARAGRAPH
        assert plan.blocks[0].span == ByteSpan(0, len(source))
        assert plan.blocks[0].lines == LineRange(1, 1)
        assert len(fields_of(plan)) == 1
        field = fields_of(plan)[0]
        assert field.kind is FieldKind.PARAGRAPH
        assert field.span == field_span
        assert field.lines == LineRange(1, 1)
        assert (
            tuple(
                (
                    region.kind,
                    region.span,
                    region.group,
                    source[region.span.start : region.span.end],
                )
                for region in field.protected_regions
            )
            == expected_regions
        )


def test_email_autolink_enforces_total_domain_limit() -> None:
    domain_253 = b".".join((b"a" * 63, b"b" * 63, b"c" * 63, b"d" * 61))
    domain_254 = b".".join((b"a" * 63, b"b" * 63, b"c" * 63, b"d" * 62))
    domain_304 = b".".join((b"a" * 60,) * 5)
    assert (len(domain_253), len(domain_254), len(domain_304)) == (253, 254, 304)

    valid_source = b"See <x@" + domain_253 + b">\n"
    valid = build(valid_source)
    assert valid.blocks[0].kind is BlockKind.PARAGRAPH
    assert valid.blocks[0].span == ByteSpan(0, 262)
    assert valid.blocks[0].lines == LineRange(1, 1)
    assert len(fields_of(valid)) == 1
    field = fields_of(valid)[0]
    assert field.kind is FieldKind.PARAGRAPH
    assert field.span == ByteSpan(0, 261)
    assert field.lines == LineRange(1, 1)
    assert field.protected_regions == (
        ProtectedRegion(ProtectedKind.AUTOLINK, ByteSpan(4, 261), None),
    )
    region = field.protected_regions[0]
    assert valid_source[region.span.start : region.span.end] == b"<x@" + domain_253 + b">"
    assert valid.diagnostics == ()

    invalid = tuple(build(b"See <x@" + domain + b">\n") for domain in (domain_254, domain_304))
    assert tuple(plan.blocks for plan in invalid) == (
        (Block(BlockKind.UNKNOWN, ByteSpan(0, 263), LineRange(1, 1), ()),),
        (Block(BlockKind.UNKNOWN, ByteSpan(0, 313), LineRange(1, 1), ()),),
    )
    assert tuple(plan.diagnostics for plan in invalid) == tuple(
        (
            PlanDiagnostic(
                Diagnostic(
                    Severity.RED,
                    "unknown_markdown_block",
                    "Markdown block could not be classified safely",
                    "Fix the malformed Markdown or add parser support before translation",
                    PATH,
                    1,
                    1,
                    None,
                ),
                ByteSpan(0, expected_end),
            ),
        )
        for expected_end in (263, 313)
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            b"Use ``a`b`` and *word*.\n",
            [
                (ProtectedKind.INLINE_CODE, 4, 11, None),
                (ProtectedKind.MARKDOWN_SYNTAX, 16, 17, None),
                (ProtectedKind.MARKDOWN_SYNTAX, 21, 22, None),
            ],
        ),
        (b"Use `a\nb` now.\n", [(ProtectedKind.INLINE_CODE, 4, 9, None)]),
        (
            b"***word***\n",
            [
                (ProtectedKind.MARKDOWN_SYNTAX, 0, 3, None),
                (ProtectedKind.MARKDOWN_SYNTAX, 7, 10, None),
            ],
        ),
        (
            b"___word___\n",
            [
                (ProtectedKind.MARKDOWN_SYNTAX, 0, 3, None),
                (ProtectedKind.MARKDOWN_SYNTAX, 7, 10, None),
            ],
        ),
        (
            b"See <user@example.com> and <span>x</span>.\n",
            [
                (ProtectedKind.AUTOLINK, 4, 22, None),
                (ProtectedKind.HTML_INLINE, 27, 33, None),
                (ProtectedKind.HTML_INLINE, 34, 41, None),
            ],
        ),
        (
            b"Visit <https://e.test> <!--x--> \\* **bold** ~~old~~.\n",
            [
                (ProtectedKind.AUTOLINK, 6, 22, None),
                (ProtectedKind.HTML_INLINE, 23, 31, None),
                (ProtectedKind.ESCAPE, 32, 34, None),
                (ProtectedKind.MARKDOWN_SYNTAX, 35, 37, None),
                (ProtectedKind.MARKDOWN_SYNTAX, 41, 43, None),
                (ProtectedKind.MARKDOWN_SYNTAX, 44, 46, None),
                (ProtectedKind.MARKDOWN_SYNTAX, 49, 51, None),
            ],
        ),
        (
            b"Use foo::bar, docs/a.md, and {{ product.name }}.\n",
            [
                (ProtectedKind.IDENTIFIER, 4, 12, None),
                (ProtectedKind.PATH, 14, 23, None),
                (ProtectedKind.TEMPLATE, 29, 47, None),
            ],
        ),
    ],
)
def test_exact_inline_regions(
    source: bytes, expected: list[tuple[ProtectedKind, int, int, int | None]]
) -> None:
    assert region_shape(source) == expected


def test_heading_anchor_and_multiline_transition_regions() -> None:
    assert region_shape(b"# Hello {#id}\n") == [(ProtectedKind.EXPLICIT_ANCHOR, 8, 13, None)]
    assert region_shape(b"  First\n Second\n") == [
        (ProtectedKind.LINE_BREAK, 7, 8, None),
        (ProtectedKind.CONTINUATION_PREFIX, 8, 9, None),
    ]


def test_unsupported_structural_delimiter_and_malformed_inline_are_unknown() -> None:
    for source in (b"****word****\n", b"Text {{ no close\n", b"Text <span no close\n"):
        plan = build(source)
        assert [b.kind for b in plan.blocks] == [BlockKind.UNKNOWN]
        assert len(plan.diagnostics) == 1
        assert plan.diagnostics[0].span == plan.blocks[0].span


@pytest.mark.parametrize(
    ("source", "kind", "field_count"),
    [
        (b"---\ntitle: X\n---\n", BlockKind.T008_FRONT_MATTER, 1),
        (b"```python\n# comment\n", BlockKind.T008_FENCE, 1),
        (b"{% include path.md %}\n", BlockKind.T008_YFM, 0),
        (b"{% note info %}\ntext\n{% endnote %}\n", BlockKind.T008_YFM, 1),
        (b":::note info\ntext\n:::\n", BlockKind.T008_YFM, 1),
        (b"<base>\n", BlockKind.T008_HTML, 0),
        (b"<script>\nalert(1)\n</script>\n", BlockKind.T008_HTML, 0),
    ],
)
def test_t008_blocks_expose_only_supported_fields_without_diagnostics(
    source: bytes, kind: BlockKind, field_count: int
) -> None:
    plan = build(source)
    assert len(plan.blocks) == 1
    assert plan.blocks[0].kind is kind
    assert len(plan.blocks[0].fields) == field_count
    assert plan.diagnostics == ()


def test_brace_yfm_attribute_tail_rejects_earlier_closer() -> None:
    malformed_source = b"{% include x %} trailing %}\n"
    malformed = build(malformed_source)
    assert malformed.blocks == (
        Block(
            BlockKind.UNKNOWN,
            ByteSpan(0, len(malformed_source)),
            LineRange(1, 1),
            (),
        ),
    )
    assert malformed.diagnostics == (
        PlanDiagnostic(
            Diagnostic(
                Severity.RED,
                "unknown_markdown_block",
                "Markdown block could not be classified safely",
                "Fix the malformed Markdown or add parser support before translation",
                PATH,
                1,
                1,
                None,
            ),
            ByteSpan(0, len(malformed_source)),
        ),
    )

    before = b"{% include before.md %}\n"
    after = b"{% include after.md %}\n"
    controls = build(before + malformed_source + after)
    assert controls.blocks == (
        Block(BlockKind.T008_YFM, ByteSpan(0, len(before)), LineRange(1, 1), ()),
        Block(
            BlockKind.UNKNOWN,
            ByteSpan(len(before), len(before) + len(malformed_source)),
            LineRange(2, 2),
            (),
        ),
        Block(
            BlockKind.T008_YFM,
            ByteSpan(len(before) + len(malformed_source), len(before + malformed_source + after)),
            LineRange(3, 3),
            (),
        ),
    )
    assert len(controls.diagnostics) == 1
    assert controls.diagnostics[0].span == controls.blocks[1].span
    assert controls.diagnostics[0].diagnostic.line_start == 2
    assert controls.diagnostics[0].diagnostic.line_end == 2


def test_html_attribute_requires_space_or_tab_separator() -> None:
    invalid_source = b"<div:id=x>\nAfter\n"
    invalid = build(invalid_source)
    assert invalid.blocks == (
        Block(
            BlockKind.UNKNOWN,
            ByteSpan(0, len(invalid_source)),
            LineRange(1, 2),
            (),
        ),
    )
    assert invalid.diagnostics == (
        PlanDiagnostic(
            Diagnostic(
                Severity.RED,
                "unknown_markdown_block",
                "Markdown block could not be classified safely",
                "Fix the malformed Markdown or add parser support before translation",
                PATH,
                1,
                2,
                None,
            ),
            ByteSpan(0, len(invalid_source)),
        ),
    )

    valid_open = b"<div id=x>\nAfter\n"
    assert build(valid_open).blocks == (
        Block(BlockKind.T008_HTML, ByteSpan(0, len(valid_open)), LineRange(1, 2), ()),
    )
    assert build(valid_open).diagnostics == ()

    self_closing = b"<div id=x />\nAfter\n"
    self_closing_plan = build(self_closing)
    assert self_closing_plan.blocks[0] == Block(
        BlockKind.T008_HTML,
        ByteSpan(0, 13),
        LineRange(1, 1),
        (),
    )
    paragraph = self_closing_plan.blocks[1]
    assert paragraph.kind is BlockKind.PARAGRAPH
    assert paragraph.span == ByteSpan(13, len(self_closing))
    assert paragraph.lines == LineRange(2, 2)
    assert len(paragraph.fields) == 1
    assert paragraph.fields[0].kind is FieldKind.PARAGRAPH
    assert paragraph.fields[0].span == ByteSpan(13, len(self_closing) - 1)
    assert paragraph.fields[0].lines == LineRange(2, 2)
    assert paragraph.fields[0].protected_regions == ()
    assert self_closing_plan.diagnostics == ()


def test_reference_definition_continuations_strip_prefix_before_precedence() -> None:
    for prefix in (b"  ", b"    ", b"\t"):
        source = b"[x]: a.md\n" + prefix + b"title\n"
        plan = build(source)
        assert plan.blocks == (
            Block(
                BlockKind.REFERENCE_DEFINITION,
                ByteSpan(0, len(source)),
                LineRange(1, 2),
                (),
            ),
        )
        assert plan.diagnostics == ()

    source = b"[x]: a.md\n  # heading\n"
    plan = build(source)
    assert len(plan.blocks) == 2
    assert plan.blocks[0] == Block(
        BlockKind.REFERENCE_DEFINITION,
        ByteSpan(0, 10),
        LineRange(1, 1),
        (),
    )
    heading = plan.blocks[1]
    assert heading.kind is BlockKind.ATX_HEADING
    assert heading.span == ByteSpan(10, len(source))
    assert heading.lines == LineRange(2, 2)
    assert len(heading.fields) == 1
    assert source[heading.fields[0].span.start : heading.fields[0].span.end] == b"heading"
    assert plan.diagnostics == ()


def test_bom_front_matter_starts_at_first_post_bom_byte() -> None:
    source = b"\xef\xbb\xbf---\ntitle: X\n---\n"
    assert block_shape(source) == [
        (BlockKind.UTF8_BOM, 0, 3, 1, 1),
        (BlockKind.T008_FRONT_MATTER, 3, len(source), 1, 3),
    ]


def test_forbidden_control_rejects_otherwise_valid_html_introducer() -> None:
    plan = build(b"<!--\x00-->\n")
    assert [block.kind for block in plan.blocks] == [BlockKind.UNKNOWN]
    assert len(plan.diagnostics) == 1


def test_control_and_malformed_html_take_their_maximal_union() -> None:
    source = b"<widget data='\x00\nclean-looking body\n"
    plan = build(source)
    assert plan.blocks == (Block(BlockKind.UNKNOWN, ByteSpan(0, len(source)), LineRange(1, 2), ()),)
    assert len(plan.diagnostics) == 1


def test_adjacent_one_line_malformed_reserved_candidates_coalesce() -> None:
    source = b"{% bad %}\n{% worse %}\nAfter\n"
    plan = build(source)
    assert [(block.kind, block.span) for block in plan.blocks] == [
        (BlockKind.UNKNOWN, ByteSpan(0, 22)),
        (BlockKind.PARAGRAPH, ByteSpan(22, 28)),
    ]
    assert len(plan.diagnostics) == 1


def test_adjacent_multiline_unknown_and_control_coalesce() -> None:
    source = b"<x>\n</x>\n\x00\n"
    plan = build(source)
    assert plan.blocks == (Block(BlockKind.UNKNOWN, ByteSpan(0, 11), LineRange(1, 3), ()),)
    assert len(plan.diagnostics) == 1
    assert plan.diagnostics[0].span == ByteSpan(0, 11)


def test_html_depth_quote_lookalikes_and_following_prose() -> None:
    listed = b"<div>\n<div>\nInner\n</div>\nOuter\n</div>\nAfter\n"
    unlisted = b"<widget>\n<widget data='</widget>'>\nInner\n</widget>\nOuter\n</widget>\nAfter\n"
    assert block_shape(listed) == [
        (BlockKind.T008_HTML, 0, 38, 1, 6),
        (BlockKind.PARAGRAPH, 38, 44, 7, 7),
    ]
    assert fields_of(build(listed))[0].span == ByteSpan(38, 43)
    assert block_shape(unlisted) == [
        (BlockKind.UNKNOWN, 0, 67, 1, 6),
        (BlockKind.PARAGRAPH, 67, 73, 7, 7),
    ]
    assert len(build(unlisted).diagnostics) == 1


def test_nested_self_close_raw_comment_and_unclosed_reserved_html() -> None:
    assert build(b"<div>\n<div data='</div>' />\nOuter\n</div>\nAfter\n").blocks[
        0
    ].span == ByteSpan(0, 41)
    complex_source = (
        b"<div>\n<!-- </div> -->\n<script>\n'</div>'\n</script>\nOuter\n</div>\nAfter\n"
    )
    assert build(complex_source).blocks[0].span == ByteSpan(0, 63)
    eof = build(b"<div>\n<div/>\n<!-- </div>\n")
    assert eof.blocks == (Block(BlockKind.T008_HTML, ByteSpan(0, 25), LineRange(1, 3), ()),)


@pytest.mark.parametrize("name", VOID_HTML_NAMES)
def test_every_void_html_name_is_deferred(name: str) -> None:
    source = f"<{name}>\n".encode()
    assert build(source).blocks[0].kind is BlockKind.T008_HTML


@pytest.mark.parametrize("name", NORMAL_HTML_NAMES)
def test_every_normal_html_name_is_depth_scanned(name: str) -> None:
    source = f"<{name}>\nbody\n</{name}>\nAfter\n".encode()
    plan = build(source)
    assert plan.blocks[0].kind is BlockKind.T008_HTML
    assert source[plan.blocks[0].span.end :] == b"After\n"


@pytest.mark.parametrize("name", RAW_HTML_NAMES)
def test_every_raw_html_name_is_deferred(name: str) -> None:
    source = f"<{name}>\n'</div>'\n</{name}>\nAfter\n".encode()
    plan = build(source)
    assert plan.blocks[0].kind is BlockKind.T008_HTML
    assert source[plan.blocks[0].span.end :] == b"After\n"


@pytest.mark.parametrize(
    "punctuation",
    [
        bytes([value])
        for value in (
            *range(0x21, 0x30),
            *range(0x3A, 0x41),
            *range(0x5B, 0x61),
            *range(0x7B, 0x7F),
        )
    ],
)
def test_every_ascii_punctuation_escape_is_protected(punctuation: bytes) -> None:
    source = b"x \\" + punctuation + b" y\n"
    assert any(
        region.kind is ProtectedKind.ESCAPE
        for region in fields_of(build(source))[0].protected_regions
    )


def test_malformed_yfm_control_and_front_matter_are_exact_unknowns() -> None:
    yfm = build(b"{% tabs %}\n{% tab A %}\nText\n{% endtabs %}\nAfter\n")
    assert yfm.blocks[0].kind is BlockKind.UNKNOWN
    assert yfm.blocks[0].span == ByteSpan(0, 42)
    assert yfm.blocks[1].kind is BlockKind.PARAGRAPH
    control = build(b"text\x00more\nnext\n")
    assert control.blocks[0].span == ByteSpan(0, 10)
    assert control.blocks[1].span == ByteSpan(10, 15)
    front = build(b"---\ntitle: X\nbody\n")
    assert front.blocks[0].kind is BlockKind.UNKNOWN
    assert front.blocks[0].span == ByteSpan(0, len(b"---\ntitle: X\nbody\n"))


def test_field_id_validation_hash_and_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    digest = ContentHash(hashlib.sha256(b"x").hexdigest())
    expected = make_field_id(digest, 1, ByteSpan(2, 7), FieldKind.HEADING)
    assert expected.value.startswith("f1-000001-") and len(expected.value) == 74
    for ordinal in (0, 1_000_000):
        with pytest.raises(InvariantViolation, match="1 <= ordinal <= 999999"):
            make_field_id(digest, ordinal, ByteSpan(0, 1), FieldKind.PARAGRAPH)
    with pytest.raises(InvariantViolation, match="expected exact int"):
        make_field_id(digest, True, ByteSpan(0, 1), FieldKind.PARAGRAPH)  # type: ignore[arg-type]

    class ConstantDigest:
        def hexdigest(self) -> str:
            return "0" * 64

    monkeypatch.setattr("ydbdoc_review_ng.plan._field_digest", lambda _: ConstantDigest())
    with pytest.raises(InvalidPlanInput) as caught:
        build(b"First\n\nSecond\n")
    assert caught.value.reason is PlanInputReason.FIELD_ID_COLLISION


def test_validate_render_wrong_source_and_exact_type_precedence() -> None:
    plan = build(b"abc\n")
    with pytest.raises(InvalidPlanInput) as size:
        render_identity(b"abc", plan)
    assert size.value.reason is PlanInputReason.SOURCE_SIZE_MISMATCH
    with pytest.raises(InvalidPlanInput) as digest:
        validate_source_plan(b"xyz\n", plan)
    assert digest.value.reason is PlanInputReason.SOURCE_HASH_MISMATCH

    class SourcePlanSubclass(SourcePlan):
        pass

    subclass = SourcePlanSubclass(
        plan.plan_version,
        plan.source_snapshot,
        plan.source_path,
        plan.source_sha256,
        plan.byte_length,
        plan.line_count,
        plan.blocks,
        plan.diagnostics,
    )
    for operation in (fields_of, plan_to_wire):
        with pytest.raises(InvariantViolation, match="expected exact SourcePlan"):
            operation(subclass)


def test_public_function_argument_precedence() -> None:
    digest = ContentHash(hashlib.sha256(b"x").hexdigest())
    span = ByteSpan(0, 1)
    plan = build(b"x\n")

    with pytest.raises(InvariantViolation, match="make_field_id.source_sha256"):
        make_field_id(object(), True, object(), object())  # type: ignore[arg-type]
    with pytest.raises(InvariantViolation, match="make_field_id.ordinal"):
        make_field_id(digest, True, object(), object())  # type: ignore[arg-type]
    with pytest.raises(InvariantViolation, match="make_field_id.span"):
        make_field_id(digest, 1, object(), object())  # type: ignore[arg-type]
    with pytest.raises(InvariantViolation, match="make_field_id.kind"):
        make_field_id(digest, 1, span, object())  # type: ignore[arg-type]

    class SourcePlanSubclass(SourcePlan):
        pass

    subclass = SourcePlanSubclass(
        plan.plan_version,
        plan.source_snapshot,
        plan.source_path,
        plan.source_sha256,
        plan.byte_length,
        plan.line_count,
        plan.blocks,
        plan.diagnostics,
    )
    for operation in (fields_of, plan_to_wire):
        with pytest.raises(InvariantViolation, match=f"{operation.__name__}.plan"):
            operation(subclass)
    for operation in (validate_source_plan, render_identity):
        with pytest.raises(InvariantViolation, match=f"{operation.__name__}.source"):
            operation(object(), subclass)  # type: ignore[arg-type]
        with pytest.raises(InvariantViolation, match=f"{operation.__name__}.plan"):
            operation(b"x\n", subclass)

    with pytest.raises(InvariantViolation, match="build_markdown_plan.source_snapshot"):
        build_markdown_plan(object(), object(), object())  # type: ignore[arg-type]
    with pytest.raises(InvariantViolation, match="build_markdown_plan.source_path"):
        build_markdown_plan(SNAPSHOT, object(), object())  # type: ignore[arg-type]
    with pytest.raises(InvariantViolation, match="build_markdown_plan.source"):
        build_markdown_plan(SNAPSHOT, PATH, object())  # type: ignore[arg-type]
    with pytest.raises(InvariantViolation, match="build_scope_plans.manifest"):
        build_scope_plans(object())  # type: ignore[arg-type]
    with pytest.raises(MalformedPlanPayload) as non_mapping:
        plan_from_wire(object())  # type: ignore[arg-type]
    assert non_mapping.value.json_path == "$"


def test_source_plan_constructor_coverage_gap_overlap_and_final_end() -> None:
    empty_hash = ContentHash(hashlib.sha256(b"xx").hexdigest())

    def make(blocks: tuple[Block, ...], length: int) -> SourcePlan:
        return SourcePlan(1, SNAPSHOT, PATH, empty_hash, length, 1, blocks, ())

    with pytest.raises(InvariantViolation, match="complete coverage from byte 0"):
        make((Block(BlockKind.BLANK, ByteSpan(1, 2), LineRange(1, 1), ()),), 2)
    with pytest.raises(InvariantViolation, match="final end == byte_length"):
        make((Block(BlockKind.BLANK, ByteSpan(0, 1), LineRange(1, 1), ()),), 2)
    for blocks in (
        (
            Block(BlockKind.BLANK, ByteSpan(0, 1), LineRange(1, 1), ()),
            Block(BlockKind.BLANK, ByteSpan(2, 3), LineRange(1, 1), ()),
        ),
        (
            Block(BlockKind.BLANK, ByteSpan(0, 2), LineRange(1, 1), ()),
            Block(BlockKind.BLANK, ByteSpan(1, 3), LineRange(1, 1), ()),
        ),
    ):
        with pytest.raises(InvariantViolation, match="adjacent ordered non-overlapping coverage"):
            make(blocks, 3)


def test_source_plan_line_count_is_exact() -> None:
    source = b" "
    original = build(source)
    failures: list[str] = []
    try:
        SourcePlan(
            original.plan_version,
            original.source_snapshot,
            original.source_path,
            original.source_sha256,
            original.byte_length,
            2,
            original.blocks,
            original.diagnostics,
        )
    except InvariantViolation as error:
        assert str(error) == "SourcePlan.line_count: expected line count consistent with plan shape"
    else:
        failures.append("direct constructor accepted inconsistent line_count")

    wire = plan_to_wire(original)
    wire_source = wire["source"]
    assert isinstance(wire_source, dict)
    wire_source["line_count"] = 2
    try:
        plan_from_wire(wire)
    except MalformedPlanPayload as error:
        assert error.json_path == "$.source.line_count"
        assert error.expected == "line count consistent with plan shape"
    else:
        failures.append("wire decoder accepted inconsistent line_count")
    assert failures == []


def test_wire_source_fields_precede_blocks() -> None:
    for source_key in ("byte_length", "line_count"):
        wire = plan_to_wire(build(b"Use `code`.\n"))
        wire_source = wire["source"]
        blocks = wire["blocks"]
        assert isinstance(wire_source, dict)
        assert isinstance(blocks, list)
        assert isinstance(blocks[0], dict)
        wire_source[source_key] = "SECRET_SOURCE_CANARY"
        blocks[0]["kind"] = "SECRET_BLOCK_CANARY"
        with pytest.raises(MalformedPlanPayload) as caught:
            plan_from_wire(wire)
        assert caught.value.json_path == f"$.source.{source_key}"


def test_wire_parent_fields_precede_children() -> None:
    wire = plan_to_wire(build(b"Use `code`.\n"))
    wire["SECRET_ROOT_CANARY"] = "SECRET_ROOT_VALUE"
    wire["source"] = "SECRET_SOURCE_CANARY"
    with pytest.raises(MalformedPlanPayload) as root_error:
        plan_from_wire(wire)
    assert root_error.value.json_path == "$"

    wire = plan_to_wire(build(b"Use `code`.\n"))
    wire_source = wire["source"]
    blocks = wire["blocks"]
    assert isinstance(wire_source, dict)
    assert isinstance(blocks, list)
    assert isinstance(blocks[0], dict)
    wire_source["repository"] = "SECRET_REPOSITORY_CANARY"
    wire_source["byte_length"] = "SECRET_LENGTH_CANARY"
    blocks[0]["kind"] = "SECRET_BLOCK_CANARY"
    with pytest.raises(MalformedPlanPayload) as source_error:
        plan_from_wire(wire)
    assert source_error.value.json_path == "$.source.repository"

    wire = plan_to_wire(build(b"Use `code`.\n"))
    blocks = wire["blocks"]
    assert isinstance(blocks, list)
    block = blocks[0]
    assert isinstance(block, dict)
    fields = block["fields"]
    assert isinstance(fields, list)
    item = fields[0]
    assert isinstance(item, dict)
    regions = item["protected_regions"]
    assert isinstance(regions, list)
    region = regions[0]
    assert isinstance(region, dict)
    block["kind"] = "SECRET_BLOCK_CANARY"
    item["field_id"] = "SECRET_FIELD_CANARY"
    with pytest.raises(MalformedPlanPayload) as block_error:
        plan_from_wire(wire)
    assert block_error.value.json_path == "$.blocks[0].kind"

    for field_key in ("field_id", "kind"):
        wire = plan_to_wire(build(b"Use `code`.\n"))
        blocks = wire["blocks"]
        assert isinstance(blocks, list)
        block = blocks[0]
        assert isinstance(block, dict)
        fields = block["fields"]
        assert isinstance(fields, list)
        item = fields[0]
        assert isinstance(item, dict)
        regions = item["protected_regions"]
        assert isinstance(regions, list)
        region = regions[0]
        assert isinstance(region, dict)
        item[field_key] = "SECRET_FIELD_CANARY"
        region["kind"] = "SECRET_REGION_CANARY"
        with pytest.raises(MalformedPlanPayload) as field_error:
            plan_from_wire(wire)
        assert field_error.value.json_path == f"$.blocks[0].fields[0].{field_key}"

    wire = plan_to_wire(build(b"Use `code`.\n"))
    blocks = wire["blocks"]
    assert isinstance(blocks, list)
    block = blocks[0]
    assert isinstance(block, dict)
    fields = block["fields"]
    assert isinstance(fields, list)
    item = fields[0]
    assert isinstance(item, dict)
    regions = item["protected_regions"]
    assert isinstance(regions, list)
    region = regions[0]
    assert isinstance(region, dict)
    region["kind"] = "SECRET_REGION_CANARY"
    region["span"] = "SECRET_SPAN_CANARY"
    with pytest.raises(MalformedPlanPayload) as region_error:
        plan_from_wire(wire)
    assert region_error.value.json_path == "$.blocks[0].fields[0].protected_regions[0].kind"

    for diagnostic_key in ("severity", "line_start"):
        wire = plan_to_wire(build(b"{{\n"))
        diagnostics = wire["diagnostics"]
        assert isinstance(diagnostics, list)
        diagnostic = diagnostics[0]
        assert isinstance(diagnostic, dict)
        diagnostic[diagnostic_key] = "SECRET_DIAGNOSTIC_CANARY"
        diagnostic["excerpt"] = "SECRET_EXCERPT_CANARY"
        diagnostic["span"] = "SECRET_SPAN_CANARY"
        with pytest.raises(MalformedPlanPayload) as diagnostic_error:
            plan_from_wire(wire)
        assert diagnostic_error.value.json_path == f"$.diagnostics[0].{diagnostic_key}"

    current: BaseException | None = diagnostic_error.value
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        assert "SECRET_DIAGNOSTIC_CANARY" not in str(current)
        assert "SECRET_DIAGNOSTIC_CANARY" not in repr(current)
        assert all("SECRET_DIAGNOSTIC_CANARY" not in repr(arg) for arg in current.args)
        current = current.__cause__ or current.__context__


def test_wire_roundtrip_strict_version_keys_order_and_canary_non_echo() -> None:
    plan = build(b"# Hello\n\nUnknown {{\n")
    wire = plan_to_wire(plan)
    assert plan_from_wire(wire) == plan
    assert list(wire) == ["plan_version", "source", "blocks", "diagnostics"]
    for bad in (0, 2, True, None):
        payload = dict(wire)
        payload["plan_version"] = bad
        with pytest.raises(UnsupportedPlanVersion) as caught:
            plan_from_wire(payload)
        assert caught.value.json_path == "$.plan_version"
    extra = dict(wire)
    extra["SECRET_SOURCE_CANARY"] = "SECRET_SOURCE_CANARY"
    with pytest.raises(MalformedPlanPayload) as caught:
        plan_from_wire(extra)
    assert "SECRET_SOURCE_CANARY" not in repr(caught.value)
    assert caught.value.json_path == "$"
    reordered = dict(wire)
    reordered["blocks"] = list(reversed(wire["blocks"]))  # type: ignore[arg-type]
    with pytest.raises(MalformedPlanPayload):
        plan_from_wire(reordered)


def test_wire_mapping_subclass_is_accepted_but_nested_subclasses_are_not() -> None:
    class Map(dict[str, object]):
        pass

    wire = plan_to_wire(build(b"Hi\n"))
    assert plan_from_wire(Map(wire)) == build(b"Hi\n")

    class String(str):
        pass

    bad = dict(wire)
    source = dict(bad["source"])  # type: ignore[arg-type]
    source["path"] = String(PATH.value)
    bad["source"] = source
    with pytest.raises(MalformedPlanPayload) as caught:
        plan_from_wire(bad)
    assert caught.value.json_path == "$.source.path"


def test_wire_constructor_failures_have_only_a_sanitized_cause() -> None:
    wire = plan_to_wire(build(b"Use `code`.\n"))
    blocks = wire["blocks"]
    assert isinstance(blocks, list)
    block = blocks[0]
    assert isinstance(block, dict)
    fields = block["fields"]
    assert isinstance(fields, list)
    item = fields[0]
    assert isinstance(item, dict)
    regions = item["protected_regions"]
    assert isinstance(regions, list)
    region = regions[0]
    assert isinstance(region, dict)
    region["group"] = 1
    with pytest.raises(MalformedPlanPayload) as caught:
        plan_from_wire(wire)
    assert caught.value.__context__ is None
    assert type(caught.value.__cause__) is InvariantViolation
    assert caught.value.__cause__.__context__ is None


def test_foreign_enum_and_record_subclasses_are_rejected() -> None:
    class ForeignBlockKind(str, Enum):
        PARAGRAPH = "paragraph"

    class ForeignFieldKind(str, Enum):
        PARAGRAPH = "paragraph"

    class ForeignProtectedKind(str, Enum):
        INLINE_CODE = "inline_code"

    class ForeignPlanInputReason(str, Enum):
        INVALID_UTF8_SOURCE = "invalid_utf8_source"

    with pytest.raises(InvariantViolation, match="Block.kind: expected exact BlockKind"):
        Block(
            ForeignBlockKind.PARAGRAPH,  # type: ignore[arg-type]
            ByteSpan(0, 1),
            LineRange(1, 1),
            (),
        )
    with pytest.raises(InvariantViolation, match="Field.kind: expected exact FieldKind"):
        Field(
            FieldId("f1-000001-" + "0" * 64),
            ForeignFieldKind.PARAGRAPH,  # type: ignore[arg-type]
            ByteSpan(0, 1),
            LineRange(1, 1),
            (),
        )
    with pytest.raises(
        InvariantViolation, match="ProtectedRegion.kind: expected exact ProtectedKind"
    ):
        ProtectedRegion(
            ForeignProtectedKind.INLINE_CODE,  # type: ignore[arg-type]
            ByteSpan(0, 1),
            None,
        )
    with pytest.raises(
        InvariantViolation, match="InvalidPlanInput.reason: expected exact PlanInputReason"
    ):
        InvalidPlanInput(ForeignPlanInputReason.INVALID_UTF8_SOURCE, PATH, 0)  # type: ignore[arg-type]

    class Integer(int):
        pass

    class SpanSubclass(ByteSpan):
        pass

    class TupleSubclass(tuple[ProtectedRegion, ...]):
        pass

    with pytest.raises(InvariantViolation, match="ByteSpan.start: expected exact int"):
        ByteSpan(Integer(0), 1)
    with pytest.raises(InvariantViolation, match="Field.span: expected exact ByteSpan"):
        Field(
            FieldId("f1-000001-" + "0" * 64),
            FieldKind.PARAGRAPH,
            SpanSubclass(0, 1),
            LineRange(1, 1),
            (),
        )
    with pytest.raises(InvariantViolation, match="Field.protected_regions: expected exact tuple"):
        Field(
            FieldId("f1-000001-" + "0" * 64),
            FieldKind.PARAGRAPH,
            ByteSpan(0, 1),
            LineRange(1, 1),
            TupleSubclass(),
        )


def test_errors_and_plan_repr_never_retain_source_or_wire_canaries() -> None:
    secret = "SECRET_SOURCE_CANARY"
    payload: Mapping[str, object] = {
        "plan_version": 1,
        "source": {
            "repository": "ydb-platform/ydb",
            "commit_sha": "a" * 40,
            "path": secret + "/../x.md",
            "sha256": "b" * 64,
            "byte_length": 0,
            "line_count": 0,
        },
        "blocks": [],
        "diagnostics": [],
    }
    with pytest.raises(MalformedPlanPayload) as caught:
        plan_from_wire(payload)
    current: BaseException | None = caught.value
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        assert secret not in str(current)
        assert secret not in repr(current)
        assert all(secret not in repr(arg) for arg in current.args)
        current = current.__cause__ or current.__context__


def test_scope_projection_uses_only_embedded_eligible_source_in_manifest_order() -> None:
    key_a = PairKey(RepoPath("a.md"))
    key_b = PairKey(RepoPath("b.md"))
    first = ScopeEntry(
        FilePair(Locale.RU, Locale.EN, RepoPath("ru/a.md"), RepoPath("en/a.md")),
        b"Alpha\n",
        b"TARGET_CANARY",
        ScopeOrigin.INITIAL,
        FileOperation.TRANSLATE,
        (key_a,),
        None,
        None,
    )
    second = ScopeEntry(
        FilePair(Locale.RU, Locale.EN, RepoPath("ru/b.md"), RepoPath("en/b.md")),
        b"Beta\n",
        None,
        ScopeOrigin.INITIAL,
        FileOperation.RENAME_TARGET_AND_TRANSLATE,
        (key_b,),
        RepoPath("en/old-b.md"),
        b"RENAME_TARGET_CANARY",
    )
    key_c = PairKey(RepoPath("c.md"))
    skipped = ScopeEntry(
        FilePair(Locale.RU, Locale.EN, RepoPath("ru/c.md"), RepoPath("en/c.md")),
        None,
        b"NON_TRANSLATION_TARGET_CANARY",
        ScopeOrigin.INITIAL,
        FileOperation.NOOP_TARGET_ALREADY_RENAMED,
        (key_c,),
        None,
        None,
    )
    manifest = ScopeManifest(
        Direction.RU_TO_EN,
        SNAPSHOT,
        LocaleRoots(RepoPath("ru"), RepoPath("en")),
        (),
        (first, second, skipped),
        0,
        len("Alpha\n") + len("Beta\n"),
    )
    plans = build_scope_plans(manifest)
    assert [plan.source_path for plan in plans] == [RepoPath("ru/a.md"), RepoPath("ru/b.md")]
    assert [plan.source_sha256.value for plan in plans] == [
        hashlib.sha256(b"Alpha\n").hexdigest(),
        hashlib.sha256(b"Beta\n").hexdigest(),
    ]
