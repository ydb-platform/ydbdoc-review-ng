"""Read the canonical policy and full normative files in their declared order."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

from ydbdoc_review_ng.policy_review.types import RULE_FILES, RULE_ROOT, ReviewError, ReviewSnapshot


@dataclass(frozen=True, slots=True)
class PolicyBundle:
    sha: str
    digest: str
    files: tuple[tuple[str, str], ...] = field(repr=False)

    @classmethod
    def load(cls, snapshot: ReviewSnapshot) -> PolicyBundle:
        rules = dict(snapshot.rules)
        entry = RULE_ROOT + "DOCUMENTATION_POLICY.md"
        expected = (entry, *(RULE_ROOT + name for name in RULE_FILES))
        if set(rules) != set(expected) or any(not rules[name].strip() for name in expected):
            raise ReviewError("normative_rules_missing")
        declarations = re.findall(r"(?m)^\d+\.\s+`([^`\n]+\.md)`", rules[entry])
        if tuple(declarations) != RULE_FILES:
            raise ReviewError("unsupported_normative_rules")
        sections = re.findall(r"(?m)^## (\d+)\. ", rules[RULE_ROOT + "DOCUMENTATION_RULES.md"])
        if sections != [str(n) for n in range(1, 16)]:
            raise ReviewError("unsupported_documentation_rules")
        ordered = tuple((name, rules[name]) for name in expected)
        digest = hashlib.sha256(json.dumps(ordered, ensure_ascii=False).encode()).hexdigest()
        return cls(snapshot.rules_sha, digest, ordered)
