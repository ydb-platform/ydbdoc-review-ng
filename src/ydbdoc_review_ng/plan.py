"""Frozen, byte-addressed source-plan vocabulary and wire schema."""

from __future__ import annotations

import hashlib
import re
from bisect import bisect_right
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import NoReturn, Protocol, cast

from ydbdoc_review_ng.domain import (
    ContentHash,
    Diagnostic,
    GitSha,
    JsonValue,
    RepoPath,
    RepositoryId,
    Severity,
    SnapshotRef,
)
from ydbdoc_review_ng.errors import InvariantViolation

__all__ = [
    "PLAN_VERSION",
    "Block",
    "BlockKind",
    "ByteSpan",
    "Field",
    "FieldId",
    "FieldKind",
    "InvalidPlanInput",
    "LineRange",
    "MalformedPlanPayload",
    "PlanDiagnostic",
    "PlanError",
    "PlanInputReason",
    "PlanSerializationError",
    "ProtectedKind",
    "ProtectedRegion",
    "SourcePlan",
    "UnsupportedPlanVersion",
    "fields_of",
    "make_field_id",
    "plan_from_wire",
    "plan_to_wire",
    "render_identity",
    "validate_source_plan",
]

PLAN_VERSION = 1
_UINT64_MAX = 2**64 - 1
_FIELD_ID = re.compile(r"f1-[0-9]{6}-[0-9a-f]{64}\Z")


class BlockKind(str, Enum):
    UTF8_BOM = "utf8_bom"
    BLANK = "blank"
    THEMATIC_BREAK = "thematic_break"
    INDENTED_CODE = "indented_code"
    REFERENCE_DEFINITION = "reference_definition"
    ATX_HEADING = "atx_heading"
    SETEXT_HEADING = "setext_heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    TABLE = "table"
    T008_FRONT_MATTER = "t008_front_matter"
    T008_YFM = "t008_yfm"
    T008_FENCE = "t008_fence"
    T008_HTML = "t008_html"
    UNKNOWN = "unknown"


class FieldKind(str, Enum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    TABLE_CELL = "table_cell"


class ProtectedKind(str, Enum):
    MARKDOWN_SYNTAX = "markdown_syntax"
    INLINE_CODE = "inline_code"
    LINK_OPEN = "link_open"
    LINK_CLOSE = "link_close"
    IMAGE_OPEN = "image_open"
    IMAGE_CLOSE = "image_close"
    AUTOLINK = "autolink"
    URL = "url"
    PATH = "path"
    IDENTIFIER = "identifier"
    TEMPLATE = "template"
    HTML_INLINE = "html_inline"
    EXPLICIT_ANCHOR = "explicit_anchor"
    ESCAPE = "escape"
    LINE_BREAK = "line_break"
    CONTINUATION_PREFIX = "continuation_prefix"


class PlanInputReason(str, Enum):
    INVALID_UTF8_SOURCE = "invalid_utf8_source"
    SOURCE_SIZE_MISMATCH = "source_size_mismatch"
    SOURCE_HASH_MISMATCH = "source_hash_mismatch"
    FIELD_ID_COLLISION = "field_id_collision"


def _invariant(owner: str, name: str, expected: str) -> InvariantViolation:
    return InvariantViolation(f"{owner}.{name}: expected {expected}")


def _exact(value: object, expected: type[object], owner: str, name: str) -> None:
    if type(value) is not expected:
        raise _invariant(owner, name, f"exact {expected.__name__}")


def _optional(value: object, expected: type[object], owner: str, name: str) -> None:
    if value is not None and type(value) is not expected:
        raise _invariant(owner, name, f"None or exact {expected.__name__}")


def _tuple_of(value: object, expected: type[object], owner: str, name: str) -> None:
    _exact(value, tuple, owner, name)
    for item in cast(tuple[object, ...], value):
        _exact(item, expected, owner, name)


@dataclass(frozen=True, slots=True)
class ByteSpan:
    start: int
    end: int

    def __post_init__(self) -> None:
        _exact(self.start, int, "ByteSpan", "start")
        _exact(self.end, int, "ByteSpan", "end")
        if self.start < 0 or self.start > self.end:
            raise _invariant("ByteSpan", "start", "0 <= start <= end")


@dataclass(frozen=True, slots=True)
class LineRange:
    start: int
    end: int

    def __post_init__(self) -> None:
        _exact(self.start, int, "LineRange", "start")
        _exact(self.end, int, "LineRange", "end")
        if self.start < 1 or self.start > self.end:
            raise _invariant("LineRange", "start", "1 <= start <= end")


@dataclass(frozen=True, slots=True)
class FieldId:
    value: str

    def __post_init__(self) -> None:
        _exact(self.value, str, "FieldId", "value")
        if _FIELD_ID.fullmatch(self.value) is None:
            raise _invariant("FieldId", "value", "f1-NNNNNN- plus 64 lowercase hex characters")


@dataclass(frozen=True, slots=True)
class ProtectedRegion:
    kind: ProtectedKind
    span: ByteSpan
    group: int | None

    def __post_init__(self) -> None:
        _exact(self.kind, ProtectedKind, "ProtectedRegion", "kind")
        _exact(self.span, ByteSpan, "ProtectedRegion", "span")
        _optional(self.group, int, "ProtectedRegion", "group")
        if self.span.start >= self.span.end:
            raise _invariant("ProtectedRegion", "span", "a nonempty span")
        is_container = self.kind in {
            ProtectedKind.LINK_OPEN,
            ProtectedKind.LINK_CLOSE,
            ProtectedKind.IMAGE_OPEN,
            ProtectedKind.IMAGE_CLOSE,
        }
        if is_container:
            if type(self.group) is not int or self.group <= 0:
                raise _invariant("ProtectedRegion", "group", "a positive exact int for containers")
        elif self.group is not None:
            raise _invariant("ProtectedRegion", "group", "None for non-containers")


@dataclass(frozen=True, slots=True)
class Field:
    field_id: FieldId
    kind: FieldKind
    span: ByteSpan
    lines: LineRange
    protected_regions: tuple[ProtectedRegion, ...]

    def __post_init__(self) -> None:
        _exact(self.field_id, FieldId, "Field", "field_id")
        _exact(self.kind, FieldKind, "Field", "kind")
        _exact(self.span, ByteSpan, "Field", "span")
        _exact(self.lines, LineRange, "Field", "lines")
        _tuple_of(self.protected_regions, ProtectedRegion, "Field", "protected_regions")
        if self.span.start >= self.span.end:
            raise _invariant("Field", "span", "a nonempty span")
        cursor = self.span.start
        groups: dict[int, list[ProtectedKind]] = {}
        for region in self.protected_regions:
            if region.span.start < cursor or region.span.end > self.span.end:
                raise _invariant(
                    "Field", "protected_regions", "ordered non-overlapping contained regions"
                )
            cursor = region.span.end
            if region.group is not None:
                groups.setdefault(region.group, []).append(region.kind)
        pairs = {
            (ProtectedKind.LINK_OPEN, ProtectedKind.LINK_CLOSE),
            (ProtectedKind.IMAGE_OPEN, ProtectedKind.IMAGE_CLOSE),
        }
        if any(tuple(kinds) not in pairs for kinds in groups.values()):
            raise _invariant("Field", "protected_regions", "one matching open/close pair per group")


@dataclass(frozen=True, slots=True)
class Block:
    kind: BlockKind
    span: ByteSpan
    lines: LineRange
    fields: tuple[Field, ...]

    def __post_init__(self) -> None:
        _exact(self.kind, BlockKind, "Block", "kind")
        _exact(self.span, ByteSpan, "Block", "span")
        _exact(self.lines, LineRange, "Block", "lines")
        _tuple_of(self.fields, Field, "Block", "fields")
        if self.span.start >= self.span.end:
            raise _invariant("Block", "span", "a nonempty span")
        fieldless = {
            BlockKind.UTF8_BOM,
            BlockKind.BLANK,
            BlockKind.THEMATIC_BREAK,
            BlockKind.INDENTED_CODE,
            BlockKind.REFERENCE_DEFINITION,
            BlockKind.T008_HTML,
            BlockKind.UNKNOWN,
        }
        if self.kind in fieldless and self.fields:
            raise _invariant("Block", "fields", "empty fields for protected or unknown block")
        cursor = self.span.start
        for item in self.fields:
            if item.span.start < cursor or item.span.end > self.span.end:
                raise _invariant("Block", "fields", "ordered non-overlapping contained fields")
            cursor = item.span.end


@dataclass(frozen=True, slots=True)
class PlanDiagnostic:
    diagnostic: Diagnostic = field(repr=False)
    span: ByteSpan

    def __post_init__(self) -> None:
        _exact(self.diagnostic, Diagnostic, "PlanDiagnostic", "diagnostic")
        _exact(self.span, ByteSpan, "PlanDiagnostic", "span")
        if self.span.start >= self.span.end:
            raise _invariant("PlanDiagnostic", "span", "a nonempty span")


@dataclass(frozen=True, slots=True)
class SourcePlan:
    plan_version: int
    source_snapshot: SnapshotRef
    source_path: RepoPath
    source_sha256: ContentHash
    byte_length: int
    line_count: int
    blocks: tuple[Block, ...]
    diagnostics: tuple[PlanDiagnostic, ...]

    def __post_init__(self) -> None:
        _exact(self.plan_version, int, "SourcePlan", "plan_version")
        _exact(self.source_snapshot, SnapshotRef, "SourcePlan", "source_snapshot")
        _exact(self.source_path, RepoPath, "SourcePlan", "source_path")
        _exact(self.source_sha256, ContentHash, "SourcePlan", "source_sha256")
        _exact(self.byte_length, int, "SourcePlan", "byte_length")
        _exact(self.line_count, int, "SourcePlan", "line_count")
        _tuple_of(self.blocks, Block, "SourcePlan", "blocks")
        _tuple_of(self.diagnostics, PlanDiagnostic, "SourcePlan", "diagnostics")
        if self.plan_version != PLAN_VERSION:
            raise _invariant("SourcePlan", "plan_version", "PLAN_VERSION")
        if self.byte_length < 0:
            raise _invariant("SourcePlan", "byte_length", "a nonnegative exact int")
        if self.line_count < 0:
            raise _invariant("SourcePlan", "line_count", "a nonnegative exact int")
        if self.byte_length == 0:
            if self.line_count != 0 or self.blocks or self.diagnostics:
                raise _invariant("SourcePlan", "blocks", "empty source shape")
            return
        if self.line_count < 1:
            raise _invariant("SourcePlan", "line_count", "at least one for nonempty source")
        if not self.blocks or self.blocks[0].span.start != 0:
            raise _invariant("SourcePlan", "blocks", "complete coverage from byte 0")
        for left, right in zip(self.blocks, self.blocks[1:], strict=False):
            if left.span.end != right.span.start:
                raise _invariant(
                    "SourcePlan", "blocks", "adjacent ordered non-overlapping coverage"
                )
        if self.blocks[-1].span.end != self.byte_length:
            raise _invariant("SourcePlan", "blocks", "final end == byte_length")
        if self.blocks[-1].lines.end != self.line_count:
            raise _invariant("SourcePlan", "line_count", "line count consistent with plan shape")
        flattened = tuple(item for block in self.blocks for item in block.fields)
        if len({item.field_id for item in flattened}) != len(flattened):
            raise _invariant("SourcePlan", "blocks", "unique field IDs")
        digests: dict[str, tuple[str, int, int, int, str]] = {}
        for ordinal, item in enumerate(flattened, 1):
            expected = make_field_id(self.source_sha256, ordinal, item.span, item.kind)
            if item.field_id != expected:
                raise _invariant("SourcePlan", "blocks", "canonical field IDs")
            descriptor = (
                self.source_sha256.value,
                ordinal,
                item.span.start,
                item.span.end,
                item.kind.value,
            )
            digest = item.field_id.value.rsplit("-", 1)[1]
            if digest in digests and digests[digest] != descriptor:
                raise _invariant("SourcePlan", "blocks", "unique field digest components")
            digests[digest] = descriptor
        expected_diagnostics: list[PlanDiagnostic] = []
        for block in self.blocks:
            if block.kind is BlockKind.UNKNOWN:
                expected_diagnostics.append(_unknown_diagnostic(self.source_path, block))
        if self.diagnostics != tuple(expected_diagnostics):
            raise _invariant(
                "SourcePlan", "diagnostics", "one canonical diagnostic per UNKNOWN block"
            )


class PlanError(ValueError):
    """Base class for plan failures."""


class InvalidPlanInput(PlanError):
    reason: PlanInputReason
    path: RepoPath
    byte_offset: int | None

    def __init__(
        self, reason: PlanInputReason, path: RepoPath, byte_offset: int | None, /
    ) -> None:
        _exact(reason, PlanInputReason, "InvalidPlanInput", "reason")
        _exact(path, RepoPath, "InvalidPlanInput", "path")
        _optional(byte_offset, int, "InvalidPlanInput", "byte_offset")
        if byte_offset is not None and byte_offset < 0:
            raise _invariant("InvalidPlanInput", "byte_offset", "None or a nonnegative exact int")
        self.reason = reason
        self.path = path
        self.byte_offset = byte_offset
        super().__init__(f"invalid_plan_input:{reason.value}")


class PlanSerializationError(PlanError):
    """Base class for plan wire failures."""


class UnsupportedPlanVersion(PlanSerializationError):
    json_path: str
    expected_version: int

    def __init__(self, json_path: str, expected_version: int, /) -> None:
        _exact(json_path, str, "UnsupportedPlanVersion", "json_path")
        _exact(expected_version, int, "UnsupportedPlanVersion", "expected_version")
        if expected_version != PLAN_VERSION:
            raise _invariant("UnsupportedPlanVersion", "expected_version", "PLAN_VERSION")
        self.json_path = json_path
        self.expected_version = expected_version
        super().__init__(f"unsupported_plan_version:{json_path}:expected={expected_version}")


class MalformedPlanPayload(PlanSerializationError):
    json_path: str
    expected: str

    def __init__(self, json_path: str, expected: str, /) -> None:
        _exact(json_path, str, "MalformedPlanPayload", "json_path")
        _exact(expected, str, "MalformedPlanPayload", "expected")
        self.json_path = json_path
        self.expected = expected
        super().__init__(f"malformed_plan_payload:{json_path}:expected={expected}")


class _Digest(Protocol):
    def hexdigest(self) -> str: ...


def _field_digest(preimage: bytes) -> _Digest:
    return hashlib.sha256(preimage)


def make_field_id(
    source_sha256: ContentHash,
    ordinal: int,
    span: ByteSpan,
    kind: FieldKind,
    /,
) -> FieldId:
    _exact(source_sha256, ContentHash, "make_field_id", "source_sha256")
    _exact(ordinal, int, "make_field_id", "ordinal")
    _exact(span, ByteSpan, "make_field_id", "span")
    _exact(kind, FieldKind, "make_field_id", "kind")
    if not 1 <= ordinal <= 999_999:
        raise _invariant("make_field_id", "ordinal", "1 <= ordinal <= 999999")
    if not 0 <= span.start <= _UINT64_MAX or not 0 <= span.end <= _UINT64_MAX:
        raise _invariant("make_field_id", "span", "uint64 offsets")
    preimage = (
        b"ydbdoc-review-ng:field-id:v1\0"
        + bytes.fromhex(source_sha256.value)
        + ordinal.to_bytes(8, "big")
        + span.start.to_bytes(8, "big")
        + span.end.to_bytes(8, "big")
        + kind.value.encode("ascii")
    )
    digest = _field_digest(preimage)
    return FieldId(f"f1-{ordinal:06d}-{digest.hexdigest()}")


def fields_of(plan: SourcePlan, /) -> tuple[Field, ...]:
    _exact(plan, SourcePlan, "fields_of", "plan")
    return tuple(item for block in plan.blocks for item in block.fields)


def _line_ranges(source: bytes) -> tuple[tuple[int, int], ...]:
    lines: list[tuple[int, int]] = []
    start = 0
    cursor = 0
    while cursor < len(source):
        if source[cursor : cursor + 2] == b"\r\n":
            cursor += 2
            lines.append((start, cursor))
            start = cursor
        elif source[cursor] in (10, 13):
            cursor += 1
            lines.append((start, cursor))
            start = cursor
        else:
            cursor += 1
    if start < len(source):
        lines.append((start, len(source)))
    return tuple(lines)


def _range_for(lines: tuple[tuple[int, int], ...], span: ByteSpan) -> LineRange:
    start = bisect_right(lines, span.start, key=lambda line: line[0])
    end = bisect_right(lines, span.end - 1, key=lambda line: line[0])
    return LineRange(start, end)


def _unknown_diagnostic(path: RepoPath, block: Block) -> PlanDiagnostic:
    return PlanDiagnostic(
        Diagnostic(
            Severity.RED,
            "unknown_markdown_block",
            "Markdown block could not be classified safely",
            "Fix the malformed Markdown or add parser support before translation",
            path,
            block.lines.start,
            block.lines.end,
            None,
        ),
        block.span,
    )


def validate_source_plan(source: bytes, plan: SourcePlan, /) -> None:
    _exact(source, bytes, "validate_source_plan", "source")
    _exact(plan, SourcePlan, "validate_source_plan", "plan")
    if plan.plan_version != PLAN_VERSION:
        raise _invariant("SourcePlan", "plan_version", "PLAN_VERSION")
    if len(source) != plan.byte_length:
        raise InvalidPlanInput(PlanInputReason.SOURCE_SIZE_MISMATCH, plan.source_path, None)
    actual_hash = hashlib.sha256(source).hexdigest()
    if actual_hash != plan.source_sha256.value:
        raise InvalidPlanInput(PlanInputReason.SOURCE_HASH_MISMATCH, plan.source_path, None)
    physical_lines = _line_ranges(source)
    if len(physical_lines) != plan.line_count:
        raise _invariant("SourcePlan", "line_count", "line count from exact source bytes")
    if source:
        cursor = 0
        for block in plan.blocks:
            if block.span.start != cursor:
                raise _invariant("SourcePlan", "blocks", "adjacent ordered coverage")
            cursor = block.span.end
            if block.lines != _range_for(physical_lines, block.span):
                raise _invariant("Block", "lines", "line range from exact source bytes")
            for item in block.fields:
                if item.lines != _range_for(physical_lines, item.span):
                    raise _invariant("Field", "lines", "line range from exact source bytes")
        if cursor != len(source):
            raise _invariant("SourcePlan", "blocks", "complete source coverage")
    elif plan.blocks:
        raise _invariant("SourcePlan", "blocks", "empty for empty source")
    for ordinal, item in enumerate(fields_of(plan), 1):
        if item.field_id != make_field_id(plan.source_sha256, ordinal, item.span, item.kind):
            raise _invariant("Field", "field_id", "canonical source-bound ID")
    expected = tuple(
        _unknown_diagnostic(plan.source_path, block)
        for block in plan.blocks
        if block.kind is BlockKind.UNKNOWN
    )
    if plan.diagnostics != expected:
        raise _invariant("SourcePlan", "diagnostics", "one diagnostic per UNKNOWN block")


def render_identity(source: bytes, plan: SourcePlan, /) -> bytes:
    _exact(source, bytes, "render_identity", "source")
    _exact(plan, SourcePlan, "render_identity", "plan")
    validate_source_plan(source, plan)
    rendered = b"".join(source[item.span.start : item.span.end] for item in plan.blocks)
    if rendered != source:
        raise _invariant("render_identity", "plan", "byte-identical complete coverage")
    return rendered


def _span_wire(span: ByteSpan) -> dict[str, JsonValue]:
    return {"start": span.start, "end": span.end}


def _lines_wire(lines: LineRange) -> dict[str, JsonValue]:
    return {"start": lines.start, "end": lines.end}


def plan_to_wire(plan: SourcePlan, /) -> dict[str, JsonValue]:
    _exact(plan, SourcePlan, "plan_to_wire", "plan")
    return {
        "plan_version": plan.plan_version,
        "source": {
            "repository": plan.source_snapshot.repository.value,
            "commit_sha": plan.source_snapshot.commit_sha.value,
            "path": plan.source_path.value,
            "sha256": plan.source_sha256.value,
            "byte_length": plan.byte_length,
            "line_count": plan.line_count,
        },
        "blocks": [
            {
                "kind": block.kind.value,
                "span": _span_wire(block.span),
                "lines": _lines_wire(block.lines),
                "fields": [
                    {
                        "field_id": item.field_id.value,
                        "kind": item.kind.value,
                        "span": _span_wire(item.span),
                        "lines": _lines_wire(item.lines),
                        "protected_regions": [
                            {
                                "kind": region.kind.value,
                                "span": _span_wire(region.span),
                                "group": region.group,
                            }
                            for region in item.protected_regions
                        ],
                    }
                    for item in block.fields
                ],
            }
            for block in plan.blocks
        ],
        "diagnostics": [
            {
                "severity": item.diagnostic.severity.value,
                "code": item.diagnostic.code,
                "message": item.diagnostic.message,
                "action": item.diagnostic.action,
                "path": item.diagnostic.path.value if item.diagnostic.path else None,
                "line_start": item.diagnostic.line_start,
                "line_end": item.diagnostic.line_end,
                "excerpt": None,
                "span": _span_wire(item.span),
            }
            for item in plan.diagnostics
        ],
    }


def _bad(path: str, expected: str) -> NoReturn:
    raise MalformedPlanPayload(path, expected)


def _keys(value: object, expected: set[str], path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        _bad(path, "object")
    mapping = cast(Mapping[object, object], value)
    if any(type(key) is not str for key in mapping) or set(mapping) != expected:
        _bad(path, "exact keys")
    return cast(Mapping[str, object], mapping)


def _string(value: object, path: str) -> str:
    if type(value) is not str:
        _bad(path, "string")
    return value


def _integer(value: object, path: str, *, nonnegative: bool = False) -> int:
    if type(value) is not int or (nonnegative and value < 0):
        _bad(path, "nonnegative exact int" if nonnegative else "exact int")
    return value


def _nullable_integer(value: object, path: str) -> int | None:
    if value is None:
        return None
    return _integer(value, path, nonnegative=True)


def _array(value: object, path: str) -> list[object]:
    if type(value) is not list:
        _bad(path, "array")
    return cast(list[object], value)


def _enum(value: object, enum: type[Enum], path: str) -> Enum:
    raw = _string(value, path)
    table = {item.value: item for item in enum}
    if raw not in table:
        _bad(path, enum.__name__)
    return table[raw]


def _construct(
    path: str, expected: str, factory: Callable[..., object], *args: object
) -> object:
    try:
        return factory(*args)
    except InvariantViolation:
        pass
    fixed = InvariantViolation(f"{path}: expected {expected}")
    raise MalformedPlanPayload(path, expected) from fixed


def _decode_span(value: object, path: str) -> ByteSpan:
    item = _keys(value, {"start", "end"}, path)
    return cast(
        ByteSpan,
        _construct(
            path,
            "valid ByteSpan",
            ByteSpan,
            _integer(item["start"], f"{path}.start", nonnegative=True),
            _integer(item["end"], f"{path}.end", nonnegative=True),
        ),
    )


def _decode_lines(value: object, path: str) -> LineRange:
    item = _keys(value, {"start", "end"}, path)
    return cast(
        LineRange,
        _construct(
            path,
            "valid LineRange",
            LineRange,
            _integer(item["start"], f"{path}.start", nonnegative=True),
            _integer(item["end"], f"{path}.end", nonnegative=True),
        ),
    )


def _decode_region(value: object, path: str) -> ProtectedRegion:
    item = _keys(value, {"kind", "span", "group"}, path)
    kind = _enum(item["kind"], ProtectedKind, f"{path}.kind")
    span = _decode_span(item["span"], f"{path}.span")
    group = _nullable_integer(item["group"], f"{path}.group")
    return cast(
        ProtectedRegion,
        _construct(
            path,
            "valid ProtectedRegion",
            ProtectedRegion,
            kind,
            span,
            group,
        ),
    )


def _decode_field(value: object, path: str) -> Field:
    item = _keys(value, {"field_id", "kind", "span", "lines", "protected_regions"}, path)
    field_id = _construct(
        f"{path}.field_id",
        "FieldId",
        FieldId,
        _string(item["field_id"], f"{path}.field_id"),
    )
    kind = _enum(item["kind"], FieldKind, f"{path}.kind")
    span = _decode_span(item["span"], f"{path}.span")
    lines = _decode_lines(item["lines"], f"{path}.lines")
    regions = tuple(
        _decode_region(raw, f"{path}.protected_regions[{index}]")
        for index, raw in enumerate(_array(item["protected_regions"], f"{path}.protected_regions"))
    )
    return cast(
        Field,
        _construct(
            path,
            "valid Field",
            Field,
            field_id,
            kind,
            span,
            lines,
            regions,
        ),
    )


def _decode_block(value: object, path: str) -> Block:
    item = _keys(value, {"kind", "span", "lines", "fields"}, path)
    kind = _enum(item["kind"], BlockKind, f"{path}.kind")
    span = _decode_span(item["span"], f"{path}.span")
    lines = _decode_lines(item["lines"], f"{path}.lines")
    values = tuple(
        _decode_field(raw, f"{path}.fields[{index}]")
        for index, raw in enumerate(_array(item["fields"], f"{path}.fields"))
    )
    return cast(
        Block,
        _construct(
            path,
            "valid Block",
            Block,
            kind,
            span,
            lines,
            values,
        ),
    )


def _decode_diagnostic(value: object, path: str) -> PlanDiagnostic:
    expected = {
        "severity",
        "code",
        "message",
        "action",
        "path",
        "line_start",
        "line_end",
        "excerpt",
        "span",
    }
    item = _keys(value, expected, path)
    severity = _enum(item["severity"], Severity, f"{path}.severity")
    code = _string(item["code"], f"{path}.code")
    message = _string(item["message"], f"{path}.message")
    action = None if item["action"] is None else _string(item["action"], f"{path}.action")
    raw_path = None if item["path"] is None else _string(item["path"], f"{path}.path")
    decoded_path = (
        None
        if raw_path is None
        else _construct(f"{path}.path", "RepoPath", RepoPath, raw_path)
    )
    line_start = _nullable_integer(item["line_start"], f"{path}.line_start")
    line_end = _nullable_integer(item["line_end"], f"{path}.line_end")
    if item["excerpt"] is not None:
        _bad(f"{path}.excerpt", "null")
    span = _decode_span(item["span"], f"{path}.span")
    diagnostic = _construct(
        path,
        "valid Diagnostic",
        Diagnostic,
        severity,
        code,
        message,
        action,
        decoded_path,
        line_start,
        line_end,
        None,
    )
    return cast(
        PlanDiagnostic,
        _construct(
            path,
            "valid PlanDiagnostic",
            PlanDiagnostic,
            diagnostic,
            span,
        ),
    )


def plan_from_wire(payload: Mapping[str, object], /) -> SourcePlan:
    if not isinstance(payload, Mapping):
        _bad("$", "object")
    if "plan_version" not in payload or type(payload["plan_version"]) is not int:
        raise UnsupportedPlanVersion("$.plan_version", PLAN_VERSION)
    if payload["plan_version"] != PLAN_VERSION:
        raise UnsupportedPlanVersion("$.plan_version", PLAN_VERSION)
    root = _keys(payload, {"plan_version", "source", "blocks", "diagnostics"}, "$")
    source = _keys(
        root["source"],
        {"repository", "commit_sha", "path", "sha256", "byte_length", "line_count"},
        "$.source",
    )
    repository = _construct(
        "$.source.repository",
        "RepositoryId",
        RepositoryId,
        _string(source["repository"], "$.source.repository"),
    )
    commit = _construct(
        "$.source.commit_sha",
        "GitSha",
        GitSha,
        _string(source["commit_sha"], "$.source.commit_sha"),
    )
    snapshot = _construct("$.source", "valid SnapshotRef", SnapshotRef, repository, commit)
    path = _construct(
        "$.source.path", "RepoPath", RepoPath, _string(source["path"], "$.source.path")
    )
    content_hash = _construct(
        "$.source.sha256",
        "ContentHash",
        ContentHash,
        _string(source["sha256"], "$.source.sha256"),
    )
    byte_length = _integer(source["byte_length"], "$.source.byte_length", nonnegative=True)
    line_count = _integer(source["line_count"], "$.source.line_count", nonnegative=True)
    blocks = tuple(
        _decode_block(raw, f"$.blocks[{index}]")
        for index, raw in enumerate(_array(root["blocks"], "$.blocks"))
    )
    diagnostics = tuple(
        _decode_diagnostic(raw, f"$.diagnostics[{index}]")
        for index, raw in enumerate(_array(root["diagnostics"], "$.diagnostics"))
    )
    failure_path = "$"
    failure_expected = "valid SourcePlan invariants"
    try:
        return SourcePlan(
            PLAN_VERSION,
            cast(SnapshotRef, snapshot),
            cast(RepoPath, path),
            cast(ContentHash, content_hash),
            byte_length,
            line_count,
            blocks,
            diagnostics,
        )
    except InvariantViolation as error:
        message = str(error)
        if message.startswith("SourcePlan.blocks"):
            failure_path = "$.blocks"
            failure_expected = "canonical complete block sequence"
        elif message.startswith("SourcePlan.diagnostics"):
            failure_path = "$.diagnostics"
            failure_expected = "canonical UNKNOWN diagnostics"
        elif message.startswith("SourcePlan.line_count"):
            failure_path = "$.source.line_count"
            failure_expected = "line count consistent with plan shape"
        elif message.startswith("SourcePlan.byte_length"):
            failure_path = "$.source.byte_length"
            failure_expected = "byte length consistent with plan shape"
    fixed = InvariantViolation(f"{failure_path}: expected {failure_expected}")
    raise MalformedPlanPayload(failure_path, failure_expected) from fixed
