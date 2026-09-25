"""Pinned, literal TOC/redirect edits, not a navigation graph or YAML rewriter."""

from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import yaml  # type: ignore[import-untyped]

from ydbdoc_review_ng.dependencies import RedirectEntry
from ydbdoc_review_ng.domain import RepoPath, SnapshotRef
from ydbdoc_review_ng.ports import SnapshotReader
from ydbdoc_review_ng.publication import FileChange
from ydbdoc_review_ng.runtime_github import RuntimeBoundaryError

_REDIRECT = re.compile(rb"(?m)^ *- from: *([^\r\n]+)\r?\n *to: *([^\r\n]+)\r?$")
_TOC_NAME = re.compile(r"^toc(?:_[A-Za-z0-9-]+)?\.ya?ml$")
_ROOT_TOC_NAMES = (
    "toc.yaml",
    "toc.yml",
    "toc_i.yaml",
    "toc_p.yaml",
    "toc_m.yaml",
    "toc_x.yaml",
    "toc_changelog.yaml",
)
_MAX_LOCAL_TOC_FILES = 100


@dataclass(frozen=True)
class _Toc:
    text: str
    items: Any
    hrefs: tuple[Any, ...]
    includes: tuple[Any, ...]


def _toc(content: bytes, error: str) -> _Toc:
    """Validate the complete YAML node tree without constructing Python objects.

    Nested flow/block mappings and local scalar includes are supported. Aliases,
    merge keys, custom tags and remote includes are rejected.
    """
    try:
        text = content.decode("utf-8")
        root = yaml.compose(text, Loader=yaml.SafeLoader)
        if not isinstance(root, yaml.MappingNode):
            raise TypeError
        stack = [root]
        seen: set[int] = set()
        hrefs = []
        includes = []
        items = None
        while stack:
            node = stack.pop()
            if id(node) in seen:
                raise ValueError
            seen.add(id(node))
            if isinstance(node, yaml.MappingNode):
                if node.tag != "tag:yaml.org,2002:map":
                    raise ValueError
                keys: set[str] = set()
                values: dict[str, Any] = {}
                for key, value in node.value:
                    if (
                        not isinstance(key, yaml.ScalarNode)
                        or key.tag != "tag:yaml.org,2002:str"
                        or key.value in keys
                    ):
                        raise ValueError
                    keys.add(key.value)
                    values[key.value] = value
                    if key.value == "include_url":
                        raise ValueError
                    if key.value == "href":
                        if (
                            not isinstance(value, yaml.ScalarNode)
                            or value.tag != "tag:yaml.org,2002:str"
                            or not value.value.strip()
                        ):
                            raise ValueError
                        hrefs.append(value)
                    if key.value == "items":
                        if value.tag != "tag:yaml.org,2002:null" and (
                            not isinstance(value, yaml.SequenceNode)
                            or any(not isinstance(child, yaml.MappingNode) for child in value.value)
                        ):
                            raise ValueError
                        if node is root:
                            items = value
                    stack.append(value)
                include = values.get("include")
                if include is not None:
                    if (
                        isinstance(include, yaml.ScalarNode)
                        and include.tag == "tag:yaml.org,2002:str"
                        and include.value.strip()
                    ):
                        includes.append(include)
                    else:
                        if isinstance(include, yaml.ScalarNode) and include.tag == (
                            "tag:yaml.org,2002:null"
                        ):
                            path = values.get("path")
                        elif isinstance(include, yaml.MappingNode):
                            path = next(
                                (
                                    child
                                    for child_key, child in include.value
                                    if isinstance(child_key, yaml.ScalarNode)
                                    and child_key.value == "path"
                                ),
                                None,
                            )
                        else:
                            raise ValueError
                        if (
                            not isinstance(path, yaml.ScalarNode)
                            or path.tag != "tag:yaml.org,2002:str"
                            or not path.value.strip()
                        ):
                            raise ValueError
                        includes.append(path)
                if node is root and not {"items", "href"} & keys:
                    raise ValueError
            elif isinstance(node, yaml.SequenceNode):
                if node.tag != "tag:yaml.org,2002:seq":
                    raise ValueError
                stack.extend(node.value)
            elif not isinstance(node, yaml.ScalarNode) or node.tag not in {
                "tag:yaml.org,2002:" + suffix
                for suffix in ("str", "null", "bool", "int", "float", "timestamp")
            }:
                raise ValueError
        return _Toc(text, items, tuple(hrefs), tuple(includes))
    except (yaml.YAMLError, UnicodeError, ValueError, TypeError, RecursionError):
        raise RuntimeBoundaryError(error) from None


def _append_toc(toc: _Toc, relative: str, title: str) -> bytes:
    items = toc.items
    if items is None:
        raise RuntimeBoundaryError("unsupported_target_toc")
    if isinstance(items, yaml.SequenceNode) and items.flow_style:
        offset = items.end_mark.index - 1
        addition = (
            (", " if items.value else "")
            + "{name: "
            + json.dumps(title)
            + ", href: "
            + json.dumps(relative)
            + "}"
        )
        result = toc.text[:offset] + addition + toc.text[offset:]
    else:
        empty = items.tag == "tag:yaml.org,2002:null"
        start = items.start_mark.index if empty else items.end_mark.index
        end = items.end_mark.index
        indent = 2 if empty else items.start_mark.column
        addition = "" if start == 0 or toc.text[start - 1] == "\n" else "\n"
        addition += " " * indent + "- name: " + json.dumps(title) + "\n"
        addition += " " * (indent + 2) + "href: " + relative + "\n"
        result = toc.text[:start] + addition + toc.text[end:]
    output = result.encode("utf-8")
    _toc(output, "unsupported_target_toc")
    return output


def _scalar(value: bytes) -> str:
    text = value.decode().strip()
    if text.startswith('"'):
        return str(json.loads(text))
    if text.startswith("'") and text.endswith("'"):
        return text[1:-1].replace("''", "'")
    if any(character in text for character in "{}[]&*!|>"):
        raise RuntimeBoundaryError("unsupported_metadata_scalar")
    return text


def read_redirects(
    reader: SnapshotReader, snapshot: SnapshotRef, locale_root: str
) -> tuple[RedirectEntry, ...]:
    content = reader.read_bytes(snapshot, RepoPath(locale_root + "/redirects.yaml"))
    if content is None:
        return ()
    matches = list(_REDIRECT.finditer(content))
    if _REDIRECT.sub(b"", content).replace(b"redirects:", b"").strip():
        raise RuntimeBoundaryError("unsupported_redirect_format")
    return tuple(
        RedirectEntry(
            RepoPath(locale_root + "/" + _scalar(match[1])),
            RepoPath(locale_root + "/" + _scalar(match[2])),
        )
        for match in matches
    )


class MetadataProducer:
    def __init__(
        self,
        reader: SnapshotReader,
        source: SnapshotRef,
        target: SnapshotRef,
        changed_paths: tuple[RepoPath, ...],
        *,
        pending: Mapping[str, bytes | None] | None = None,
    ) -> None:
        self.reader, self.source, self.target = reader, source, target
        self.changed_paths = changed_paths
        self.pending = pending if pending is not None else {}

    def _target_bytes(self, path: RepoPath) -> bytes | None:
        if path.value in self.pending:
            return self.pending[path.value]
        return self.reader.read_bytes(self.target, path)

    def _nearest_target_toc(
        self, target_root: str, target_toc: RepoPath
    ) -> tuple[RepoPath, bytes, _Toc] | None:
        name = posixpath.basename(target_toc.value)
        directory = posixpath.dirname(target_toc.value)
        while directory == target_root or directory.startswith(target_root + "/"):
            candidate = RepoPath(posixpath.join(directory, name))
            content = self._target_bytes(candidate)
            if content is not None:
                return candidate, content, _toc(content, "unsupported_target_toc")
            if directory == target_root:
                break
            directory = posixpath.dirname(directory)
        return None

    def _source_toc_paths(self, source_root: str, seeds: set[str]) -> tuple[str, ...]:
        paths: set[str] = set()
        inspected: set[str] = set()
        active: set[str] = set()

        def visit(path: str, *, required: bool) -> None:
            if path in active:
                raise RuntimeBoundaryError("unsupported_source_toc")
            if path in inspected:
                return
            active.add(path)
            inspected.add(path)
            paths.add(path)
            if len(inspected) > _MAX_LOCAL_TOC_FILES:
                raise RuntimeBoundaryError("unsupported_source_toc")
            content = self.reader.read_bytes(self.source, RepoPath(path))
            if content is None:
                if required:
                    raise RuntimeBoundaryError("unsupported_source_toc")
                active.remove(path)
                return
            view = _toc(content, "unsupported_source_toc")
            for node in view.includes:
                relative = node.value
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), relative))
                if (
                    relative.startswith("/")
                    or resolved == source_root
                    or not resolved.startswith(source_root + "/")
                    or _TOC_NAME.fullmatch(posixpath.basename(resolved)) is None
                ):
                    raise RuntimeBoundaryError("unsupported_source_toc")
                visit(resolved, required=True)
            active.remove(path)

        for seed in sorted(seeds):
            visit(seed, required=False)
        return tuple(sorted(paths))

    def changes(
        self,
        source_path: RepoPath,
        target_path: RepoPath,
        *,
        new: bool = False,
        old: RepoPath | None = None,
    ) -> tuple[FileChange, ...]:
        if not new and old is None:
            return ()
        source_root = source_path.value.split("/core/", 1)[0] + "/core"
        target_root = target_path.value.split("/core/", 1)[0] + "/core"
        toc_paths = {
            path.value
            for path in self.changed_paths
            if path.value.startswith(source_root + "/")
            and _TOC_NAME.fullmatch(path.value.rsplit("/", 1)[-1]) is not None
        }
        toc_paths.update({source_root + "/toc.yaml", source_root + "/toc.yml"})
        source_tocs = set(self._source_toc_paths(source_root, toc_paths))
        directory = posixpath.dirname(source_path.value)
        while directory == source_root or directory.startswith(source_root + "/"):
            source_tocs.update(posixpath.join(directory, name) for name in _ROOT_TOC_NAMES)
            if directory == source_root:
                break
            directory = posixpath.dirname(directory)
        changes = []
        for source_toc in sorted(source_tocs):
            target_toc = RepoPath(target_root + source_toc[len(source_root) :])
            source_bytes = self.reader.read_bytes(self.source, RepoPath(source_toc))
            source_toc_view = (
                None if source_bytes is None else _toc(source_bytes, "unsupported_source_toc")
            )
            target_bytes = self._target_bytes(target_toc)
            target_toc_view = (
                None if target_bytes is None else _toc(target_bytes, "unsupported_target_toc")
            )
            if old is not None:
                source_matches = (
                    []
                    if source_toc_view is None
                    else [
                        node
                        for node in source_toc_view.hrefs
                        if posixpath.normpath(
                            posixpath.join(posixpath.dirname(source_toc), node.value)
                        )
                        == source_path.value
                    ]
                )
                if target_toc_view is None:
                    if source_matches:
                        raise RuntimeBoundaryError("unsupported_target_toc")
                    continue
                matches = [
                    node
                    for node in target_toc_view.hrefs
                    if posixpath.normpath(
                        posixpath.join(posixpath.dirname(target_toc.value), node.value)
                    )
                    == old.value
                ]
                if not matches:
                    if not source_matches:
                        continue
                    relative = posixpath.relpath(
                        target_path.value, posixpath.dirname(target_toc.value)
                    )
                    if any(
                        posixpath.normpath(
                            posixpath.join(posixpath.dirname(target_toc.value), node.value)
                        )
                        == target_path.value
                        for node in target_toc_view.hrefs
                    ):
                        continue
                    after = _append_toc(
                        target_toc_view,
                        relative,
                        posixpath.basename(target_path.value).removesuffix(".md"),
                    )
                    changes.append(FileChange(target_toc, target_bytes, after))
                    continue
                if len(matches) != 1:
                    raise RuntimeBoundaryError("ambiguous_toc_preimage")
                node = matches[0]
                replacement = posixpath.relpath(
                    target_path.value, posixpath.dirname(target_toc.value)
                )
                if node.style is not None:
                    replacement = json.dumps(replacement)
                after = (
                    target_toc_view.text[: node.start_mark.index]
                    + replacement
                    + target_toc_view.text[node.end_mark.index :]
                ).encode("utf-8")
                _toc(after, "unsupported_target_toc")
            else:
                matches = (
                    []
                    if source_toc_view is None
                    else [
                        node
                        for node in source_toc_view.hrefs
                        if posixpath.normpath(
                            posixpath.join(posixpath.dirname(source_toc), node.value)
                        )
                        == source_path.value
                    ]
                )
                if not matches:
                    continue
                if target_toc_view is None:
                    fallback = self._nearest_target_toc(target_root, target_toc)
                    if fallback is None:
                        raise RuntimeBoundaryError("unsupported_target_toc")
                    target_toc, target_bytes, target_toc_view = fallback
                relative = posixpath.relpath(target_path.value, posixpath.dirname(target_toc.value))
                if any(node.value == relative for node in target_toc_view.hrefs):
                    continue
                after = _append_toc(
                    target_toc_view,
                    relative,
                    posixpath.basename(target_path.value).removesuffix(".md"),
                )
            changes.append(FileChange(target_toc, target_bytes, after))
        if old is not None:
            # Diplodoc redirects are locale-relative; core remains part of the path.
            locale_root = target_root.rsplit("/", 1)[0]
            redirect_path = RepoPath(locale_root + "/redirects.yaml")
            before = self._target_bytes(redirect_path)
            content = before or b"redirects:\n"
            matches = list(_REDIRECT.finditer(content))
            remainder = _REDIRECT.sub(b"", content).replace(b"redirects:", b"").strip()
            if remainder:
                raise RuntimeBoundaryError("unsupported_redirect_format")
            old_relative = posixpath.relpath(old.value, locale_root)
            new_relative = posixpath.relpath(target_path.value, locale_root)
            pairs = [(_scalar(match[1]), _scalar(match[2])) for match in matches]
            if any(left == new_relative or right == old_relative for left, right in pairs):
                raise RuntimeBoundaryError("redirect_chain")
            existing = [right for left, right in pairs if left == old_relative]
            if existing and existing != [new_relative]:
                raise RuntimeBoundaryError("redirect_conflict")
            if not existing:
                after = (
                    content.rstrip(b"\n")
                    + (
                        "\n  - from: "
                        + json.dumps(old_relative)
                        + "\n    to: "
                        + json.dumps(new_relative)
                        + "\n"
                    ).encode()
                )
                changes.append(FileChange(redirect_path, before, after))
        return tuple(changes)
