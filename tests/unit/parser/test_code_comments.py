from __future__ import annotations

from itertools import pairwise

import pytest

from ydbdoc_review_ng.domain import GitSha, RepoPath, RepositoryId, SnapshotRef
from ydbdoc_review_ng.parser.markdown import build_markdown_plan
from ydbdoc_review_ng.plan import BlockKind, FieldKind, SourcePlan, fields_of, render_identity

SNAPSHOT = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("c" * 40))
PATH = RepoPath("ydb/docs/en/code.md")


def build(source: bytes) -> SourcePlan:
    return build_markdown_plan(SNAPSHOT, PATH, source)


def values(source: bytes) -> list[bytes]:
    return [source[field.span.start : field.span.end] for field in fields_of(build(source))]


@pytest.mark.parametrize("language", [b"cpp", b"c++", b"cc", b"cxx", b"java", b"javascript", b"js"])
def test_slash_comment_language_aliases_extract_real_comment_only(language: bytes) -> None:
    source = (
        b"```" + language + b'\nconst char *url = "https://host/path"; // Translate this\n```\n'
    )

    assert values(source) == [b"Translate this"]


@pytest.mark.parametrize("language", [b"python", b"py", b"bash", b"sh", b"shell", b"yaml", b"yml"])
def test_hash_comment_language_aliases_extract_real_comment_only(language: bytes) -> None:
    source = b"```" + language + b'\nvalue = "# not a comment" # Translate this\n```\n'

    assert values(source) == [b"Translate this"]


def test_html_comment_is_extracted_but_quoted_marker_is_not() -> None:
    source = (
        b'```html\n<div data-text="<!-- not a comment -->"><!-- Translate this --></div>\n```\n'
    )

    assert values(source) == [b"Translate this"]


def test_javascript_backtick_and_escaped_quotes_hide_comment_markers() -> None:
    source = (
        b'```js\nconst a = "escaped \\" // hidden";\nconst b = `/* hidden */`; // Visible\n```\n'
    )

    assert values(source) == [b"Visible"]


def test_slash_block_comments_can_span_crlf_lines_and_keep_utf8_text() -> None:
    source = "```cpp\r\nint x; /* Первая\r\nВторая */ int y;\r\n```".encode()
    plan = build(source)
    fields = fields_of(plan)

    assert len(fields) == 1
    assert fields[0].kind is FieldKind.PARAGRAPH
    assert source[fields[0].span.start : fields[0].span.end] == "Первая\r\nВторая".encode()
    assert fields[0].lines.start == 2
    assert fields[0].lines.end == 3
    assert render_identity(source, plan) == source


def test_html_comments_can_span_lines_without_a_final_newline() -> None:
    source = b"```html\n<!-- First\nSecond -->\n```"

    assert values(source) == [b"First\nSecond"]
    assert render_identity(source, build(source)) == source


def test_unsupported_fence_is_wholly_protected() -> None:
    source = b'```go\nvalue := "// hidden" // also protected\n```\n'
    plan = build(source)

    assert len(plan.blocks) == 1
    assert plan.blocks[0].kind is BlockKind.T008_FENCE
    assert plan.blocks[0].fields == ()
    assert fields_of(plan) == ()
    assert render_identity(source, plan) == source


def test_comment_fields_are_ordered_non_overlapping_and_stable() -> None:
    source = b"~~~cxx\n// One\nint x; /* Two */ // Three\n~~~\n"
    first = build(source)
    second = build(source)
    fields = fields_of(first)

    assert [source[field.span.start : field.span.end] for field in fields] == [
        b"One",
        b"Two",
        b"Three",
    ]
    assert [field.field_id for field in fields] == [field.field_id for field in fields_of(second)]
    assert all(left.span.end <= right.span.start for left, right in pairwise(fields))
    assert render_identity(source, first) == source


@pytest.mark.parametrize(
    "source",
    [
        b'```python\ntext = """first\n# string content\nlast"""\n# Translate real\n```\n',
        b'```bash\nvalue="first\n# string content\nlast"\n# Translate real\n```\n',
    ],
    ids=["python-triple-quoted", "bash-multiline-quoted"],
)
def test_t017_f07_hash_markers_inside_multiline_strings_are_not_comments(
    source: bytes,
) -> None:
    assert values(source) == [b"Translate real"]


def test_t017_b2_python_triple_single_escaped_terminator_stays_in_string() -> None:
    source = (
        b"```python\ntext = '''first\n\\''' still string # hidden\nlast'''\n# Translate real\n```\n"
    )

    assert values(source) == [b"Translate real"]


@pytest.mark.parametrize(
    "source",
    [
        (b'```yaml\nvalue: "first\n  # not a comment\n  last"\n# Real comment\n```\n'),
        (b'```java\nString text = """\n// not a comment\n""";\n// Real comment\n```\n'),
        (
            b'```html\n<div data-text="first\n<!-- not a comment -->\nlast">'
            b"<!-- Real comment --></div>\n```\n"
        ),
    ],
    ids=["yaml-quoted-scalar", "java-text-block", "html-quoted-attribute"],
)
def test_t017_r06_multiline_strings_hide_markers_but_not_later_comments(
    source: bytes,
) -> None:
    assert values(source) == [b"Real comment"]


@pytest.mark.parametrize(
    "source",
    [
        (b'```cpp\nconst char* text = R"(first\n// not a comment\nlast)";\n// Real comment\n```\n'),
        (b"```yaml\nvalue: |\n  # not a comment\n  Last line\n# Real comment\n```\n"),
    ],
    ids=["cpp-raw-string", "yaml-literal-scalar"],
)
def test_t017_n02_literal_forms_hide_markers_but_not_following_comments(
    source: bytes,
) -> None:
    assert values(source) == [b"Real comment"]
