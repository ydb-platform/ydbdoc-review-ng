from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

import pytest

from ydbdoc_review_ng import quality
from ydbdoc_review_ng.domain import ModelRole
from ydbdoc_review_ng.models import ModelRequest
from ydbdoc_review_ng.models.types import mutable_json

SOURCE_FILES = {
    "docs/ru/article.md": "# Статья\n\nПолный исходный текст.\n".encode(),
    "docs/ru/toc.yaml": "items:\n  - name: Статья\n    href: article.md\n".encode(),
}
FINAL_FILES = {
    "docs/en/article.md": b"# Article\n\nThe complete corrected text.\n",
    "docs/en/toc.yaml": b"items:\n  - name: Article\n    href: article.md\n",
}
GLOSSARY_FILES = {
    "docs/ru/glossary.md": ("Термин и определение.\n" * 600).encode(),
    "docs/en/glossary.md": ("Term and definition.\n" * 600).encode(),
}
FINDING = {
    "reason": "Название расходится со статьёй.",
    "expected_correction": "Используйте одно название в статье и TOC.",
    "searchable_snippet": "Article",
    "target_path": "docs/en/toc.yaml",
    "target_line": 2,
}


def request(*, operator_context: str | None = None) -> ModelRequest:
    return quality.build_pr_arbiter_request(
        model="independent-arbiter",
        source_files=SOURCE_FILES,
        translated_files=FINAL_FILES,
        glossary_files=GLOSSARY_FILES,
        operator_context=operator_context,
    )


def test_arbiter_renders_exact_canonical_prompt() -> None:
    packaged = (
        resources.files("ydbdoc_review_ng.quality")
        .joinpath("prompts/arbiter.txt")
        .read_text(encoding="utf-8")
    )
    built = request()
    assert built.developer_prompt == packaged
    assert "<source-pr-files>" not in built.developer_prompt
    assert built.prompt.endswith(
        "Before answering, check completeness, terminology, technical literals and "
        "inline-code, damaged sentences, TOC correctness, and every supplied file."
    )


def test_arbiter_loads_packaged_prompt_for_each_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prompt = tmp_path / "prompts" / "arbiter.txt"
    prompt.parent.mkdir()
    prompt.write_text("First instructions", encoding="utf-8")
    monkeypatch.setattr(resources, "files", lambda package: tmp_path)
    assert request().developer_prompt == "First instructions"
    prompt.write_text("Edited instructions", encoding="utf-8")
    assert request().developer_prompt == "Edited instructions"


def test_arbiter_receives_complete_final_pr_and_glossary() -> None:
    built = request(operator_context="Проверьте согласованность названия.")
    for tag, files in (
        ("source-pr-files", SOURCE_FILES),
        ("translation-pr-files", FINAL_FILES),
        ("project-glossary", GLOSSARY_FILES),
    ):
        content = built.prompt.split(f"<{tag}>\n", 1)[1].split(f"\n</{tag}>", 1)[0]
        assert json.loads(content) == {path: text.decode() for path, text in files.items()}
    assert built.role is ModelRole.ARBITER
    assert built.model == "independent-arbiter"
    assert (
        "<operator-context>\nПроверьте согласованность названия.</operator-context>"
        in built.prompt
    )


def test_arbiter_renders_missing_target_and_preserves_literal_template_tokens() -> None:
    built = quality.build_pr_arbiter_request(
        model="independent-arbiter",
        source_files={"source.md": b"Literal {{ PROJECT_GLOSSARY }}"},
        translated_files={"target.md": None},
        glossary_files=GLOSSARY_FILES,
    )
    source = built.prompt.split("<source-pr-files>\n", 1)[1].split("\n</source-pr-files>")[0]
    targets = built.prompt.split("<translation-pr-files>\n", 1)[1].split(
        "\n</translation-pr-files>"
    )[0]
    assert json.loads(source) == {"source.md": "Literal {{ PROJECT_GLOSSARY }}"}
    assert json.loads(targets) == {"target.md": None}


def test_arbiter_schema_requests_only_strict_verdict_and_five_finding_fields() -> None:
    schema = mutable_json(request().schema)
    assert set(schema["properties"]) == {"verdict", "findings"}
    assert schema["required"] == ["verdict", "findings"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["verdict"]["enum"] == ["GREEN", "YELLOW", "RED"]
    finding = schema["properties"]["findings"]["items"]
    assert set(finding["properties"]) == set(FINDING)
    assert set(finding["required"]) == set(FINDING)
    assert finding["additionalProperties"] is False
    assert finding["properties"]["target_path"] == {"type": "string", "enum": list(FINAL_FILES)}
    assert finding["properties"]["target_line"] == {"type": ["integer", "null"], "minimum": 1}
    assert finding["properties"]["searchable_snippet"] == {
        "type": ["string", "null"],
        "minLength": 1,
    }


@pytest.mark.parametrize("as_bytes", [False, True])
def test_arbiter_accepts_green_with_no_findings(as_bytes: bool) -> None:
    raw = '{"verdict":"GREEN","findings":[]}'
    result = quality.parse_pr_arbiter_response(
        raw.encode() if as_bytes else raw, target_files=FINAL_FILES
    )
    assert result.verdict is quality.Verdict.GREEN
    assert result.findings == ()
    assert result.corrected_markdown is None


@pytest.mark.parametrize("verdict", ["YELLOW", "RED"])
def test_arbiter_accepts_exact_existing_target_finding(verdict: str) -> None:
    result = quality.parse_pr_arbiter_response(
        json.dumps({"verdict": verdict, "findings": [FINDING]}, ensure_ascii=False).encode(),
        target_files=FINAL_FILES,
    )
    assert result.verdict.value == verdict
    (finding,) = result.findings
    assert finding.target_path == "docs/en/toc.yaml"
    assert finding.target_line == 2
    assert finding.searchable_snippet == "Article"
    assert finding.reason == "Название расходится со статьёй."
    assert finding.expected_correction == "Используйте одно название в статье и TOC."
    assert result.corrected_markdown is None


def test_arbiter_accepts_missing_target_with_null_location() -> None:
    finding = {**FINDING, "target_line": None, "searchable_snippet": None}
    result = quality.parse_pr_arbiter_response(
        json.dumps({"verdict": "RED", "findings": [finding]}),
        target_files={"docs/en/toc.yaml": None},
    )
    assert result.verdict is quality.Verdict.RED
    (accepted,) = result.findings
    assert accepted.target_path == "docs/en/toc.yaml"
    assert accepted.target_line is None
    assert accepted.searchable_snippet is None


@pytest.mark.parametrize("verdict,findings", [("GREEN", [FINDING]), ("YELLOW", []), ("RED", [])])
def test_arbiter_rejects_verdict_inconsistent_with_findings(
    verdict: str, findings: list[dict[str, object]]
) -> None:
    with pytest.raises(quality.CriticResponseError) as exc:
        quality.parse_pr_arbiter_response(
            json.dumps({"verdict": verdict, "findings": findings}), target_files=FINAL_FILES
        )
    assert exc.value.reason is quality.CriticResponseErrorReason.INCONSISTENT_RESULT


@pytest.mark.parametrize(
    "update",
    [
        {"target_path": "docs/en/unknown.md"},
        {"target_path": None},
        {"target_line": True},
        {"target_line": 0},
        {"target_line": -1},
        {"target_line": 2.0},
        {"target_line": "2"},
        {"target_line": 1},
        {"target_line": 10},
        {"target_line": None},
        {"reason": " "},
        {"reason": None},
        {"expected_correction": ""},
        {"expected_correction": None},
        {"searchable_snippet": ""},
        {"searchable_snippet": "article"},
        {"searchable_snippet": " Article "},
        {"searchable_snippet": "Article\n    href"},
        {"searchable_snippet": None},
        {"target_line": None, "searchable_snippet": None},
        {"repairable": True},
        {"field_ids": []},
        {"files": {}},
        {"unexpected": "value"},
    ],
)
def test_arbiter_rejects_invalid_existing_target_findings(update: dict[str, object]) -> None:
    raw = json.dumps({"verdict": "YELLOW", "findings": [{**FINDING, **update}]})
    with pytest.raises(quality.CriticResponseError):
        quality.parse_pr_arbiter_response(raw, target_files=FINAL_FILES)


@pytest.mark.parametrize("field", list(FINDING))
def test_arbiter_rejects_each_missing_finding_field(field: str) -> None:
    finding = {name: value for name, value in FINDING.items() if name != field}
    with pytest.raises(quality.CriticResponseError):
        quality.parse_pr_arbiter_response(
            json.dumps({"verdict": "RED", "findings": [finding]}), target_files=FINAL_FILES
        )


@pytest.mark.parametrize("line,snippet", [(2, "Article"), (None, "Article"), (2, None)])
def test_arbiter_rejects_nonnull_location_for_missing_target(
    line: int | None, snippet: str | None
) -> None:
    finding = {**FINDING, "target_line": line, "searchable_snippet": snippet}
    with pytest.raises(quality.CriticResponseError):
        quality.parse_pr_arbiter_response(
            json.dumps({"verdict": "RED", "findings": [finding]}),
            target_files={"docs/en/toc.yaml": None},
        )


@pytest.mark.parametrize("content", [b"", b"items:\n  - name: Corrected\n"])
def test_arbiter_checks_current_final_bytes_and_does_not_treat_empty_as_missing(
    content: bytes,
) -> None:
    for finding in (FINDING, {**FINDING, "target_line": None, "searchable_snippet": None}):
        with pytest.raises(quality.CriticResponseError):
            quality.parse_pr_arbiter_response(
                json.dumps({"verdict": "RED", "findings": [finding]}),
                target_files={"docs/en/toc.yaml": content},
            )


def test_arbiter_checks_unicode_snippet_on_crlf_line_without_normalization() -> None:
    finding = {**FINDING, "target_line": 2, "searchable_snippet": "Ошибка 🙂"}
    result = quality.parse_pr_arbiter_response(
        json.dumps({"verdict": "RED", "findings": [finding]}),
        target_files={"docs/en/toc.yaml": "Первая строка\r\nОшибка 🙂\r\n".encode()},
    )
    assert result.findings[0].searchable_snippet == "Ошибка 🙂"
    assert result.findings[0].target_line == 2


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        "null",
        "{}",
        '{"verdict":null,"findings":[]}',
        '{"verdict":"BLUE","findings":[]}',
        '{"verdict":"GREEN"}',
        '{"findings":[]}',
        '{"verdict":"GREEN","findings":null}',
        '{"verdict":"RED","findings":[null]}',
        '{"verdict":"GREEN","findings":[],"files":{}}',
        '{"verdict":"GREEN","findings":[],"corrected_markdown":"text"}',
        '{"verdict":"GREEN","findings":[],"repairable":true}',
        '{"files":{}}',
    ],
)
def test_arbiter_rejects_malformed_or_correcting_responses(raw: str) -> None:
    with pytest.raises(quality.CriticResponseError):
        quality.parse_pr_arbiter_response(raw, target_files=FINAL_FILES)


@pytest.mark.parametrize(
    "raw",
    [
        '{"verdict":"GREEN","verdict":"RED","findings":[]}',
        '{"verdict":"GREEN","findings":[],"findings":[]}',
        '{"verdict":"RED","findings":[{"target_line":1,"target_line":2}]}',
        '{"verdict":"RED","findings":[{"unexpected":{"x":1,"x":2}}]}',
    ],
)
def test_arbiter_rejects_duplicate_keys_at_every_depth(raw: str) -> None:
    with pytest.raises(quality.CriticResponseError) as exc:
        quality.parse_pr_arbiter_response(raw, target_files=FINAL_FILES)
    assert exc.value.reason is quality.CriticResponseErrorReason.DUPLICATE_KEY


@pytest.mark.parametrize(
    "raw",
    [
        b'{"verdict":"GREEN","findings":[],"bad":"\xff"}',
        '{"verdict":"GREEN","findings":[]}'.encode("utf-16"),
        json.dumps({"verdict": "RED", "findings": [{**FINDING, "reason": "\ud800"}]}),
        json.dumps(
            {"verdict": "RED", "findings": [{**FINDING, "reason": "\ud800"}]}, ensure_ascii=False
        ),
    ],
)
def test_arbiter_rejects_non_utf8_response_text(raw: str | bytes) -> None:
    with pytest.raises(quality.CriticResponseError):
        quality.parse_pr_arbiter_response(raw, target_files=FINAL_FILES)


def test_arbiter_rejects_invalid_utf8_in_unreferenced_existing_target() -> None:
    with pytest.raises(quality.CriticResponseError):
        quality.parse_pr_arbiter_response(
            '{"verdict":"GREEN","findings":[]}',
            target_files={**FINAL_FILES, "docs/en/invalid.md": b"\xff"},
        )
