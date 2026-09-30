"""Small GitHub REST boundary. No checkout, shell, hooks, or repository code execution."""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from time import sleep
from typing import Any

from ydbdoc_review_ng.domain import GitSha, RepoPath, SnapshotRef
from ydbdoc_review_ng.errors import SafeDiagnosticError
from ydbdoc_review_ng.publication import PublicationContext, PublicationPlan
from ydbdoc_review_ng.reporting import Comment
from ydbdoc_review_ng.trace import traced

JsonTransport = Callable[[str, str, object], Any]
_CONTENT_CACHE_MAX_ENTRIES = 4096
_CONTENT_CACHE_MAX_BYTES = 16 * 1024 * 1024
_RETRYABLE_GITHUB_STATUSES = frozenset({429, 500, 502, 503, 504})
_GITHUB_GET_RETRY_DELAYS = (0.25, 1.0)


class RuntimeBoundaryError(SafeDiagnosticError):
    """Only fixed diagnostics cross a remote-service boundary."""


@dataclass(frozen=True, slots=True)
class IssueEvent:
    event_id: int
    event: str
    label: str | None
    actor: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class OperatorComment:
    comment_id: int
    author: str
    created_at: datetime
    updated_at: datetime
    body: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class TranslationProvenance:
    source_pr: int
    source_sha: GitSha


@dataclass(frozen=True, slots=True)
class PullRequestIdentity:
    number: int
    head_repository: str
    head_branch: str
    head_sha: GitSha
    base_repository: str
    base_branch: str
    provenance: TranslationProvenance | None


def _positive_id(value: Any) -> int:
    if type(value) is not int or value < 1:
        raise ValueError
    return value


def _text(value: Any) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError
    return value


def _timestamp(value: Any) -> datetime:
    if (
        type(value) is not str
        or re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?"
            r"(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)",
            value,
        )
        is None
    ):
        raise ValueError
    return datetime.fromisoformat(value)


def _single_page(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise TypeError
    if len(value) > 100:
        raise RuntimeBoundaryError("github_result_exceeds_single_page")
    return value


def _translation_provenance(body: str) -> TranslationProvenance | None:
    if "ydbdoc-source-" not in body:
        return None
    prs = re.findall(r"<!-- ydbdoc-source-pr:([1-9][0-9]*) -->", body)
    shas = re.findall(r"<!-- ydbdoc-source-sha:([0-9a-f]{40}) -->", body)
    if len(prs) != 1 or len(shas) != 1 or body.count("ydbdoc-source-") != 2:
        raise RuntimeBoundaryError("translation_provenance_invalid")
    return TranslationProvenance(int(prs[0]), GitSha(shas[0]))


class GitHubHTTP:
    def __init__(
        self,
        read_token: str,
        mutation_token: str,
        *,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        self._read_token = read_token
        self._mutation_token = mutation_token
        self._sleep = sleeper

    def __call__(self, method: str, path: str, payload: object) -> Any:
        endpoint = path.split("?", 1)[0]
        with traced("github", "request", method=method, endpoint=endpoint):
            token = (
                self._read_token
                if method == "GET" and path != "/user"
                else self._mutation_token
            )
            if not token:
                raise RuntimeBoundaryError("github_credentials_missing")
            request = urllib.request.Request(
                "https://api.github.com" + path,
                data=None if payload is None else json.dumps(payload).encode(),
                method=method,
                headers={
                    "Authorization": "Bearer " + token,
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "Content-Type": "application/json",
                },
            )
            for attempt in range(len(_GITHUB_GET_RETRY_DELAYS) + 1):
                try:
                    with urllib.request.urlopen(request, timeout=60) as response:
                        # Do not silently publish a truncated scope or duplicate a comment.
                        if 'rel="next"' in response.headers.get("Link", ""):
                            raise RuntimeBoundaryError("github_result_exceeds_single_page")
                        return json.loads(response.read())
                except urllib.error.HTTPError as error:
                    if error.code == 404 and method == "GET":
                        return None
                    retryable = method == "GET" and error.code in _RETRYABLE_GITHUB_STATUSES
                except (OSError, ValueError):
                    retryable = method == "GET"
                if not retryable or attempt == len(_GITHUB_GET_RETRY_DELAYS):
                    raise RuntimeBoundaryError("github_request_failed") from None
                self._sleep(_GITHUB_GET_RETRY_DELAYS[attempt])
            raise AssertionError("bounded GitHub GET retry exhausted")


class GitHubBackend:
    repository = "ydb-platform/ydb"
    prefix = "/repos/ydb-platform/ydb"

    def __init__(self, transport: JsonTransport) -> None:
        self.transport = transport
        self.source_pr: int | None = None
        self.source_sha: GitSha | None = None
        self._content_cache: dict[tuple[SnapshotRef, RepoPath], bytes | None] = {}
        self._content_cache_bytes = 0

    def request(self, method: str, path: str, payload: object = None) -> Any:
        return self.transport(method, self.prefix + path, payload)

    def read_issue_events(self, pr_number: int, /) -> tuple[IssueEvent, ...]:
        """Read one complete page, retaining the actual actor and event time."""
        rows = self.request("GET", f"/issues/{pr_number}/events?per_page=100")
        try:
            events = tuple(
                IssueEvent(
                    _positive_id(row["id"]),
                    _text(row["event"]),
                    _text(row["label"]["name"])
                    if row["event"] in {"labeled", "unlabeled"}
                    else None,
                    _text(row["actor"]["login"]),
                    _timestamp(row["created_at"]),
                )
                for row in _single_page(rows)
            )
            if len({event.event_id for event in events}) != len(events):
                raise ValueError
            return events
        except (KeyError, TypeError, ValueError):
            raise RuntimeBoundaryError("issue_events_invalid") from None

    def read_operator_comments(self, pr_number: int, /) -> tuple[OperatorComment, ...]:
        """Unlike QA comments, these keep authors and timestamps for authorization."""
        rows = self.request("GET", f"/issues/{pr_number}/comments?per_page=100")
        try:
            comments = []
            for row in _single_page(rows):
                if type(row["body"]) is not str:
                    raise ValueError
                created_at = _timestamp(row["created_at"])
                updated_at = _timestamp(row["updated_at"])
                if updated_at < created_at:
                    raise ValueError
                comments.append(
                    OperatorComment(
                        _positive_id(row["id"]),
                        _text(row["user"]["login"]),
                        created_at,
                        updated_at,
                        row["body"],
                    )
                )
            if len({comment.comment_id for comment in comments}) != len(comments):
                raise ValueError
            return tuple(comments)
        except (KeyError, TypeError, ValueError):
            raise RuntimeBoundaryError("operator_comments_invalid") from None

    def read_pull_request_identity(self, pr_number: int, /) -> PullRequestIdentity:
        """Read existing publication markers; callers must match them to a checkpoint."""
        row = self.request("GET", f"/pulls/{pr_number}")
        try:
            body = row["body"]
            if body is None:
                body = ""
            if type(body) is not str or _positive_id(row["number"]) != pr_number:
                raise ValueError
            return PullRequestIdentity(
                pr_number,
                _text(row["head"]["repo"]["full_name"]),
                _text(row["head"]["ref"]),
                GitSha(row["head"]["sha"]),
                _text(row["base"]["repo"]["full_name"]),
                _text(row["base"]["ref"]),
                _translation_provenance(body),
            )
        except (KeyError, TypeError, ValueError):
            raise RuntimeBoundaryError("pull_request_identity_invalid") from None

    def read_bytes(self, snapshot: SnapshotRef, path: RepoPath, /) -> bytes | None:
        if snapshot.repository.value != self.repository:
            raise RuntimeBoundaryError("repository_mismatch")
        key = (snapshot, path)
        if key in self._content_cache:
            return self._content_cache[key]
        data = self.request(
            "GET",
            "/contents/"
            + urllib.parse.quote(path.value, safe="/")
            + "?ref="
            + snapshot.commit_sha.value,
        )
        if data is None:
            content = None
        elif (
            not isinstance(data, dict)
            or data.get("type") != "file"
            or data.get("encoding") != "base64"
        ):
            raise RuntimeBoundaryError("unsupported_repository_object")
        else:
            content = base64.b64decode(data["content"])
        size = 0 if content is None else len(content)
        # Scope/link/terminology reads repeatedly address the same immutable files.
        # Do not cache branch heads, mutable API records, or failed requests.
        if (
            len(self._content_cache) < _CONTENT_CACHE_MAX_ENTRIES
            and self._content_cache_bytes + size <= _CONTENT_CACHE_MAX_BYTES
        ):
            self._content_cache[key] = content
            self._content_cache_bytes += size
        return content

    def head(self, branch: str) -> GitSha | None:
        data = self.request("GET", "/git/ref/heads/" + urllib.parse.quote(branch, safe="/"))
        return None if data is None else GitSha(data["object"]["sha"])

    def commit(self, context: PublicationContext, plan: PublicationPlan, /) -> GitSha:
        if plan.metadata:
            raise RuntimeBoundaryError("metadata_must_be_materialized_before_validation")
        parent = self.request("GET", "/git/commits/" + context.current_head.value)
        tree = []
        for change in plan.files:
            if change.before == change.after:
                continue
            blob = None
            if change.after is not None:
                blob = self.request(
                    "POST",
                    "/git/blobs",
                    {
                        "encoding": "base64",
                        "content": base64.b64encode(change.after).decode("ascii"),
                    },
                )["sha"]
            tree.append({"path": change.path.value, "mode": "100644", "type": "blob", "sha": blob})
        tree_sha = self.request(
            "POST", "/git/trees", {"base_tree": parent["tree"]["sha"], "tree": tree}
        )["sha"]
        result = self.request(
            "POST",
            "/git/commits",
            {
                "message": "Synchronize documentation translation",
                "tree": tree_sha,
                "parents": [context.current_head.value],
            },
        )
        return GitSha(result["sha"])

    def push(self, context: PublicationContext, sha: GitSha, /) -> None:
        current = self.head(context.branch)
        expected = context.expected_branch_head
        if expected is None:
            if context.branch_must_exist:
                raise RuntimeBoundaryError("head_disappeared")
            if current is not None:
                raise RuntimeBoundaryError("head_changed")
            self.request(
                "POST", "/git/refs", {"ref": "refs/heads/" + context.branch, "sha": sha.value}
            )
        elif current != expected:
            raise RuntimeBoundaryError("head_changed")
        else:
            self.request(
                "PATCH",
                "/git/refs/heads/" + urllib.parse.quote(context.branch, safe="/"),
                {"sha": sha.value, "force": expected != context.current_head},
            )

    def find_pr(self, repository: str, branch: str, base: str, /) -> int | None:
        if repository != self.repository:
            raise RuntimeBoundaryError("repository_mismatch")
        query = urllib.parse.urlencode(
            {"state": "open", "head": "ydb-platform:" + branch, "base": base, "per_page": 100}
        )
        matches = self.request("GET", "/pulls?" + query)
        if len(matches) > 1:
            raise RuntimeBoundaryError("ambiguous_translation_pr")
        return int(matches[0]["number"]) if matches else None

    def create_pr(self, context: PublicationContext, sha: GitSha, /) -> int:
        result = self.request(
            "POST",
            "/pulls",
            {
                "title": f"PR #{self.source_pr} translation",
                "head": context.branch,
                "base": context.base,
                "body": f"<!-- ydbdoc-source-pr:{self.source_pr} -->\n"
                f"<!-- ydbdoc-source-sha:{self.source_sha.value if self.source_sha else ''} -->\n"
                "Checked translation commit: " + sha.value,
            },
        )
        return int(result["number"])

    def update_pr(self, pr_number: int, context: PublicationContext, sha: GitSha, /) -> None:
        current = self.request("GET", f"/pulls/{pr_number}")
        body = current.get("body") or ""
        import re

        body = re.sub(r"<!-- ydbdoc-source-(?:pr|sha):[^>]* -->\n?", "", body)
        body = re.sub(
            r"^Checked translation commit: [0-9a-f]{40}\n?",
            "",
            body,
            flags=re.MULTILINE,
        )
        provenance = (
            f"<!-- ydbdoc-source-pr:{self.source_pr} -->\n"
            f"<!-- ydbdoc-source-sha:{self.source_sha.value if self.source_sha else ''} -->\n"
        )
        self.request(
            "PATCH",
            f"/pulls/{pr_number}",
            {
                "base": context.base,
                "body": provenance + "Checked translation commit: " + sha.value + "\n" + body,
            },
        )

    def list_comments(self, pr_number: int, /) -> tuple[Comment, ...]:
        publisher_id: int | None = None
        try:
            publisher = self.transport("GET", "/user", None)
            if not isinstance(publisher, dict) or type(publisher.get("id")) is not int:
                raise RuntimeBoundaryError("github_publisher_identity_invalid")
            publisher_id = publisher["id"]
        except RuntimeBoundaryError:
            # GitHub App installation tokens (including Actions GITHUB_TOKEN)
            # cannot call the user endpoint. Their repository mutations have
            # the stable github-actions[bot] actor instead. Marker filtering in
            # the reporter still prevents unrelated bot comments from matching.
            pass
        rows = self.request("GET", f"/issues/{pr_number}/comments?per_page=100")
        return tuple(
            Comment(
                int(row["id"]),
                row["user"].get("id") == publisher_id
                if publisher_id is not None
                else row["user"].get("login") == "github-actions[bot]"
                and row["user"].get("type") == "Bot",
                row["body"],
            )
            for row in rows
        )

    def create_comment(self, pr_number: int, body: str, /) -> None:
        self.request("POST", f"/issues/{pr_number}/comments", {"body": body})

    def update_comment(self, pr_number: int, comment_id: int, body: str, /) -> None:
        self.request("PATCH", f"/issues/comments/{comment_id}", {"body": body})
