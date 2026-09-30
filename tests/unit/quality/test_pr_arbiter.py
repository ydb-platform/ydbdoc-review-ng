from __future__ import annotations

import json

import pytest

from ydbdoc_review_ng import quality
from ydbdoc_review_ng.domain import ModelRole
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
TARGET_PATHS = tuple(FINAL_FILES)
FINDING = {
    "repairable": True,
    "reason": "Название расходится со статьёй.",
    "expected_correction": "Используйте одно название в статье и TOC.",
    "searchable_snippet": "Article",
    "target_path": "docs/en/toc.yaml",
    "target_line": 2,
}


def request():
    return quality.build_pr_arbiter_request(
        model="independent-arbiter",
        source_files=SOURCE_FILES,
        translated_files=FINAL_FILES,
        glossary_files=GLOSSARY_FILES,
        operator_context="Проверьте согласованность названия.",
    )


def test_arbiter_receives_complete_final_pr_and_glossary() -> None:
    built = request()
    for tag, files in (
        ("source-pr-files", SOURCE_FILES),
        ("translation-pr-files", FINAL_FILES),
        ("project-glossary", GLOSSARY_FILES),
    ):
        content = built.prompt.split(f"<{tag}>\n", 1)[1].split(f"\n</{tag}>", 1)[0]
        assert json.loads(content) == {path: text.decode() for path, text in files.items()}
    assert built.role is ModelRole.ARBITER
    assert built.model == "independent-arbiter"
    assert "<operator-context>\nПроверьте согласованность названия." in built.prompt


def test_arbiter_does_not_request_corrected_files() -> None:
    schema = mutable_json(request().schema)
    assert set(schema["properties"]) == {"verdict", "findings"}
    assert schema["required"] == ["verdict", "findings"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["verdict"]["enum"] == ["GREEN", "YELLOW", "RED"]
    finding_schema = schema["properties"]["findings"]["items"]
    assert set(finding_schema["properties"]) == set(FINDING)
    assert finding_schema["additionalProperties"] is False


@pytest.mark.parametrize("verdict", ["GREEN", "YELLOW", "RED"])
@pytest.mark.parametrize("with_finding", [False, True])
def test_arbiter_preserves_model_verdict_independently_of_finding_count(
    verdict: str, with_finding: bool
) -> None:
    findings = [FINDING] if with_finding else []
    result = quality.parse_pr_arbiter_response(
        json.dumps({"verdict": verdict, "findings": findings}), target_paths=TARGET_PATHS
    )
    assert result.verdict.value == verdict
    assert len(result.findings) == len(findings)
    assert result.corrected_markdown is None


@pytest.mark.parametrize("repairable", [False, True])
def test_arbiter_yellow_preserves_model_verdict(repairable: bool) -> None:
    finding = {**FINDING, "repairable": repairable}
    result = quality.parse_pr_arbiter_response(
        json.dumps({"verdict": "YELLOW", "findings": [finding]}).encode(),
        target_paths=TARGET_PATHS,
    )
    assert result.verdict is quality.Verdict.YELLOW
    assert result.findings == (
        quality.Finding(
            repairable,
            "Название расходится со статьёй.",
            "Используйте одно название в статье и TOC.",
            "Article",
            "docs/en/toc.yaml",
            2,
        ),
    )


def test_arbiter_findings_allow_both_pr_paths() -> None:
    schema = mutable_json(request().schema)
    path_schema = schema["properties"]["findings"]["items"]["properties"]["target_path"]
    assert path_schema == {"type": "string", "enum": list(TARGET_PATHS)}
    result = quality.parse_pr_arbiter_response(
        json.dumps(
            {
                "verdict": "RED",
                "findings": [{**FINDING, "target_path": path} for path in TARGET_PATHS],
            }
        ),
        target_paths=TARGET_PATHS,
    )
    assert tuple(f.target_path for f in result.findings) == TARGET_PATHS


@pytest.mark.parametrize(
    "update",
    [
        {"target_path": "docs/en/unknown.md"},
        {"target_line": True},
        {"target_line": 0},
        {"reason": " "},
        {"expected_correction": None},
        {"searchable_snippet": ""},
        {"repairable": "true"},
        {"files": {}},
    ],
)
def test_arbiter_rejects_invalid_findings(update: dict[str, object]) -> None:
    raw = json.dumps({"verdict": "YELLOW", "findings": [{**FINDING, **update}]})
    with pytest.raises(quality.CriticResponseError):
        quality.parse_pr_arbiter_response(raw, target_paths=TARGET_PATHS)


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        '{"verdict":null,"findings":[]}',
        '{"verdict":"BLUE","findings":[]}',
        '{"verdict":"GREEN","findings":null}',
        '{"verdict":"GREEN","verdict":"RED","findings":[]}',
        '{"verdict":"GREEN","findings":[],"files":{}}',
        '{"verdict":"GREEN","findings":[],"corrected_markdown":"text"}',
        '{"files":{}}',
    ],
)
def test_arbiter_rejects_malformed_or_correcting_responses(raw: str) -> None:
    with pytest.raises(quality.CriticResponseError):
        quality.parse_pr_arbiter_response(raw, target_paths=TARGET_PATHS)
