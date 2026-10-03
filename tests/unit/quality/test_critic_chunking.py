"""Critic/arbiter split oversized reviews into whole file-pair chunks (§4)."""

from __future__ import annotations

import json

from tests.support.scripted_models import ScriptedModels
from ydbdoc_review_ng.domain import ModelRole
from ydbdoc_review_ng.models import ModelRequest
from ydbdoc_review_ng.quality.repair import review_pr
from ydbdoc_review_ng.quality.types import Verdict


class _Scripted(ScriptedModels):
    pass


def _target_paths(request: ModelRequest) -> list[str]:
    if request.schema is not None:
        files = request.schema["properties"]["files"]
        return list(files["required"])
    block = request.prompt.split("<translation-pr-files>\n", 1)[1].split(
        "\n</translation-pr-files>", 1
    )[0]
    return list(json.loads(block))


def test_oversized_review_splits_into_whole_file_pair_chunks_and_pushes_each() -> None:
    """One request that does not fit → chunks by whole pairs; each success pushes."""
    source = {
        "docs/ru/a.md": b"# A\n",
        "docs/ru/b.md": b"# B\n",
    }
    translated = {
        "docs/en/a.md": b"# Draft A\n",
        "docs/en/b.md": b"# Draft B\n",
    }
    pushes: list[tuple[str, ...]] = []

    def fits(request: ModelRequest) -> bool:
        if request.role is ModelRole.CRITIC:
            return len(_target_paths(request)) <= 1
        findings_enum = request.schema["properties"]["findings"]["items"]["properties"][
            "target_path"
        ]["enum"]
        return len(list(findings_enum)) <= 1

    models = _Scripted(
        [
            json.dumps({"files": {"docs/en/a.md": "# Fixed A\n"}}),
            json.dumps({"files": {"docs/en/b.md": "# Fixed B\n"}}),
            json.dumps({"verdict": "GREEN", "findings": []}),
            json.dumps({"verdict": "GREEN", "findings": []}),
        ]
    )

    corrected, final = review_pr(
        models,
        critic_model="critic",
        arbiter_model="arbiter",
        source_files=source,
        translated_files=translated,
        glossary_files={},
        validate_files=lambda files: None,
        on_successful_critic_chunk=lambda files: pushes.append(tuple(sorted(files))),
        request_fits=fits,
    )

    assert corrected == {
        "docs/en/a.md": b"# Fixed A\n",
        "docs/en/b.md": b"# Fixed B\n",
    }
    critic_calls = [call for call in models.calls if call.role is ModelRole.CRITIC]
    arbiter_calls = [call for call in models.calls if call.role is ModelRole.ARBITER]
    assert len(arbiter_calls) == 2
    assert {_target_paths(call)[0] for call in critic_calls} == {
        "docs/en/a.md",
        "docs/en/b.md",
    }
    assert pushes == [("docs/en/a.md",), ("docs/en/b.md",)]
    assert final.verdict is Verdict.GREEN


def test_fitting_multi_file_pr_still_uses_one_pair_per_critic_call() -> None:
    """Even when the whole PR fits the 1M window, critic must not mega-batch."""
    source = {
        "docs/ru/a.md": b"# A\n",
        "docs/ru/b.md": b"# B\n",
    }
    translated = {
        "docs/en/a.md": b"# Draft A\n",
        "docs/en/b.md": b"# Draft B\n",
    }
    models = _Scripted(
        [
            json.dumps({"files": {"docs/en/a.md": "# Fixed A\n"}}),
            json.dumps({"files": {"docs/en/b.md": "# Fixed B\n"}}),
            json.dumps({"verdict": "GREEN", "findings": []}),
            json.dumps({"verdict": "GREEN", "findings": []}),
        ]
    )

    corrected, final = review_pr(
        models,
        critic_model="critic",
        arbiter_model="arbiter",
        source_files=source,
        translated_files=translated,
        glossary_files={},
        validate_files=lambda files: None,
        request_fits=lambda _request: True,
    )

    assert corrected == {
        "docs/en/a.md": b"# Fixed A\n",
        "docs/en/b.md": b"# Fixed B\n",
    }
    critic_calls = [call for call in models.calls if call.role is ModelRole.CRITIC]
    arbiter_calls = [call for call in models.calls if call.role is ModelRole.ARBITER]
    assert len(arbiter_calls) == 2
    first_turns = []
    seen: set[str] = set()
    for call in critic_calls:
        path = _target_paths(call)[0]
        if path not in seen:
            first_turns.append([path])
            seen.add(path)
    assert first_turns == [["docs/en/a.md"], ["docs/en/b.md"]]
    assert final.verdict is Verdict.GREEN


def test_dual_locale_glossary_is_reduced_to_relevant_sections() -> None:
    ru_glossary = (
        b"## BlobDepot {#blobdepot}\n\n**BlobDepot** stores blobs.\n\n"
        b"## Unrelated {#unrelated}\n\n**UnrelatedTerm** never matches.\n"
    )
    en_glossary = (
        b"## BlobDepot {#blobdepot}\n\n**BlobDepot** stores blobs.\n\n"
        b"## Unrelated {#unrelated}\n\n**UnrelatedTerm** never matches.\n"
    )
    models = _Scripted(
        [
            json.dumps({"files": {"docs/en/a.md": "# BlobDepot fixed\n"}}),
            json.dumps({"verdict": "GREEN", "findings": []}),
        ]
    )

    review_pr(
        models,
        critic_model="critic",
        arbiter_model="arbiter",
        source_files={"docs/ru/a.md": b"# BlobDepot overview\n"},
        translated_files={"docs/en/a.md": b"# BlobDepot draft\n"},
        glossary_files={
            "ydb/docs/ru/core/concepts/glossary.md": ru_glossary,
            "ydb/docs/en/core/concepts/glossary.md": en_glossary,
        },
        validate_files=lambda files: None,
    )

    glossary = json.loads(
        models.calls[0].prompt.split("<project-glossary>\n", 1)[1].split(
            "\n</project-glossary>", 1
        )[0]
    )
    assert set(glossary) == {"relevant-paired-sections"}
    assert "blobdepot" in glossary["relevant-paired-sections"]
    assert "UnrelatedTerm" not in glossary["relevant-paired-sections"]
    assert models.calls[0].max_output_tokens is not None
    assert models.calls[0].max_output_tokens <= 32_768


def test_toc_critic_receives_runtime_computed_target_only_references() -> None:
    source_path = "docs/ru/toc.yaml"
    target_path = "docs/en/toc.yaml"
    source_before = "items:\n- name: Existing\n  href: existing.md\n"
    source_after = (
        source_before + "- name: Added\n  href: added.md\n"
    )
    target = (
        "items:\n"
        "- name: Existing\n  href: existing.md\n"
        "- name: Target only\n  href: replacing_nodes.md\n"
        "- name: Added\n  href: added.md\n"
    )
    models = _Scripted(
        [
            json.dumps({"files": {target_path: target}}),
            json.dumps({"verdict": "GREEN", "findings": []}),
        ]
    )

    review_pr(
        models,
        critic_model="critic",
        arbiter_model="arbiter",
        source_files={source_path: source_after.encode()},
        translated_files={target_path: target.encode()},
        glossary_files={},
        validate_files=lambda files: None,
        toc_snapshots={
            source_path: {"before": source_before, "after": source_after}
        },
    )

    prompt = models.calls[0].prompt
    assert "mandatory preserved target-only references" in prompt
    assert '["href:replacing_nodes.md"]' in prompt


def test_single_pair_that_does_not_fit_is_left_unreviewed_and_forces_red() -> None:
    """Pair that never fits stays as-is for arbiter; overall verdict is RED."""
    source = {
        "docs/ru/a.md": b"# A\n",
        "docs/ru/huge.md": b"# Huge\n",
    }
    translated = {
        "docs/en/a.md": b"# Draft A\n",
        "docs/en/huge.md": b"# Draft Huge\n",
    }

    def fits(request: ModelRequest) -> bool:
        if request.role is ModelRole.CRITIC:
            return _target_paths(request) == ["docs/en/a.md"]
        paths = request.schema["properties"]["findings"]["items"]["properties"]["target_path"][
            "enum"
        ]
        return list(paths) == ["docs/en/a.md"]

    models = _Scripted(
        [
            json.dumps({"files": {"docs/en/a.md": "# Fixed A\n"}}),
            json.dumps({"verdict": "GREEN", "findings": []}),
        ]
    )

    corrected, final = review_pr(
        models,
        critic_model="critic",
        arbiter_model="arbiter",
        source_files=source,
        translated_files=translated,
        glossary_files={},
        validate_files=lambda files: None,
        request_fits=fits,
    )

    assert corrected == {
        "docs/en/a.md": b"# Fixed A\n",
        "docs/en/huge.md": b"# Draft Huge\n",
    }
    assert final.verdict is Verdict.RED
    assert any(
        finding.target_path == "docs/en/huge.md"
        and finding.target_line is None
        and finding.searchable_snippet is None
        and finding.reason == "Файл не удалось проверить в доступном контексте модели."
        and "Уменьш" in finding.expected_correction
        for finding in final.findings
    )
    assert any(call.role is ModelRole.CRITIC for call in models.calls)
    assert any(call.role is ModelRole.ARBITER for call in models.calls)
    assert all(
        call.role is ModelRole.CRITIC or call.role is ModelRole.ARBITER
        for call in models.calls
    )
