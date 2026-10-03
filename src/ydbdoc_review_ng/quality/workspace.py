"""In-memory critic workspace: read / grep / apply_patch with RO mounts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final

PATCH_ABSOLUTE_BYTE_CAP: Final[int] = 8 * 1024
PATCH_RELATIVE_RATIO: Final[float] = 0.40
SMALL_FILE_BYTE_THRESHOLD: Final[int] = 2 * 1024


class ToolErrorReason(str, Enum):
    UNKNOWN_PATH = "unknown_path"
    READ_ONLY = "read_only"
    INVALID_ARGS = "invalid_args"
    INVALID_PATCH = "invalid_patch"
    OVERSIZED_PATCH = "oversized_patch"


class ToolError(ValueError):
    def __init__(self, reason: ToolErrorReason, detail: str = "", /) -> None:
        self.reason = reason
        self.detail = detail
        suffix = f": {detail}" if detail else ""
        super().__init__(f"tool_error:{reason.value}{suffix}")


@dataclass(frozen=True, slots=True)
class LineRange:
    start: int  # 1-based inclusive
    end: int  # 1-based inclusive

    def __post_init__(self) -> None:
        if type(self.start) is not int or type(self.end) is not int:
            raise TypeError("LineRange bounds must be int")
        if self.start < 1 or self.end < self.start:
            raise ValueError("LineRange bounds invalid")

    def covers(self, other: LineRange, /) -> bool:
        return self.start <= other.start and self.end >= other.end


def _normalize_path(path: str) -> str:
    if type(path) is not str or not path.strip():
        raise ToolError(ToolErrorReason.UNKNOWN_PATH, "empty path")
    if path.startswith(("/", "\\")):
        raise ToolError(ToolErrorReason.UNKNOWN_PATH, path)
    parts: list[str] = []
    for part in path.replace("\\", "/").split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            raise ToolError(ToolErrorReason.UNKNOWN_PATH, path)
        parts.append(part)
    if not parts:
        raise ToolError(ToolErrorReason.UNKNOWN_PATH, path)
    return "/".join(parts)


def _split_lines(data: bytes) -> list[str]:
    text = data.decode("utf-8")
    if text == "":
        return []
    # Keep a trailing empty slot only when the file ends with a newline? For
    # editing, treat content as newline-separated lines without a phantom
    # trailing empty line when the file ends with \n.
    if text.endswith("\n"):
        return text[:-1].split("\n")
    return text.split("\n")


def _join_lines(lines: list[str]) -> bytes:
    if not lines:
        return b""
    return ("\n".join(lines) + "\n").encode("utf-8")


@dataclass(frozen=True, slots=True)
class _Hunk:
    old_start: int | None  # 1-based; None = search by context
    old_count: int | None
    new_start: int | None
    new_count: int | None
    lines: tuple[str, ...]  # raw hunk body lines including leading markers


def _parse_hunks(patch: str) -> list[_Hunk]:
    if type(patch) is not str or not patch.strip():
        raise ToolError(ToolErrorReason.INVALID_PATCH, "empty patch")
    lines = patch.replace("\r\n", "\n").split("\n")
    # Drop trailing empty from final newline.
    if lines and lines[-1] == "":
        lines = lines[:-1]
    hunks: list[_Hunk] = []
    index = 0
    while index < len(lines):
        header = lines[index]
        if not header.startswith("@@"):
            raise ToolError(ToolErrorReason.INVALID_PATCH, f"expected @@, got {header!r}")
        old_start: int | None = None
        old_count: int | None = None
        new_start: int | None = None
        new_count: int | None = None
        body_header = header[2:]
        body_header = body_header.removeprefix(" ")
        if body_header.endswith("@@"):
            body_header = body_header[:-2].rstrip()
        # Forms: "@@" alone, "@@ -a,b +c,d @@", or "@@\n" already split.
        if body_header.startswith("-"):
            # Parse -old_start[,old_count] +new_start[,new_count]
            try:
                left, right = body_header.split("+", 1)
                left = left.strip()
                right = right.strip().split()[0] if right.strip() else ""
                if not left.startswith("-"):
                    raise ValueError
                left = left[1:]
                if "," in left:
                    old_start_s, old_count_s = left.split(",", 1)
                    old_start, old_count = int(old_start_s), int(old_count_s)
                else:
                    old_start, old_count = int(left), 1
                if "," in right:
                    new_start_s, new_count_s = right.split(",", 1)
                    new_start, new_count = int(new_start_s), int(new_count_s)
                else:
                    new_start, new_count = int(right), 1
            except ValueError as error:
                raise ToolError(
                    ToolErrorReason.INVALID_PATCH, f"bad hunk header {header!r}"
                ) from error
        index += 1
        body: list[str] = []
        while index < len(lines) and not lines[index].startswith("@@"):
            body.append(lines[index])
            index += 1
        if not body:
            raise ToolError(ToolErrorReason.INVALID_PATCH, "empty hunk body")
        for line in body:
            if not line or line[0] not in {" ", "-", "+"}:
                raise ToolError(ToolErrorReason.INVALID_PATCH, f"bad hunk line {line!r}")
        hunks.append(
            _Hunk(old_start, old_count, new_start, new_count, tuple(body))
        )
    if not hunks:
        raise ToolError(ToolErrorReason.INVALID_PATCH, "no hunks")
    return hunks


def _hunk_old_lines(hunk: _Hunk) -> list[str]:
    return [line[1:] for line in hunk.lines if line[0] in {" ", "-"}]


def _hunk_new_lines(hunk: _Hunk) -> list[str]:
    return [line[1:] for line in hunk.lines if line[0] in {" ", "+"}]


def _find_hunk_offset(lines: list[str], hunk: _Hunk) -> int:
    old = _hunk_old_lines(hunk)
    if not old:
        # Pure insertion: use new_start/old_start if provided, else end.
        if hunk.old_start is not None:
            # old_start is 1-based index of first affected old line; for
            # insert-only with old_count=0, insert before that line.
            return max(0, hunk.old_start - 1)
        return len(lines)
    if hunk.old_start is not None:
        start = hunk.old_start - 1
        if start < 0 or start + len(old) > len(lines):
            raise ToolError(ToolErrorReason.INVALID_PATCH, "hunk out of range")
        if lines[start : start + len(old)] != old:
            raise ToolError(ToolErrorReason.INVALID_PATCH, "hunk context mismatch")
        return start
    # Search unique occurrence.
    matches = [
        index
        for index in range(len(lines) - len(old) + 1)
        if lines[index : index + len(old)] == old
    ]
    if len(matches) != 1:
        raise ToolError(
            ToolErrorReason.INVALID_PATCH,
            f"context matches={len(matches)} expected 1",
        )
    return matches[0]


def _changed_span_bytes(hunk: _Hunk) -> int:
    removed = sum(len(line) + 1 for line in hunk.lines if line.startswith("-"))
    added = sum(len(line) + 1 for line in hunk.lines if line.startswith("+"))
    return max(removed, added)


def _enforce_patch_caps(file_bytes: bytes, hunks: list[_Hunk]) -> None:
    span = sum(_changed_span_bytes(hunk) for hunk in hunks)
    if span > PATCH_ABSOLUTE_BYTE_CAP:
        raise ToolError(ToolErrorReason.OVERSIZED_PATCH, f"span={span}")
    size = len(file_bytes)
    if size >= SMALL_FILE_BYTE_THRESHOLD and span > int(size * PATCH_RELATIVE_RATIO):
        raise ToolError(ToolErrorReason.OVERSIZED_PATCH, f"span={span} size={size}")


def apply_unified_hunks(data: bytes, patch: str) -> tuple[bytes, tuple[LineRange, ...]]:
    """Apply one or more unified hunks; return new bytes and touched new ranges."""
    hunks = _parse_hunks(patch)
    _enforce_patch_caps(data, hunks)
    lines = _split_lines(data)
    # Apply from bottom to top when starts are known; otherwise sequential search.
    # Sequential unique-context search is stable for non-overlapping hunks.
    touched: list[LineRange] = []
    # Work on a copy; for multiple hunks, re-find each against current lines.
    for hunk in hunks:
        offset = _find_hunk_offset(lines, hunk)
        old = _hunk_old_lines(hunk)
        new = _hunk_new_lines(hunk)
        lines[offset : offset + len(old)] = new
        # Pending re-read covers only mutated new lines (+), not unchanged
        # context (space). Pure deletion anchors on the nearest surviving line.
        changed_new: list[int] = []
        cursor = offset
        for raw in hunk.lines:
            marker = raw[0]
            if marker == " ":
                cursor += 1
            elif marker == "-":
                continue
            elif marker == "+":
                changed_new.append(cursor + 1)
                cursor += 1
        if changed_new:
            touched.append(LineRange(changed_new[0], changed_new[-1]))
        elif old and lines:
            anchor = min(max(offset, 1), len(lines))
            touched.append(LineRange(anchor, anchor))
    # Merge overlapping touched ranges for convenience.
    if not touched:
        return _join_lines(lines), ()
    touched.sort(key=lambda item: (item.start, item.end))
    merged: list[LineRange] = [touched[0]]
    for item in touched[1:]:
        previous = merged[-1]
        if item.start <= previous.end + 1:
            merged[-1] = LineRange(previous.start, max(previous.end, item.end))
        else:
            merged.append(item)
    return _join_lines(lines), tuple(merged)


class CriticWorkspace:
    """Per-chunk in-memory mounts for the tool-using critic."""

    def __init__(
        self,
        *,
        writable_path: str,
        writable_bytes: bytes,
        read_only: dict[str, bytes],
    ) -> None:
        self._writable_path = _normalize_path(writable_path)
        if type(writable_bytes) is not bytes:
            raise TypeError("writable_bytes must be bytes")
        self._writable = writable_bytes
        normalized_ro: dict[str, bytes] = {}
        for path, content in read_only.items():
            key = _normalize_path(path)
            if key == self._writable_path:
                raise ValueError("read_only path collides with writable path")
            if type(content) is not bytes:
                raise TypeError("read_only values must be bytes")
            normalized_ro[key] = content
        self._read_only = normalized_ro

    def writable_path(self) -> str:
        return self._writable_path

    def writable_bytes(self) -> bytes:
        return self._writable

    def _resolve(self, path: str, *, for_write: bool) -> str:
        key = _normalize_path(path)
        if key == self._writable_path:
            return key
        if key in self._read_only:
            if for_write:
                raise ToolError(ToolErrorReason.READ_ONLY, key)
            return key
        raise ToolError(ToolErrorReason.UNKNOWN_PATH, key)

    def _bytes_at(self, key: str) -> bytes:
        if key == self._writable_path:
            return self._writable
        return self._read_only[key]

    def read(self, path: str, *, start_line: int, end_line: int) -> str:
        key = self._resolve(path, for_write=False)
        if type(start_line) is not int or type(end_line) is not int:
            raise ToolError(ToolErrorReason.INVALID_ARGS, "line bounds")
        if start_line < 1 or end_line < start_line:
            raise ToolError(ToolErrorReason.INVALID_ARGS, "line bounds")
        lines = _split_lines(self._bytes_at(key))
        if not lines:
            if start_line == 1 and end_line == 1:
                return "1|\n"
            raise ToolError(ToolErrorReason.INVALID_ARGS, "empty file window")
        if end_line > len(lines):
            raise ToolError(ToolErrorReason.INVALID_ARGS, "end past EOF")
        chunks = [f"{number}|{lines[number - 1]}\n" for number in range(start_line, end_line + 1)]
        return "".join(chunks)

    def grep(self, pattern: str, *, path: str | None = None) -> list[tuple[str, int, str]]:
        if type(pattern) is not str or not pattern:
            raise ToolError(ToolErrorReason.INVALID_ARGS, "empty pattern")
        keys: list[str]
        if path is None:
            keys = [self._writable_path, *sorted(self._read_only)]
        else:
            keys = [self._resolve(path, for_write=False)]
        hits: list[tuple[str, int, str]] = []
        for key in keys:
            for number, line in enumerate(_split_lines(self._bytes_at(key)), start=1):
                if pattern in line:
                    hits.append((key, number, line))
        return hits

    def apply_patch(self, path: str, patch: str) -> tuple[LineRange, ...]:
        key = self._resolve(path, for_write=True)
        if key != self._writable_path:
            raise ToolError(ToolErrorReason.READ_ONLY, key)
        new_bytes, touched = apply_unified_hunks(self._writable, patch)
        self._writable = new_bytes
        return touched
