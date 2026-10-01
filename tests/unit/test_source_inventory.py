from __future__ import annotations

import json
from dataclasses import replace

import pytest

from ydbdoc_review_ng.continuation import SourceSemanticAction
from ydbdoc_review_ng.domain import GitSha


def frozen(inventory):
    return replace(
        inventory,
        source_base_sha=GitSha("a" * 40),
        source_head_sha=GitSha("b" * 40),
        semantic_actions=tuple(
            SourceSemanticAction(item.path, item.operation, "none", None)
            for item in inventory.files
        ),
    )


def codec():
    from ydbdoc_review_ng.continuation import (
        decode_source_inventory,
        encode_source_inventory,
        normalize_source_inventory,
    )

    return normalize_source_inventory, encode_source_inventory, decode_source_inventory


def test_normalized_inventory_roundtrip_keeps_only_consumed_facts():
    normalize, encode, decode = codec()
    inventory = normalize(
        [
            {
                "filename": "ru/new.md",
                "status": "renamed",
                "previous_filename": "ru/old.md",
                "changes": 0,
                "patch": "private bytes",
                "sha": "irrelevant",
            },
            {"filename": "ru/toc.yaml", "status": "modified", "patch": "private bytes"},
        ]
    )
    inventory = frozen(inventory)
    encoded = encode(inventory)
    assert json.loads(encoded)["files"] == [
        {
            "path": "ru/new.md",
            "status": "renamed",
            "previous_path": "ru/old.md",
            "rename_changed": False,
        },
        {
            "path": "ru/toc.yaml",
            "status": "modified",
            "previous_path": None,
            "rename_changed": None,
        },
    ]
    assert decode(encoded) == inventory
    assert "private" not in encoded
    assert replace(inventory, files=inventory.files) == inventory


@pytest.mark.parametrize(
    "raw",
    [
        '[{"path":"a.md","status":"modified","previous_path":null,"rename_changed":null,"patch":"secret"}]',
        '[{"path":"a.md","path":"b.md","status":"modified","previous_path":null,"rename_changed":null}]',
        '[{"path":"a.md","status":"renamed","previous_path":null,"rename_changed":false}]',
        '[{"path":"../a.md","status":"modified","previous_path":null,"rename_changed":null}]',
        '[{"path":"a.md","status":"modified","previous_path":null,"rename_changed":0}]',
        "{}",
    ],
)
def test_inventory_rejects_unknown_duplicate_or_invalid_facts(raw):
    _, _, decode = codec()
    with pytest.raises(ValueError):
        decode(
            '{"source_base_sha":null,"source_head_sha":null,"semantic_actions":[],"files":'
            + raw
            + "}"
        )


def test_inventory_rejects_duplicate_paths_and_preserves_more_than_one_api_page():
    normalize, _, _ = codec()
    with pytest.raises(ValueError):
        normalize([{"filename": "a.md", "status": "modified"}] * 2)
    assert (
        len(
            normalize(
                [{"filename": f"{index}.md", "status": "modified"} for index in range(101)]
            ).files
        )
        == 101
    )


def test_inventory_rejects_legacy_list_format():
    _, _, decode = codec()
    with pytest.raises(ValueError):
        decode("[]")


def test_inventory_bytes_are_bound_as_ydb_binary_string():
    from ydbdoc_review_ng.runtime_ydb import parameter_types

    assert parameter_types({"source_inventory": b"[]"}) == {"source_inventory": "String"}


def test_inventory_codec_preserves_immutable_diff_snapshots():
    normalize, encode, decode = codec()
    original = frozen(normalize([{"filename": "ru/a.md", "status": "modified"}]))
    wire = json.loads(encode(original))
    assert wire["source_base_sha"] == "a" * 40 and wire["source_head_sha"] == "b" * 40
    assert wire["files"][0]["path"] == "ru/a.md"
    assert decode(encode(original)) == original


@pytest.mark.parametrize(
    "defect", ["missing", "extra", "invalid", "half_pinned", "unpinned", "duplicate"]
)
def test_inventory_codec_rejects_invalid_diff_provenance(defect):
    _, _, decode = codec()
    wire = {
        "files": [],
        "semantic_actions": [],
        "source_base_sha": "a" * 40,
        "source_head_sha": "b" * 40,
    }
    if defect == "missing":
        wire.pop("source_base_sha")
    elif defect == "extra":
        wire["moving_branch"] = "main"
    elif defect == "invalid":
        wire["source_base_sha"] = "main"
    elif defect == "half_pinned":
        wire["source_base_sha"] = None
    elif defect == "unpinned":
        wire["source_base_sha"] = wire["source_head_sha"] = None
    raw = json.dumps(wire)
    if defect == "duplicate":
        raw = raw.replace('"files": []', '"files": [], "files": []')
    with pytest.raises(ValueError):
        decode(raw)


def semantic_envelope():
    return {
        "source_base_sha": "a" * 40,
        "source_head_sha": "b" * 40,
        "files": [
            {
                "path": "ru/toc.yaml",
                "status": "modified",
                "previous_path": None,
                "rename_changed": None,
            }
        ],
        "semantic_actions": [
            {
                "path": "ru/toc.yaml",
                "operation": "modify",
                "action": "toc_delta",
                "toc_delta": "Add exact new entry.",
            }
        ],
    }


def test_inventory_codec_roundtrips_exact_frozen_semantic_actions():
    _, encode, decode = codec()
    wire = semantic_envelope()
    assert json.loads(encode(decode(json.dumps(wire)))) == wire


@pytest.mark.parametrize(
    "defect",
    ["missing", "empty", "duplicate", "unknown_path", "operation", "action", "toc_delta", "extra"],
)
def test_inventory_codec_requires_exact_semantic_actions(defect):
    _, _, decode = codec()
    wire = semantic_envelope()
    action = wire["semantic_actions"][0]
    if defect == "missing":
        wire.pop("semantic_actions")
    elif defect == "empty":
        wire["semantic_actions"] = []
    elif defect == "duplicate":
        wire["semantic_actions"].append(dict(action))
    elif defect == "unknown_path":
        action["path"] = "ru/other.yaml"
    elif defect == "operation":
        action["operation"] = "add"
    elif defect == "action":
        action["action"] = "unknown"
    elif defect == "toc_delta":
        action["toc_delta"] = None
    else:
        action["old_path"] = "ru/toc.yaml"
    with pytest.raises(ValueError):
        decode(json.dumps(wire))
