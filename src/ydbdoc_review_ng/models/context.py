"""Exact UTF-8 byte accounting for complete model wire requests."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

CONTEXT_WINDOW = 1_048_576


@dataclass(frozen=True, slots=True)
class ContextBudget:
    body: bytes = field(repr=False)
    input_tokens: int
    max_tokens: int


def serialize_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def calculate_context_budget(serialize_request: Callable[[int], bytes]) -> ContextBudget:
    """Resolve max_tokens using the same complete serialization sent to transport.

    At decimal digit boundaries compact JSON can have no fixed point: reducing
    max_tokens by one removes a digit and restores one byte of context. One
    trailing JSON whitespace byte resolves that case and is counted on the wire.
    """
    for digits in range(len(str(CONTEXT_WINDOW)), 0, -1):
        lower = 10 ** (digits - 1)
        max_tokens = CONTEXT_WINDOW - len(serialize_request(lower))
        if max_tokens < 1:
            continue
        if lower <= max_tokens < lower * 10:
            body = serialize_request(max_tokens)
        elif max_tokens == lower - 1:
            body = serialize_request(max_tokens) + b" "
        else:
            continue
        return ContextBudget(body, len(body), max_tokens)
    raise ValueError("model context cannot fit the complete wire request")
