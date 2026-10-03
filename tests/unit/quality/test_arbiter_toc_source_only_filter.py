"""Arbiter must not RED for source-only TOC entries omitted from EN."""

from __future__ import annotations

import json

from tests.support.scripted_models import ScriptedModels
from tests.support.tool_critic_scripts import finish_only
from ydbdoc_review_ng.quality.repair import review_pr
from ydbdoc_review_ng.quality.types import Verdict

SOURCE_TOC = """\
items:
  - name: SelfHeal
    href: selfheal.md
  - name: BlobDepot
    href: blobdepot.md
"""

TARGET_TOC = """\
items:
  - name: BlobDepot
    href: blobdepot.md
  - name: Replacing a node's FQDN
    href: replacing_nodes.md
"""


def test_arbiter_source_only_toc_finding_is_dropped_to_green() -> None:
    source = {"docs/ru/toc_i.yaml": SOURCE_TOC.encode()}
    translated = {"docs/en/toc_i.yaml": TARGET_TOC.encode()}
    arbiter = {
        "verdict": "RED",
        "findings": [
            {
                "target_path": "docs/en/toc_i.yaml",
                "searchable_snippet": "items:",
                "reason": "В целевом TOC отсутствует пункт SelfHeal (selfheal.md).",
                "expected_correction": "Добавить пункт Working with SelfHeal с href: selfheal.md.",
            }
        ],
    }
    models = ScriptedModels([finish_only(), json.dumps(arbiter, ensure_ascii=False)])

    _corrected, final = review_pr(
        models,
        critic_model="critic",
        arbiter_model="arbiter",
        source_files=source,
        translated_files=translated,
        glossary_files={},
        validate_files=lambda files: None,
        toc_snapshots={
            "docs/ru/toc_i.yaml": {"before": None, "after": SOURCE_TOC},
        },
    )

    assert final.verdict is Verdict.GREEN
    assert final.findings == ()
