import base64
from dataclasses import replace

import pytest

from ydbdoc_review_ng.policy_review.admission import ReviewPR
from ydbdoc_review_ng.policy_review.github import PREFIX, ReviewGitHub
from ydbdoc_review_ng.policy_review.types import ReviewError

from .helpers import PATH, snapshot_json

pytestmark = pytest.mark.unit
PR = ReviewPR(42, "member", "a" * 40, "b" * 40, True, False, 1)


def row() -> dict:
    return {"number": 42, "user": {"login": "member"}, "head": {"sha": PR.head_sha},
            "base": {"sha": PR.base_sha, "repo": {"full_name": "ydb-platform/ydb"}},
            "state": "open", "draft": False, "changed_files": 1}


def test_collaborator_checks_real_http_result_and_operator_needs_write() -> None:
    def api(method, path, payload):
        return {"permission": "triage"} if path.endswith("/permission") else True
    github = ReviewGitHub(api)
    assert github.collaborator("member") and not github.operator("member")
    assert not ReviewGitHub(lambda *args: None).collaborator("external")
    with pytest.raises(ReviewError, match="review_collaborator_response_invalid"):
        ReviewGitHub(lambda *args: {}).collaborator("external")


def test_permission_api_failure_is_not_external_author_approval() -> None:
    def api(*args):
        raise ReviewError("review_github_request_failed")
    with pytest.raises(ReviewError):
        ReviewGitHub(api).operator("maintainer")


def test_all_file_pages_are_read_and_missing_page_cannot_silently_trim_scope() -> None:
    calls = []
    def api(method, path, payload):
        calls.append(path)
        start = 100 if "page=2" in path else 0
        return [{"filename": f"file-{n}"} for n in range(start, min(start + 100, 101))]
    github = ReviewGitHub(api)
    assert len(github.files(replace(PR, changed_files=101))) == 101 and len(calls) == 2
    with pytest.raises(ReviewError, match="review_file_inventory_incomplete"):
        ReviewGitHub(lambda *args: []).files(PR)


def test_policy_comes_from_base_and_before_from_merge_base_not_pr_rules() -> None:
    fixture = snapshot_json()
    reads = []
    def api(method, path, payload):
        if "/compare/" in path:
            return {"merge_base_commit": {"sha": "c" * 40}}
        if "/contents/" in path:
            reads.append(path)
            content_path = path.split("/contents/", 1)[1].split("?", 1)[0]
            body = fixture["rules"].get(content_path, "# Text\n\nDescription.\n")
            return {"type": "file", "encoding": "base64", "size": len(body),
                    "content": base64.b64encode(body.encode()).decode()}
        return row()
    snapshot = ReviewGitHub(api).snapshot(PR, [{"filename": PATH, "status": "modified"}])
    assert snapshot is not None and snapshot.base_sha == "c" * 40
    assert all(p.endswith("ref=" + PR.base_sha) for p in reads if "/.ruler/" in p)
    assert any(p.endswith("ref=" + "c" * 40) for p in reads if PATH in p)


def test_missing_canonical_policy_fails_without_fallback_to_pr_or_fixture() -> None:
    def api(method, path, payload):
        return {"merge_base_commit": {"sha": "c" * 40}} if "/compare/" in path else None
    with pytest.raises(ReviewError, match="review_required_content_missing"):
        ReviewGitHub(api).snapshot(PR, [{"filename": PATH, "status": "added"}])


@pytest.mark.parametrize("kind", ["symlink", "submodule", "dir"])
def test_links_to_repository_objects_never_execute_or_follow_them(kind: str) -> None:
    github = ReviewGitHub(lambda *args: {"type": kind, "encoding": "base64", "size": 0})
    with pytest.raises(ReviewError, match="review_content_invalid"):
        github.read_text(PATH, PR.head_sha)


def test_comment_marker_spoof_by_other_user_is_not_updated() -> None:
    writes = []
    def api(method, path, payload):
        if path == "/user":
            return {"login": "publisher"}
        if path.endswith("/pulls/42"):
            return row()
        if "/comments?" in path:
            return [{"id": 99, "user": {"login": "attacker"},
                     "body": "<!-- ydbdoc-policy-review:v1 -->fake"}]
        writes.append((method, path, payload))
        return {}
    ReviewGitHub(api).publish(42, PR.head_sha, "report", "neutral")
    assert writes[0][2]["head_sha"] == PR.head_sha
    assert writes[-1][0] == "POST" and writes[-1][1] == PREFIX + "/issues/42/comments"


def test_own_report_is_updated_and_new_head_prevents_any_publication() -> None:
    writes = []
    def api(method, path, payload):
        if path == "/user":
            return {"login": "publisher"}
        if path.endswith("/pulls/42"):
            return row()
        if "/comments?" in path:
            return [{"id": 99, "user": {"login": "publisher"},
                     "body": "<!-- ydbdoc-policy-review:v1 -->old"}]
        writes.append((method, path, payload))
        return {}
    ReviewGitHub(api).publish(42, PR.head_sha, "report", "neutral")
    assert writes[-1][0:2] == ("PATCH", PREFIX + "/issues/comments/99")
    with pytest.raises(ReviewError, match="superseded"):
        ReviewGitHub(api).publish(42, "c" * 40, "old report", "neutral")
    assert len(writes) == 2
