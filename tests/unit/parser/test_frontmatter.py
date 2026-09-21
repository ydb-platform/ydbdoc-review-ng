from __future__ import annotations

from itertools import pairwise

import pytest

from ydbdoc_review_ng.domain import GitSha, RepoPath, RepositoryId, SnapshotRef
from ydbdoc_review_ng.parser.markdown import build_markdown_plan
from ydbdoc_review_ng.plan import (
    BlockKind,
    FieldKind,
    SourcePlan,
    fields_of,
    render_identity,
    validate_source_plan,
)

SNAPSHOT = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a" * 40))
PATH = RepoPath("ydb/docs/en/frontmatter.md")


def build(source: bytes) -> SourcePlan:
    return build_markdown_plan(SNAPSHOT, PATH, source)


def field_values(source: bytes) -> list[bytes]:
    return [source[field.span.start : field.span.end] for field in fields_of(build(source))]


def test_frontmatter_title_and_description_are_the_only_translatable_values() -> None:
    source = (
        "---\r\n"
        'title: "Заголовок"\r\n'
        "description: Описание страницы\r\n"
        "layout: documentation\r\n"
        "tags: [one, two]\r\n"
        "---\r\n"
        "Body\r\n"
    ).encode()

    plan = build(source)

    frontmatter = plan.blocks[0]
    assert frontmatter.kind is BlockKind.T008_FRONT_MATTER
    assert [field.kind for field in frontmatter.fields] == [
        FieldKind.HEADING,
        FieldKind.PARAGRAPH,
    ]
    assert [source[field.span.start : field.span.end] for field in frontmatter.fields] == [
        "Заголовок".encode(),
        "Описание страницы".encode(),
    ]
    assert b'"' not in source[frontmatter.fields[0].span.start : frontmatter.fields[0].span.end]
    assert b"layout" not in b"".join(
        source[field.span.start : field.span.end] for field in frontmatter.fields
    )
    validate_source_plan(source, plan)
    assert render_identity(source, plan) == source


def test_frontmatter_quotes_comments_empty_values_and_nested_keys_stay_protected() -> None:
    source = (
        b"---\n"
        b"title: 'Quoted title' # syntax outside the scalar\n"
        b'description: "Quoted description"\n'
        b"empty: \n"
        b"metadata:\n"
        b"  title: Nested title\n"
        b"---\n"
    )

    assert field_values(source) == [b"Quoted title", b"Quoted description"]


def test_frontmatter_without_final_newline_keeps_stable_non_overlapping_fields() -> None:
    source = b"---\ntitle: Title\ndescription: Description\n---"

    first = build(source)
    second = build(source)
    fields = fields_of(first)

    assert [source[field.span.start : field.span.end] for field in fields] == [
        b"Title",
        b"Description",
    ]
    assert first == second
    assert [field.field_id for field in fields] == [field.field_id for field in fields_of(second)]
    assert all(left.span.end <= right.span.start for left, right in pairwise(fields))
    assert render_identity(source, first) == source


@pytest.mark.parametrize("header", [b"|  ", b">  ", b"|2  "])
def test_t017_b1_valid_block_headers_with_trailing_spaces_expose_content(
    header: bytes,
) -> None:
    source = b"---\n" + b"description: " + header + b"\n  First line\n    Second line\n---\n"

    assert field_values(source) == [b"First line\n    Second line"]
