"""Direction classifier returns only translation_required, direction, and reason."""

from __future__ import annotations

import json

import pytest

from ydbdoc_review_ng import direction
from ydbdoc_review_ng.continuation import normalize_source_inventory
from ydbdoc_review_ng.direction import InventoryFile, inventory_request
from ydbdoc_review_ng.domain import GitSha, ModelRole, RepositoryId, RepoPath, SnapshotRef


def inventory():
    return normalize_source_inventory(
        [
            {
                "filename": "ru/new.md",
                "status": "renamed",
                "previous_filename": "ru/old.md",
                "changes": 1,
            }
        ]
    )


def response(**overrides):
    payload = {
        "translation_required": True,
        "direction": "ru_to_en",
        "reason": "New page needs translation",
    }
    payload.update(overrides)
    return payload


def test_classifier_accepts_direction_only_payload():
    classified = direction.parse_inventory_response(json.dumps(response()), inventory())
    assert classified.translation_required is True
    assert classified.direction is direction.Direction.RU_TO_EN
    assert classified.reason == "New page needs translation"
    assert getattr(classified, "files", ()) == ()


def test_classifier_accepts_no_translation_without_direction():
    classified = direction.parse_inventory_response(
        json.dumps(response(translation_required=False, direction=None, reason="No docs")),
        inventory(),
    )
    assert classified.translation_required is False
    assert classified.direction is None


def test_classifier_rejects_model_file_actions():
    payload = response()
    payload["files"] = [
        {
            "path": "ru/new.md",
            "operation": "rename",
            "old_path": "ru/old.md",
            "new_path": "ru/new.md",
            "action": "page",
            "toc_delta": None,
        }
    ]
    with pytest.raises(ValueError):
        direction.parse_inventory_response(json.dumps(payload), inventory())


@pytest.mark.parametrize(
    "defect",
    ["missing_reason", "empty_reason", "bad_direction", "bad_required", "extra_field", "dup_key"],
)
def test_classifier_rejects_invalid_direction_payload(defect):
    payload = response()
    raw = json.dumps(payload)
    if defect == "missing_reason":
        payload.pop("reason")
        raw = json.dumps(payload)
    elif defect == "empty_reason":
        payload["reason"] = "  "
        raw = json.dumps(payload)
    elif defect == "bad_direction":
        payload["direction"] = "sideways"
        raw = json.dumps(payload)
    elif defect == "bad_required":
        payload["translation_required"] = 1
        raw = json.dumps(payload)
    elif defect == "extra_field":
        payload["note"] = "x"
        raw = json.dumps(payload)
    else:
        raw = raw.replace(
            '"translation_required": true',
            '"translation_required": false, "translation_required": true',
        )
    with pytest.raises(ValueError):
        direction.parse_inventory_response(raw, inventory())


def test_inventory_request_schema_has_no_files_array():
    change = inventory().files[0]
    item = InventoryFile(change, None, b"x", RepoPath("en/new.md"), None)
    repo = RepositoryId("ydb-platform/ydb")
    before = SnapshotRef(repo, GitSha("a" * 40))
    after = SnapshotRef(repo, GitSha("b" * 40))
    request = inventory_request((item,), before, after, (), "deepseek-v4-flash")
    assert request.role is ModelRole.DIRECTION
    assert "files" not in request.schema["properties"]
    assert set(request.schema["required"]) == {
        "translation_required",
        "direction",
        "reason",
    }
