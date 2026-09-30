import json
import urllib.error

import pytest

from ydbdoc_review_ng.domain import GitSha
from ydbdoc_review_ng.publication import PublicationContext
from ydbdoc_review_ng.runtime_github import GitHubBackend, GitHubHTTP, RuntimeBoundaryError


class Response:
    def __init__(self, payload: object | None = None) -> None:
        self.headers: dict[str, str] = {}
        self.payload = {"ok": True} if payload is None else payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def test_translation_pr_title_names_the_authoritative_source_pr() -> None:
    calls: list[tuple[str, str, object]] = []

    def transport(method: str, path: str, payload: object) -> object:
        calls.append((method, path, payload))
        return {"number": 53839}

    backend = GitHubBackend(transport)
    backend.source_pr = 51079
    backend.source_sha = GitSha("a" * 40)
    context = PublicationContext(
        "ydb-platform/ydb",
        "translation/pr-51079",
        "main",
        "main",
        GitSha("b" * 40),
    )

    assert backend.create_pr(context, GitSha("c" * 40)) == 53839
    assert calls[0][0:2] == ("POST", "/repos/ydb-platform/ydb/pulls")
    assert calls[0][2]["title"] == "PR #51079 translation"


def test_rerun_replaces_only_the_captured_translation_head() -> None:
    old_head = GitSha("a" * 40)
    base = GitSha("b" * 40)
    translated = GitSha("c" * 40)
    calls: list[tuple[str, str, object]] = []

    def transport(method: str, path: str, payload: object) -> object:
        calls.append((method, path, payload))
        if "/git/ref/heads/" in path:
            return {"object": {"sha": old_head.value}}
        if "/git/refs/heads/" in path:
            return {}
        raise AssertionError(path)

    context = PublicationContext(
        "ydb-platform/ydb",
        "translation/pr-42",
        "main",
        "main",
        base,
        expected_branch_head=old_head,
    )

    GitHubBackend(transport).push(context, translated)

    assert calls[-1][0] == "PATCH"
    assert calls[-1][2] == {"sha": translated.value, "force": True}


def test_rerun_does_not_replace_a_translation_head_that_moved() -> None:
    old_head = GitSha("a" * 40)
    calls: list[tuple[str, str, object]] = []

    def transport(method: str, path: str, payload: object) -> object:
        calls.append((method, path, payload))
        return {"object": {"sha": "d" * 40}}

    context = PublicationContext(
        "ydb-platform/ydb",
        "translation/pr-42",
        "main",
        "main",
        GitSha("b" * 40),
        expected_branch_head=old_head,
    )

    with pytest.raises(RuntimeBoundaryError, match="^head_changed$"):
        GitHubBackend(transport).push(context, GitSha("c" * 40))

    assert not any(method == "PATCH" for method, _path, _payload in calls)


def test_existing_pr_body_records_the_new_translation_commit() -> None:
    calls: list[tuple[str, str, object]] = []

    def transport(method: str, path: str, payload: object) -> object:
        calls.append((method, path, payload))
        if method == "GET":
            return {
                "body": "<!-- ydbdoc-source-pr:1 -->\n"
                "<!-- ydbdoc-source-sha:" + "d" * 40 + " -->\n"
                "Checked translation commit: " + "e" * 40 + "\n"
                "Operator note\n"
            }
        return {}

    backend = GitHubBackend(transport)
    backend.source_pr = 42
    backend.source_sha = GitSha("a" * 40)
    translated = GitSha("c" * 40)
    context = PublicationContext(
        "ydb-platform/ydb", "translation/pr-42", "main", "main", GitSha("b" * 40)
    )

    backend.update_pr(43, context, translated)

    body = calls[-1][2]["body"]
    assert body.count("ydbdoc-source-pr:") == 1
    assert body.count("ydbdoc-source-sha:") == 1
    assert body.count("Checked translation commit:") == 1
    assert f"Checked translation commit: {translated.value}" in body
    assert body.endswith("Operator note\n")


@pytest.mark.parametrize(
    ("method", "expected_token"),
    [
        ("GET", "read-token"),
        ("POST", "mutation-token"),
        ("PATCH", "mutation-token"),
        ("PUT", "mutation-token"),
        ("DELETE", "mutation-token"),
    ],
)
def test_github_http_routes_reads_and_mutations_to_distinct_credentials(
    method, expected_token, monkeypatch, capsys
) -> None:
    requests = []

    def urlopen(request, timeout):
        requests.append(request)
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)

    result = GitHubHTTP("read-token", "mutation-token")(method, "/resource", None)

    assert result == {"ok": True}
    assert len(requests) == 1
    assert requests[0].get_header("Authorization") == f"Bearer {expected_token}"
    output = capsys.readouterr().err
    events = [
        json.loads(line.removeprefix("YDBDOC_TRACE ")) for line in output.splitlines()
    ]
    assert [(event["status"], event["method"], event["endpoint"]) for event in events] == [
        ("start", method, "/resource"),
        ("ok", method, "/resource"),
    ]
    assert "read-token" not in output
    assert "mutation-token" not in output


@pytest.mark.parametrize(
    ("method", "path", "read_token", "mutation_token"),
    [
        ("GET", "/resource", "", "mutation-token"),
        ("GET", "/user", "read-token", ""),
        ("POST", "/resource", "read-token", ""),
    ],
)
def test_github_http_rejects_the_missing_credential_for_the_method(
    method, path, read_token, mutation_token, monkeypatch
) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("missing credentials must fail before network I/O")

    monkeypatch.setattr("urllib.request.urlopen", forbidden)

    with pytest.raises(RuntimeBoundaryError, match="^github_credentials_missing$"):
        GitHubHTTP(read_token, mutation_token)(method, path, None)


def test_failed_authenticated_read_is_not_retried_without_credentials(monkeypatch) -> None:
    authorizations = []

    def unauthorized(request, timeout):
        authorizations.append(request.get_header("Authorization"))
        raise urllib.error.HTTPError(request.full_url, 401, "unauthorized", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", unauthorized)

    with pytest.raises(RuntimeBoundaryError, match="^github_request_failed$"):
        GitHubHTTP("read-token", "mutation-token")("GET", "/resource", None)

    assert authorizations == ["Bearer read-token"]


def test_github_http_retries_transient_get_without_retrying_mutations(monkeypatch) -> None:
    get_attempts = 0
    delays: list[float] = []

    def transient_get(request, timeout):
        nonlocal get_attempts
        get_attempts += 1
        if get_attempts < 3:
            raise OSError("temporary connection failure")
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", transient_get)

    result = GitHubHTTP(
        "read-token", "mutation-token", sleeper=delays.append
    )("GET", "/resource", None)

    assert result == {"ok": True}
    assert get_attempts == 3
    assert delays == [0.25, 1.0]

    mutation_attempts = 0

    def transient_mutation(request, timeout):
        nonlocal mutation_attempts
        mutation_attempts += 1
        raise OSError("uncertain mutation result")

    monkeypatch.setattr("urllib.request.urlopen", transient_mutation)

    with pytest.raises(RuntimeBoundaryError, match="^github_request_failed$"):
        GitHubHTTP("read-token", "mutation-token", sleeper=delays.append)(
            "POST", "/resource", {"value": 1}
        )

    assert mutation_attempts == 1


def test_github_http_retries_only_retryable_http_statuses(monkeypatch) -> None:
    attempts = 0

    def unavailable_then_ok(request, timeout):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise urllib.error.HTTPError(request.full_url, 503, "unavailable", {}, None)
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", unavailable_then_ok)

    assert GitHubHTTP("read-token", "mutation-token", sleeper=lambda _delay: None)(
        "GET", "/resource", None
    ) == {"ok": True}
    assert attempts == 2


def test_comment_ownership_uses_mutation_identity_while_comment_list_uses_read_token(
    monkeypatch,
) -> None:
    requests = []

    def urlopen(request, timeout):
        authorization = request.get_header("Authorization")
        path = request.full_url.removeprefix("https://api.github.com")
        requests.append((path, authorization))
        if path == "/user":
            if authorization != "Bearer mutation-token":
                raise urllib.error.HTTPError(request.full_url, 403, "forbidden", {}, None)
            return Response({"id": 72})
        if path == "/repos/ydb-platform/ydb/issues/42/comments?per_page=100":
            return Response([{"id": 7, "user": {"id": 72}, "body": "GREEN"}])
        raise AssertionError(path)

    monkeypatch.setattr("urllib.request.urlopen", urlopen)

    comments = GitHubBackend(GitHubHTTP("read-token", "mutation-token")).list_comments(42)

    assert comments[0].authored_by_publisher is True
    assert requests == [
        ("/user", "Bearer mutation-token"),
        (
            "/repos/ydb-platform/ydb/issues/42/comments?per_page=100",
            "Bearer read-token",
        ),
    ]


def test_actions_installation_token_uses_bot_comment_identity_without_user_endpoint() -> None:
    def transport(method, path, payload):
        if path == "/user":
            raise RuntimeBoundaryError("github_request_failed")
        if path.endswith("/issues/42/comments?per_page=100"):
            return [
                {
                    "id": 7,
                    "user": {
                        "id": 41898282,
                        "type": "Bot",
                        "login": "github-actions[bot]",
                    },
                    "body": "<!-- ydbdoc-qa-report -->",
                },
                {
                    "id": 8,
                    "user": {"id": 1, "type": "User", "login": "maintainer"},
                    "body": "human comment",
                },
            ]
        raise AssertionError((method, path, payload))

    comments = GitHubBackend(transport).list_comments(42)

    assert [item.authored_by_publisher for item in comments] == [True, False]


def test_immutable_content_reads_are_cached_but_new_snapshots_are_read():
    import base64

    from ydbdoc_review_ng.domain import RepoPath, RepositoryId, SnapshotRef

    calls = []
    def transport(method, path, payload):
        calls.append(path)
        return {'type': 'file', 'encoding': 'base64', 'content': base64.b64encode(path.encode()).decode()}
    backend = GitHubBackend(transport)
    first = SnapshotRef(RepositoryId('ydb-platform/ydb'), GitSha('a' * 40))
    second = SnapshotRef(first.repository, GitSha('b' * 40))
    path = RepoPath('ydb/docs/ru/core/page.md')
    before = backend.read_bytes(first, path)
    assert backend.read_bytes(first, path) == before
    assert len(calls) == 1
    assert backend.read_bytes(second, path) != before
    assert len(calls) == 2


def test_immutable_missing_content_is_cached_but_transport_errors_are_not():
    from ydbdoc_review_ng.domain import RepoPath, RepositoryId, SnapshotRef

    calls = []
    def transport(method, path, payload):
        calls.append(path)
        if len(calls) == 1:
            raise RuntimeBoundaryError('temporary_failure')
    backend = GitHubBackend(transport)
    snapshot = SnapshotRef(RepositoryId('ydb-platform/ydb'), GitSha('a' * 40))
    path = RepoPath('ydb/docs/ru/core/missing.md')
    with pytest.raises(RuntimeBoundaryError):
        backend.read_bytes(snapshot, path)
    assert backend.read_bytes(snapshot, path) is None
    assert backend.read_bytes(snapshot, path) is None
    assert len(calls) == 2


def test_branch_heads_are_never_cached():
    calls = []
    def transport(method, path, payload):
        calls.append(path)
        return {'object': {'sha': ('a' if len(calls) == 1 else 'b') * 40}}
    backend = GitHubBackend(transport)
    assert backend.head('translation/pr-42') != backend.head('translation/pr-42')
    assert len(calls) == 2


@pytest.mark.parametrize('budget', ['_CONTENT_CACHE_MAX_ENTRIES', '_CONTENT_CACHE_MAX_BYTES'])
def test_content_cache_stops_admitting_entries_at_budget(monkeypatch, budget):
    import base64

    from ydbdoc_review_ng import runtime_github
    from ydbdoc_review_ng.domain import RepoPath, RepositoryId, SnapshotRef
    monkeypatch.setattr(runtime_github, budget, 0)
    calls = []
    def transport(method, path, payload):
        calls.append(path)
        return {'type': 'file', 'encoding': 'base64', 'content': base64.b64encode(b'content').decode()}
    backend = GitHubBackend(transport)
    snapshot = SnapshotRef(RepositoryId('ydb-platform/ydb'), GitSha('a' * 40))
    path = RepoPath('page.md')
    assert backend.read_bytes(snapshot, path) == b'content'
    assert backend.read_bytes(snapshot, path) == b'content'
    assert len(calls) == 2
