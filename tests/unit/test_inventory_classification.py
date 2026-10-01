import json

import pytest

from ydbdoc_review_ng import direction
from ydbdoc_review_ng.continuation import normalize_source_inventory


def response():
    return {
        "translation_required": True,
        "direction": "ru_to_en",
        "reason": "New page",
        "files": [
            {
                "path": "ru/new.md",
                "operation": "rename",
                "old_path": "ru/old.md",
                "new_path": "ru/new.md",
                "action": "page",
                "toc_delta": None,
            }
        ],
    }


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


@pytest.mark.parametrize(
    "defect",
    [
        "duplicate",
        "missing",
        "unknown",
        "operation",
        "old_path",
        "new_path",
        "extra",
        "row_extra",
        "boolean",
        "toc",
        "noop",
    ],
)
def test_classifier_parser_rejects_incomplete_or_rewritten_inventory(defect):
    value = response()
    row = value["files"][0]
    if defect == "duplicate":
        value["files"].append(dict(row))
    elif defect == "missing":
        value["files"] = []
    elif defect == "unknown":
        row["path"] = "ru/unknown.md"
    elif defect == "operation":
        row["operation"] = "add"
    elif defect in {"old_path", "new_path"}:
        row[defect] = "ru/invented.md"
    elif defect == "extra":
        value["bytes"] = "invented"
    elif defect == "row_extra":
        row["sha256"] = "invented"
    elif defect == "boolean":
        value["translation_required"] = 1
    elif defect == "toc":
        row["action"] = "toc_delta"
    elif defect == "noop":
        value["translation_required"] = False
    with pytest.raises(ValueError):
        direction.parse_inventory_response(json.dumps(value), inventory())


def test_classifier_parser_preserves_git_facts_and_toc_delta_meaning():
    value = response()
    value["files"][0].update(action="toc_delta", toc_delta="Rename the navigation entry.")
    original = inventory()
    classified = direction.parse_inventory_response(json.dumps(value), original)
    assert classified.direction is direction.Direction.RU_TO_EN
    assert classified.files[0].change is original.files[0]
    assert classified.files[0].toc_delta == "Rename the navigation entry."


def test_classifier_parser_rejects_duplicate_json_keys():
    raw = json.dumps(response()).replace(
        '"translation_required": true',
        '"translation_required": false, "translation_required": true',
    )
    with pytest.raises(ValueError):
        direction.parse_inventory_response(raw, inventory())
