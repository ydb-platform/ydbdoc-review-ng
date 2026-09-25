"""Deterministic localization of protected documentation link destinations."""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping

_YDB_HOSTS = frozenset({"ydb.tech", "www.ydb.tech"})
_WIKIPEDIA_HOST = re.compile(r"^(?P<locale>[a-z][a-z0-9-]*)\.wikipedia\.org$")
_MAX_WIKIPEDIA_RESPONSE_BYTES = 1_000_000

JsonFetcher = Callable[[str], object]

_MARKDOWN_DESTINATION = re.compile(rb"(?<!!)\[[^\]]*\]\(<?([^\s)>]+)>?\)")


def _anchor_key(value: str) -> str:
    return "-".join(part.removesuffix("s") for part in value.split("-"))


def closest_target_anchor(fragment: str, anchors: set[str] | frozenset[str], /) -> str | None:
    """Resolve the conservative singular/plural spelling difference, or decline."""
    key = _anchor_key(fragment.casefold())
    matches = sorted(anchor for anchor in anchors if _anchor_key(anchor.casefold()) == key)
    return matches[0] if len(matches) == 1 else None


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
    source_values = [match.group(1).decode("utf-8") for match in _MARKDOWN_DESTINATION.finditer(source)]
    target_values = [match.group(1).decode("utf-8") for match in _MARKDOWN_DESTINATION.finditer(target)]
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
