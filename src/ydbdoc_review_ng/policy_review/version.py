"""Resolve a movable tag and verify it matches the already downloaded action bundle."""

from __future__ import annotations

import hashlib
from pathlib import Path

from ydbdoc_review_ng.policy_review.github import JsonAPI
from ydbdoc_review_ng.policy_review.types import ReviewError, sha

_PREFIX = "/repos/ydb-platform/ydbdoc-review-ng"
_SKILL = "src/ydbdoc_review_ng/policy_review/skill"
_FIXED = {".github/actions/policy-review/action.yml", "scripts/run_policy_review.py",
          "docker/review-requirements.lock"}


def action_revision(root: Path, api: JsonAPI) -> str:
    reference = api("GET", _PREFIX + "/git/ref/tags/v1.0.1", None)
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
        expected = {name for name in blobs if name in _FIXED
                    or (name.startswith("src/") and name.endswith(".py"))
                    or (name.rpartition("/")[0] == _SKILL and name.endswith(".md"))}
        local = {p.relative_to(root).as_posix() for p in
                 [*(root / "src").rglob("*.py"), *(root / _SKILL).glob("*.md")]}
        local.update(name for name in _FIXED if (root / name).is_file())
        if local != expected or not _FIXED <= local:
            raise ValueError
        for name in local:
            body = (root / name).read_bytes()
            digest = hashlib.sha1(b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest()
            if blobs[name] != digest:
                raise ValueError
        return revision
    except (KeyError, TypeError, ValueError, OSError):
        raise ReviewError("review_action_version_changed") from None
