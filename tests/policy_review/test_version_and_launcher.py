import hashlib
import importlib.util
from pathlib import Path

import pytest

from ydbdoc_review_ng.policy_review.types import ReviewError
from ydbdoc_review_ng.policy_review.version import action_revision

pytestmark = pytest.mark.unit


def test_moved_tag_is_used_only_when_downloaded_bundle_matches(tmp_path: Path) -> None:
    files = [".github/actions/policy-review/action.yml", "scripts/run_policy_review.py",
             "docker/review-image.json", "docker/review-requirements.lock", "src/package/code.py"]
    entries = []
    for name in files:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"trusted")
        entries.append({"path": name, "type": "blob", "sha": hashlib.sha1(b"blob 7\0trusted").hexdigest()})
    def api(method, path, payload):
        return {"object": {"type": "commit", "sha": "a" * 40}} if "/ref/" in path else {
            "truncated": False, "tree": entries}
    assert action_revision(tmp_path, api) == "a" * 40
    (tmp_path / files[-1]).write_bytes(b"changed")
    with pytest.raises(ReviewError, match="review_action_version_changed"):
        action_revision(tmp_path, api)


def test_worker_docker_command_excludes_write_and_admission_tokens() -> None:
    path = Path(__file__).resolve().parents[2] / "scripts/run_policy_review.py"
    spec = importlib.util.spec_from_file_location("policy_launcher", path)
    assert spec is not None and spec.loader is not None
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    image = "ghcr.io/ydb-platform/ydbdoc-review-ng@sha256:" + "a" * 64
    args = launcher.docker_arguments(image, "worker", Path("/input"), Path("/output"), 1001, 1001)
    assert "YDBDOC_REVIEW_ADMISSION_TOKEN" not in args and "YDBDOC_REVIEW_PUBLISH_TOKEN" not in args
    assert "--read-only" in args and "--user" in args
    assert any("dst=/review/input,readonly" in arg for arg in args)
    assert "--privileged" not in args and not any("docker.sock" in arg for arg in args)
    with pytest.raises(ReviewError, match="review_image_not_released"):
        launcher.docker_arguments("image:latest", "worker", Path("/in"), Path("/out"), 1001, 1001)
    with pytest.raises(ReviewError, match="review_runner_must_not_be_root"):
        launcher.docker_arguments(image, "worker", Path("/in"), Path("/out"), 0, 0)
