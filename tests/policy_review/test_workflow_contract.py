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
    assert workflow["permissions"] == {"contents": "read", "checks": "write",
                                       "pull-requests": "write", "issues": "write"}
    assert "api-key" not in gate["steps"][0]["with"]
    assert "vars.YDBDOC_REVIEW_ENABLED == 'true' &&" in gate["if"]
    assert "checkout" not in (ROOT / "examples/ydbdoc-policy-review.yml").read_text()


def test_completed_run_finalizer_has_no_model_credentials_or_mutable_pr_code() -> None:
    text = (ROOT / "examples/ydbdoc-policy-review-finalize.yml").read_text()
    assert "workflow_run:" in text and "types: [completed]" in text
    assert "api-key" not in text and "YANDEX_API_KEY" not in text
    assert "head.sha" not in text and "checkout" not in text
    assert "if: vars.YDBDOC_REVIEW_ENABLED == 'true'" in text


def test_release_reference_and_credentials_match_translation_conventions() -> None:
    review = yaml.safe_load((ROOT / "examples/ydbdoc-policy-review.yml").read_text())
    finalizer = yaml.safe_load((ROOT / "examples/ydbdoc-policy-review-finalize.yml").read_text())
    expected = "ydb-platform/ydbdoc-review-ng/.github/actions/policy-review@v1.2.0"
    for job in (review["jobs"]["gate"], review["jobs"]["review"], finalizer["jobs"]["finalize"]):
        step = job["steps"][0]
        assert step["uses"] == expected
        assert step["with"]["publish-token"] == "${{ github.token }}"
    config = review["jobs"]["review"]["steps"][0]["with"]
    assert config["api-key"] == "${{ secrets.YANDEX_CLOUD_API_KEY_DOC_REVIEW }}"
    assert config["folder-id"] == "${{ secrets.YANDEX_CLOUD_FOLDER_DOC_REVIEW }}"
    assert config["admission-token"] == "${{ github.token }}"
    assert config["ydb-sa-key"] == "${{ secrets.YDB_SA_KEY }}"
