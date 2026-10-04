"""Hard publication gates after thin whole-file translation.

Fail closed: a file that fails these gates is not reader-facing product and must
not be soft-published for critic to “figure out later”.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from ydbdoc_review_ng.translation.split_backtick import count_split_backtick_identifiers

# Continuous source-locale letters (RU) in an EN target.
_CYRILLIC_RUN = re.compile(r"[А-Яа-яЁё]{3,}")
_INCLUDE = re.compile(
    r"\{%\s*include\s+\[[^\]]*\]\(([^)]+)\)\s*%\}",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class PublicationGateFailure:
    code: str
    detail: str


def leftover_source_locale_runs(text: str, /, *, source_locale: str) -> tuple[str, ...]:
    """Return leftover source-script runs that must not ship in the target."""
    if source_locale != "ru":
        return ()
    return tuple(sorted(set(_CYRILLIC_RUN.findall(text))))


def include_targets(text: str, /) -> tuple[str, ...]:
    return tuple(_INCLUDE.findall(text))


def missing_relative_includes(
    text: str,
    /,
    *,
    target_path: str,
    available_paths: set[str],
) -> tuple[str, ...]:
    """Relative include destinations that are absent from the published set."""
    base = PurePosixPath(target_path).parent
    missing: list[str] = []
    for raw in include_targets(text):
        destination = raw.strip()
        if not destination or destination.startswith(("/", "http://", "https://")):
            continue
        resolved = PurePosixPath(base / destination)
        # Normalize a/b/../c → a/c without resolving outside the docs tree.
        parts: list[str] = []
        for part in resolved.parts:
            if part == ".":
                continue
            if part == "..":
                if parts:
                    parts.pop()
                continue
            parts.append(part)
        normalized = "/".join(parts)
        if normalized not in available_paths:
            missing.append(destination)
    return tuple(missing)


def check_publication_gates(
    target: bytes,
    /,
    *,
    source_locale: str,
    target_path: str,
    available_paths: set[str] | None = None,
) -> tuple[PublicationGateFailure, ...]:
    """Return zero or more hard gate failures for one translated file."""
    if type(target) is not bytes:
        raise TypeError("target must be exact bytes")
    try:
        text = target.decode("utf-8")
    except UnicodeDecodeError:
        return (PublicationGateFailure("utf8_invalid", "target is not valid UTF-8"),)

    failures: list[PublicationGateFailure] = []
    leftover = leftover_source_locale_runs(text, source_locale=source_locale)
    if leftover:
        sample = ", ".join(leftover[:5])
        failures.append(
            PublicationGateFailure(
                "source_locale_echo",
                f"target still contains source-locale prose ({len(leftover)} runs): {sample}",
            )
        )
    mangled = count_split_backtick_identifiers(target)
    if mangled:
        failures.append(
            PublicationGateFailure(
                "split_backtick_identifiers",
                f"target still contains {mangled} split-backtick identifier runs",
            )
        )
    if available_paths is not None:
        missing = missing_relative_includes(
            text, target_path=target_path, available_paths=available_paths
        )
        if missing:
            failures.append(
                PublicationGateFailure(
                    "missing_include_target",
                    "relative include targets missing: " + ", ".join(missing[:8]),
                )
            )
    return tuple(failures)
