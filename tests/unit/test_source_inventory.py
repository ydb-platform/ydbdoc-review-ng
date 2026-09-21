from __future__ import annotations

import json
from dataclasses import replace

import pytest


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
    encoded = encode(inventory)
    assert json.loads(encoded) == [
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
        decode(raw)


def test_inventory_rejects_duplicate_paths_and_exceeding_existing_page_bound():
    normalize, _, _ = codec()
    with pytest.raises(ValueError):
        normalize([{"filename": "a.md", "status": "modified"}] * 2)
    with pytest.raises(ValueError):
        normalize([{"filename": f"{index}.md", "status": "modified"} for index in range(101)])


def test_inventory_bytes_are_bound_as_ydb_binary_string():
    from ydbdoc_review_ng.runtime_ydb import parameter_types

    assert parameter_types({"source_inventory": b"[]"}) == {"source_inventory": "String"}
