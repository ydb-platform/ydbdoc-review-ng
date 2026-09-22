import json
import urllib.error

import pytest

from ydbdoc_review_ng.runtime_github import GitHubHTTP, RuntimeBoundaryError


class Response:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return json.dumps({"ok": True}).encode()


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
    ("method", "read_token", "mutation_token"),
    [
        ("GET", "", "mutation-token"),
        ("POST", "read-token", ""),
    ],
)
def test_github_http_rejects_the_missing_credential_for_the_method(
    method, read_token, mutation_token, monkeypatch
) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("missing credentials must fail before network I/O")

    monkeypatch.setattr("urllib.request.urlopen", forbidden)

    with pytest.raises(RuntimeBoundaryError, match="^github_credentials_missing$"):
        GitHubHTTP(read_token, mutation_token)(method, "/resource", None)


def test_failed_authenticated_read_is_not_retried_without_credentials(monkeypatch) -> None:
    authorizations = []

    def unauthorized(request, timeout):
        authorizations.append(request.get_header("Authorization"))
        raise urllib.error.HTTPError(request.full_url, 401, "unauthorized", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", unauthorized)

    with pytest.raises(RuntimeBoundaryError, match="^github_request_failed$"):
        GitHubHTTP("read-token", "mutation-token")("GET", "/resource", None)

    assert authorizations == ["Bearer read-token"]
