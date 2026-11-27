"""Bounded GitHub API access without checkout, redirects or executing PR code."""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from ydbdoc_review_ng.policy_review.admission import REPOSITORY, ReviewPR, login
from ydbdoc_review_ng.policy_review.references import markdown_targets
from ydbdoc_review_ng.policy_review.types import (
    ROOT,
    RULE_FILES,
    RULE_ROOT,
    ReviewDiff,
    ReviewError,
    ReviewSnapshot,
    doc_path,
    repo_path,
    sha,
)

PREFIX = "/repos/" + REPOSITORY
JsonAPI = Callable[[str, str, object], Any]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class ReviewHTTP:
    def __init__(self, token: str, *, repository: str = REPOSITORY,
                 anonymous_pr_read: bool = False) -> None:
        if repository not in {REPOSITORY, "ydb-platform/ydbdoc-review-ng"}:
            raise ReviewError("review_github_configuration_invalid")
        self._token = token
        self._prefix = "/repos/" + repository
        self._anonymous_pr_read = anonymous_pr_read and repository == REPOSITORY
        self._opener = urllib.request.build_opener(_NoRedirect())

    def __call__(self, method: str, path: str, payload: object = None) -> Any:
        anonymous = (not self._token and self._anonymous_pr_read and method == "GET"
                     and re.fullmatch(re.escape(PREFIX) + r"/pulls/[1-9][0-9]*", path) is not None)
        if (not self._token and not anonymous
                or not path.startswith(self._prefix + "/") and path != "/user" or "#" in path):
            raise ReviewError("review_github_configuration_invalid")
        headers = {"Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = "Bearer " + self._token
        request = urllib.request.Request(
            "https://api.github.com" + path, method=method,
            data=None if payload is None else json.dumps(payload).encode(),
            headers=headers,
        )
        try:
            with self._opener.open(request, timeout=60) as response:
                body = response.read(25_000_001)
                if len(body) > 25_000_000:
                    raise ReviewError("review_github_response_limit")
                return True if response.status == 204 else json.loads(body)
        except urllib.error.HTTPError as error:
            if error.code == 404 and method == "GET":
                return None
            if error.code == 404 and method == "DELETE":
                return True
            raise ReviewError("review_github_request_failed") from None
        except (OSError, ValueError):
            raise ReviewError("review_github_request_failed") from None


class ReviewGitHub:
    def __init__(self, read: JsonAPI, admission: JsonAPI | None = None,
                 publish: JsonAPI | None = None, checks: JsonAPI | None = None) -> None:
        self._read, self._admission, self._publish = read, admission or read, publish or read
        self._checks = checks or self._publish

    def read_pr(self, pr: int) -> ReviewPR:
        row = self._read("GET", PREFIX + f"/pulls/{pr}", None)
        try:
            if row["number"] != pr or row["base"]["repo"]["full_name"] != REPOSITORY:
                raise ValueError
            if row["state"] not in {"open", "closed"} or type(row["draft"]) is not bool:
                raise ValueError
            count = row["changed_files"]
            if type(count) is not int or not 0 <= count <= 3000:
                raise ReviewError("review_scope_limit_exceeded")
            return ReviewPR(pr, login(row["user"]["login"]), sha(row["head"]["sha"]),
                            sha(row["base"]["sha"]), row["state"] == "open", row["draft"], count)
        except (KeyError, TypeError, ValueError):
            raise ReviewError("review_pr_invalid") from None

    def collaborator(self, actor: str) -> bool:
        row = self._admission("GET", PREFIX + "/collaborators/" + login(actor), None)
        if row is not True and row is not None:
            raise ReviewError("review_collaborator_response_invalid")
        return row is True

    def operator(self, actor: str) -> bool:
        row = self._admission("GET", PREFIX + "/collaborators/" + login(actor) + "/permission", None)
        if row is None:
            return False
        if type(row) is not dict or row.get("permission") not in {
            "none", "read", "triage", "write", "maintain", "admin"
        }:
            raise ReviewError("review_permission_response_invalid")
        return row["permission"] in {"write", "maintain", "admin"}

    def remove_review_label(self, pr: int) -> None:
        self._publish("DELETE", PREFIX + f"/issues/{pr}/labels/doc_review", None)

    def files(self, pr: ReviewPR) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for page in range(1, 31):
            batch = self._read("GET", PREFIX + f"/pulls/{pr.number}/files?per_page=100&page={page}", None)
            if type(batch) is not list or len(batch) > 100 or any(type(r) is not dict for r in batch):
                raise ReviewError("review_file_inventory_invalid")
            rows.extend({key: r[key] for key in ("filename", "previous_filename", "status", "patch", "additions", "deletions") if key in r}
                        for r in batch)
            if len(batch) < 100 or len(rows) == pr.changed_files:
                break
        if len(rows) != pr.changed_files or len({r.get("filename") for r in rows}) != len(rows):
            raise ReviewError("review_file_inventory_incomplete")
        return rows

    def read_text(self, path: str, revision: str, *, required: bool = True) -> str | None:
        path, revision = repo_path(path), sha(revision)
        row = self._read("GET", PREFIX + "/contents/" + urllib.parse.quote(path, safe="/")
                         + "?ref=" + revision, None)
        if row is None and not required:
            return None
        if row is None:
            raise ReviewError("review_required_content_missing")
        try:
            if row["type"] != "file" or row["encoding"] != "base64" or row["size"] > 2_000_000:
                raise ValueError
            raw = base64.b64decode(row["content"], validate=False)
            if len(raw) > 2_000_000:
                raise ValueError
            return raw.decode("utf-8")
        except (KeyError, TypeError, ValueError):
            raise ReviewError("review_content_invalid") from None

    def review_diff(self, pr: ReviewPR, files: list[dict[str, Any]],
                    rules_sha: str | None = None) -> ReviewDiff:
        """The files endpoint supplies three-dot hunks. Base reads use the labeled base tip."""
        row = self._read("GET", PREFIX + f"/pulls/{pr.number}", None)
        metadata: dict[str, object] = {
            "repository": REPOSITORY, "pr": pr.number, "title": str(row.get("title", ""))[:512],
            "description": str(row.get("body") or "")[:8000], "author": pr.author,
            "base_branch": row["base"].get("ref"), "head_branch": row["head"].get("ref"),
            "labels": [x["name"] for x in row.get("labels", [])[:30]],
            "requested_reviewers": [x["login"] for x in row.get("requested_reviewers", [])[:30]],
        }
        for endpoint in ("comments", "reviews"):
            rows = self._read("GET", PREFIX + f"/pulls/{pr.number}/{endpoint}?per_page=31", None)
            if not isinstance(rows, list):
                raise ReviewError("review_comments_invalid")
            metadata[endpoint] = [
                {"path": x.get("path"), "line": x.get("line"), "body": str(x.get("body") or "")[:400]}
                for x in rows[:30]
            ]
            metadata[endpoint + "_possibly_truncated"] = len(rows) > 30 or any(
                len(str(x.get("body") or "")) > 400 for x in rows[:30])
        current = self.read_pr(pr.number)
        if (current.head_sha, current.base_sha) != (pr.head_sha, pr.base_sha):
            raise ReviewError("superseded")
        return ReviewDiff(pr.head_sha, pr.base_sha, sha(rules_sha or pr.base_sha), metadata, files)

    def snapshot(self, pr: ReviewPR, files: list[dict[str, Any]],
                 rules_sha: str | None = None) -> ReviewSnapshot | None:
        relevant = [r for r in files if str(r.get("filename", "")).startswith(ROOT)
                    or str(r.get("previous_filename", "")).startswith(ROOT)]
        if not relevant:
            return None
        if len(relevant) > 100:
            raise ReviewError("review_scope_limit_exceeded")
        compared = self._read("GET", PREFIX + "/compare/" + pr.base_sha + "..." + pr.head_sha, None)
        try:
            base = sha(compared["merge_base_commit"]["sha"])
        except (KeyError, TypeError):
            raise ReviewError("review_merge_base_invalid") from None
        rules_revision = sha(rules_sha) if rules_sha else pr.base_sha
        rules = {RULE_ROOT + name: self.read_text(RULE_ROOT + name, rules_revision)
                 for name in ("DOCUMENTATION_POLICY.md", *RULE_FILES)}
        total_bytes = sum(len(v.encode("utf-8")) for v in rules.values() if v is not None)
        def account(value: str | None) -> None:
            nonlocal total_bytes
            total_bytes += len(value.encode("utf-8")) if value is not None else 0
            if total_bytes > 10_000_000:
                raise ReviewError("snapshot_text_limit_exceeded")
        changes: list[dict[str, Any]] = []
        for row in relevant:
            path = row["filename"]
            old = row.get("previous_filename", path)
            status = row.get("status")
            if status not in {"added", "modified", "removed", "renamed", "changed", "copied"}:
                raise ReviewError("review_file_status_invalid")
            if path.startswith(ROOT):
                doc_path(path)
                if path.endswith(".md"):
                    before = self.read_text(old, base) if old.startswith(ROOT) and status != "added" else None
                    after = None if status == "removed" else self.read_text(path, pr.head_sha)
                    account(before)
                    account(after)
                    changes.append({"path": path, "before": before, "after": after})
            if status == "renamed" and old.startswith(ROOT) and old != path and old.endswith(".md"):
                before = self.read_text(old, base)
                account(before)
                changes.append({"path": doc_path(old), "before": before, "after": None})
        if not changes:
            return None
        context: dict[str, str] = {}
        changed_paths = {r["path"] for r in changes}
        # Includes and directly linked Markdown pages are data, never executable configs.
        for change in changes:
            for resolved in markdown_targets(change["path"], change["after"] or ""):
                if resolved in changed_paths or resolved in context:
                    continue
                if len(context) >= 200:
                    raise ReviewError("review_context_limit_exceeded")
                value = self.read_text(resolved, pr.head_sha, required=False)
                if value is not None:
                    account(value)
                    context[resolved] = value
        if self.read_pr(pr.number).head_sha != pr.head_sha:
            raise ReviewError("superseded")
        return ReviewSnapshot.from_json({"head_sha": pr.head_sha, "base_sha": base,
                                        "rules_sha": rules_revision, "rules": rules,
                                        "files": changes, "context": context})

    def publish(self, pr: int, head: str, body: str, conclusion: str) -> None:
        if self.read_pr(pr).head_sha != head:
            raise ReviewError("superseded")
        self._checks("POST", PREFIX + "/check-runs", {
            "name": "YDB documentation policy review", "head_sha": head,
            "status": "completed", "conclusion": conclusion,
            "output": {"title": "Проверка документации YDB", "summary": body[:60000]},
        })
        marker = "<!-- ydbdoc-policy-review:v1 -->"
        try:
            identity = self._publish("GET", "/user", None)
            actor = identity["login"]
        except (ReviewError, KeyError, TypeError):
            actor = "github-actions[bot]"
        matches: list[int] = []
        for page in range(1, 31):
            comments = self._read("GET", PREFIX + f"/issues/{pr}/comments?per_page=100&page={page}", None)
            if type(comments) is not list:
                raise ReviewError("review_comments_invalid")
            for row in comments:
                if row.get("user", {}).get("login") == actor and row.get("body", "").startswith(marker):
                    matches.append(row["id"])
            if len(comments) < 100:
                break
        else:
            raise ReviewError("review_comment_inventory_limit")
        if self.read_pr(pr).head_sha != head:
            raise ReviewError("superseded")
        public_body = marker + "\n\nПроверенный SHA: `" + head + "`.\n\n" + body
        if matches:
            self._publish("PATCH", PREFIX + f"/issues/comments/{min(matches)}", {"body": public_body})
        else:
            self._publish("POST", PREFIX + f"/issues/{pr}/comments", {"body": public_body})
