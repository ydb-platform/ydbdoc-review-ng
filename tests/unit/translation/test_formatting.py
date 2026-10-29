"""Build-compatible heading boundaries without rewriting document contents."""

import pytest

from ydbdoc_review_ng.translation.formatting import normalize_heading_spacing


@pytest.mark.parametrize('newline', [b'\n', b'\r\n', b'\r'])
def test_heading_boundaries_preserve_case_anchors_and_line_endings(newline):
    draft = newline.join([b'intro', b'## lower case {#anchor}', b'### next', b'body'])
    expected = newline.join([
        b'intro', b'', b'## lower case {#anchor}', b'', b'### next', b'', b'body'
    ])
    assert normalize_heading_spacing(draft) == expected
    assert normalize_heading_spacing(expected) == expected


@pytest.mark.parametrize('draft', [
    b'```bash\n# comment\necho x\n```\n',
    b'~~~text\n# example\nbody\n~~~\n',
    b'````markdown\n```\n# example\nbody\n```\n````\n',
    b'    # indented code\n    body\n',
    b'- item\n\n  ```bash\n  # comment\n  echo x\n  ```\n',
    b'> ```bash\n> # comment\n> echo x\n> ```\n',
    b'<pre>\n# example\nbody\n</pre>\n',
    b'---\ntitle: Example\ndescription: |\n  # literal\n  body\n---\n',
    b'---\n# malformed front matter\nbody\n',
    b'Paragraph with a hard break.  \nNext line with `log`_`config`.\n',
    b'## heading without final newline',
    b'# title\n\nBody\n\n## next\n',
])
def test_code_metadata_prose_and_existing_spacing_are_unchanged(draft):
    assert normalize_heading_spacing(draft) == draft


def test_frontmatter_is_preserved_while_following_heading_is_separated():
    metadata = b'---\ntitle: Example\ndescription: Keep separate\n---\n'
    assert normalize_heading_spacing(metadata + b'# title\nbody\n') == (
        metadata + b'\n# title\n\nbody\n'
    )


def test_unicode_line_separator_is_not_a_markdown_line_boundary():
    draft = "first\u2028second\n## heading\nbody\n".encode()
    assert normalize_heading_spacing(draft) == "first\u2028second\n\n## heading\n\nbody\n".encode()


def test_indented_yaml_scalar_delimiter_does_not_end_frontmatter():
    draft = b'---\ndescription: |\n  ---\n# metadata comment\ntitle: Example\n---\n'
    assert normalize_heading_spacing(draft) == draft
