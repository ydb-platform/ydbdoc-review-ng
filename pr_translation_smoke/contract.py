import json
import re
from collections.abc import Iterable
from typing import Any


class ContractError(ValueError):
    """The model response cannot be used to build a candidate."""


def build_field_map_schema(
    field_ids: Iterable[str],
    *,
    required_tokens: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    ordered_ids = list(field_ids)
    if len(ordered_ids) != len(set(ordered_ids)):
        raise ContractError("requested field IDs must be unique")
    token_map = required_tokens or {}
    properties: dict[str, dict[str, Any]] = {}
    for field_id in ordered_ids:
        definition: dict[str, Any] = {"type": "string"}
        tokens = token_map.get(field_id, [])
        if tokens:
            separator = "[^<>]*"
            sequence = separator.join(re.escape(token) for token in tokens)
            definition["pattern"] = f"^{separator}{sequence}{separator}$"
        else:
            definition["pattern"] = "^[^<>]*$"
        properties[field_id] = definition
    return {
        "type": "object",
        "properties": properties,
        "required": ordered_ids,
        "additionalProperties": False,
    }


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_and_validate_field_map(raw: str, field_ids: Iterable[str]) -> dict[str, str]:
    requested = list(field_ids)
    try:
        parsed = json.loads(raw, object_pairs_hook=_reject_duplicate_pairs)
    except ContractError:
        raise
    except json.JSONDecodeError as error:
        raise ContractError(f"invalid JSON: {error.msg}") from error

    if not isinstance(parsed, dict):
        raise ContractError("response root must be an object")

    requested_set = set(requested)
    actual_set = set(parsed)
    missing = requested_set - actual_set
    extra = actual_set - requested_set
    if missing:
        raise ContractError(f"missing field IDs: {sorted(missing)}")
    if extra:
        raise ContractError(f"unexpected field IDs: {sorted(extra)}")
    for field_id, value in parsed.items():
        if not isinstance(value, str):
            raise ContractError(f"value for {field_id} must be a string")
    return parsed
