"""Source-only placeholder restoration, assembly, and protected verification."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Mapping
from enum import Enum
from typing import TypeVar

import yaml  # type: ignore[import-untyped]

from ydbdoc_review_ng.parser.markdown import build_markdown_plan
from ydbdoc_review_ng.plan import (
    Block,
    BlockKind,
    Field,
    ProtectedKind,
    SourcePlan,
    fields_of,
    validate_source_plan,
)
from ydbdoc_review_ng.translation.contract import TranslationRequest, build_translation_request

_TOKEN = re.compile(r"\[\[[A-Z_]+_[0-9]{4}\]\]")
_PLACEHOLDER_LIKE = re.compile(r"\[\[[A-Z_]+.*?\]\]")
_OPEN = {ProtectedKind.LINK_OPEN, ProtectedKind.IMAGE_OPEN}
_CLOSE = {ProtectedKind.LINK_CLOSE, ProtectedKind.IMAGE_CLOSE}
_ProtectedEntry = tuple[str, bytes]
_ProtectedGroup = tuple[_ProtectedEntry, ...]
_ProtectedBlockSignature = tuple[
    tuple[_ProtectedEntry, ...],
    tuple[_ProtectedEntry, ...],
    tuple[_ProtectedGroup, ...],
]
_T = TypeVar("_T")


class AssemblyErrorReason(str, Enum):
    RESPONSE_FIELDS_MISMATCH = "response_fields_mismatch"
    PLACEHOLDER_MISMATCH = "placeholder_mismatch"
    BROKEN_CONTAINER_NESTING = "broken_container_nesting"
    CANDIDATE_REVALIDATION_FAILED = "candidate_revalidation_failed"


class AssemblyError(ValueError):
    def __init__(self, reason: AssemblyErrorReason, /) -> None:
        self.reason = reason
        super().__init__(f"translation_assembly:{reason.value}")


class ProtectedMismatch(ValueError):
    def __init__(self, field_position: int, /) -> None:
        self.field_position = field_position
        super().__init__(f"protected_fragment_mismatch:field_position={field_position}")


def placeholder_like_tokens(value: str, /) -> tuple[str, ...]:
    """Extract exactly the placeholder-like forms considered by strict validation."""
    return tuple(_PLACEHOLDER_LIKE.findall(value))


def _validated_value(value: str, request_field: object) -> bytes:
    from ydbdoc_review_ng.translation.contract import TranslationField

    assert type(request_field) is TranslationField
    expected = {item.token: item for item in request_field.placeholders}
    found = tuple(_TOKEN.findall(value))
    if Counter(found) != Counter(expected.keys()) or placeholder_like_tokens(value) != found:
        raise AssemblyError(AssemblyErrorReason.PLACEHOLDER_MISMATCH)
    stack: list[int] = []
    for token in found:
        placeholder = expected[token]
        if placeholder.kind in _OPEN:
            assert placeholder.group is not None
            stack.append(placeholder.group)
        elif placeholder.kind in _CLOSE:
            if not stack or stack.pop() != placeholder.group:
                raise AssemblyError(AssemblyErrorReason.BROKEN_CONTAINER_NESTING)
    if stack:
        raise AssemblyError(AssemblyErrorReason.BROKEN_CONTAINER_NESTING)
    restored = value
    for token, placeholder in expected.items():
        restored = restored.replace(token, placeholder.source_bytes.decode("utf-8"))
    return restored.encode("utf-8")


def validate_translation_values(
    request: TranslationRequest, translations: dict[str, str], /
) -> None:
    """Validate an exact response map and every field-local protected placeholder."""
    if type(request) is not TranslationRequest:
        raise AssemblyError(AssemblyErrorReason.RESPONSE_FIELDS_MISMATCH)
    if type(translations) is not dict or set(translations) != set(request.requested_ids):
        raise AssemblyError(AssemblyErrorReason.RESPONSE_FIELDS_MISMATCH)
    if any(type(key) is not str or type(value) is not str for key, value in translations.items()):
        raise AssemblyError(AssemblyErrorReason.RESPONSE_FIELDS_MISMATCH)
    for item in request.fields:
        _validated_value(translations[item.field_id], item)


def _protected_signature(data: bytes, plan: SourcePlan, position: int) -> tuple[object, ...]:
    field = fields_of(plan)[position]
    ordered_singles: list[tuple[str, bytes]] = []
    movable_singles: list[tuple[str, bytes]] = []
    groups: dict[int, list[tuple[str, bytes]]] = {}
    for region in field.protected_regions:
        entry = (region.kind.value, data[region.span.start : region.span.end])
        if region.group is None:
            if region.kind in {ProtectedKind.INLINE_CODE, ProtectedKind.TEMPLATE}:
                movable_singles.append(entry)
            else:
                ordered_singles.append(entry)
        else:
            groups.setdefault(region.group, []).append(entry)
    return (
        tuple(ordered_singles),
        tuple(sorted(movable_singles)),
        tuple(tuple(items) for items in groups.values()),
    )


def _block_protected_signature(data: bytes, block: Block) -> _ProtectedBlockSignature:
    ordered_singles: list[tuple[str, bytes]] = []
    movable_singles: list[tuple[str, bytes]] = []
    groups: dict[int, list[tuple[str, bytes]]] = {}
    regions = sorted(
        (region for field in block.fields for region in field.protected_regions),
        key=lambda region: region.span.start,
    )
    for region in regions:
        entry = (region.kind.value, data[region.span.start : region.span.end])
        if region.group is None:
            if region.kind in {ProtectedKind.INLINE_CODE, ProtectedKind.TEMPLATE}:
                movable_singles.append(entry)
            else:
                ordered_singles.append(entry)
        else:
            groups.setdefault(region.group, []).append(entry)
    return (
        tuple(ordered_singles),
        tuple(sorted(movable_singles)),
        tuple(tuple(items) for items in groups.values()),
    )


def _is_subsequence(required: tuple[_T, ...], actual: tuple[_T, ...]) -> bool:
    position = 0
    for item in actual:
        if position < len(required) and item == required[position]:
            position += 1
    return position == len(required)


def _non_field_slices(data: bytes, plan: SourcePlan) -> tuple[bytes, ...]:
    slices: list[bytes] = []
    cursor = 0
    for field in fields_of(plan):
        slices.append(data[cursor : field.span.start])
        cursor = field.span.end
    slices.append(data[cursor:])
    return tuple(slices)


def _validate_frontmatter_yaml(data: bytes, plan: SourcePlan) -> None:
    for block in plan.blocks:
        if block.kind is not BlockKind.T008_FRONT_MATTER:
            continue
        lines = data[block.span.start : block.span.end].splitlines(keepends=True)
        if len(lines) < 3:
            raise ValueError("invalid frontmatter")
        payload = b"".join(lines[1:-1]).decode("utf-8")
        loaded = yaml.safe_load(payload)
        if not isinstance(loaded, Mapping):
            raise TypeError("invalid frontmatter")


def _frontmatter_keys(data: bytes, plan: SourcePlan) -> tuple[tuple[object, ...], ...]:
    result: list[tuple[object, ...]] = []
    for block in plan.blocks:
        if block.kind is not BlockKind.T008_FRONT_MATTER:
            continue
        lines = data[block.span.start : block.span.end].splitlines(keepends=True)
        loaded = yaml.compose(b"".join(lines[1:-1]).decode("utf-8"))
        if not isinstance(loaded, yaml.MappingNode):
            raise TypeError("invalid frontmatter")
        result.append(tuple(key.value for key, _value in loaded.value))
    return tuple(result)


def _is_frontmatter_field(plan: SourcePlan, field: Field) -> bool:
    return any(
        block.kind is BlockKind.T008_FRONT_MATTER
        and block.span.start <= field.span.start
        and field.span.end <= block.span.end
        for block in plan.blocks
    )


def _frontmatter_style(source: bytes, field: Field) -> str:
    before = source[field.span.start - 1] if field.span.start else None
    after = source[field.span.end] if field.span.end < len(source) else None
    if before == after == 34:
        return "double"
    if before == after == 39:
        return "single"
    line_start = source.rfind(b"\n", 0, field.span.start) + 1
    prefix = source[line_start : field.span.start]
    if prefix and not prefix.strip(b" \t"):
        return "block"
    return "plain"


def _frontmatter_value(source: bytes, field: Field, restored: bytes) -> bytes:
    style = _frontmatter_style(source, field)
    text = restored.decode("utf-8")
    if style == "double":
        return json.dumps(text, ensure_ascii=False)[1:-1].encode("utf-8")
    if style == "single":
        return text.replace("'", "''").encode("utf-8")
    if style == "block":
        return restored
    try:
        loaded = yaml.safe_load("value: " + text + "\n")
    except yaml.YAMLError:
        loaded = None
    if isinstance(loaded, Mapping) and type(loaded.get("value")) is str and loaded["value"] == text:
        return restored
    return json.dumps(text, ensure_ascii=False).encode("utf-8")


def _normalize_encoded_frontmatter_slices(
    source: bytes,
    source_plan: SourcePlan,
    target: bytes,
    target_plan: SourcePlan,
    target_slices: list[bytes],
) -> None:
    for position, (source_field, target_field) in enumerate(
        zip(fields_of(source_plan), fields_of(target_plan), strict=True)
    ):
        if (
            _is_frontmatter_field(source_plan, source_field)
            and _frontmatter_style(source, source_field) == "plain"
            and _is_frontmatter_field(target_plan, target_field)
            and _frontmatter_style(target, target_field) == "double"
            and target_slices[position].endswith(b'"')
            and target_slices[position + 1].startswith(b'"')
        ):
            target_slices[position] = target_slices[position][:-1]
            target_slices[position + 1] = target_slices[position + 1][1:]


def verify_protected_fragments(
    source: bytes,
    source_plan: SourcePlan,
    target: bytes,
    target_plan: SourcePlan,
    /,
    *,
    exact_non_field_slices: bool = True,
) -> None:
    """Compare protected fragments by field position and kind/container contract."""
    if type(exact_non_field_slices) is not bool:
        raise TypeError("exact_non_field_slices must be a boolean")
    validate_source_plan(source, source_plan)
    validate_source_plan(target, target_plan)
    _validate_frontmatter_yaml(source, source_plan)
    _validate_frontmatter_yaml(target, target_plan)
    if not exact_non_field_slices and _frontmatter_keys(
        source, source_plan
    ) != _frontmatter_keys(target, target_plan):
        raise ProtectedMismatch(1)
    source_fields = fields_of(source_plan)
    target_fields = fields_of(target_plan)
    if exact_non_field_slices and len(source_fields) != len(target_fields):
        raise ProtectedMismatch(min(len(source_fields), len(target_fields)) + 1)
    if exact_non_field_slices:
        source_slices = _non_field_slices(source, source_plan)
        target_slices = list(_non_field_slices(target, target_plan))
        _normalize_encoded_frontmatter_slices(
            source, source_plan, target, target_plan, target_slices
        )
        if source_slices != tuple(target_slices):
            mismatch = next(
                position
                for position, (source_slice, target_slice) in enumerate(
                    zip(source_slices, target_slices, strict=True), 1
                )
                if source_slice != target_slice
            )
            raise ProtectedMismatch(mismatch)
    if exact_non_field_slices:
        for position in range(len(source_fields)):
            if _protected_signature(source, source_plan, position) != _protected_signature(
                target, target_plan, position
            ):
                raise ProtectedMismatch(position + 1)
        return

    source_signatures = tuple(
        _block_protected_signature(source, block) for block in source_plan.blocks
    )
    target_signatures = tuple(
        _block_protected_signature(target, block) for block in target_plan.blocks
    )
    source_ordered = tuple(item for ordered, _, _ in source_signatures for item in ordered)
    target_ordered = tuple(item for ordered, _, _ in target_signatures for item in ordered)
    source_movable = tuple(item for _, movable, _ in source_signatures for item in movable)
    target_movable = tuple(item for _, movable, _ in target_signatures for item in movable)
    source_groups = tuple(item for _, _, groups in source_signatures for item in groups)
    target_groups = tuple(item for _, _, groups in target_signatures for item in groups)
    allowed_target_only = {
        ProtectedKind.ESCAPE.value,
        ProtectedKind.LINE_BREAK.value,
        ProtectedKind.CONTINUATION_PREFIX.value,
    }
    source_strict = tuple(item for item in source_ordered if item[0] not in allowed_target_only)
    target_strict = tuple(item for item in target_ordered if item[0] not in allowed_target_only)
    if (
        not _is_subsequence(source_ordered, target_ordered)
        or Counter(source_strict) != Counter(target_strict)
        or Counter(source_movable) != Counter(target_movable)
        or Counter(source_groups) != Counter(target_groups)
    ):
        raise ProtectedMismatch(1)


def assemble_candidate(
    source: bytes,
    plan: SourcePlan,
    request: TranslationRequest,
    translations: dict[str, str],
    /,
) -> bytes:
    """Assemble exclusively from authoritative source and validated translated fields."""
    validate_source_plan(source, plan)
    if type(request) is not TranslationRequest or request != build_translation_request(
        source, plan
    ):
        raise AssemblyError(AssemblyErrorReason.RESPONSE_FIELDS_MISMATCH)
    validate_translation_values(request, translations)
    frontmatter_ids = {
        field.field_id.value
        for block in plan.blocks
        if block.kind is BlockKind.T008_FRONT_MATTER
        for field in block.fields
    }
    source_fields = {field.field_id.value: field for field in fields_of(plan)}
    replacements = {}
    for item in request.fields:
        restored = _validated_value(translations[item.field_id], item)
        if item.field_id in frontmatter_ids:
            restored = _frontmatter_value(source, source_fields[item.field_id], restored)
        replacements[item.field_id] = restored
    result: list[bytes] = []
    cursor = 0
    for field in fields_of(plan):
        result.append(source[cursor : field.span.start])
        result.append(replacements[field.field_id.value])
        cursor = field.span.end
    result.append(source[cursor:])
    candidate = b"".join(result)
    try:
        candidate_plan = build_markdown_plan(plan.source_snapshot, plan.source_path, candidate)
        if candidate_plan.diagnostics:
            raise ValueError
        verify_protected_fragments(source, plan, candidate, candidate_plan)
    except (UnicodeError, ValueError, TypeError, yaml.YAMLError) as error:
        if isinstance(error, AssemblyError):
            raise
        raise AssemblyError(AssemblyErrorReason.CANDIDATE_REVALIDATION_FAILED) from None
    return candidate
