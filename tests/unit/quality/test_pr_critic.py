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
    "docs/ru/article.md": '# Статья\n\nВся информация, включая "кавычки".\n'.encode(),
    "docs/ru/toc.yaml": "items:\n  - name: Статья\n    href: article.md\n".encode(),
}
TRANSLATED_FILES = {
    "docs/en/article.md": b'# Article\n\nAll information, including "quotes".\n',
    "docs/en/toc.yaml": b"items:\n  - name: Article\n    href: article.md\n",
}
GLOSSARY_FILES = {
    "docs/ru/glossary.md": ("# Глоссарий\n\n" + "Термин и определение.\n" * 600).encode(),
    "docs/en/glossary.md": ("# Glossary\n\n" + "Term and definition.\n" * 600).encode(),
}
TARGET_PATHS = tuple(TRANSLATED_FILES)


def request(*, operator_context: str | None = None) -> ModelRequest:
    return quality.build_pr_critic_request(
        model="critic-model",
        source_files=SOURCE_FILES,
        translated_files=TRANSLATED_FILES,
        glossary_files=GLOSSARY_FILES,
        operator_context=operator_context,
    )


def block(prompt: str, name: str) -> dict[str, str]:
    return json.loads(prompt.split(f"<{name}>\n", 1)[1].split(f"\n</{name}>", 1)[0])


def test_critic_renders_exact_shipped_prompt_template() -> None:
    template = (
        resources.files("ydbdoc_review_ng.quality")
        .joinpath("prompts/critic.txt")
        .read_text(encoding="utf-8")
    )
    built = request()
    assert built.developer_prompt == template
    assert "<source-pr-files>" not in built.developer_prompt
    assert "Use only these tools: read, grep, apply_patch, finish." in template
    assert built.prompt.endswith(
        "Before finish, check completeness, terminology, technical literals and "
        "inline-code, damaged sentences, TOC correctness, and the supplied pair. "
        "Use tools only; end with finish after mandatory re-reads."
    )


def test_critic_contains_complete_two_file_inputs_and_glossary() -> None:
    built = request()
    assert block(built.prompt, "source-pr-files") == {
        path: text.decode() for path, text in SOURCE_FILES.items()
    }
    assert block(built.prompt, "translation-pr-files") == {
        path: text.decode() for path, text in TRANSLATED_FILES.items()
    }
    assert block(built.prompt, "project-glossary") == {
        path: text.decode() for path, text in GLOSSARY_FILES.items()
    }
    assert built.role is ModelRole.CRITIC
    assert built.model == "critic-model"
    assert built.target_path is None
    assert built.developer_prompt is not None
    assert built.max_output_tokens == 4_096
    assert built.schema is None
    assert built.tools is not None
    tool_names = {
        item["function"]["name"]
        for item in mutable_json(built.tools)
        if isinstance(item, dict)
    }
    assert tool_names == {"read", "grep", "apply_patch", "finish"}
    assert built.messages is not None


def test_template_edit_changes_next_request_without_workflow_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = tmp_path / "prompts" / "critic.txt"
    template.parent.mkdir()
    template.write_text("First instructions", encoding="utf-8")
    monkeypatch.setattr(resources, "files", lambda package: tmp_path)
    assert request().developer_prompt == "First instructions"
    template.write_text("Edited instructions", encoding="utf-8")
    assert request().developer_prompt == "Edited instructions"


def test_template_tokens_in_file_contents_are_not_interpolated() -> None:
    text = b"Literal {{ PROJECT_GLOSSARY }} and {{ TRANSLATION_PR_FILES }}\n"
    built = quality.build_pr_critic_request(
        model="critic-model",
        source_files={"docs/ru/article.md": text},
        translated_files={"docs/en/article.md": text},
        glossary_files=GLOSSARY_FILES,
    )
    assert block(built.prompt, "source-pr-files") == {"docs/ru/article.md": text.decode()}
    assert block(built.prompt, "translation-pr-files") == {"docs/en/article.md": text.decode()}


def test_operator_context_is_separate_from_file_maps() -> None:
    context = "Уточните термин в статье.\nСохраните структуру.\n"
    built = request(operator_context=context)
    assert f"<operator-context>\n{context}</operator-context>" in built.prompt
    assert built.prompt.endswith("mandatory re-reads.")
    assert context not in block(built.prompt, "source-pr-files").values()
    assert context not in block(built.prompt, "translation-pr-files").values()


@pytest.mark.parametrize("as_bytes", [False, True])
def test_files_response_preserves_full_utf8_contents(as_bytes: bool) -> None:
    files = {
        "docs/en/article.md": '# Статья 🙂\r\n\n"Quotes" and \\ escapes.\n\n',
        "docs/en/toc.yaml": TRANSLATED_FILES["docs/en/toc.yaml"].decode(),
    }
    raw = json.dumps({"files": files}, ensure_ascii=False)
    assert quality.parse_pr_critic_response(
        raw.encode() if as_bytes else raw, target_paths=TARGET_PATHS
    ) == {path: text.encode() for path, text in files.items()}


def test_files_response_accepts_unchanged_and_empty_complete_files() -> None:
    files = {path: text.decode() for path, text in TRANSLATED_FILES.items()}
    assert (
        quality.parse_pr_critic_response(json.dumps({"files": files}), target_paths=TARGET_PATHS)
        == TRANSLATED_FILES
    )
    assert quality.parse_pr_critic_response(
        '{"files":{"empty.md":""}}', target_paths=("empty.md",)
    ) == {"empty.md": b""}


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        "null",
        "{}",
        '{"corrected_markdown":"text"}',
        '{"files":{"a.md":"A","b.md":"B"},"verdict":"GREEN"}',
        '{"files":[]}',
        '{"files":null}',
        '{"files":{"a.md":"A"}}',
        '{"files":{"a.md":"A","b.md":"B","unknown.md":"C"}}',
        '{"files":{"a.md":"A","b.md":"B","a.md":"duplicate"}}',
        '{"files":{"a.md":"A","b.md":"B"},"files":{"a.md":"A","b.md":"B"}}',
        '{"files":{"a.md":[],"b.md":"B"}}',
        '{"files":{"a.md":null,"b.md":"B"}}',
        '{"files":{"a.md":{},"b.md":"B"}}',
        '{"files":{"a.md":1,"b.md":"B"}}',
        '{"files":{"a.md":true,"b.md":"B"}}',
        '{"files":{"a.md":NaN,"b.md":"B"}}',
        '{"files":{"a.md":"\\ud800","b.md":"B"}}',
        '{"files":{"a.md":"\\udfff","b.md":"B"}}',
        '{"files":{"a.md":"\ud800","b.md":"B"}}',
        b'{"files":{"a.md":"\xff","b.md":"B"}}',
        '{"files":{"a.md":"A","b.md":"B"}}'.encode("utf-16"),
    ],
)
def test_files_response_rejects_missing_unknown_duplicate_and_nonstring_entries(
    raw: str | bytes,
) -> None:
    with pytest.raises(quality.CriticResponseError):
        quality.parse_pr_critic_response(raw, target_paths=("a.md", "b.md"))
