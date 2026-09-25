"""Pure-stdlib, lossless structural planner for the T007 Markdown subset."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from ydbdoc_review_ng.domain import ContentHash, Diagnostic, RepoPath, Severity, SnapshotRef
from ydbdoc_review_ng.errors import InvariantViolation
from ydbdoc_review_ng.parser.code_comments import comment_spans, comment_style
from ydbdoc_review_ng.parser.core import (
    _contains_control,
    _depth_close,
    _indent,
    _Line,
    _line_index,
    _lines,
    _outside_quote_end,
    _reserved_html_start,
    _tag_at,
)
from ydbdoc_review_ng.parser.frontmatter import frontmatter_fields
from ydbdoc_review_ng.parser.yfm import yfm_title_spans
from ydbdoc_review_ng.plan import (
    PLAN_VERSION,
    Block,
    BlockKind,
    ByteSpan,
    Field,
    FieldKind,
    InvalidPlanInput,
    LineRange,
    PlanDiagnostic,
    PlanInputReason,
    ProtectedKind,
    ProtectedRegion,
    SourcePlan,
    make_field_id,
    validate_source_plan,
)
from ydbdoc_review_ng.scope import FileOperation, ScopeManifest

__all__ = ["build_markdown_plan", "build_scope_plans"]

_VOID = frozenset(b"area base br col embed hr img input link meta param source track wbr".split())
_NORMAL = frozenset(
    b"address article aside basefont blockquote body caption center colgroup dd details "
    b"dialog dir div dl dt fieldset figcaption figure footer form frame frameset h1 h2 h3 "
    b"h4 h5 h6 head header html iframe legend li main menu menuitem nav noframes ol optgroup "
    b"option p search section summary table tbody td tfoot th thead title tr ul".split()
)
_RAW = frozenset((b"script", b"style", b"pre", b"textarea"))
_FIELD_BLOCKS = frozenset(
    (BlockKind.ATX_HEADING, BlockKind.SETEXT_HEADING, BlockKind.PARAGRAPH, BlockKind.LIST_ITEM)
)
_LIST = re.compile(rb"^( {0,3})(?:[-+*]|[0-9]{1,9}[.)])(?: {1,4}|\t)(?=\S)")
_ATX = re.compile(rb"^( {0,3})(#{1,6})(?:[ \t]+|$)")
_SETEXT = re.compile(rb"^ {0,3}(?:=+|-+)[ \t]*$")
_THEMATIC = re.compile(rb"^ {0,3}([*_-])(?:[ \t]*\1){2,}[ \t]*$")
_REFERENCE = re.compile(rb"^ {0,3}\[[^\]\x00-\x1f\x7f]+\]:[ \t]+\S")
_FENCE = re.compile(rb"^ {0,3}(`{3,}|~{3,})([^\x00-\x08\x0b\x0c\x0e-\x1f\x7f]*)$")
_DELIMITER_CELL = re.compile(rb"^:?-{3,}:?$")
_ANCHOR = re.compile(rb"\{#[A-Za-z0-9_.:-]+\}")
_URL = re.compile(rb"[A-Za-z][A-Za-z0-9+.-]{1,31}://[^\s<>]+")
_FILENAME = re.compile(rb"[A-Za-z0-9_-][A-Za-z0-9_.-]*\.[A-Za-z0-9][A-Za-z0-9_-]*")
_PATH = re.compile(
    rb"(?:/|\./|\.\./)?[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+"
    rb"|" + _FILENAME.pattern
)
_IDENTIFIER = re.compile(rb"[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)*")
_URI_AUTOLINK = re.compile(rb"<[A-Za-z][A-Za-z0-9+.-]{1,31}:[\x21-\x3b\x3d\x3f-\x7e]+>")
_EMAIL_AUTOLINK = re.compile(
    rb"<[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*@"
    rb"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
    rb"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?>"
)


@dataclass(frozen=True, slots=True)
class _FieldDraft:
    kind: FieldKind
    span: ByteSpan
    regions: tuple[ProtectedRegion, ...]


@dataclass(frozen=True, slots=True)
class _BlockDraft:
    kind: BlockKind
    span: ByteSpan
    fields: tuple[_FieldDraft, ...]


def _invariant(owner: str, name: str, expected: str) -> InvariantViolation:
    return InvariantViolation(f"{owner}.{name}: expected {expected}")


def _exact(value: object, expected: type[object], owner: str, name: str) -> None:
    if type(value) is not expected:
        raise _invariant(owner, name, f"exact {expected.__name__}")


def _body(source: bytes, line: _Line, start: int | None = None) -> bytes:
    return source[line.start if start is None else start : line.body_end]


def _line_range(lines: tuple[_Line, ...], span: ByteSpan) -> LineRange:
    start = lines[_line_index(lines, span.start)].number
    end = lines[_line_index(lines, span.end - 1)].number
    return LineRange(start, end)


def _full_end(lines: tuple[_Line, ...], index: int) -> int:
    return lines[index].end


def _next_nonblank(source: bytes, lines: tuple[_Line, ...], index: int) -> int | None:
    probe = index
    while probe < len(lines):
        if _body(source, lines[probe]).strip(b" \t"):
            return probe
        probe += 1
    return None


def _front_matter_end(
    source: bytes, lines: tuple[_Line, ...], index: int, start: int
) -> tuple[BlockKind, int] | None:
    if _body(source, lines[index], start).strip(b" \t") != b"---":
        return None
    has_key = False
    for probe in range(index + 1, len(lines)):
        body = _body(source, lines[probe])
        if body.strip(b" \t") in {b"---", b"..."}:
            if not has_key:
                return None
            return BlockKind.T008_FRONT_MATTER, lines[probe].end
        if re.match(rb"^[ \t]*[A-Za-z_][A-Za-z0-9_-]*[ \t]*:", body):
            has_key = True
    if has_key:
        return BlockKind.UNKNOWN, len(source)
    return None


def _fence_end(source: bytes, lines: tuple[_Line, ...], index: int) -> int | None:
    match = _FENCE.match(_body(source, lines[index]))
    if match is None:
        return None
    marker = match.group(1)
    if marker.startswith(b"`") and b"`" in match.group(2):
        return None
    close = re.compile(
        rb"^ {0,3}" + re.escape(marker[:1]) + rb"{" + str(len(marker)).encode() + rb",}[ \t]*$"
    )
    for probe in range(index + 1, len(lines)):
        if close.match(_body(source, lines[probe])):
            return lines[probe].end
    return len(source)


def _fence_fields(
    source: bytes, lines: tuple[_Line, ...], index: int, end: int
) -> tuple[_FieldDraft, ...]:
    match = _FENCE.match(_body(source, lines[index]))
    assert match is not None
    info = match.group(2).strip(b" \t")
    language = (info.split(None, 1)[0] if info else b"").lower()
    marker = match.group(1)
    close = re.compile(
        rb"^ {0,3}" + re.escape(marker[:1]) + rb"{" + str(len(marker)).encode() + rb",}[ \t]*$"
    )
    content_start = lines[index].end
    final_index = _line_index(lines, end - 1)
    content_end = (
        lines[final_index].start if close.match(_body(source, lines[final_index])) else end
    )
    if language == b"text":
        content = source[content_start:content_end]
        separator = re.search(rb"(?:\r?\n)[ \t]*(?:\r?\n)", content)
        if separator is None:
            return ()
        field_start = content_start + separator.end()
        field_end = content_end
        while field_end > field_start and source[field_end - 1] in (9, 10, 13, 32):
            field_end -= 1
        field, malformed = _field_for_span(
            source, lines, FieldKind.PARAGRAPH, field_start, field_end
        )
        return () if malformed or field is None else (field,)
    style = comment_style(language)
    if style is None:
        return ()
    fields: list[_FieldDraft] = []
    for span in comment_spans(source[content_start:content_end], content_start, style):
        field, malformed = _field_for_span(source, lines, FieldKind.PARAGRAPH, span.start, span.end)
        if malformed:
            return ()
        if field is not None:
            fields.append(field)
    return tuple(fields)


def _directive(body: bytes) -> tuple[str, str, bool] | None:
    first_closer = body.find(b"%}")
    brace_body = body
    if first_closer >= 0:
        if body[first_closer + 2 :].strip(b" \t"):
            return None
        brace_body = body[: first_closer + 2]
    brace = re.fullmatch(
        rb" {0,3}\{%[ \t]+([a-z]+)(?:[ \t]+([^\x00-\x08\x0b\x0c\x0e-\x1f\x7f]*?))?[ \t]+%\}",
        brace_body,
    )
    if brace is not None:
        name = brace.group(1).decode("ascii")
        attrs = brace.group(2) or b""
        return "brace", name, bool(attrs.strip(b" \t"))
    colon = re.fullmatch(
        rb" {0,3}:::([a-z]+)(?:[ \t]+([^\x00-\x08\x0b\x0c\x0e-\x1f\x7f]*))?[ \t]*", body
    )
    if colon is not None:
        return "colon", colon.group(1).decode("ascii"), bool((colon.group(2) or b"").strip())
    if re.fullmatch(rb" {0,3}:::[ \t]*", body):
        return "colon", "end", False
    return None


def _reserved_yfm(body: bytes) -> bool:
    stripped = body.lstrip(b" ")
    return len(body) - len(stripped) <= 3 and stripped.startswith((b"{%", b":::"))


def _yfm_source_owned_end(source: bytes, lines: tuple[_Line, ...], index: int) -> int | None:
    fence_end = _fence_end(source, lines, index)
    if fence_end is not None:
        return fence_end
    html = _html_block(source, lines, index, lines[index].start)
    return None if html is None else html[1]


def _yfm_end(source: bytes, lines: tuple[_Line, ...], index: int) -> tuple[BlockKind, int] | None:
    body = _body(source, lines[index])
    token = _directive(body)
    supported = {"include", "note", "cut", "tabs", "tab", "endnote", "endcut", "endtabs", "endtab"}
    if token is None:
        if _reserved_yfm(body):
            return BlockKind.UNKNOWN, lines[index].end
        return None
    form, name, _attrs = token
    if name not in supported and not (form == "colon" and name == "end"):
        return BlockKind.UNKNOWN, lines[index].end
    if name == "include" and form == "brace":
        return BlockKind.T008_YFM, lines[index].end
    if name.startswith("end") or name == "end":
        return BlockKind.UNKNOWN, lines[index].end
    if name not in {"note", "cut", "tabs", "tab"}:
        return BlockKind.UNKNOWN, lines[index].end
    stack: list[tuple[str, str]] = [(form, name)]
    allowed = {
        "note": {"note", "cut", "tabs", "tab"},
        "cut": {"note", "cut", "tabs", "tab"},
        "tabs": {"tab", "note", "cut"},
        "tab": {"note", "cut"},
    }
    probe = index + 1
    while probe < len(lines):
        source_owned_end = _yfm_source_owned_end(source, lines, probe)
        if source_owned_end is not None:
            probe = _line_index(lines, source_owned_end - 1) + 1
            continue
        candidate = _directive(_body(source, lines[probe]))
        if candidate is None:
            if _reserved_yfm(_body(source, lines[probe])):
                return BlockKind.UNKNOWN, lines[probe].end
            probe += 1
            continue
        inner_form, inner_name, inner_attrs = candidate
        if inner_name == "include" and inner_form == "brace":
            probe += 1
            continue
        if inner_name == "end" and inner_form == "colon":
            if stack[-1][0] != "colon":
                return BlockKind.UNKNOWN, lines[probe].end
            stack.pop()
        elif inner_name.startswith("end"):
            expected = "end" + stack[-1][1]
            if inner_form != "brace" or inner_name != expected or inner_attrs:
                return BlockKind.UNKNOWN, lines[probe].end
            stack.pop()
        elif inner_name in {"note", "cut", "tabs", "tab"}:
            if inner_name not in allowed[stack[-1][1]]:
                return BlockKind.UNKNOWN, lines[probe].end
            stack.append((inner_form, inner_name))
        else:
            return BlockKind.UNKNOWN, lines[probe].end
        if not stack:
            return BlockKind.T008_YFM, lines[probe].end
        probe += 1
    return BlockKind.UNKNOWN, len(source)


def _shift_field(field: _FieldDraft, offset: int) -> _FieldDraft:
    return _FieldDraft(
        field.kind,
        ByteSpan(field.span.start + offset, field.span.end + offset),
        tuple(
            ProtectedRegion(
                region.kind,
                ByteSpan(region.span.start + offset, region.span.end + offset),
                region.group,
            )
            for region in field.regions
        ),
    )


def _yfm_fields(
    source: bytes, lines: tuple[_Line, ...], start: int, end: int
) -> tuple[tuple[_FieldDraft, ...], bool]:
    directive_lines: list[_Line] = []
    probe = _line_index(lines, start)
    final_index = _line_index(lines, end - 1)
    while probe <= final_index:
        source_owned_end = _yfm_source_owned_end(source, lines, probe)
        if source_owned_end is not None:
            probe = _line_index(lines, min(source_owned_end, end) - 1) + 1
            continue
        line = lines[probe]
        if _reserved_yfm(_body(source, line)):
            directive_lines.append(line)
        probe += 1

    fields: list[_FieldDraft] = []
    for line in directive_lines:
        for span in yfm_title_spans(source, line.start, line.end):
            field, malformed = _field_for_span(
                source, lines, FieldKind.HEADING, span.start, span.end
            )
            if malformed:
                return (), True
            if field is not None:
                fields.append(field)

    segment_start = lines[_line_index(lines, start)].end
    for line in directive_lines[1:]:
        if segment_start < line.start:
            segment = source[segment_start : line.start]
            for block in _scan(segment, _lines(segment)):
                fields.extend(_shift_field(field, segment_start) for field in block.fields)
        segment_start = line.end
    if segment_start < end:
        segment = source[segment_start:end]
        for block in _scan(segment, _lines(segment)):
            fields.extend(_shift_field(field, segment_start) for field in block.fields)
    return tuple(sorted(fields, key=lambda field: field.span.start)), False


def _raw_end(source: bytes, lines: tuple[_Line, ...], name: bytes, start: int) -> int:
    cursor = start
    needle = b"</" + name
    lowered = source.lower()
    while True:
        found = lowered.find(needle, cursor)
        if found < 0:
            return len(source)
        token = _tag_at(source, found)
        if token is not None and token.closing and token.name == name:
            return lines[_line_index(lines, token.end - 1)].end
        cursor = found + 1


def _html_block(
    source: bytes, lines: tuple[_Line, ...], index: int, start: int
) -> tuple[BlockKind, int] | None:
    line = lines[index]
    body = _body(source, line, start)
    leading = len(body) - len(body.lstrip(b" "))
    if leading > 3:
        return None
    token_start = start + leading
    view = source[token_start:]
    terminator: bytes | None = None
    if view.startswith(b"<!--"):
        terminator = b"-->"
    elif view.startswith(b"<![CDATA["):
        terminator = b"]]>"
    if terminator is not None:
        found = source.find(terminator, token_start + len(view[: len(terminator)]))
        end = (
            len(source) if found < 0 else lines[_line_index(lines, found + len(terminator) - 1)].end
        )
        return BlockKind.T008_HTML, end
    if view.startswith(b"<?"):
        quoted_end = _outside_quote_end(source, token_start + 2, b"?>")
        end = len(source) if quoted_end is None else lines[_line_index(lines, quoted_end - 1)].end
        return BlockKind.T008_HTML, end
    if len(view) >= 3 and view.startswith(b"<!") and 65 <= view[2] <= 90:
        declaration_end = _outside_quote_end(source, token_start + 3, b">")
        end = (
            len(source)
            if declaration_end is None
            else lines[_line_index(lines, declaration_end - 1)].end
        )
        return BlockKind.T008_HTML, end
    if not _reserved_html_start(body):
        return None
    tag = _tag_at(source, token_start)
    if tag is None:
        if len(view) >= 2 and view[0] == 60 and (65 <= view[1] <= 90 or 97 <= view[1] <= 122):
            return BlockKind.UNKNOWN, len(source)
        return BlockKind.UNKNOWN, line.end
    listed = tag.name in _VOID or tag.name in _NORMAL or tag.name in _RAW
    if tag.closing:
        return (BlockKind.T008_HTML if listed else BlockKind.UNKNOWN), line.end
    if tag.self_closing or tag.name in _VOID:
        return (BlockKind.T008_HTML if listed else BlockKind.UNKNOWN), line.end
    if tag.name in _RAW:
        return BlockKind.T008_HTML, _raw_end(source, lines, tag.name, tag.end)
    end = _depth_close(source, lines, tag, raw_names=_RAW)
    return (BlockKind.T008_HTML if tag.name in _NORMAL else BlockKind.UNKNOWN), end


def _b15_candidate_end(source: bytes, lines: tuple[_Line, ...], index: int) -> int | None:
    body = _body(source, lines[index])
    has_control = _contains_control(body)
    yfm = None if has_control else _yfm_end(source, lines, index)
    html = _html_block(source, lines, index, lines[index].start)
    if has_control:
        if html is not None:
            return html[1]
        probe = index + 1
        while probe < len(lines) and _contains_control(_body(source, lines[probe])):
            probe += 1
        return lines[probe - 1].end
    if yfm is not None:
        return yfm[1] if yfm[0] is BlockKind.UNKNOWN else None
    if html is not None and html[0] is BlockKind.UNKNOWN:
        return html[1]
    return None


def _code_ranges(data: bytes, base: int) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    cursor = 0
    while cursor < len(data):
        if data[cursor] != 96:
            cursor += 1
            continue
        start = cursor
        while cursor < len(data) and data[cursor] == 96:
            cursor += 1
        count = cursor - start
        search = cursor
        while search < len(data):
            found = data.find(b"`", search)
            if found < 0:
                break
            end = found
            while end < len(data) and data[end] == 96:
                end += 1
            if end - found == count:
                result.append((base + start, base + end))
                cursor = end
                break
            search = end
        else:
            continue
        if not result or result[-1][0] != base + start:
            cursor = start + count
    return result


def _overlaps(start: int, end: int, regions: list[ProtectedRegion]) -> bool:
    return any(start < item.span.end and end > item.span.start for item in regions)


def _balanced_suffix(data: bytes, close_bracket: int) -> int | None:
    cursor = close_bracket + 1
    if cursor >= len(data):
        return None
    if data[cursor] == 91:
        end = data.find(b"]", cursor + 1)
        return None if end < 0 else end + 1
    if data[cursor] != 40:
        return None
    depth = 1
    quote: int | None = None
    cursor += 1
    while cursor < len(data):
        byte = data[cursor]
        if byte == 92:
            cursor += 2
            continue
        if quote is not None:
            if byte == quote:
                quote = None
        elif byte in (34, 39):
            quote = byte
        elif byte == 40:
            depth += 1
        elif byte == 41:
            depth -= 1
            if depth == 0:
                return cursor + 1
        cursor += 1
    return None


def _container_regions(
    data: bytes, base: int, occupied: list[ProtectedRegion]
) -> list[ProtectedRegion]:
    result: list[ProtectedRegion] = []
    cursor = 0
    group = 1
    while cursor < len(data):
        image = data.startswith(b"![", cursor)
        if not image and data[cursor : cursor + 1] != b"[":
            cursor += 1
            continue
        open_end = cursor + (2 if image else 1)
        if _overlaps(base + cursor, base + open_end, occupied):
            cursor = open_end
            continue
        probe = open_end
        depth = 0
        close = -1
        while probe < len(data):
            if _overlaps(base + probe, base + probe + 1, occupied):
                probe += 1
                continue
            if data[probe] == 92:
                probe += 2
                continue
            if data[probe] == 91:
                depth += 1
            elif data[probe] == 93:
                if depth == 0:
                    close = probe
                    break
                depth -= 1
            probe += 1
        suffix_end = None if close < 0 else _balanced_suffix(data, close)
        if close < 0 or suffix_end is None or _overlaps(base + close, base + suffix_end, occupied):
            cursor = open_end
            continue
        open_kind = ProtectedKind.IMAGE_OPEN if image else ProtectedKind.LINK_OPEN
        close_kind = ProtectedKind.IMAGE_CLOSE if image else ProtectedKind.LINK_CLOSE
        outer_group = group
        group += 1
        result.extend(
            (
                ProtectedRegion(open_kind, ByteSpan(base + cursor, base + open_end), outer_group),
                ProtectedRegion(close_kind, ByteSpan(base + close, base + suffix_end), outer_group),
            )
        )
        nested = _container_regions(data[open_end:close], base + open_end, occupied)
        nested_groups = sorted({item.group for item in nested if item.group is not None})
        group_map = {
            nested_group: group + index for index, nested_group in enumerate(nested_groups)
        }
        result.extend(
            ProtectedRegion(
                item.kind,
                item.span,
                None if item.group is None else group_map[item.group],
            )
            for item in nested
        )
        group += len(nested_groups)
        cursor = suffix_end
    return sorted(result, key=lambda item: (item.span.start, item.span.end))


def _flanking(data: bytes, start: int, end: int) -> tuple[bool, bool]:
    def category(byte: int | None) -> str:
        if byte is None or byte in b" \t\r\n":
            return "W"
        if (
            0x21 <= byte <= 0x2F
            or 0x3A <= byte <= 0x40
            or 0x5B <= byte <= 0x60
            or 0x7B <= byte <= 0x7E
        ):
            return "P"
        return "O"

    previous = category(data[start - 1] if start else None)
    following = category(data[end] if end < len(data) else None)
    left = following != "W" and (following != "P" or previous in {"W", "P"})
    right = previous != "W" and (previous != "P" or following in {"W", "P"})
    return left, right


def _inline_regions(
    source: bytes, span: ByteSpan, lines: tuple[_Line, ...]
) -> tuple[tuple[ProtectedRegion, ...], bool]:
    data = source[span.start : span.end]
    regions = [
        ProtectedRegion(ProtectedKind.INLINE_CODE, ByteSpan(start, end), None)
        for start, end in _code_ranges(data, span.start)
    ]
    regions.extend(_container_regions(data, span.start, regions))
    occupied = bytearray(len(data))

    def mark(region: ProtectedRegion) -> None:
        start = region.span.start - span.start
        end = region.span.end - span.start
        occupied[start:end] = b"\1" * (end - start)

    def overlaps(start: int, end: int) -> bool:
        return occupied.find(b"\1", start - span.start, end - span.start) >= 0

    def append(region: ProtectedRegion) -> None:
        regions.append(region)
        mark(region)

    for region in regions:
        mark(region)
    cursor = 0
    malformed = False
    while cursor < len(data):
        absolute = span.start + cursor
        if overlaps(absolute, absolute + 1):
            cursor += 1
            continue
        match = _URI_AUTOLINK.match(data, cursor)
        if match is None:
            email = _EMAIL_AUTOLINK.match(data, cursor)
            if email is not None:
                domain = email.group().rsplit(b"@", 1)[1][:-1]
                if len(domain) <= 253:
                    match = email
        if match is not None:
            append(
                ProtectedRegion(
                    ProtectedKind.AUTOLINK,
                    ByteSpan(span.start + match.start(), span.start + match.end()),
                    None,
                )
            )
            cursor = match.end()
            continue
        if data.startswith(b"<!--", cursor):
            end = data.find(b"-->", cursor + 4)
            if end < 0 or b"\n" in data[cursor : end + 3] or b"\r" in data[cursor : end + 3]:
                malformed = True
                break
            append(
                ProtectedRegion(
                    ProtectedKind.HTML_INLINE,
                    ByteSpan(absolute, span.start + end + 3),
                    None,
                )
            )
            cursor = end + 3
            continue
        if (
            data[cursor : cursor + 1] == b"<"
            and cursor + 1 < len(data)
            and (
                data[cursor + 1 : cursor + 2] == b"/"
                or data[cursor + 1] in b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
            )
        ):
            line_limit = min(
                (
                    position
                    for position in (data.find(b"\n", cursor), data.find(b"\r", cursor))
                    if position >= 0
                ),
                default=len(data),
            )
            tag = _tag_at(data, cursor, line_limit)
            if tag is None:
                malformed = True
                break
            append(
                ProtectedRegion(
                    ProtectedKind.HTML_INLINE,
                    ByteSpan(absolute, span.start + tag.end),
                    None,
                )
            )
            cursor = tag.end
            continue
        if data.startswith((b"{{", b"{%"), cursor):
            closer = b"}}" if data.startswith(b"{{", cursor) else b"%}"
            line_end = min(
                (
                    position
                    for position in (data.find(b"\n", cursor), data.find(b"\r", cursor))
                    if position >= 0
                ),
                default=len(data),
            )
            end = data.find(closer, cursor + 2, line_end)
            if end < 0:
                malformed = True
                break
            append(
                ProtectedRegion(
                    ProtectedKind.TEMPLATE,
                    ByteSpan(absolute, span.start + end + 2),
                    None,
                )
            )
            cursor = end + 2
            continue
        anchor = _ANCHOR.match(data, cursor)
        if anchor is not None:
            append(
                ProtectedRegion(
                    ProtectedKind.EXPLICIT_ANCHOR,
                    ByteSpan(span.start + anchor.start(), span.start + anchor.end()),
                    None,
                )
            )
            cursor = anchor.end()
            continue
        if data[cursor : cursor + 1] == b"\\" and cursor + 1 < len(data):
            escaped = data[cursor + 1]
            if (
                0x21 <= escaped <= 0x2F
                or 0x3A <= escaped <= 0x40
                or 0x5B <= escaped <= 0x60
                or 0x7B <= escaped <= 0x7E
            ):
                append(
                    ProtectedRegion(ProtectedKind.ESCAPE, ByteSpan(absolute, absolute + 2), None)
                )
                cursor += 2
                continue
        match = _URL.match(data, cursor)
        if match is not None:
            end = match.end()
            while end > match.start() and data[end - 1] in b".,;:!?":
                end -= 1
            for closing, opening_byte in ((41, 40), (93, 91), (125, 123)):
                while (
                    end > match.start()
                    and data[end - 1] == closing
                    and data[match.start() : end].count(bytes([closing]))
                    > data[match.start() : end].count(bytes([opening_byte]))
                ):
                    end -= 1
            append(ProtectedRegion(ProtectedKind.URL, ByteSpan(absolute, span.start + end), None))
            cursor = end
            continue
        match = _PATH.match(data, cursor)
        if match is not None:
            end = match.end()
            terminal_start = data.rfind(b"/", match.start(), end) + 1
            terminal = data[terminal_start:end]
            if terminal and not terminal.strip(b"."):
                end -= max(len(terminal) - 2, 0)
            else:
                while end > match.start() and data[end - 1] == 46:
                    end -= 1
            raw_path = data[match.start() : end]
            before = data[cursor - 1] if cursor else None
            after = data[end] if end < len(data) else None
            leading_boundary = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-/"
            trailing_boundary = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-/"
            explicit = raw_path.startswith((b"/", b"./", b"../"))
            hierarchical = raw_path.count(b"/") >= 2
            filename = _FILENAME.fullmatch(raw_path.rsplit(b"/", 1)[-1]) is not None
            if (
                raw_path.lower() not in {b"e.g", b"i.e"}
                and
                (explicit or hierarchical or filename)
                and (before is None or before not in leading_boundary)
                and (after is None or after not in trailing_boundary)
            ):
                append(
                    ProtectedRegion(
                        ProtectedKind.PATH,
                        ByteSpan(span.start + match.start(), span.start + end),
                        None,
                    )
                )
                cursor = end
                continue
        match = _IDENTIFIER.match(data, cursor)
        if match is not None:
            raw = match.group()
            before = data[cursor - 1] if cursor else None
            after = data[match.end()] if match.end() < len(data) else None
            boundary = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_:"
            marker_run = raw.startswith(b"_") and len(raw) > 1 and raw[1] == 95
            qualified = (b"_" in raw or b"::" in raw) and not marker_run
            if (
                qualified
                and (before is None or before not in boundary)
                and (after is None or after not in boundary)
            ):
                append(
                    ProtectedRegion(
                        ProtectedKind.IDENTIFIER,
                        ByteSpan(span.start + match.start(), span.start + match.end()),
                        None,
                    )
                )
                cursor = match.end()
                continue
        cursor += 1

    if malformed:
        return (), True

    marker_candidates: list[tuple[int, int, bytes, bool, bool]] = []
    cursor = 0
    while cursor < len(data):
        if data[cursor] not in (42, 95, 126) or overlaps(
            span.start + cursor, span.start + cursor + 1
        ):
            cursor += 1
            continue
        end = cursor + 1
        while end < len(data) and data[end] == data[cursor]:
            end += 1
        length = end - cursor
        left, right = _flanking(data, cursor, end)
        if (data[cursor] in (42, 95) and length >= 4 or data[cursor] == 126 and length >= 3) and (
            left or right
        ):
            return (), True
        eligible = (
            data[cursor] in (42, 95) and length in (1, 2, 3) or data[cursor] == 126 and length == 2
        )
        if eligible:
            if data[cursor] == 95:
                previous = data[cursor - 1] if cursor else None
                following = data[end] if end < len(data) else None
                _, _ = previous, following
                can_open = left and (
                    not right or previous is not None and _flanking_punctuation(previous)
                )
                can_close = right and (
                    not left or following is not None and _flanking_punctuation(following)
                )
            else:
                can_open, can_close = left, right
            marker_candidates.append((cursor, end, data[cursor : cursor + 1], can_open, can_close))
        cursor = end
    stacks: dict[tuple[bytes, int], list[tuple[int, int]]] = {}
    for start, end, marker, can_open, can_close in marker_candidates:
        key = (marker, end - start)
        stack = stacks.setdefault(key, [])
        if can_close and stack:
            opening_run = stack.pop()
            append(
                ProtectedRegion(
                    ProtectedKind.MARKDOWN_SYNTAX,
                    ByteSpan(span.start + opening_run[0], span.start + opening_run[1]),
                    None,
                )
            )
            append(
                ProtectedRegion(
                    ProtectedKind.MARKDOWN_SYNTAX,
                    ByteSpan(span.start + start, span.start + end),
                    None,
                )
            )
        elif can_open:
            stack.append((start, end))

    first_line_index = _line_index(lines, span.start)
    last_line_index = _line_index(lines, span.end - 1)
    for line_index in range(first_line_index, last_line_index):
        line = lines[line_index]
        transition_start = line.body_end
        while (
            transition_start > max(line.start, span.start)
            and source[transition_start - 1] in b" \t"
        ):
            transition_start -= 1
        if not overlaps(transition_start, line.end):
            append(
                ProtectedRegion(
                    ProtectedKind.LINE_BREAK, ByteSpan(transition_start, line.end), None
                )
            )
        next_line = lines[line_index + 1]
        prefix_end = _content_start(_body(source, next_line), next_line.start, continuation=True)
        if (
            prefix_end > next_line.start
            and prefix_end < span.end
            and not overlaps(next_line.start, prefix_end)
        ):
            append(
                ProtectedRegion(
                    ProtectedKind.CONTINUATION_PREFIX,
                    ByteSpan(next_line.start, prefix_end),
                    None,
                )
            )
    return tuple(sorted(regions, key=lambda item: (item.span.start, item.span.end))), False


def _flanking_punctuation(byte: int) -> bool:
    return (
        0x21 <= byte <= 0x2F or 0x3A <= byte <= 0x40 or 0x5B <= byte <= 0x60 or 0x7B <= byte <= 0x7E
    )


def _content_start(body: bytes, absolute: int, *, continuation: bool = False) -> int:
    cursor = 0
    while cursor < len(body) and cursor < 3 and body[cursor] == 32:
        cursor += 1
    quote_cursor = cursor
    saw_quote = False
    while quote_cursor < len(body) and body[quote_cursor] == 62:
        saw_quote = True
        quote_cursor += 1
        if quote_cursor < len(body) and body[quote_cursor] in b" \t":
            quote_cursor += 1
    if saw_quote:
        return absolute + quote_cursor
    if continuation:
        while cursor < len(body) and body[cursor] in b" \t":
            cursor += 1
    return absolute + cursor


def _has_unprotected_alnum(
    source: bytes, span: ByteSpan, regions: tuple[ProtectedRegion, ...]
) -> bool:
    pieces: list[bytes] = []
    cursor = span.start
    for region in regions:
        pieces.append(source[cursor : region.span.start])
        cursor = region.span.end
    pieces.append(source[cursor : span.end])
    return any(character.isalnum() for character in b"".join(pieces).decode("utf-8"))


def _field_for_span(
    source: bytes,
    lines: tuple[_Line, ...],
    kind: FieldKind,
    start: int,
    end: int,
    *,
    trim_trailing: bool = True,
) -> tuple[_FieldDraft | None, bool]:
    while trim_trailing and end > start and source[end - 1] in b" \t":
        end -= 1
    if start >= end:
        return None, False
    span = ByteSpan(start, end)
    regions, malformed = _inline_regions(source, span, lines)
    if malformed:
        return None, True
    if not _has_unprotected_alnum(source, span, regions):
        return None, False
    return _FieldDraft(kind, span, regions), False


def _split_table_row(source: bytes, line: _Line) -> list[tuple[int, int]] | None:
    body = _body(source, line)
    code = _code_ranges(body, line.start)
    cuts: list[int] = []
    cursor = 0
    while cursor < len(body):
        absolute = line.start + cursor
        if body[cursor] == 92:
            cursor += 2
            continue
        if body[cursor] == 124 and not any(start <= absolute < end for start, end in code):
            cuts.append(cursor)
        cursor += 1
    if not cuts:
        return None
    boundaries = [-1, *cuts, len(body)]
    cells = [(boundaries[i] + 1, boundaries[i + 1]) for i in range(len(boundaries) - 1)]
    if cells and body.startswith(b"|"):
        cells.pop(0)
    if cells and body.endswith(b"|"):
        cells.pop()
    return [(line.start + start, line.start + end) for start, end in cells]


def _trim_cell(source: bytes, cell: tuple[int, int]) -> tuple[int, int]:
    start, end = cell
    while start < end and source[start] in b" \t":
        start += 1
    while end > start and source[end - 1] in b" \t":
        end -= 1
    return start, end


def _table_end(
    source: bytes, lines: tuple[_Line, ...], index: int
) -> tuple[int, list[list[tuple[int, int]]]] | None:
    if index + 1 >= len(lines):
        return None
    header = _split_table_row(source, lines[index])
    delimiter = _split_table_row(source, lines[index + 1])
    if header is None or delimiter is None or len(header) != len(delimiter):
        return None
    if not all(
        _DELIMITER_CELL.fullmatch(source[start:end].strip(b" \t")) for start, end in delimiter
    ):
        return None
    rows = [header]
    probe = index + 2
    while probe < len(lines):
        cells = _split_table_row(source, lines[probe])
        if cells is None or len(cells) != len(header):
            break
        rows.append(cells)
        probe += 1
    return lines[probe - 1].end, rows


def _starts_stronger(
    source: bytes,
    lines: tuple[_Line, ...],
    index: int,
    *,
    body: bytes | None = None,
) -> bool:
    body = _body(source, lines[index]) if body is None else body
    if _contains_control(body):
        return True
    if body.strip(b" \t") == b"":
        return True
    if _indent(body) >= 4 or body.startswith(b"\t"):
        return True
    if (
        _FENCE.match(body)
        or _ATX.match(body)
        or _LIST.match(body)
        or _THEMATIC.match(body)
        or _REFERENCE.match(body)
    ):
        return True
    if _reserved_yfm(body) or _reserved_html_start(body):
        return True
    return _table_end(source, lines, index) is not None


def _paragraph_end(source: bytes, lines: tuple[_Line, ...], index: int) -> int:
    first = _body(source, lines[index])
    first_start = _content_start(first, lines[index].start)
    blockquote = first_start > lines[index].start + min(_indent(first), 3)
    probe = index + 1
    while probe < len(lines):
        body = _body(source, lines[probe])
        if not body.strip(b" \t") or _starts_stronger(source, lines, probe):
            break
        if blockquote:
            content = _content_start(body, lines[probe].start)
            if content == lines[probe].start + min(_indent(body), 3):
                break
        elif body.startswith(b"\t") or _indent(body) >= 4:
            break
        probe += 1
    return lines[probe - 1].end


def _list_end(source: bytes, lines: tuple[_Line, ...], index: int, column: int) -> int:
    probe = index + 1
    last = index
    while probe < len(lines):
        body = _body(source, lines[probe])
        if not body.strip(b" \t"):
            following = _next_nonblank(source, lines, probe + 1)
            if following is None or _indent(_body(source, lines[following])) < column:
                break
            last = probe
            probe += 1
            continue
        if _LIST.match(body):
            break
        if _indent(body) < column or _starts_stronger(source, lines, probe):
            break
        last = probe
        probe += 1
    return lines[last].end


def _known_field_draft(
    source: bytes,
    lines: tuple[_Line, ...],
    kind: BlockKind,
    start: int,
    end: int,
    extra: object = None,
) -> tuple[tuple[_FieldDraft, ...], bool]:
    first_index = _line_index(lines, start)
    first_line = lines[first_index]
    body = _body(source, first_line, start)
    if kind is BlockKind.ATX_HEADING:
        match = _ATX.match(body)
        assert match is not None
        content_start = start + match.end()
        content_end = first_line.body_end
        close = re.search(rb"[ \t]+#+[ \t]*$", source[content_start:content_end])
        if close is not None:
            content_end = content_start + close.start()
        field_kind = FieldKind.HEADING
    elif kind is BlockKind.SETEXT_HEADING:
        content_start = _content_start(body, start)
        content_end = first_line.body_end
        field_kind = FieldKind.HEADING
    elif kind is BlockKind.LIST_ITEM:
        match = _LIST.match(body)
        assert match is not None
        content_start = start + match.end()
        last_line = lines[_line_index(lines, end - 1)]
        content_end = last_line.body_end
        field_kind = FieldKind.LIST_ITEM
    else:
        content_start = _content_start(body, start)
        last_line = lines[_line_index(lines, end - 1)]
        content_end = last_line.body_end
        field_kind = FieldKind.PARAGRAPH
    candidate, malformed = _field_for_span(source, lines, field_kind, content_start, content_end)
    return (() if candidate is None else (candidate,)), malformed


def _scan(source: bytes, lines: tuple[_Line, ...]) -> list[_BlockDraft]:
    drafts: list[_BlockDraft] = []
    cursor = 0
    first_post_bom = 0
    if source.startswith(b"\xef\xbb\xbf"):
        drafts.append(_BlockDraft(BlockKind.UTF8_BOM, ByteSpan(0, 3), ()))
        cursor = 3
        first_post_bom = 3
    while cursor < len(source):
        index = _line_index(lines, cursor)
        line = lines[index]
        body = _body(source, line, cursor)
        start = cursor
        kind: BlockKind
        end: int
        fields: tuple[_FieldDraft, ...] = ()

        has_control = _contains_control(body)
        front = (
            _front_matter_end(source, lines, index, cursor)
            if cursor == first_post_bom and not has_control
            else None
        )
        fence = None if has_control else _fence_end(source, lines, index)
        yfm = None if has_control else _yfm_end(source, lines, index)
        html = _html_block(source, lines, index, cursor)
        if has_control and html is not None:
            html = (BlockKind.UNKNOWN, html[1])
        table = _table_end(source, lines, index)
        if front is not None:
            kind, end = front
            if kind is BlockKind.T008_FRONT_MATTER:
                front_fields: list[_FieldDraft] = []
                malformed = False
                for field_kind, span in frontmatter_fields(source, start, end):
                    quoted = (
                        span.start > 0
                        and span.end < len(source)
                        and source[span.start - 1] == source[span.end]
                        and source[span.start - 1] in (34, 39)
                    )
                    line_start = source.rfind(b"\n", 0, span.start) + 1
                    prefix = source[line_start : span.start]
                    block_scalar = bool(prefix) and not prefix.strip(b" \t")
                    multiline_plain = (
                        not quoted
                        and not block_scalar
                        and any(
                            newline in source[span.start : span.end] for newline in (b"\n", b"\r")
                        )
                    )
                    field, bad = _field_for_span(
                        source,
                        lines,
                        field_kind,
                        span.start,
                        span.end,
                        trim_trailing=not quoted,
                    )
                    malformed = malformed or bad
                    if field is not None:
                        representation_kinds = {ProtectedKind.ESCAPE}
                        if quoted or multiline_plain:
                            representation_kinds.update(
                                {ProtectedKind.LINE_BREAK, ProtectedKind.CONTINUATION_PREFIX}
                            )
                        field = _FieldDraft(
                            field.kind,
                            field.span,
                            tuple(
                                region
                                for region in field.regions
                                if region.kind not in representation_kinds
                            ),
                        )
                        front_fields.append(field)
                if malformed:
                    kind = BlockKind.UNKNOWN
                else:
                    fields = tuple(front_fields)
        elif fence is not None:
            kind, end = BlockKind.T008_FENCE, fence
            fields = _fence_fields(source, lines, index, end)
        elif yfm is not None:
            kind, end = yfm
            if kind is BlockKind.T008_YFM:
                fields, malformed = _yfm_fields(source, lines, start, end)
                if malformed:
                    kind = BlockKind.UNKNOWN
        elif html is not None:
            kind, end = html
        elif not body.strip(b" \t"):
            probe = index + 1
            while probe < len(lines) and not _body(source, lines[probe]).strip(b" \t"):
                probe += 1
            kind, end = BlockKind.BLANK, lines[probe - 1].end
        elif has_control:
            probe = index + 1
            while probe < len(lines) and _contains_control(_body(source, lines[probe])):
                probe += 1
            kind, end = BlockKind.UNKNOWN, lines[probe - 1].end
        elif body.startswith(b"\t") or _indent(body) >= 4:
            probe = index + 1
            last = index
            while probe < len(lines):
                candidate = _body(source, lines[probe])
                if candidate.strip(b" \t") and not (
                    candidate.startswith(b"\t") or _indent(candidate) >= 4
                ):
                    break
                if not candidate.strip(b" \t"):
                    following = _next_nonblank(source, lines, probe + 1)
                    if following is None or not (
                        _body(source, lines[following]).startswith(b"\t")
                        or _indent(_body(source, lines[following])) >= 4
                    ):
                        break
                last = probe
                probe += 1
            kind, end = BlockKind.INDENTED_CODE, lines[last].end
        elif _THEMATIC.match(body):
            kind, end = BlockKind.THEMATIC_BREAK, line.end
        elif _REFERENCE.match(body):
            probe = index + 1
            while probe < len(lines):
                candidate = _body(source, lines[probe])
                if not candidate.strip(b" \t"):
                    break
                if candidate.startswith(b"\t"):
                    continuation = candidate[1:]
                elif _indent(candidate) >= 2:
                    continuation = candidate.lstrip(b" ")
                else:
                    break
                if _starts_stronger(source, lines, probe, body=continuation):
                    break
                probe += 1
            kind, end = BlockKind.REFERENCE_DEFINITION, lines[probe - 1].end
        elif table is not None:
            end, rows = table
            table_fields: list[_FieldDraft] = []
            malformed = False
            for row in rows:
                for cell in row:
                    cell_start, cell_end = _trim_cell(source, cell)
                    field_candidate, bad = _field_for_span(
                        source, lines, FieldKind.TABLE_CELL, cell_start, cell_end
                    )
                    malformed = malformed or bad
                    if field_candidate is not None:
                        table_fields.append(field_candidate)
            kind = BlockKind.UNKNOWN if malformed else BlockKind.TABLE
            fields = () if malformed else tuple(table_fields)
        elif _ATX.match(body):
            kind, end = BlockKind.ATX_HEADING, line.end
            fields, malformed = _known_field_draft(source, lines, kind, start, end)
            if malformed:
                kind, fields = BlockKind.UNKNOWN, ()
        elif index + 1 < len(lines) and _SETEXT.match(_body(source, lines[index + 1])):
            kind, end = BlockKind.SETEXT_HEADING, lines[index + 1].end
            fields, malformed = _known_field_draft(source, lines, kind, start, end)
            if malformed:
                kind, fields = BlockKind.UNKNOWN, ()
        else:
            list_match = _LIST.match(body)
            if list_match is not None:
                column = list_match.end()
                kind, end = BlockKind.LIST_ITEM, _list_end(source, lines, index, column)
                fields, malformed = _known_field_draft(source, lines, kind, start, end)
                if malformed:
                    kind, fields = BlockKind.UNKNOWN, ()
            elif _reserved_yfm(body) or _reserved_html_start(body):
                kind, end = BlockKind.UNKNOWN, line.end
            else:
                kind, end = BlockKind.PARAGRAPH, _paragraph_end(source, lines, index)
                fields, malformed = _known_field_draft(source, lines, kind, start, end)
                if malformed:
                    kind, fields = BlockKind.UNKNOWN, ()
        if kind is BlockKind.UNKNOWN:
            while end < len(source):
                candidate_end = _b15_candidate_end(source, lines, _line_index(lines, end))
                if candidate_end is None:
                    break
                end = candidate_end
        if end <= start:
            raise AssertionError("scanner failed to consume source")
        drafts.append(_BlockDraft(kind, ByteSpan(start, end), fields))
        cursor = end
    return drafts


def build_markdown_plan(
    source_snapshot: SnapshotRef,
    source_path: RepoPath,
    source: bytes,
    /,
) -> SourcePlan:
    _exact(source_snapshot, SnapshotRef, "build_markdown_plan", "source_snapshot")
    _exact(source_path, RepoPath, "build_markdown_plan", "source_path")
    _exact(source, bytes, "build_markdown_plan", "source")
    if len(source) > 2**64 - 1:
        raise _invariant(
            "build_markdown_plan",
            "source",
            "byte length <= 18446744073709551615",
        )
    digest = ContentHash(hashlib.sha256(source).hexdigest())
    try:
        source.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise InvalidPlanInput(
            PlanInputReason.INVALID_UTF8_SOURCE, source_path, error.start
        ) from None
    physical_lines = _lines(source)
    drafts = _scan(source, physical_lines) if source else []
    ordinal = 0
    seen_ids: set[str] = set()
    seen_digests: dict[str, tuple[str, int, int, int, str]] = {}
    blocks: list[Block] = []
    for draft in drafts:
        built_fields: list[Field] = []
        for item in draft.fields:
            ordinal += 1
            field_id = make_field_id(digest, ordinal, item.span, item.kind)
            descriptor = (
                digest.value,
                ordinal,
                item.span.start,
                item.span.end,
                item.kind.value,
            )
            component = field_id.value.rsplit("-", 1)[1]
            if field_id.value in seen_ids or (
                component in seen_digests and seen_digests[component] != descriptor
            ):
                raise InvalidPlanInput(PlanInputReason.FIELD_ID_COLLISION, source_path, None)
            seen_ids.add(field_id.value)
            seen_digests[component] = descriptor
            built_fields.append(
                Field(
                    field_id,
                    item.kind,
                    item.span,
                    _line_range(physical_lines, item.span),
                    item.regions,
                )
            )
        blocks.append(
            Block(
                draft.kind,
                draft.span,
                _line_range(physical_lines, draft.span),
                tuple(built_fields),
            )
        )
    diagnostics = tuple(
        PlanDiagnostic(
            Diagnostic(
                Severity.RED,
                "unknown_markdown_block",
                "Markdown block could not be classified safely",
                "Fix the malformed Markdown or add parser support before translation",
                source_path,
                block.lines.start,
                block.lines.end,
                None,
            ),
            block.span,
        )
        for block in blocks
        if block.kind is BlockKind.UNKNOWN
    )
    plan = SourcePlan(
        PLAN_VERSION,
        source_snapshot,
        source_path,
        digest,
        len(source),
        len(physical_lines),
        tuple(blocks),
        diagnostics,
    )
    validate_source_plan(source, plan)
    return plan


def build_scope_plans(manifest: ScopeManifest, /) -> tuple[SourcePlan, ...]:
    _exact(manifest, ScopeManifest, "build_scope_plans", "manifest")
    return tuple(
        build_markdown_plan(manifest.scope_snapshot, entry.pair.source_path, entry.source_content)
        for entry in manifest.entries
        if entry.operation in {FileOperation.TRANSLATE, FileOperation.RENAME_TARGET_AND_TRANSLATE}
        and entry.source_content is not None
    )
