"""Private byte-scanning helpers for the Markdown planner."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass

__all__ = ()


@dataclass(frozen=True, slots=True)
class _Line:
    start: int
    body_end: int
    end: int
    number: int


@dataclass(frozen=True, slots=True)
class _Tag:
    name: bytes
    end: int
    closing: bool
    self_closing: bool


_NAME_START = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_NAME_BODY = _NAME_START + b"0123456789-"
_ATTR_START = _NAME_START + b"_:"
_ATTR_BODY = _ATTR_START + b"0123456789.-"
_FORBIDDEN = frozenset((*range(9), 11, 12, *range(14, 32), 127))


def _lines(source: bytes) -> tuple[_Line, ...]:
    result: list[_Line] = []
    start = 0
    cursor = 0
    number = 1
    while cursor < len(source):
        if source[cursor : cursor + 2] == b"\r\n":
            result.append(_Line(start, cursor, cursor + 2, number))
            cursor += 2
            start = cursor
            number += 1
        elif source[cursor] in (10, 13):
            result.append(_Line(start, cursor, cursor + 1, number))
            cursor += 1
            start = cursor
            number += 1
        else:
            cursor += 1
    if start < len(source):
        result.append(_Line(start, len(source), len(source), number))
    return tuple(result)


def _line_index(lines: tuple[_Line, ...], offset: int) -> int:
    index = bisect_right(lines, offset, key=lambda line: line.start) - 1
    if index >= 0:
        line = lines[index]
        if line.start <= offset < line.end or line.start == offset == line.body_end == line.end:
            return index
    if lines and offset == lines[-1].end:
        return len(lines) - 1
    raise AssertionError("offset outside physical lines")


def _contains_control(data: bytes) -> bool:
    return any(byte in _FORBIDDEN for byte in data)


def _indent(body: bytes) -> int:
    return len(body) - len(body.lstrip(b" "))


def _tag_at(source: bytes, start: int, limit: int | None = None) -> _Tag | None:
    stop = len(source) if limit is None else min(limit, len(source))
    if start >= stop or source[start] != 60:
        return None
    cursor = start + 1
    closing = False
    if cursor < stop and source[cursor] == 47:
        closing = True
        cursor += 1
    name_start = cursor
    if cursor >= stop or source[cursor] not in _NAME_START:
        return None
    cursor += 1
    while cursor < stop and source[cursor] in _NAME_BODY:
        cursor += 1
    name = source[name_start:cursor].lower()
    if closing:
        while cursor < stop and source[cursor] in b" \t":
            cursor += 1
        if cursor < stop and source[cursor] == 62:
            return _Tag(name, cursor + 1, True, False)
        return None
    self_closing = False
    while cursor < stop:
        separator_start = cursor
        while cursor < stop and source[cursor] in b" \t":
            cursor += 1
        has_separator = cursor > separator_start
        if cursor >= stop:
            return None
        if source[cursor] == 62:
            return _Tag(name, cursor + 1, False, self_closing)
        if source[cursor] == 47:
            probe = cursor + 1
            while probe < stop and source[probe] in b" \t":
                probe += 1
            if probe < stop and source[probe] == 62:
                return _Tag(name, probe + 1, False, True)
            return None
        if not has_separator or source[cursor] not in _ATTR_START:
            return None
        cursor += 1
        while cursor < stop and source[cursor] in _ATTR_BODY:
            cursor += 1
        probe = cursor
        while probe < stop and source[probe] in b" \t":
            probe += 1
        if probe >= stop or source[probe] != 61:
            continue
        cursor = probe + 1
        while cursor < stop and source[cursor] in b" \t":
            cursor += 1
        if cursor >= stop:
            return None
        if source[cursor] in (34, 39):
            quote = source[cursor]
            cursor += 1
            while cursor < stop and source[cursor] != quote:
                if source[cursor] in _FORBIDDEN:
                    return None
                cursor += 1
            if cursor >= stop:
                return None
            cursor += 1
        else:
            value_start = cursor
            forbidden = b" \t\"'`=<>"
            while cursor < stop and source[cursor] not in forbidden:
                if source[cursor] in _FORBIDDEN:
                    return None
                cursor += 1
            if cursor == value_start:
                return None
    return None


def _outside_quote_end(source: bytes, start: int, token: bytes) -> int | None:
    quote: int | None = None
    cursor = start
    while cursor < len(source):
        byte = source[cursor]
        if quote is None and byte in (34, 39):
            quote = byte
        elif quote == byte:
            quote = None
        elif quote is None and source.startswith(token, cursor):
            return cursor + len(token)
        cursor += 1
    return None


def _reserved_html_start(body: bytes) -> bool:
    stripped = body.lstrip(b" ")
    if len(body) - len(stripped) > 3:
        return False
    return (
        stripped.startswith((b"<!--", b"<![CDATA[", b"<?", b"<!", b"</"))
        or len(stripped) >= 2
        and stripped[0] == 60
        and stripped[1] in _NAME_START
    )


def _line_end_containing(lines: tuple[_Line, ...], offset: int) -> int:
    if not lines:
        return 0
    probe = min(offset, lines[-1].end - 1)
    return lines[_line_index(lines, probe)].end


def _skip_reserved(source: bytes, cursor: int) -> int | None:
    if source.startswith(b"<!--", cursor):
        end = source.find(b"-->", cursor + 4)
        return None if end < 0 else end + 3
    if source.startswith(b"<![CDATA[", cursor):
        end = source.find(b"]]>", cursor + 9)
        return None if end < 0 else end + 3
    if source.startswith(b"<?", cursor):
        return _outside_quote_end(source, cursor + 2, b"?>")
    if cursor + 2 < len(source) and source.startswith(b"<!", cursor) and 65 <= source[cursor + 2] <= 90:
        return _outside_quote_end(source, cursor + 3, b">")
    return None


def _depth_close(
    source: bytes,
    lines: tuple[_Line, ...],
    opener: _Tag,
    *,
    raw_names: frozenset[bytes],
) -> int:
    depth = 1
    cursor = opener.end
    while cursor < len(source):
        found = source.find(b"<", cursor)
        if found < 0:
            return len(source)
        reserved_end = _skip_reserved(source, found)
        if reserved_end is not None:
            cursor = reserved_end
            continue
        if source.startswith((b"<!--", b"<![CDATA[", b"<?", b"<!"), found):
            return len(source)
        token = _tag_at(source, found)
        if token is None:
            if found + 1 < len(source) and (
                source[found + 1] in _NAME_START or source.startswith(b"</", found)
            ):
                return len(source)
            cursor = found + 1
            continue
        if not token.closing and not token.self_closing and token.name in raw_names:
            needle = b"</" + token.name
            raw_cursor = token.end
            while True:
                candidate = source.lower().find(needle, raw_cursor)
                if candidate < 0:
                    return len(source)
                closing = _tag_at(source, candidate)
                if closing is not None and closing.closing and closing.name == token.name:
                    cursor = closing.end
                    break
                raw_cursor = candidate + 1
            continue
        if token.name == opener.name:
            if token.closing:
                depth -= 1
                if depth == 0:
                    return _line_end_containing(lines, token.end - 1)
            elif not token.self_closing:
                depth += 1
        cursor = token.end
    return len(source)
