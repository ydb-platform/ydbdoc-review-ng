import hashlib
from pathlib import Path

import pytest

from ydbdoc_review_ng.policy_review.types import ReviewError
from ydbdoc_review_ng.policy_review.version import action_revision

pytestmark = pytest.mark.unit


def test_moved_tag_is_used_only_when_downloaded_bundle_matches(tmp_path: Path) -> None:
    files = [".github/actions/policy-review/action.yml", "scripts/run_policy_review.py",
             "docker/review-requirements.lock", "src/ydbdoc_review_ng/policy_review/skill/SKILL.md", "src/package/code.py"]
    entries = []
    for name in files:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"trusted")
        entries.append({"path": name, "type": "blob", "sha": hashlib.sha1(b"blob 7\0trusted").hexdigest()})
    def api(method, path, payload):
        if "/ref/" in path:
            assert path.endswith("/git/ref/tags/v1.0.1")
            return {"object": {"type": "commit", "sha": "a" * 40}}
        return {"truncated": False, "tree": entries}
    assert action_revision(tmp_path, api) == "a" * 40
    (tmp_path / files[-2]).write_bytes(b"changed")
    with pytest.raises(ReviewError, match="review_action_version_changed"):
        action_revision(tmp_path, api)
