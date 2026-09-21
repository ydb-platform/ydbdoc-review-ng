"""Conservative byte-span extraction for YFM container titles."""

from __future__ import annotations

import re

from ydbdoc_review_ng.plan import ByteSpan

__all__ = ()

_BRACE = re.compile(rb"^ {0,3}\{%[ \t]+(note|cut|tab)(?:[ \t]+(.*?))?[ \t]+%\}[ \t]*$")
_COLON = re.compile(rb"^ {0,3}:::(note|cut|tab)(?:[ \t]+(.*?))?[ \t]*$")


def _line_body(line: bytes) -> bytes:
    return line[:-2] if line.endswith(b"\r\n") else line.rstrip(b"\r\n")


def _title(attrs: bytes, name: bytes, base: int) -> ByteSpan | None:
    leading = len(attrs) - len(attrs.lstrip(b" \t"))
    value = attrs[leading:].rstrip(b" \t")
    value_base = base + leading
    if name == b"note":
        modifier = re.match(rb"[^ \t]+", value)
        if modifier is None:
            return None
        remainder = value[modifier.end() :]
        skipped = len(remainder) - len(remainder.lstrip(b" \t"))
        value_base += modifier.end() + skipped
        value = remainder[skipped:].rstrip(b" \t")
    if not value:
        return None
    if value[0] in (34, 39) and len(value) >= 2 and value[-1] == value[0]:
        return None if len(value) == 2 else ByteSpan(value_base + 1, value_base + len(value) - 1)
    return ByteSpan(value_base, value_base + len(value))


def yfm_title_spans(source: bytes, start: int, end: int) -> tuple[ByteSpan, ...]:
    result: list[ByteSpan] = []
    cursor = start
    for line in source[start:end].splitlines(keepends=True):
        body = _line_body(line)
        match = _BRACE.fullmatch(body) or _COLON.fullmatch(body)
        if match is not None and match.group(2) is not None:
            span = _title(match.group(2), match.group(1), cursor + match.start(2))
            if span is not None:
                result.append(span)
        cursor += len(line)
    return tuple(result)
