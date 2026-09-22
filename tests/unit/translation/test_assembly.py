from __future__ import annotations

import json

import pytest

from ydbdoc_review_ng.domain import GitSha, RepoPath, RepositoryId, SnapshotRef
from ydbdoc_review_ng.parser.markdown import build_markdown_plan
from ydbdoc_review_ng.plan import ProtectedKind, SourcePlan
from ydbdoc_review_ng.translation import (
    AssemblyError,
    AssemblyErrorReason,
    ProtectedMismatch,
    TranslationRequest,
    assemble_candidate,
    build_translation_request,
    parse_translation_response,
    verify_protected_fragments,
)

SNAPSHOT = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a" * 40))
PATH = RepoPath("ydb/docs/en/test.md")


def prepared(source: bytes) -> tuple[SourcePlan, TranslationRequest]:
    plan = build_markdown_plan(SNAPSHOT, PATH, source)
    request = build_translation_request(source, plan)
    return plan, request


@pytest.mark.parametrize(
    "source",
    [
        b'---\ntitle: "Visit https://safe.example /docs/guide.md `SELECT 1` {#keep} {{ name }} Type_Name"\n---\n',
        b'{% cut "Visit https://safe.example /docs/guide.md `SELECT 1` {#keep} {{ name }} Type_Name" %}\nBody\n{% endcut %}\n',
        b"```python\n# Visit https://safe.example /docs/guide.md `SELECT 1` {#keep} {{ name }} Type_Name\n```\n",
    ],
    ids=["frontmatter", "yfm-title", "fenced-comment"],
)
def test_t017_f03_structured_and_comment_fields_protect_inline_technical_fragments(
    source: bytes,
) -> None:
    plan, request = prepared(source)
    assert len(request.fields) >= 1
    field = request.fields[0]
    assert {item.kind for item in field.placeholders} >= {
        ProtectedKind.URL,
        ProtectedKind.PATH,
        ProtectedKind.INLINE_CODE,
        ProtectedKind.EXPLICIT_ANCHOR,
        ProtectedKind.TEMPLATE,
        ProtectedKind.IDENTIFIER,
    }
    assert "https://safe.example" not in field.text

    attacked = field.text.replace(field.placeholders[0].token, "https://evil.example")
    translations = {item.field_id: item.text for item in request.fields}
    translations[field.field_id] = attacked
    with pytest.raises(AssemblyError) as caught:
        assemble_candidate(source, plan, request, translations)
    assert caught.value.reason is AssemblyErrorReason.PLACEHOLDER_MISMATCH


@pytest.mark.parametrize(
    "source",
    [
        b"Connect to grpc://safe.example:2135 using guide.md\n",
        b'---\ntitle: "Connect to grpc://safe.example:2135 using guide.md"\n---\n',
        b'{% cut "Connect to grpc://safe.example:2135 using guide.md" %}\nBody\n{% endcut %}\n',
        b"```python\n# Connect to grpc://safe.example:2135 using guide.md\n```\n",
    ],
    ids=["prose", "frontmatter", "yfm-title", "fenced-comment"],
)
def test_t017_r03_endpoint_and_filename_are_source_owned_in_every_field_surface(
    source: bytes,
) -> None:
    plan, request = prepared(source)
    field = request.fields[0]
    endpoint = next(
        item for item in field.placeholders if item.source_bytes == b"grpc://safe.example:2135"
    )
    filename = next(item for item in field.placeholders if item.source_bytes == b"guide.md")
    assert (endpoint.kind, filename.kind) == (ProtectedKind.URL, ProtectedKind.PATH)

    attacked = field.text.replace(endpoint.token, "grpc://evil.example:2135").replace(
        filename.token, "other.md"
    )
    translations = {item.field_id: item.text for item in request.fields}
    translations[field.field_id] = attacked
    with pytest.raises(AssemblyError) as caught:
        assemble_candidate(source, plan, request, translations)
    assert caught.value.reason is AssemblyErrorReason.PLACEHOLDER_MISMATCH

    target = source.replace(b"grpc://safe.example:2135", b"grpc://evil.example:2135").replace(
        b"guide.md", b"other.md"
    )
    with pytest.raises(ProtectedMismatch):
        verify_protected_fragments(
            source,
            plan,
            target,
            build_markdown_plan(SNAPSHOT, PATH, target),
        )


def test_t017_n01_sentence_final_filename_is_protected_without_period() -> None:
    source = b"Open guide.md.\n"
    plan, request = prepared(source)
    field = request.fields[0]

    path = next(item for item in field.placeholders if item.kind is ProtectedKind.PATH)
    assert path.source_bytes == b"guide.md"
    assert field.text.endswith(path.token + ".")

    with pytest.raises(AssemblyError) as caught:
        assemble_candidate(source, plan, request, {field.field_id: "Open other.md."})
    assert caught.value.reason is AssemblyErrorReason.PLACEHOLDER_MISMATCH

    target = b"Open other.md.\n"
    with pytest.raises(ProtectedMismatch):
        verify_protected_fragments(
            source,
            plan,
            target,
            build_markdown_plan(SNAPSHOT, PATH, target),
        )


def test_t017_f06_frontmatter_quotes_and_block_scalar_content_assemble_as_valid_yaml() -> None:
    import yaml

    source = (
        b'---\ntitle: "Original title"\ndescription: |\n  First line\n  Second line\n---\nBody\n'
    )
    plan, request = prepared(source)
    translations = {field.field_id: field.text for field in request.fields}
    block_field = next(field for field in request.fields if "First line" in field.text)
    title_field = next(field for field in request.fields if "Original title" in field.text)
    assert block_field.text != "|"
    translations[title_field.field_id] = 'A "quoted" title'
    translations[block_field.field_id] = block_field.text.replace(
        "First line", "Translated first"
    ).replace("Second line", "Translated second")

    candidate = assemble_candidate(source, plan, request, translations)
    frontmatter = candidate.split(b"---\n", 2)[1]
    decoded = yaml.safe_load(frontmatter)

    assert decoded == {
        "title": 'A "quoted" title',
        "description": "Translated first\nTranslated second\n",
    }
    assert b"description: |\n" in candidate


def test_t017_f06_plain_frontmatter_is_quoted_when_translation_needs_yaml_encoding() -> None:
    import yaml

    source = b"---\ntitle: Original title\n---\n"
    plan, request = prepared(source)
    field = request.fields[0]

    candidate = assemble_candidate(
        source,
        plan,
        request,
        {field.field_id: "Русский: Original title"},
    )

    assert b'title: "' in candidate
    assert yaml.safe_load(candidate.split(b"---\n", 2)[1]) == {"title": "Русский: Original title"}


def test_t017_r05_multiline_quoted_frontmatter_uses_logical_value() -> None:
    import yaml

    source = b'---\ntitle: "First line\n  Second line"\n---\n'
    plan, request = prepared(source)

    assert [field.text for field in request.fields] == ["First line Second line"]
    candidate = assemble_candidate(
        source,
        plan,
        request,
        {request.fields[0].field_id: request.fields[0].text},
    )
    assert yaml.safe_load(candidate.split(b"---\n", 2)[1]) == {"title": "First line Second line"}


def test_t017_r05_escaped_quoted_frontmatter_is_encoded_once() -> None:
    import yaml

    source = b'---\ntitle: "An \\"escaped\\" title"\n---\n'
    plan, request = prepared(source)

    assert [field.text for field in request.fields] == ['An "escaped" title']
    candidate = assemble_candidate(
        source,
        plan,
        request,
        {request.fields[0].field_id: request.fields[0].text},
    )
    assert yaml.safe_load(candidate.split(b"---\n", 2)[1]) == {"title": 'An "escaped" title'}


def test_t017_r05_quoted_content_space_is_preserved_for_safe_assembly() -> None:
    import yaml

    source = b'---\ntitle: "Title "\n---\n'
    plan, request = prepared(source)

    assert [field.text for field in request.fields] == ["Title "]
    candidate = assemble_candidate(
        source,
        plan,
        request,
        {request.fields[0].field_id: 'A "quoted" title'},
    )
    assert b'title: "A \\"quoted\\" title"' in candidate
    assert yaml.safe_load(candidate.split(b"---\n", 2)[1]) == {"title": 'A "quoted" title'}


def test_t017_r05_invalid_final_frontmatter_is_typed_assembly_error() -> None:
    source = b"---\ndescription: |\n  Original\n---\n"
    plan, request = prepared(source)

    with pytest.raises(AssemblyError) as caught:
        assemble_candidate(
            source,
            plan,
            request,
            {request.fields[0].field_id: "Broken\nbad: ["},
        )
    assert caught.value.reason is AssemblyErrorReason.CANDIDATE_REVALIDATION_FAILED


def test_t017_n03_multiline_plain_frontmatter_uses_complete_logical_value() -> None:
    import yaml

    source = "---\ntitle: Первая строка\n  Вторая строка\n---\n".encode()
    plan, request = prepared(source)

    assert [field.text for field in request.fields] == ["Первая строка Вторая строка"]
    candidate = assemble_candidate(
        source,
        plan,
        request,
        {request.fields[0].field_id: "First line Second line"},
    )

    assert yaml.safe_load(candidate.split(b"---\n", 2)[1]) == {"title": "First line Second line"}
    assert "Вторая".encode() not in candidate


def test_t017_q01_yaml_sequence_block_scalar_is_source_owned() -> None:
    source = (
        b"```yaml\nsteps:\n  - run: |\n      # not a comment\n"
        b"      Last line\n# Real comment\n```\n"
    )
    plan, request = prepared(source)

    assert [field.text for field in request.fields] == ["Real comment"]
    candidate = assemble_candidate(
        source,
        plan,
        request,
        {request.requested_ids[0]: "Translated real comment"},
    )
    assert b"      # not a comment\n" in candidate
    assert b"# Translated real comment\n" in candidate

    attacked = source.replace(b"not a comment", b"CHANGED STRING")
    with pytest.raises(ProtectedMismatch):
        verify_protected_fragments(
            source,
            plan,
            attacked,
            build_markdown_plan(SNAPSHOT, PATH, attacked),
        )


def test_t017_q01_yaml_quoted_key_block_scalar_is_source_owned() -> None:
    source = b'```yaml\n"value": |\n  # not a comment\n  Last line\n# Real comment\n```\n'
    plan, request = prepared(source)

    assert [field.text for field in request.fields] == ["Real comment"]
    candidate = assemble_candidate(
        source,
        plan,
        request,
        {request.requested_ids[0]: "Translated real comment"},
    )
    assert b"  # not a comment\n" in candidate
    assert b"# Translated real comment\n" in candidate

    attacked = source.replace(b"not a comment", b"CHANGED STRING")
    with pytest.raises(ProtectedMismatch):
        verify_protected_fragments(
            source,
            plan,
            attacked,
            build_markdown_plan(SNAPSHOT, PATH, attacked),
        )


def test_assembly_restores_protected_source_bytes_and_is_deterministic() -> None:
    source = b"# Read [guide](/docs/path) with `SELECT 1`, {{ name }}, and <b>x</b>\n"
    plan, request = prepared(source)
    field = request.fields[0]
    translated = "Lire " + " ".join(item.token for item in field.placeholders)
    values = parse_translation_response(json.dumps({field.field_id: translated}), request)
    first = assemble_candidate(source, plan, request, values)
    second = assemble_candidate(source, plan, request, values)
    assert first == second
    assert b"/docs/path" in first
    assert b"`SELECT 1`" in first
    assert b"{{ name }}" in first
    assert b"<b>" in first and b"</b>" in first


@pytest.mark.parametrize("mutation", ["missing", "unknown", "repeated"])
def test_assembly_rejects_bad_placeholder_sets(mutation: str) -> None:
    source = b"Read `code` now\n"
    plan, request = prepared(source)
    token = request.fields[0].placeholders[0].token
    value = {
        "missing": "Lire",
        "unknown": "Lire [[INLINE_CODE_9999]]",
        "repeated": token + token,
    }[mutation]
    with pytest.raises(AssemblyError) as caught:
        assemble_candidate(source, plan, request, {request.requested_ids[0]: value})
    assert caught.value.reason is AssemblyErrorReason.PLACEHOLDER_MISMATCH


def test_assembly_rejects_broken_container_nesting_but_allows_sibling_pair_move() -> None:
    source = b"[one](/a) and [two](/b)\n"
    plan, request = prepared(source)
    placeholders = request.fields[0].placeholders
    by_group: dict[int, list[str]] = {}
    singles: list[str] = []
    for item in placeholders:
        if item.group is None:
            singles.append(item.token)
        else:
            by_group.setdefault(item.group, []).append(item.token)
    groups = list(by_group.values())
    valid = "Deux " + " ".join(groups[1] + singles[1:] + groups[0] + singles[:1])
    assemble_candidate(source, plan, request, {request.requested_ids[0]: valid})
    broken = " ".join([groups[0][0], groups[1][0], groups[0][1], groups[1][1], *singles])
    with pytest.raises(AssemblyError) as caught:
        assemble_candidate(source, plan, request, {request.requested_ids[0]: broken})
    assert caught.value.reason is AssemblyErrorReason.BROKEN_CONTAINER_NESTING


def test_candidate_reparse_failure_is_typed() -> None:
    source = b"Plain text\n"
    plan, request = prepared(source)
    with pytest.raises(AssemblyError) as caught:
        assemble_candidate(source, plan, request, {request.requested_ids[0]: "bad\x00text"})
    assert caught.value.reason is AssemblyErrorReason.CANDIDATE_REVALIDATION_FAILED


def test_live_slash_joined_prose_translation_assembles() -> None:
    source = "Принудительная блокировка/разблокировка пользователя\n".encode()
    plan, request = prepared(source)

    assert (
        assemble_candidate(
            source,
            plan,
            request,
            {request.requested_ids[0]: "Forced user blocking/unblocking"},
        )
        == b"Forced user blocking/unblocking\n"
    )


def test_live_slash_joined_prose_translation_verifies() -> None:
    source = "Принудительная блокировка/разблокировка пользователя\n".encode()
    target = b"Forced user blocking/unblocking\n"

    verify_protected_fragments(
        source,
        build_markdown_plan(SNAPSHOT, PATH, source),
        target,
        build_markdown_plan(SNAPSHOT, PATH, target),
    )


def test_sentence_final_extension_path_rejects_model_mutation() -> None:
    source = b"Open docs/a.md.\n"
    plan, request = prepared(source)

    with pytest.raises(AssemblyError) as caught:
        assemble_candidate(
            source,
            plan,
            request,
            {request.requested_ids[0]: "Open docs/evil.md."},
        )
    assert caught.value.reason is AssemblyErrorReason.PLACEHOLDER_MISMATCH


def test_sentence_final_extension_path_rejects_manual_mutation() -> None:
    source = b"Open docs/a.md.\n"
    target = b"Open docs/evil.md.\n"

    with pytest.raises(ProtectedMismatch):
        verify_protected_fragments(
            source,
            build_markdown_plan(SNAPSHOT, PATH, source),
            target,
            build_markdown_plan(SNAPSHOT, PATH, target),
        )


def test_assembly_preserves_fenced_code_and_comment_syntax_around_translated_comments() -> None:
    source = b"```cpp\nint x; // explain x\n/* explain y */\n```\n"
    plan, request = prepared(source)
    translations = {
        request.fields[0].field_id: "expliquer x",
        request.fields[1].field_id: "expliquer y",
    }
    assert assemble_candidate(source, plan, request, translations) == (
        b"```cpp\nint x; // expliquer x\n/* expliquer y */\n```\n"
    )


@pytest.mark.parametrize(
    "source,target",
    [
        (b"See [x](/good)\n", b"Voir [x](/bad)\n"),
        (b"Use `SELECT 1`\n", b"Utilisez `DROP TABLE x`\n"),
        (b"Path /a/b/config.yaml\n", b"Chemin /x/y/config.yaml\n"),
    ],
)
def test_verify_rejects_manual_protected_changes(source: bytes, target: bytes) -> None:
    source_plan = build_markdown_plan(SNAPSHOT, PATH, source)
    target_plan = build_markdown_plan(SNAPSHOT, PATH, target)
    with pytest.raises(ProtectedMismatch):
        verify_protected_fragments(source, source_plan, target, target_plan)


def test_verify_accepts_translated_prose_with_unchanged_protected_fragments() -> None:
    source = b"See [guide](/good) and `SELECT 1`\n"
    target = b"Voir [guide](/good) et `SELECT 1`\n"
    verify_protected_fragments(
        source,
        build_markdown_plan(SNAPSHOT, PATH, source),
        target,
        build_markdown_plan(SNAPSHOT, PATH, target),
    )


def test_linked_image_restores_both_source_urls_and_preserves_translated_alt_text() -> None:
    source = b"[![diagram](/good.png)](/outer)\n"
    plan, request = prepared(source)
    field = request.fields[0]

    assert "/good.png" not in field.text
    assert "/outer" not in field.text
    assert [item.source_bytes for item in field.placeholders] == [
        b"[",
        b"![",
        b"](/good.png)",
        b"](/outer)",
    ]

    translated = field.text.replace("diagram", "diagramme")
    assert assemble_candidate(source, plan, request, {field.field_id: translated}) == (
        b"[![diagramme](/good.png)](/outer)\n"
    )


@pytest.mark.parametrize(
    ("source_url", "changed_url"),
    [(b"/good.png", "/evil.png"), (b"/outer", "/elsewhere")],
)
def test_linked_image_rejects_model_and_manual_changes_to_either_url(
    source_url: bytes, changed_url: str
) -> None:
    source = b"[![diagram](/good.png)](/outer)\n"
    plan, request = prepared(source)
    field = request.fields[0]
    placeholder = next(item for item in field.placeholders if source_url in item.source_bytes)
    attacked_value = field.text.replace(
        placeholder.token, placeholder.source_bytes.decode()
    ).replace(source_url.decode(), changed_url)

    with pytest.raises(AssemblyError) as caught:
        assemble_candidate(source, plan, request, {field.field_id: attacked_value})
    assert caught.value.reason is AssemblyErrorReason.PLACEHOLDER_MISMATCH

    target = source.replace(source_url, changed_url.encode())
    with pytest.raises(ProtectedMismatch):
        verify_protected_fragments(
            source,
            plan,
            target,
            build_markdown_plan(SNAPSHOT, PATH, target),
        )
