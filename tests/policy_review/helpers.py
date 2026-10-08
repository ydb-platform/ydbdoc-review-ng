from __future__ import annotations

import json
from decimal import Decimal

from ydbdoc_review_ng.models import HttpResponse
from ydbdoc_review_ng.policy_review.types import RULE_FILES, RULE_IDS, RULE_ROOT, ReviewSnapshot

PATH = "ydb/docs/ru/concepts/test.md"


def snapshot_json(*, before: str | None = None, after: str = "# Test\n\nОписание.\n") -> dict[str, object]:
    # Synthetic policies are fixtures, never a production source of normative rules.
    rules = {
        RULE_ROOT + "DOCUMENTATION_POLICY.md": "\n".join(
            f"{n}. `{name}`" for n, name in enumerate(RULE_FILES, 1)
        ),
        RULE_ROOT + "GENERAL_RULES.md": "Ясный язык.",
        RULE_ROOT + "FORMAT_RULES.md": "Markdown/YFM.",
        RULE_ROOT + "DOCUMENTATION_RULES.md": "\n".join(f"## {n}. Rule" for n in range(1, 16)),
    }
    return {"head_sha": "a" * 40, "base_sha": "b" * 40, "rules_sha": "b" * 40,
            "rules": rules, "context": {},
            "files": [{"path": PATH, "before": before, "after": after}]}


def snapshot(**kwargs: str | None) -> ReviewSnapshot:
    return ReviewSnapshot.from_json(snapshot_json(**kwargs))  # type: ignore[arg-type]


def semantic_json(*, findings: list[dict[str, object]] | None = None) -> str:
    return json.dumps({"findings": findings or [], "coverage": [
        {"path": PATH, "rule_id": rule,
         "status": "not_applicable" if rule == "DOC.13" else "checked",
         "reason": "Изображений нет." if rule == "DOC.13" else "Проверено."}
        for rule in RULE_IDS
    ]}, ensure_ascii=False)


def response(body: str, cost: Decimal | None = Decimal("0.1")) -> HttpResponse:
    return HttpResponse(200, json.dumps({
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": body}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }).encode(), cost)
