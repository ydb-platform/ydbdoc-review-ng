"""Deterministic localization of protected documentation link destinations."""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Callable, Mapping

from ydbdoc_review_ng.anchors import markdown_heading_outline

_YDB_HOSTS = frozenset({"ydb.tech", "www.ydb.tech"})
_WIKIPEDIA_HOST = re.compile(r"^(?P<locale>[a-z][a-z0-9-]*)\.wikipedia\.org$")
_MAX_WIKIPEDIA_RESPONSE_BYTES = 1_000_000

JsonFetcher = Callable[[str], object]

_MARKDOWN_DESTINATION = re.compile(rb"(?<!!)\[[^\]]*\]\(<?([^\s)>]+)>?\)")
_REFERENCE_DESTINATION = re.compile(
    rb"(?m)^ {0,3}\[[^\]\r\n]+\]:[ \t]+<?([^\s>]+)>?"
)
_URI_AUTOLINK_DESTINATION = re.compile(
    rb"<((?:https?|ftp)://[^\s<>]+)>"
)


def _anchor_key(value: str) -> str:
    return "-".join(part.removesuffix("s") for part in value.split("-"))


def closest_target_anchor(fragment: str, anchors: set[str] | frozenset[str], /) -> str | None:
    """Resolve the conservative singular/plural spelling difference, or decline."""
    key = _anchor_key(fragment.casefold())
    matches = sorted(anchor for anchor in anchors if _anchor_key(anchor.casefold()) == key)
    return matches[0] if len(matches) == 1 else None


def paired_target_anchor(fragment: str, source_page: bytes, target_page: bytes, /) -> str | None:
    """Map a heading only inside an unambiguous interval of shared anchor landmarks."""
    source_outline = markdown_heading_outline(source_page)
    target_outline = markdown_heading_outline(target_page)
    if not source_outline or not target_outline:
        return None
    matches = [index for index, (_level, anchor) in enumerate(source_outline) if anchor == fragment]
    if len(matches) != 1:
        return None
    source_index = matches[0]
    source_counts = Counter(anchor for _level, anchor in source_outline if anchor is not None)
    target_counts = Counter(anchor for _level, anchor in target_outline if anchor is not None)
    target_positions = {
        anchor: index
        for index, (_level, anchor) in enumerate(target_outline)
        if anchor is not None and target_counts[anchor] == 1
    }
    landmarks = [
        (index, target_positions[anchor])
        for index, (_level, anchor) in enumerate(source_outline)
        if anchor is not None
        and source_counts[anchor] == 1
        and anchor in target_positions
        and source_outline[index][0] == target_outline[target_positions[anchor]][0]
    ]
    if source_outline[source_index][1] in target_positions:
        return source_outline[source_index][1]
    before = [pair for pair in landmarks if pair[0] < source_index]
    after = [pair for pair in landmarks if pair[0] > source_index]
    if not before or not after:
        return None
    source_before, target_before = before[-1]
    source_after, target_after = after[0]
    if target_before >= target_after:
        return None
    source_segment = source_outline[source_before + 1 : source_after]
    target_segment = target_outline[target_before + 1 : target_after]
    if len(source_segment) != 1 or tuple(level for level, _anchor in source_segment) != tuple(
        level for level, _anchor in target_segment
    ):
        return None
    target_index = target_before + (source_index - source_before)
    target_anchor = target_outline[target_index][1]
    if target_anchor is None or target_counts[target_anchor] != 1:
        return None
    return target_anchor


def _opaque_markdown_spans(content: bytes, /) -> tuple[tuple[int, int], ...]:
    """Return fenced, indented, inline-code, frontmatter, and comment byte spans."""
    spans: list[tuple[int, int]] = []
    lines = content.splitlines(keepends=True)
    offsets: list[int] = []
    cursor = 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line)

    def container_body(line: bytes) -> bytes:
        body = line.rstrip(b"\r\n")
        cursor = 0
        while True:
            prefix = re.match(rb" {0,3}>[ \t]?", body[cursor:])
            if prefix is None:
                return body[cursor:]
            cursor += prefix.end()

    index = 0
    if lines and lines[0].rstrip(b"\r\n") == b"---":
        for probe in range(1, len(lines)):
            if lines[probe].rstrip(b"\r\n") in {b"---", b"..."}:
                spans.append((0, offsets[probe] + len(lines[probe])))
                index = probe + 1
                break
    while index < len(lines):
        body = container_body(lines[index])
        opening = re.match(rb"^ {0,3}(`{3,}|~{3,})", body)
        if opening is not None:
            marker = opening.group(1)
            closing = re.compile(
                rb"^ {0,3}" + re.escape(marker[:1]) + rb"{" + str(len(marker)).encode() + rb",}[ \t]*$"
            )
            probe = index + 1
            while probe < len(lines) and closing.match(container_body(lines[probe])) is None:
                probe += 1
            end = len(content) if probe == len(lines) else offsets[probe] + len(lines[probe])
            spans.append((offsets[index], end))
            index = probe + 1
            continue
        if body.startswith((b"    ", b"\t")):
            spans.append((offsets[index], offsets[index] + len(lines[index])))
        index += 1

    for match in re.finditer(rb"<!--[\s\S]*?(?:-->|\Z)", content):
        spans.append((match.start(), match.end()))

    def occupied(start: int, end: int) -> bool:
        return any(start < opaque_end and end > opaque_start for opaque_start, opaque_end in spans)

    cursor = 0
    while cursor < len(content):
        if content[cursor] != 0x60 or occupied(cursor, cursor + 1):
            cursor += 1
            continue
        end_run = cursor + 1
        while end_run < len(content) and content[end_run] == 0x60:
            end_run += 1
        marker = content[cursor:end_run]
        closing = content.find(marker, end_run)
        if closing < 0:
            cursor = end_run
            continue
        spans.append((cursor, closing + len(marker)))
        cursor = closing + len(marker)
    return tuple(sorted(spans))


def _markdown_destination_spans(content: bytes, /) -> tuple[tuple[int, int, str], ...]:
    """Inventory model-visible Markdown destinations without double-counting overlaps."""
    opaque = _opaque_markdown_spans(content)
    matches = []
    for pattern in (
        _MARKDOWN_DESTINATION,
        _REFERENCE_DESTINATION,
        _URI_AUTOLINK_DESTINATION,
    ):
        for match in pattern.finditer(content):
            if any(
                match.start(1) < opaque_end and match.end(1) > opaque_start
                for opaque_start, opaque_end in opaque
            ):
                continue
            matches.append((match.start(1), match.end(1), match.group(1).decode("utf-8")))
    result: list[tuple[int, int, str]] = []
    for item in sorted(matches):
        if any(item[0] < end and item[1] > start for start, end, _value in result):
            continue
        result.append(item)
    return tuple(result)


def markdown_link_destinations(content: bytes, /) -> tuple[str, ...]:
    return tuple(value for _start, _end, value in _markdown_destination_spans(content))


def _same_internal_page(source: str, target: str) -> bool:
    """Return whether two destinations address the same YDB documentation page."""
    try:
        left = urllib.parse.urlsplit(source)
        right = urllib.parse.urlsplit(target)
    except ValueError:
        return False
    if (left.scheme or left.netloc or right.scheme or right.netloc) and (
        (left.hostname or "") not in _YDB_HOSTS
        or (right.hostname or "") not in _YDB_HOSTS
    ):
        return False
    left_path = re.sub(r"^/docs/(?:ru|en)(?=/|$)", "/docs/{locale}", left.path)
    right_path = re.sub(r"^/docs/(?:ru|en)(?=/|$)", "/docs/{locale}", right.path)
    return left_path == right_path and left.query == right.query


def existing_target_link_overrides(source: bytes, target: bytes | None, /) -> dict[str, str]:
    """Reuse a target-local fragment only when it unambiguously belongs to the same page."""
    if target is None:
        return {}
    source_values = list(markdown_link_destinations(source))
    target_values = list(markdown_link_destinations(target))
    result: dict[str, str] = {}
    for source_value in source_values:
        matches = {
            target_value
            for target_value in target_values
            if _same_internal_page(source_value, target_value)
        }
        if len(matches) == 1:
            target_value = matches.pop()
            fragment = urllib.parse.urlsplit(target_value).fragment
            if fragment:
                source_parts = urllib.parse.urlsplit(source_value)
                result[source_value] = urllib.parse.urlunsplit(
                    (
                        source_parts.scheme,
                        source_parts.netloc,
                        source_parts.path,
                        source_parts.query,
                        fragment,
                    )
                )
    return result


def _fetch_json(url: str) -> object:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "ydbdoc-review-ng/1.1 (documentation translation)"},
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=3.0) as response:
        body = response.read(_MAX_WIKIPEDIA_RESPONSE_BYTES + 1)
    if len(body) > _MAX_WIKIPEDIA_RESPONSE_BYTES:
        raise ValueError("Wikipedia response is too large")
    return json.loads(body)


class WikipediaLanglinks:
    """Resolve official Wikipedia interlanguage links with fail-open caching."""

    def __init__(self, fetch_json: JsonFetcher = _fetch_json) -> None:
        self._fetch_json = fetch_json
        self._cache: dict[tuple[str, str], str] = {}

    def __call__(self, source_url: str, target_locale: str, /) -> str:
        key = (source_url, target_locale)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        result = source_url
        try:
            parsed = urllib.parse.urlsplit(source_url)
            host_match = _WIKIPEDIA_HOST.fullmatch(parsed.hostname or "")
            if (
                parsed.scheme not in {"http", "https"}
                or host_match is None
                or host_match.group("locale") == target_locale
                or not parsed.path.startswith("/wiki/")
                or parsed.query
                or parsed.fragment
            ):
                self._cache[key] = result
                return result
            title = urllib.parse.unquote(parsed.path.removeprefix("/wiki/"))
            query = urllib.parse.urlencode(
                {
                    "action": "query",
                    "prop": "langlinks",
                    "titles": title,
                    "lllang": target_locale,
                    "llprop": "url",
                    "redirects": "1",
                    "format": "json",
                    "formatversion": "2",
                }
            )
            payload = self._fetch_json(
                f"https://{parsed.hostname}/w/api.php?{query}"
            )
            if not isinstance(payload, Mapping):
                raise TypeError("invalid Wikipedia response")
            query_value = payload.get("query")
            if not isinstance(query_value, Mapping):
                raise TypeError("invalid Wikipedia response")
            pages = query_value.get("pages")
            if not isinstance(pages, list) or not pages or not isinstance(pages[0], Mapping):
                raise ValueError("invalid Wikipedia response")
            langlinks = pages[0].get("langlinks")
            if isinstance(langlinks, list):
                for item in langlinks:
                    if not isinstance(item, Mapping) or item.get("lang") != target_locale:
                        continue
                    candidate = item.get("url")
                    if isinstance(candidate, str) and self._valid_target(candidate, target_locale):
                        result = candidate
                        break
        except Exception:  # noqa: BLE001 - an optional external lookup must never fail a job.
            result = source_url
        self._cache[key] = result
        return result

    @staticmethod
    def _valid_target(url: str, target_locale: str, /) -> bool:
        try:
            parsed = urllib.parse.urlsplit(url)
        except ValueError:
            return False
        return (
            parsed.scheme == "https"
            and parsed.hostname == f"{target_locale}.wikipedia.org"
            and parsed.path.startswith("/wiki/")
            and not parsed.query
            and not parsed.fragment
        )


class LinkDestinationResolver:
    """Apply the only supported deterministic locale-specific URL rewrites."""

    def __init__(
        self,
        source_locale: str,
        target_locale: str,
        wikipedia: WikipediaLanglinks | None = None,
        *,
        overrides: Mapping[str, str] | None = None,
        invalid_sources: frozenset[str] = frozenset(),
    ) -> None:
        self.source_locale = source_locale
        self.target_locale = target_locale
        self.wikipedia = wikipedia
        self.overrides = dict(overrides or {})
        self.invalid_sources = invalid_sources

    def __call__(self, destination: str, /) -> str:
        if destination.startswith("<") and destination.endswith(">"):
            return "<" + self(destination[1:-1]) + ">"
        if any(character.isspace() for character in destination):
            return destination
        overridden = self.overrides.get(destination)
        if overridden is not None and _same_internal_page(destination, overridden):
            destination = overridden
        try:
            parsed = urllib.parse.urlsplit(destination)
        except ValueError:
            return destination
        host = parsed.hostname or ""
        if host in _YDB_HOSTS:
            path = self._localized_docs_path(parsed.path)
            return urllib.parse.urlunsplit(
                (parsed.scheme, parsed.netloc, path, parsed.query, parsed.fragment)
            )
        if not parsed.scheme and not parsed.netloc and parsed.path.startswith("/docs/"):
            path = self._localized_docs_path(parsed.path)
            return urllib.parse.urlunsplit(("", "", path, parsed.query, parsed.fragment))
        if self.wikipedia is not None and _WIKIPEDIA_HOST.fullmatch(host):
            return self.wikipedia(destination, self.target_locale)
        return destination

    def allows(self, source: str, target: str, /) -> bool:
        if source in self.invalid_sources:
            return False
        expected = self(source)
        if target == expected:
            return True
        if expected != source or self.wikipedia is None:
            return False
        try:
            source_value = urllib.parse.urlsplit(source)
        except ValueError:
            return False
        if (
            _WIKIPEDIA_HOST.fullmatch(source_value.hostname or "") is None
            or not source_value.path.startswith("/wiki/")
            or source_value.query
            or source_value.fragment
        ):
            return False
        return self.wikipedia._valid_target(target, self.target_locale)

    def _localized_docs_path(self, path: str, /) -> str:
        prefix = f"/docs/{self.source_locale}"
        if path != prefix and not path.startswith(prefix + "/"):
            return path
        return f"/docs/{self.target_locale}" + path[len(prefix) :]


def localize_markdown_link_destinations(
    content: bytes, resolver: LinkDestinationResolver, /
) -> bytes:
    """Rewrite model-visible Markdown destinations through the deterministic resolver."""
    localized = bytearray(content)
    for start, end, source in reversed(_markdown_destination_spans(content)):
        target = resolver(source)
        if not resolver.allows(source, target):
            raise ValueError("translation_link_invalid")
        localized[start:end] = target.encode("utf-8")
    return bytes(localized)


def markdown_link_destinations_are_localized(
    source: bytes, target: bytes, resolver: LinkDestinationResolver, /
) -> bool:
    """Require the candidate to preserve the resolver-approved destination multiset."""
    source_values = markdown_link_destinations(source)
    expected = tuple(resolver(value) for value in source_values)
    if any(
        not resolver.allows(source_value, target_value)
        for source_value, target_value in zip(source_values, expected, strict=True)
    ):
        return False
    changed_pairs = tuple(
        (source_value, target_value)
        for source_value, target_value in zip(source_values, expected, strict=True)
        if target_value != source_value
    )
    if not changed_pairs:
        return True
    target_values = Counter(markdown_link_destinations(target))
    expected_values = Counter(expected)
    protected_values = {
        value for pair in changed_pairs for value in pair
    }
    return all(target_values[value] == expected_values[value] for value in protected_values)
