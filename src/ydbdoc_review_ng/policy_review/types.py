"""Validated immutable inputs and grounded findings for policy review."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from ydbdoc_review_ng.errors import SafeDiagnosticError

ROOT = "ydb/docs/"
RULE_ROOT = ROOT + ".ruler/"
RULE_FILES = ("GENERAL_RULES.md", "FORMAT_RULES.md", "DOCUMENTATION_RULES.md")
RULE_IDS = tuple(f"DOC.{n}" for n in range(1, 16))
PRIORITIES = {
    **{f"DOC.{n}": "critical" for n in (3, 7, 8)},
    **{f"DOC.{n}": "high" for n in (1, 2, 6, 9, 14)},
    **{f"DOC.{n}": "medium" for n in (4, 5, 10, 11, 12)},
    **{f"DOC.{n}": "low" for n in (13, 15)},
    **{rule: "medium" for rule in ("FORMAT.MD009", "FORMAT.MD032", "FORMAT.YQL")},
}


class ReviewError(SafeDiagnosticError):
    """Only payload-free diagnostics may cross the public CLI boundary."""


def doc_path(value: object) -> str:
    if (
        type(value) is not str
        or not value.startswith(ROOT)
        or len(value) > 1024
        or "\\" in value
        or any(ord(c) < 32 for c in value)
        or any(p in {"", ".", ".."} for p in value.split("/"))
        or PurePosixPath(value).is_absolute()
    ):
        raise ReviewError("invalid_documentation_path")
    return value


def sha(value: object) -> str:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise ReviewError("invalid_snapshot_sha")
    return value


def text(value: object, *, max_bytes: int = 2_000_000) -> str:
    if type(value) is not str or len(value.encode("utf-8")) > max_bytes:
        raise ReviewError("snapshot_text_limit_exceeded")
    return value


@dataclass(frozen=True, slots=True)
class ReviewFile:
    path: str
    before: str | None = field(repr=False)
    after: str | None = field(repr=False)


@dataclass(frozen=True, slots=True)
class ReviewSnapshot:
    head_sha: str
    base_sha: str
    rules_sha: str
    files: tuple[ReviewFile, ...]
    rules: tuple[tuple[str, str], ...] = field(repr=False)
    context: tuple[tuple[str, str], ...] = field(repr=False)

    @classmethod
    def from_json(cls, value: object) -> ReviewSnapshot:
        if type(value) is not dict or set(value) != {
            "head_sha", "base_sha", "rules_sha", "files", "rules", "context"
        }:
            raise ReviewError("invalid_snapshot_schema")
        head, base, rules_sha = (sha(value[key]) for key in ("head_sha", "base_sha", "rules_sha"))
        raw_files = value["files"]
        if type(raw_files) is not list or not 1 <= len(raw_files) <= 100:
            raise ReviewError("snapshot_file_limit_exceeded")
        files: list[ReviewFile] = []
        for raw in raw_files:
            if type(raw) is not dict or set(raw) != {"path", "before", "after"}:
                raise ReviewError("invalid_snapshot_schema")
            path = doc_path(raw["path"])
            before = None if raw["before"] is None else text(raw["before"])
            after = None if raw["after"] is None else text(raw["after"])
            if before is None and after is None:
                raise ReviewError("invalid_file_change")
            files.append(ReviewFile(path, before, after))
        if len({f.path for f in files}) != len(files):
            raise ReviewError("duplicate_snapshot_path")
        maps: list[tuple[tuple[str, str], ...]] = []
        for name in ("rules", "context"):
            mapping = value[name]
            if type(mapping) is not dict or len(mapping) > 200:
                raise ReviewError("invalid_snapshot_schema")
            maps.append(tuple((doc_path(k), text(v)) for k, v in mapping.items()))
        total_bytes = sum(
            len(s.encode("utf-8"))
            for s in [*(v for mapping in maps for _, v in mapping),
                      *(s for f in files for s in (f.before, f.after) if s is not None)]
        )
        if total_bytes > 10_000_000:
            raise ReviewError("snapshot_text_limit_exceeded")
        return cls(head, base, rules_sha, tuple(files), maps[0], maps[1])


@dataclass(frozen=True, slots=True)
class Finding:
    rule_id: str
    path: str
    line: int
    quote: str
    explanation: str
    suggestion: str
    priority: str

    def to_json(self) -> dict[str, object]:
        return {key: getattr(self, key) for key in self.__dataclass_fields__}
