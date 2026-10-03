"""Checkpoint field recovery retained after retiring per-document semantic repair."""

import json

import pytest

from tests.support.scripted_models import ScriptedModels
from ydbdoc_review_ng.domain import GitSha, RepoPath, RepositoryId, SnapshotRef
from ydbdoc_review_ng.parser.markdown import build_markdown_plan
from ydbdoc_review_ng.quality import QualityInputError, Verdict
from ydbdoc_review_ng.quality.repair import _derive_target_translations, review_pr
from ydbdoc_review_ng.translation import assemble_candidate, build_translation_request

SNAPSHOT = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a" * 40))
SOURCE_PATH = RepoPath("ydb/docs/en/example.md")
TARGET_PATH = RepoPath("ydb/docs/ru/example.md")


def test_review_pr_arbiter_validates_findings_against_corrected_final_bytes() -> None:
    models = ScriptedModels(
        [
            '{"files":{"en/a.md":"# Corrected\\n"}}',
            json.dumps(
                {
                    "verdict": "YELLOW",
                    "findings": [
                        {
                            "target_path": "en/a.md",
                            "searchable_snippet": "Corrected",
                            "reason": "Неточно переведён заголовок.",
                            "expected_correction": "Уточните заголовок по исходному тексту.",
                        }
                    ],
                }
            ),
        ]
    )

    corrected, final = review_pr(
        models,
        critic_model="critic",
        arbiter_model="arbiter",
        source_files={"ru/a.md": "# Исходный\n".encode()},
        translated_files={"en/a.md": b"# Original\n"},
        glossary_files={},
        validate_files=lambda files: None,
    )

    assert corrected == {"en/a.md": b"# Corrected\n"}
    assert final.verdict is Verdict.YELLOW
    assert final.findings[0].searchable_snippet == "Corrected"
    assert final.findings[0].target_line == 1


def test_checkpoint_recovers_current_target_prose_and_protected_links() -> None:
    source = b"# Install YDB\n\nRead [the guide](/docs/guide) before starting.\n"
    target = "# Установка YDB\n\nПрочитайте [руководство](/docs/guide) до начала.\n".encode()
    plan = build_markdown_plan(SNAPSHOT, SOURCE_PATH, source)
    request = build_translation_request(source, plan)

    values = _derive_target_translations(source, plan, request, target, TARGET_PATH)

    assert values[request.requested_ids[0]] == "Установка YDB"
    assert "Прочитайте" in values[request.requested_ids[1]]
    assert "руководство" in values[request.requested_ids[1]]
    assert "Read" not in values[request.requested_ids[1]]
    assert assemble_candidate(source, plan, request, values) == target


def test_checkpoint_preserves_logical_escaped_frontmatter_values() -> None:
    source = b'---\ntitle: "An \\"escaped\\" title"\ndescription: "Old description"\n---\n'
    target = b'---\ntitle: "A \\"quoted\\" title"\ndescription: "Corrected description"\n---\n'
    plan = build_markdown_plan(SNAPSHOT, SOURCE_PATH, source)
    request = build_translation_request(source, plan)

    values = _derive_target_translations(source, plan, request, target, TARGET_PATH)

    title_id, description_id = request.requested_ids
    assert values == {title_id: 'A "quoted" title', description_id: "Corrected description"}
    assert assemble_candidate(source, plan, request, values) == target


@pytest.mark.parametrize(
    "target",
    [
        b"Run `other` and open [the guide](/guide).\n",
        b"Run `ydb` and open [the guide](/wrong).\n",
        b"Run `ydb` and open [the guide](/guide).\n\nExtra paragraph.\n",
    ],
)
def test_checkpoint_rejects_changed_protected_content_or_structure(target: bytes) -> None:
    source = b"Run `ydb` and open [the guide](/guide).\n"
    plan = build_markdown_plan(SNAPSHOT, SOURCE_PATH, source)
    request = build_translation_request(source, plan)

    with pytest.raises(QualityInputError):
        _derive_target_translations(source, plan, request, target, TARGET_PATH)


def test_derive_malformed_yaml_frontmatter_is_quality_input_not_parser_error() -> None:
    """REQUIREMENTS §2/§4.1: critic YAML diagnostics soft-fail as QualityInputError (#1)."""
    source = b"---\ntitle: Good\n---\nBody text here.\n"
    malformed = b"---\ntitle: [broken\n---\nCorrected body text.\n"
    plan = build_markdown_plan(SNAPSHOT, SOURCE_PATH, source)
    request = build_translation_request(source, plan)

    with pytest.raises(QualityInputError):
        _derive_target_translations(source, plan, request, malformed, TARGET_PATH)


def test_review_pr_soft_publishes_malformed_yaml_critic_correction() -> None:
    """REQUIREMENTS §4.1: valid critic UTF-8 with broken YAML still replaces the draft (#1)."""
    source = b"---\ntitle: Good\n---\nBody text here.\n"
    draft = b"---\ntitle: Draft\n---\nBody text here.\n"
    malformed = b"---\ntitle: [broken\n---\nCorrected body text.\n"
    plan = build_markdown_plan(SNAPSHOT, SOURCE_PATH, source)
    request = build_translation_request(source, plan)
    target = "ydb/docs/ru/example.md"

    models = ScriptedModels(
        [
            json.dumps({"files": {target: malformed.decode("utf-8")}}),
            json.dumps({"verdict": "GREEN", "findings": []}),
        ]
    )

    def validate(files: dict[str, bytes]) -> None:
        for name, content in files.items():
            try:
                _derive_target_translations(source, plan, request, content, RepoPath(name))
            except QualityInputError:
                return

    corrected, final = review_pr(
        models,
        critic_model="critic",
        arbiter_model="arbiter",
        source_files={"ydb/docs/en/example.md": source},
        translated_files={target: draft},
        glossary_files={},
        validate_files=validate,
    )

    assert corrected[target] == malformed
    assert final.verdict is Verdict.GREEN
