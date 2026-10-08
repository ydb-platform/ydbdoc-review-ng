from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_only_admitted_review_or_explicit_stop_enters_cancelling_group() -> None:
    workflow = yaml.safe_load((ROOT / "examples/ydbdoc-policy-review.yml").read_text())
    assert "concurrency" not in workflow
    gate = workflow["jobs"]["gate"]
    review = workflow["jobs"]["review"]
    assert "concurrency" not in gate
    assert review["needs"] == "gate"
    assert "outputs.admitted" in review["if"] and "outputs.stop" in review["if"]
    assert review["concurrency"]["cancel-in-progress"] is True
    assert "pull_request.number" in review["concurrency"]["group"]
    assert workflow["permissions"] == {"contents": "read"}
    assert "api-key" not in gate["steps"][0]["with"]
    assert "checkout" not in (ROOT / "examples/ydbdoc-policy-review.yml").read_text()


def test_completed_run_finalizer_has_no_model_credentials_or_mutable_pr_code() -> None:
    text = (ROOT / "examples/ydbdoc-policy-review-finalize.yml").read_text()
    assert "workflow_run:" in text and "types: [completed]" in text
    assert "api-key" not in text and "YANDEX_API_KEY" not in text
    assert "head.sha" not in text and "checkout" not in text
