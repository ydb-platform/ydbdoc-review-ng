import pytest

from pr_translation_smoke.contract import (
    ContractError,
    build_field_map_schema,
    parse_and_validate_field_map,
)


def test_schema_closes_object_and_requires_every_requested_field():
    schema = build_field_map_schema(["field_001", "field_002"])

    assert schema == {
        "type": "object",
        "properties": {
            "field_001": {"type": "string", "pattern": "^[^<>]*$"},
            "field_002": {"type": "string", "pattern": "^[^<>]*$"},
        },
        "required": ["field_001", "field_002"],
        "additionalProperties": False,
    }


def test_schema_requires_source_references_in_their_original_order():
    schema = build_field_map_schema(
        ["field_001"],
        required_tokens={"field_001": ["<S10_2>", "<S20_3>"]},
    )

    assert schema["properties"]["field_001"] == {
        "type": "string",
        "pattern": "^[^<>]*<S10_2>[^<>]*<S20_3>[^<>]*$",
    }


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ('{"field_001":"one"}', "missing field IDs"),
        ('{"field_001":"one","field_002":"two","extra":"x"}', "unexpected field IDs"),
        ('{"field_001":"one","field_001":"again","field_002":"two"}', "duplicate JSON key"),
        ('{"field_001":"one","field_002":2}', "must be a string"),
    ],
)
def test_field_map_rejects_every_shape_violation(raw, message):
    with pytest.raises(ContractError, match=message):
        parse_and_validate_field_map(raw, ["field_001", "field_002"])


def test_field_map_accepts_exact_string_map():
    assert parse_and_validate_field_map(
        '{"field_001":"one","field_002":"two"}',
        ["field_001", "field_002"],
    ) == {"field_001": "one", "field_002": "two"}
