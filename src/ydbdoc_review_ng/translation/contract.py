"""Strict provider-facing translation request and response contract."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum

import yaml  # type: ignore[import-untyped]

from ydbdoc_review_ng.plan import (
    BlockKind,
    Field,
    ProtectedKind,
    SourcePlan,
    fields_of,
    validate_source_plan,
)


@dataclass(frozen=True, slots=True)
class Placeholder:
    token: str
    source_bytes: bytes
    kind: ProtectedKind
    group: int | None


@dataclass(frozen=True, slots=True)
class TranslationField:
    field_id: str
    text: str
    placeholders: tuple[Placeholder, ...]


@dataclass(frozen=True, slots=True)
class TranslationRequest:
    requested_ids: tuple[str, ...]
    fields: tuple[TranslationField, ...]


class ResponseErrorReason(str, Enum):
    MALFORMED_JSON = "malformed_json"
    ROOT_NOT_OBJECT = "root_not_object"
    DUPLICATE_FIELD_ID = "duplicate_field_id"
    FIELD_IDS_MISMATCH = "field_ids_mismatch"
    NON_STRING_VALUE = "non_string_value"


class ResponseError(ValueError):
    def __init__(self, reason: ResponseErrorReason, /) -> None:
        self.reason = reason
        super().__init__(f"translation_response:{reason.value}")


class _ObjectPairs(list[tuple[object, object]]):
    pass


def _logical_frontmatter_text(
    source: bytes, plan: SourcePlan, field: Field, encoded: bytes
) -> str | None:
    if not any(
        block.kind is BlockKind.T008_FRONT_MATTER
        and block.span.start <= field.span.start
        and field.span.end <= block.span.end
        for block in plan.blocks
    ):
        return None
    before = source[field.span.start - 1] if field.span.start else None
    after = source[field.span.end] if field.span.end < len(source) else None
    line_start = source.rfind(b"\n", 0, field.span.start) + 1
    prefix = source[line_start : field.span.start]
    block_scalar = bool(prefix) and not prefix.strip(b" \t")
    if before == after and before in (34, 39):
        quote = bytes((before,))
        scalar = quote + encoded + quote
    elif not block_scalar and (
        b"\n" in source[field.span.start : field.span.end]
        or b"\r" in source[field.span.start : field.span.end]
    ):
        scalar = encoded
    else:
        return None
    loaded = yaml.safe_load(b"value: " + scalar + b"\n")
    if type(loaded) is not dict or type(loaded.get("value")) is not str:
        return None
    return str(loaded["value"])


def field_request_text(source: bytes, plan: SourcePlan, field: Field, encoded: bytes) -> str:
    """Return the provider representation for an already placeholderized field."""
    logical = _logical_frontmatter_text(source, plan, field, encoded)
    return encoded.decode("utf-8") if logical is None else logical


def build_translation_request(source: bytes, plan: SourcePlan, /) -> TranslationRequest:
    """Replace each protected source region with a deterministic field-local token."""
    if type(source) is not bytes or type(plan) is not SourcePlan:
        raise TypeError("source and plan must have exact public contract types")
    validate_source_plan(source, plan)
    request_fields: list[TranslationField] = []
    for field in fields_of(plan):
        chunks: list[bytes] = []
        placeholders: list[Placeholder] = []
        cursor = field.span.start
        field_source = source[field.span.start : field.span.end]
        used_tokens: set[bytes] = set()
        next_token = 1
        for region in field.protected_regions:
            while True:
                token = f"[[{region.kind.value.upper()}_{next_token:04d}]]"
                next_token += 1
                encoded_token = token.encode("ascii")
                if encoded_token not in field_source and encoded_token not in used_tokens:
                    used_tokens.add(encoded_token)
                    break
            chunks.append(source[cursor : region.span.start])
            chunks.append(encoded_token)
            protected = source[region.span.start : region.span.end]
            placeholders.append(Placeholder(token, protected, region.kind, region.group))
            cursor = region.span.end
        chunks.append(source[cursor : field.span.end])
        encoded = b"".join(chunks)
        request_fields.append(
            TranslationField(
                field.field_id.value,
                field_request_text(source, plan, field, encoded),
                tuple(placeholders),
            )
        )
    frozen_fields = tuple(request_fields)
    return TranslationRequest(tuple(item.field_id for item in frozen_fields), frozen_fields)


def parse_translation_response(raw: str | bytes, request: TranslationRequest, /) -> dict[str, str]:
    """Parse JSON without permitting duplicate keys or provider schema drift."""
    if type(raw) not in {str, bytes} or type(request) is not TranslationRequest:
        raise TypeError("raw and request must have exact public contract types")
    try:
        value = json.loads(raw, object_pairs_hook=_ObjectPairs)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ResponseError(ResponseErrorReason.MALFORMED_JSON) from None
    if type(value) is not _ObjectPairs:
        raise ResponseError(ResponseErrorReason.ROOT_NOT_OBJECT)
    pairs = value
    keys: list[str] = []
    for key, _item in pairs:
        if type(key) is not str:
            raise ResponseError(ResponseErrorReason.FIELD_IDS_MISMATCH)
        keys.append(key)
    if len(keys) != len(set(keys)):
        raise ResponseError(ResponseErrorReason.DUPLICATE_FIELD_ID)
    if set(keys) != set(request.requested_ids) or len(keys) != len(request.requested_ids):
        raise ResponseError(ResponseErrorReason.FIELD_IDS_MISMATCH)
    if any(type(item) is not str for _key, item in pairs):
        raise ResponseError(ResponseErrorReason.NON_STRING_VALUE)
    return {key: item for key, item in pairs}  # type: ignore[misc]
