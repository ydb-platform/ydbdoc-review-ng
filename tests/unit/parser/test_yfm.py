from __future__ import annotations

import pytest

from ydbdoc_review_ng.domain import GitSha, RepoPath, RepositoryId, SnapshotRef
from ydbdoc_review_ng.parser.markdown import build_markdown_plan
from ydbdoc_review_ng.plan import BlockKind, FieldKind, SourcePlan, fields_of, render_identity

SNAPSHOT = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("b" * 40))
PATH = RepoPath("ydb/docs/en/yfm.md")


def build(source: bytes) -> SourcePlan:
    return build_markdown_plan(SNAPSHOT, PATH, source)


@pytest.mark.parametrize(
    ("source", "title"),
    [
        (b'{% note info "Important note" %}\nBody\n{% endnote %}\n', b"Important note"),
        (b'{% cut "Read more" %}\nBody\n{% endcut %}\n', b"Read more"),
        (b'{% tabs %}\n{% tab "First tab" %}\nBody\n{% endtab %}\n{% endtabs %}\n', b"First tab"),
    ],
    ids=["note-title", "cut-title", "tab-title"],
)
def test_brace_yfm_title_is_a_public_heading_field(source: bytes, title: bytes) -> None:
    plan = build(source)

    assert len(plan.blocks) == 1
    assert plan.blocks[0].kind is BlockKind.T008_YFM
    assert len(plan.blocks[0].fields) == 2
    field = plan.blocks[0].fields[0]
    assert field.kind is FieldKind.HEADING
    assert source[field.span.start : field.span.end] == title
    assert (
        source[plan.blocks[0].fields[1].span.start : plan.blocks[0].fields[1].span.end] == b"Body"
    )
    assert render_identity(source, plan) == source


@pytest.mark.parametrize(
    ("source", "title"),
    [
        (b':::note info "Colon note"\r\nBody\r\n:::\r\n', b"Colon note"),
        (b':::cut "Colon cut"\r\nBody\r\n:::\r\n', b"Colon cut"),
        (b':::tabs\r\n:::tab "Colon tab"\r\nBody\r\n:::\r\n:::', b"Colon tab"),
    ],
    ids=["note-title", "cut-title", "tab-title"],
)
def test_colon_yfm_titles_preserve_crlf_and_optional_final_newline(
    source: bytes, title: bytes
) -> None:
    plan = build(source)

    assert [source[field.span.start : field.span.end] for field in fields_of(plan)] == [
        title,
        b"Body",
    ]
    assert render_identity(source, plan) == source


def test_yfm_non_title_attributes_and_include_remain_protected() -> None:
    source = (
        b"{% include path/to/file.md %}\n"
        b"{% note warning %}\nBody\n{% endnote %}\n"
        b"{% cut %}\nBody\n{% endcut %}\n"
    )

    plan = build(source)

    assert [source[field.span.start : field.span.end] for field in fields_of(plan)] == [
        b"Body",
        b"Body",
    ]
    assert [block.kind for block in plan.blocks] == [
        BlockKind.T008_YFM,
        BlockKind.T008_YFM,
        BlockKind.T008_YFM,
    ]
    assert render_identity(source, plan) == source


def test_nested_yfm_titles_follow_source_order_and_have_stable_ids() -> None:
    source = (
        '{% note info "Заметка" %}\n{% cut "Подробнее" %}\nBody\n{% endcut %}\n{% endnote %}\n'
    ).encode()

    first = build(source)
    second = build(source)
    fields = fields_of(first)

    assert [source[field.span.start : field.span.end] for field in fields] == [
        "Заметка".encode(),
        "Подробнее".encode(),
        b"Body",
    ]
    assert [field.field_id for field in fields] == [field.field_id for field in fields_of(second)]
    assert all(
        fields[index].span.end <= fields[index + 1].span.start for index in range(len(fields) - 1)
    )


def test_t017_f05_yfm_container_body_exposes_supported_markdown_fields() -> None:
    source = (
        b'{% note info "Note title" %}\n'
        b"Body paragraph\n\n"
        b'{% cut "Cut title" %}\n'
        b"- List body\n"
        b"{% endcut %}\n"
        b"{% tabs %}\n"
        b'{% tab "Tab title" %}\n'
        b"| Head |\n"
        b"| --- |\n"
        b"| Cell |\n\n"
        b"```python\n"
        b"# Comment body\n"
        b"```\n"
        b"{% endtab %}\n"
        b"{% endtabs %}\n"
        b"{% endnote %}\n"
    )

    plan = build(source)
    fields = fields_of(plan)
    values = [source[field.span.start : field.span.end] for field in fields]

    assert values == [
        b"Note title",
        b"Body paragraph",
        b"Cut title",
        b"List body",
        b"Tab title",
        b"Head",
        b"Cell",
        b"Comment body",
    ]
    assert [field.kind for field in fields] == [
        FieldKind.HEADING,
        FieldKind.PARAGRAPH,
        FieldKind.HEADING,
        FieldKind.LIST_ITEM,
        FieldKind.HEADING,
        FieldKind.TABLE_CELL,
        FieldKind.TABLE_CELL,
        FieldKind.PARAGRAPH,
    ]
    assert all(b"{%" not in value and b"%}" not in value for value in values)
    assert render_identity(source, plan) == source


def test_t017_r04_yfm_container_preserves_enclosing_unsupported_fence() -> None:
    source = (
        b'{% note info "Notice" %}\n'
        b"```text\n"
        b'{% cut "Literal sample" %}\n'
        b"SELECT 1;\n"
        b"{% endcut %}\n"
        b"```\n"
        b"{% endnote %}\n"
    )

    plan = build(source)

    assert [source[field.span.start : field.span.end] for field in fields_of(plan)] == [b"Notice"]
    assert [block.kind for block in plan.blocks] == [BlockKind.T008_YFM]
    assert render_identity(source, plan) == source
