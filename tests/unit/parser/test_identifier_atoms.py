"""P0: identifier atoms must not split on bare ESCAPE underscores."""

from __future__ import annotations

from ydbdoc_review_ng.domain import GitSha, RepoPath, RepositoryId, SnapshotRef
from ydbdoc_review_ng.parser.markdown import build_markdown_plan
from ydbdoc_review_ng.plan import ProtectedKind, fields_of
from ydbdoc_review_ng.translation.document import prepare_document

SNAPSHOT = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a" * 40))
PATH = RepoPath("ydb/docs/ru/test.md")


def _plan(source: bytes):
    return build_markdown_plan(SNAPSHOT, PATH, source)


def _regions(source: bytes) -> list[tuple[ProtectedKind, bytes]]:
    plan = _plan(source)
    return [
        (region.kind, source[region.span.start : region.span.end])
        for field in fields_of(plan)
        for region in field.protected_regions
        if region.kind is not ProtectedKind.MARKDOWN_SYNTAX
    ]


def test_escaped_bs_controller_is_one_identifier_atom() -> None:
    source = b"See BS\\_CONTROLLER status.\n"
    assert _regions(source) == [(ProtectedKind.IDENTIFIER, b"BS\\_CONTROLLER")]


def test_escaped_create_failed_and_pool_name_are_atoms() -> None:
    source = b"Codes CREATE\\_FAILED and POOL\\_NAME.\n"
    assert _regions(source) == [
        (ProtectedKind.IDENTIFIER, b"CREATE\\_FAILED"),
        (ProtectedKind.IDENTIFIER, b"POOL\\_NAME"),
    ]


def test_page_controller_escape_does_not_split_into_bare_controller() -> None:
    """Regression for page\\_CONTROLLER / class-style mangling after ESCAPE split."""
    source = b"Klass page\\_CONTROLLER monitoring.\n"
    regions = _regions(source)
    assert regions == [(ProtectedKind.IDENTIFIER, b"page\\_CONTROLLER")]
    prepared = prepare_document(source, _plan(source))
    assert "CONTROLLER" not in prepared.chunks[0].text
    assert "page" not in prepared.chunks[0].text
    assert all(
        item.kind is ProtectedKind.IDENTIFIER and b"CONTROLLER" in item.source_bytes
        for item in prepared.placeholders
    )


def test_unescaped_identifiers_still_atomic() -> None:
    source = b"See BS_CONTROLLER and CREATE_FAILED.\n"
    assert _regions(source) == [
        (ProtectedKind.IDENTIFIER, b"BS_CONTROLLER"),
        (ProtectedKind.IDENTIFIER, b"CREATE_FAILED"),
    ]


def test_inline_code_identifier_stays_inline_code() -> None:
    source = b"Use `POOL_NAME` now.\n"
    assert _regions(source) == [(ProtectedKind.INLINE_CODE, b"`POOL_NAME`")]


def test_lone_escape_underscore_outside_identifier_remains_escape() -> None:
    source = b"Space \\_ separated.\n"
    assert _regions(source) == [(ProtectedKind.ESCAPE, b"\\_")]


def test_prepare_restores_canonical_unescaped_identifier() -> None:
    source = b"See BS\\_CONTROLLER now.\n"
    plan = _plan(source)
    prepared = prepare_document(source, plan)
    assert len(prepared.placeholders) == 1
    assert prepared.placeholders[0].kind is ProtectedKind.IDENTIFIER
    assert prepared.placeholders[0].source_bytes == b"BS_CONTROLLER"
    from ydbdoc_review_ng.translation.document import restore_document

    restored = restore_document(source, plan, prepared, (prepared.chunks[0].text,))
    assert b"BS_CONTROLLER" in restored
    assert b"\\_" not in restored
