"""#14: critic/arbiter receive TOC before/after and binary manifest."""

from __future__ import annotations

import json

from ydbdoc_review_ng.quality.critic import build_pr_arbiter_request, build_pr_critic_request


def _section(prompt: str, tag: str) -> object:
    return json.loads(prompt.split(f"<{tag}>\n", 1)[1].split(f"\n</{tag}>", 1)[0])


def test_critic_request_includes_toc_snapshots_and_binary_manifest() -> None:
    request = build_pr_critic_request(
        model="critic",
        source_files={"ydb/docs/ru/a.md": b"# A\n", "ydb/docs/ru/toc.yaml": b"items:\n- href: a.md\n"},
        translated_files={
            "ydb/docs/en/a.md": b"# A\n",
            "ydb/docs/en/toc.yaml": b"items:\n- name: A\n  href: a.md\n",
        },
        glossary_files={},
        toc_snapshots={
            "ydb/docs/ru/toc.yaml": {
                "before": "items:\n",
                "after": "items:\n- href: a.md\n",
            }
        },
        binary_manifest={
            "ydb/docs/en/logo.png": {"action": "copy", "source_path": "ydb/docs/ru/logo.png"}
        },
    )
    assert _section(request.prompt, "source-toc-snapshots") == {
        "ydb/docs/ru/toc.yaml": {
            "before": "items:\n",
            "after": "items:\n- href: a.md\n",
        }
    }
    assert _section(request.prompt, "binary-manifest") == {
        "ydb/docs/en/logo.png": {"action": "copy", "source_path": "ydb/docs/ru/logo.png"}
    }
    assert "ydb/docs/en/toc.yaml" in _section(request.prompt, "translation-pr-files")


def test_arbiter_request_includes_same_review_context() -> None:
    request = build_pr_arbiter_request(
        model="arbiter",
        source_files={"ydb/docs/ru/a.md": b"# A\n"},
        translated_files={"ydb/docs/en/a.md": b"# A\n"},
        glossary_files={},
        toc_snapshots={"ydb/docs/ru/toc.yaml": {"before": None, "after": "items:\n"}},
        binary_manifest={},
    )
    assert _section(request.prompt, "source-toc-snapshots") == {
        "ydb/docs/ru/toc.yaml": {"before": None, "after": "items:\n"}
    }
    assert _section(request.prompt, "binary-manifest") == {}
