"""Small quote-aware comment scanner for allowlisted fenced languages."""

from __future__ import annotations

import re

from ydbdoc_review_ng.plan import ByteSpan

__all__ = ()

_SLASH = frozenset((b"cpp", b"c++", b"cc", b"cxx", b"java", b"javascript", b"js"))
_PYTHON = frozenset((b"python", b"py"))
_BASH = frozenset((b"bash", b"sh", b"shell"))
_HASH = frozenset((b"yaml", b"yml"))
_SQL = frozenset((b"sql", b"yql"))
_YAML_BLOCK_HEADER = re.compile(
    rb'^( *)(?:-[ \t]+)?(?:[A-Za-z_][A-Za-z0-9_-]*|"[A-Za-z_][A-Za-z0-9_-]*")'
    rb"[ \t]*:[ \t]*"
    rb"[|>](?:[1-9][+-]?|[+-][1-9]?)?(?:[ \t]+#.*|[ \t]*)$"
)


def comment_style(language: bytes) -> str | None:
    normalized = language.lower()
    if normalized == b"java":
        return "java"
    if normalized in _SLASH:
        return "slash"
    if normalized in _PYTHON:
        return "python"
    if normalized in _BASH:
        return "bash"
    if normalized in _HASH:
        return "hash"
    if normalized in _SQL:
        return "sql"
    if normalized == b"html":
        return "html"
    return None


def _trimmed(data: bytes, start: int, end: int, base: int) -> ByteSpan | None:
    while start < end and data[start] in (9, 10, 13, 32):
        start += 1
    while end > start and data[end - 1] in (9, 10, 13, 32):
        end -= 1
    return None if start == end else ByteSpan(base + start, base + end)


def _line_end(data: bytes, cursor: int) -> int:
    lf = data.find(b"\n", cursor)
    end = len(data) if lf < 0 else lf
    return end - 1 if end > cursor and data[end - 1] == 13 else end


def _yaml_block_end(data: bytes, cursor: int) -> int | None:
    line_end = _line_end(data, cursor)
    match = _YAML_BLOCK_HEADER.fullmatch(data[cursor:line_end])
    if match is None:
        return None
    parent_indent = len(match.group(1))
    probe = line_end
    if probe < len(data) and data[probe] == 13:
        probe += 1
    if probe < len(data) and data[probe] == 10:
        probe += 1
    while probe < len(data):
        content_end = _line_end(data, probe)
        body = data[probe:content_end]
        if body.strip(b" \t"):
            indent = len(body) - len(body.lstrip(b" "))
            if indent <= parent_indent:
                return probe
        probe = content_end
        if probe < len(data) and data[probe] == 13:
            probe += 1
        if probe < len(data) and data[probe] == 10:
            probe += 1
    return len(data)


def _cpp_raw_closer(data: bytes, cursor: int) -> tuple[bytes, int] | None:
    if not data.startswith(b'R"', cursor):
        return None
    delimiter_end = data.find(b"(", cursor + 2, min(len(data), cursor + 19))
    if delimiter_end < 0:
        return None
    delimiter = data[cursor + 2 : delimiter_end]
    if any(byte in b" ()\\\t\r\n" for byte in delimiter):
        return None
    return b")" + delimiter + b'"', delimiter_end + 1


def comment_spans(data: bytes, base: int, style: str) -> tuple[ByteSpan, ...]:
    result: list[ByteSpan] = []
    cursor = 0
    quote: int | None = None
    triple: bytes | None = None
    raw_closer: bytes | None = None
    while cursor < len(data):
        current = data[cursor]
        if raw_closer is not None:
            if data.startswith(raw_closer, cursor):
                cursor += len(raw_closer)
                raw_closer = None
                continue
            cursor += 1
            continue
        if triple is not None:
            if current == 92 and cursor + 1 < len(data):
                cursor += 2
                continue
            if data.startswith(triple, cursor):
                cursor += 3
                triple = None
                continue
            cursor += 1
            continue
        if quote is not None:
            if current == 92 and cursor + 1 < len(data):
                cursor += 2
                continue
            if style == "hash" and quote == 39 and data.startswith(b"''", cursor):
                cursor += 2
                continue
            if current == quote or (
                current in (10, 13) and quote != 96 and style not in {"bash", "hash", "html"}
            ):
                quote = None
            cursor += 1
            continue
        if style == "hash" and (cursor == 0 or data[cursor - 1] == 10):
            block_end = _yaml_block_end(data, cursor)
            if block_end is not None:
                cursor = block_end
                continue
        if style == "slash":
            raw = _cpp_raw_closer(data, cursor)
            if raw is not None:
                raw_closer, cursor = raw
                continue
        if (
            style == "python"
            and data.startswith((b'"""', b"'''"), cursor)
            or style == "java"
            and data.startswith(b'"""', cursor)
        ):
            triple = data[cursor : cursor + 3]
            cursor += 3
            continue
        if current in (34, 39) or (style in {"slash", "java"} and current == 96):
            quote = current
            cursor += 1
            continue
        if style in {"slash", "java"} and data.startswith(b"//", cursor):
            end = _line_end(data, cursor + 2)
            span = _trimmed(data, cursor + 2, end, base)
            if span is not None:
                result.append(span)
            cursor = end
            continue
        if style in {"hash", "python", "bash"} and current == 35:
            end = _line_end(data, cursor + 1)
            span = _trimmed(data, cursor + 1, end, base)
            if span is not None:
                result.append(span)
            cursor = end
            continue
        if style == "sql" and data.startswith(b"--", cursor):
            end = _line_end(data, cursor + 2)
            span = _trimmed(data, cursor + 2, end, base)
            if span is not None:
                result.append(span)
            cursor = end
            continue
        if style in {"slash", "java"} and data.startswith(b"/*", cursor):
            closer = data.find(b"*/", cursor + 2)
            end = len(data) if closer < 0 else closer
            span = _trimmed(data, cursor + 2, end, base)
            if span is not None:
                result.append(span)
            cursor = len(data) if closer < 0 else closer + 2
            continue
        if style == "html" and data.startswith(b"<!--", cursor):
            closer = data.find(b"-->", cursor + 4)
            end = len(data) if closer < 0 else closer
            span = _trimmed(data, cursor + 4, end, base)
            if span is not None:
                result.append(span)
            cursor = len(data) if closer < 0 else closer + 3
            continue
        cursor += 1
    return tuple(result)
