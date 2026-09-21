from __future__ import annotations

import json

import pytest

from ydbdoc_review_ng.domain import RepoPath


def codec():
    from ydbdoc_review_ng.continuation import decode_scope_target_paths, encode_scope_target_paths

    return encode_scope_target_paths, decode_scope_target_paths


def test_scope_selection_codec_preserves_canonical_paths_only():
    encode, decode = codec()
    paths = (RepoPath("en/a.md"), RepoPath("en/b.md"))
    assert encode(paths) == '["en/a.md","en/b.md"]'
    assert decode(b'["en/a.md","en/b.md"]') == paths
    assert encode(()) == "[]"
    assert decode("[]") == ()


@pytest.mark.parametrize(
    "raw",
    [
        "{}",
        "null",
        '["en/a.md", "en/a.md"]',
        '["en/b.md", "en/a.md"]',
        '["../outside.md"]',
        '["/absolute.md"]',
        "[1]",
        "[false]",
        '[{"path":"en/a.md","text":"secret"}]',
    ],
)
def test_scope_selection_decode_rejects_wrong_shape_duplicates_order_and_paths(raw):
    _, decode = codec()
    with pytest.raises(ValueError):
        decode(raw)


@pytest.mark.parametrize(
    "paths",
    [
        [RepoPath("en/a.md")],
        ("en/a.md",),
        (RepoPath("en/a.md"), RepoPath("en/a.md")),
        (RepoPath("en/b.md"), RepoPath("en/a.md")),
    ],
)
def test_scope_selection_encode_rejects_noncanonical_runtime_values(paths):
    encode, _ = codec()
    with pytest.raises(ValueError):
        encode(paths)


def test_scope_selection_bounds_are_enforced_on_encode_and_decode():
    encode, decode = codec()
    paths = tuple(RepoPath(f"en/{index:05d}.md") for index in range(10_100))
    assert decode(encode(paths)) == paths
    assert decode(encode((RepoPath("x" * 4096),))) == (RepoPath("x" * 4096),)
    for invalid in (paths + (RepoPath("en/10100.md"),), (RepoPath("x" * 4097),)):
        with pytest.raises(ValueError):
            encode(invalid)
        with pytest.raises(ValueError):
            decode(json.dumps([path.value for path in invalid]))
    with pytest.raises(ValueError):
        decode(" " * 4_000_000 + "[]")


def test_scope_selection_binary_value_is_bound_as_ydb_string():
    from ydbdoc_review_ng.runtime_ydb import parameter_types

    assert parameter_types({"scope_target_paths": b"[]"}) == {"scope_target_paths": "String"}
