"""P0/P1 witness: raw translator dump is not a product success without critic."""

from __future__ import annotations

import json

from tests.support.scripted_models import ScriptedModels
from tests.support.tool_critic_scripts import patch_read_finish
from ydbdoc_review_ng.domain import ModelRole
from ydbdoc_review_ng.models import AttemptError, ModelCallResult
from ydbdoc_review_ng.quality.repair import review_pr
from ydbdoc_review_ng.quality.types import Verdict


class _Scripted(ScriptedModels):
    pass


def test_critic_transport_failure_is_red_without_arbiter_on_raw_draft() -> None:
    """Critic 503/transport after retry → RED; arbiter must not GREEN the raw dump."""
    source = {"docs/ru/a.md": b"# A\n"}
    translated = {"docs/en/a.md": b"# Raw translator dump\n"}
    models = _Scripted(
        [
            ModelCallResult(None, AttemptError.TRANSPORT, ()),
            ModelCallResult(None, AttemptError.TRANSPORT, ()),
            # If arbiter were wrongly invoked, a GREEN here must not become success.
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
    )

    assert [call.role for call in models.calls] == [ModelRole.CRITIC, ModelRole.CRITIC]
    assert corrected == translated
    assert final.verdict is Verdict.RED
    assert final.findings
    assert final.findings[0].target_path == "docs/en/a.md"


def test_critic_invalid_contract_after_retry_is_red() -> None:
    source = {"docs/ru/a.md": b"# A\n"}
    translated = {"docs/en/a.md": b"# Draft\n"}
    # Two sessions, each starts with a non-tool text response → protocol/contract RED.
    models = _Scripted(
        [
            ModelCallResult("not-tools", None, ()),
            ModelCallResult("still-not-tools", None, ()),
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
    )

    assert [call.role for call in models.calls] == [ModelRole.CRITIC, ModelRole.CRITIC]
    assert final.verdict is Verdict.RED
    assert any(item.target_path == "docs/en/a.md" for item in final.findings)


def test_successful_critic_still_reaches_arbiter() -> None:
    source = {"docs/ru/a.md": b"# A\n"}
    translated = {"docs/en/a.md": b"# Draft\n"}
    reviewed = b"# Reviewed\n"
    models = _Scripted(
        [
            *patch_read_finish("docs/en/a.md", translated["docs/en/a.md"], reviewed),
            json.dumps({"verdict": "GREEN", "findings": []}),
        ]
    )
    published: list[dict[str, bytes]] = []

    corrected, final = review_pr(
        models,
        critic_model="critic",
        arbiter_model="arbiter",
        source_files=source,
        translated_files=translated,
        glossary_files={},
        validate_files=lambda files: None,
        on_successful_critic_chunk=published.append,
    )

    assert corrected == {"docs/en/a.md": reviewed}
    assert published == [{"docs/en/a.md": reviewed}]
    assert final.verdict is Verdict.GREEN
    assert any(call.role is ModelRole.ARBITER for call in models.calls)
    assert models.calls[0].tools is not None
    assert models.calls[0].schema is None
