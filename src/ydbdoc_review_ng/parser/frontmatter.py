"""Byte-span extraction for the two translatable front matter scalars."""

from __future__ import annotations

import re

from ydbdoc_review_ng.plan import ByteSpan, FieldKind

__all__ = ()

_KEY = re.compile(rb"^(title|description)[ \t]*:[ \t]*(.*)$")
_BLOCK_HEADER = re.compile(rb"[|>](?:[1-9][+-]?|[+-][1-9]?)?(?:[ \t]+#.*|[ \t]*)")


def _line_body(line: bytes) -> bytes:
    return line[:-2] if line.endswith(b"\r\n") else line.rstrip(b"\r\n")


def _quoted_end(value: bytes, quote: int) -> int | None:
    cursor = 1
    while cursor < len(value):
        if value[cursor] == quote:
            if quote == 39 and cursor + 1 < len(value) and value[cursor + 1] == quote:
                cursor += 2
                continue
            return cursor
        if quote == 34 and value[cursor] == 92 and cursor + 1 < len(value):
            cursor += 2
        else:
            cursor += 1
    return None


def _scalar_span(value: bytes, base: int) -> ByteSpan | None:
    if not value:
        return None
    if value[0] in (34, 39):
        end = _quoted_end(value, value[0])
        if end is None or end == 1:
            return None
        return ByteSpan(base + 1, base + end)
    comment = re.search(rb"[ \t]+#", value)
    end = len(value) if comment is None else comment.start()
    while end and value[end - 1] in (32, 9):
        end -= 1
    return None if end == 0 else ByteSpan(base, base + end)


def frontmatter_fields(
    source: bytes, start: int, end: int
) -> tuple[tuple[FieldKind, ByteSpan], ...]:
    result: list[tuple[FieldKind, ByteSpan]] = []
    lines = source[start:end].splitlines(keepends=True)
    offsets: list[int] = []
    cursor = start
    for line in lines:
        offsets.append(cursor)
        cursor += len(line)
    for index, line in enumerate(lines):
        body = _line_body(line)
        match = _KEY.fullmatch(body)
        if match is not None:
            value = match.group(2)
            value_start = offsets[index] + match.start(2)
            span: ByteSpan | None
            if _BLOCK_HEADER.fullmatch(value):
                indent: int | None = None
                content_start: int | None = None
                content_end: int | None = None
                for probe in range(index + 1, len(lines)):
                    candidate = _line_body(lines[probe])
                    if candidate.strip(b" \t") in {b"---", b"..."}:
                        break
                    if not candidate.strip(b" \t"):
                        continue
                    leading = len(candidate) - len(candidate.lstrip(b" "))
                    if leading == 0 or indent is not None and leading < indent:
                        break
                    if indent is None:
                        indent = leading
                        content_start = offsets[probe] + indent
                    content_end = offsets[probe] + len(candidate)
                span = (
                    None
                    if content_start is None or content_end is None
                    else ByteSpan(content_start, content_end)
                )
            else:
                if value[:1] in {b'"', b"'"}:
                    scalar = source[value_start:end]
                else:
                    scalar_end = value_start + len(value)
                    for probe in range(index + 1, len(lines)):
                        candidate = _line_body(lines[probe])
                        if candidate.strip(b" \t") in {b"---", b"..."}:
                            break
                        if candidate.strip(b" \t") and not candidate.startswith(b" "):
                            break
                        scalar_end = offsets[probe] + len(candidate)
                    scalar = source[value_start:scalar_end]
                span = _scalar_span(scalar, value_start)
            if span is not None:
                kind = FieldKind.HEADING if match.group(1) == b"title" else FieldKind.PARAGRAPH
                result.append((kind, span))
    return tuple(result)
