"""Resolve a movable tag and verify it matches the already downloaded action bundle."""

from __future__ import annotations

import hashlib
from pathlib import Path

from ydbdoc_review_ng.policy_review.github import JsonAPI
from ydbdoc_review_ng.policy_review.types import ReviewError, sha

_PREFIX = "/repos/ydb-platform/ydbdoc-review-ng"


def action_revision(root: Path, api: JsonAPI) -> str:
    reference = api("GET", _PREFIX + "/git/ref/tags/doc-review-stable", None)
    try:
        for _ in range(5):
            kind, revision = reference["object"]["type"], sha(reference["object"]["sha"])
            if kind == "commit":
                break
            if kind != "tag":
                raise ValueError
            reference = api("GET", _PREFIX + "/git/tags/" + revision, None)
        else:
            raise ValueError
        tree = api("GET", _PREFIX + "/git/trees/" + revision + "?recursive=1", None)
        if tree["truncated"] is not False:
            raise ValueError
        blobs = {entry["path"]: entry["sha"] for entry in tree["tree"] if entry["type"] == "blob"}
        paths = [root / ".github/actions/policy-review/action.yml",
                 root / "scripts/run_policy_review.py", root / "docker/review-image.json",
                 root / "docker/review-requirements.lock", *sorted((root / "src").rglob("*.py"))]
        for path in paths:
            body = path.read_bytes()
            digest = hashlib.sha1(b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest()
            if blobs.get(path.relative_to(root).as_posix()) != digest:
                raise ValueError
        return revision
    except (KeyError, TypeError, ValueError, OSError):
        raise ReviewError("review_action_version_changed") from None
