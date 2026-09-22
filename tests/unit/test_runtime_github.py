import json
import urllib.error

import pytest

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
    method, expected_token, monkeypatch
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
