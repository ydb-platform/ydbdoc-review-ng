"""Copy missing locale-local document assets from the authoritative snapshot."""

from __future__ import annotations

import posixpath
from collections.abc import Mapping
from urllib.parse import unquote, urlsplit

from ydbdoc_review_ng.domain import RepoPath, SnapshotRef
from ydbdoc_review_ng.plan import ProtectedKind, SourcePlan, fields_of
from ydbdoc_review_ng.ports import SnapshotReader
from ydbdoc_review_ng.publication import FileChange
from ydbdoc_review_ng.runtime_github import RuntimeBoundaryError

_ASSET_EXTENSIONS = frozenset(
    {".svg", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".avif", ".ico", ".pdf"}
)
_MAX_ASSETS = 100
_MAX_ASSET_BYTES = 20 * 1024 * 1024


def missing_assets(
    reader: SnapshotReader,
    source_snapshot: SnapshotRef,
    target_snapshot: SnapshotRef,
    source_path: RepoPath,
    target_path: RepoPath,
    source: bytes,
    plan: SourcePlan,
    pending: Mapping[str, bytes | None],
    /,
) -> tuple[FileChange, ...]:
    """Preserve existing localized assets; never fetch remote URLs or escape a locale."""
    source_root = "/".join(source_path.value.split("/")[:3]) + "/"
    target_root = "/".join(target_path.value.split("/")[:3]) + "/"
    if (
        source_root not in {"ydb/docs/ru/", "ydb/docs/en/"}
        or target_root not in {"ydb/docs/ru/", "ydb/docs/en/"}
        or source_root == target_root
    ):
        raise RuntimeBoundaryError("asset_locale_invalid")
    assets = {
        path: value
        for path, value in pending.items()
        if posixpath.splitext(path)[1].lower() in _ASSET_EXTENSIONS and value is not None
    }
    changes: list[FileChange] = []
    for field in fields_of(plan):
        for region in field.protected_regions:
            if region.kind not in {ProtectedKind.IMAGE_CLOSE, ProtectedKind.LINK_CLOSE}:
                continue
            suffix = source[region.span.start : region.span.end].decode("utf-8")
            if not suffix.startswith("](") or not suffix.endswith(")"):
                continue
            destination = suffix[2:-1].strip()
            if not destination:
                continue
            destination = (
                destination[1:].split(">", 1)[0]
                if destination.startswith("<")
                else destination.split(maxsplit=1)[0]
            )
            try:
                url = urlsplit(destination)
            except ValueError:
                continue
            path = unquote(url.path)
            if url.scheme or url.netloc or path.startswith("/") or "\\" in path:
                continue
            if posixpath.splitext(path)[1].lower() not in _ASSET_EXTENSIONS:
                continue
            resolved = posixpath.normpath(
                posixpath.join(posixpath.dirname(source_path.value), path)
            )
            if not resolved.startswith(source_root):
                continue
            target = target_root + resolved.removeprefix(source_root)
            if target in assets:
                continue
            target_asset = RepoPath(target)
            if reader.read_bytes(target_snapshot, target_asset) is not None:
                continue
            content = reader.read_bytes(source_snapshot, RepoPath(resolved))
            if content is None:
                raise RuntimeBoundaryError("source_asset_missing")
            assets[target] = content
            if len(assets) > _MAX_ASSETS or sum(map(len, assets.values())) > _MAX_ASSET_BYTES:
                raise RuntimeBoundaryError("asset_scope_limit_exceeded")
            changes.append(FileChange(target_asset, None, content))
    return tuple(changes)
