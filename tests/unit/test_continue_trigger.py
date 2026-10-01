from __future__ import annotations

import importlib
import json

import pytest

from ydbdoc_review_ng.runtime_github import GitHubBackend, RuntimeBoundaryError


def event(identifier=1, *, at="2026-09-22T10:00:00Z", actor="writer", kind="labeled"):
    return {
        "id": identifier,
        "event": kind,
        "label": {"name": "doc_continue"},
        "actor": {"login": actor},
        "created_at": at,
    }


def comment(identifier=2, *, at="2026-09-22T09:00:00Z", author="writer", body=None):
    return {
        "id": identifier,
        "user": {"login": author},
        "created_at": at,
        "updated_at": at,
        "body": "/ydbdoc continue\nUse RU as source." if body is None else body,
    }


def select(events, comments, allowed=frozenset({"writer"})):
    module = importlib.import_module("ydbdoc_review_ng.runtime_continue")
    routes = {
        "/repos/ydb-platform/ydb/issues/42/events?per_page=100": events,
        "/repos/ydb-platform/ydb/issues/42/comments?per_page=100": comments,
    }

    def transport(method, path, payload):
        assert method == "GET" and payload is None
        return routes[path]

    return module.select_continue_trigger(GitHubBackend(transport), 42, allowed)


def test_latest_label_and_latest_allowed_command_are_selected_by_time():
    context = "  private context\r\n\r\nsecond line\n  "
    result = select(
        [
            event(5, at="2026-09-22T12:00:00Z"),
            event(4, at="2026-09-22T11:00:00Z", kind="unlabeled"),
            event(1, actor="outsider"),
        ],
        [
            comment(6, at="2026-09-22T11:45:00Z", author="outsider"),
            comment(3, at="2026-09-22T11:30:00Z", body="/ydbdoc continue\r\n" + context),
            comment(2),
            comment(7, at="2026-09-22T13:00:00Z"),
            comment(8, at="2026-09-22T11:55:00Z", body="ordinary discussion"),
        ],
    )
    assert result.label_event_id == 5
    assert result.comment_id == 3
    assert result.label_actor == result.comment_author == "writer"
    assert result.operator_context == context
    assert "private context" not in repr(result)


@pytest.mark.parametrize(
    "events, comments",
    [
        ([], [comment()]),
        ([event(actor="outsider")], [comment()]),
        ([event()], []),
        ([event()], [comment(author="outsider")]),
        ([event()], [comment(at="2026-09-22T10:00:00Z")]),
        ([event()], [comment(at="2026-09-22T10:00:01Z")]),
        ([event()], [comment(body="/ydbdoc continue")]),
        ([event()], [comment(body="/ydbdoc continue\n \n\t")]),
        ([event()], [comment(body=" /ydbdoc continue\ncontext")]),
        ([event()], [comment(body="/ydbdoc continue extra\ncontext")]),
        ([event()], [comment(body="intro\n/ydbdoc continue\ncontext")]),
        ([event()], [comment(), comment(3, at="2026-09-22T09:30:00Z", body="/ydbdoc continue\n")]),
        ([event(), event(3)], [comment()]),
        ([event()], [comment(), comment(3)]),
        ([event(), event(3, at="2026-09-22T11:00:00Z", kind="unlabeled")], [comment()]),
        ([event(at="invalid private context")], [comment()]),
        ([event(at="2026-09-22T10:00:00")], [comment()]),
        ([event()], [comment(at="invalid private context")]),
        ([event()], [{"body": "/ydbdoc continue\nprivate context"}]),
        ([{"event": "labeled", "label": {"name": "doc_continue"}}], [comment()]),
        ([event(), event(3, at="invalid private context")], [comment()]),
    ],
)
def test_invalid_admission_never_uses_fallback_or_exposes_context(events, comments):
    with pytest.raises(RuntimeBoundaryError) as error:
        select(events, comments)
    assert "private context" not in str(error.value)


def test_strict_comparison_uses_timestamp_not_lexical_order():
    result = select(
        [event(at="2026-09-22T10:00:00Z")],
        [comment(at="2026-09-22T11:59:59+02:00")],
    )
    assert result.comment_id == 2


@pytest.mark.parametrize("field", ["actor", "created_at", "label", "id", "event"])
def test_malformed_event_metadata_is_fail_closed(field):
    broken = event()
    broken[field] = None
    with pytest.raises(RuntimeBoundaryError):
        select([broken], [comment()])


def test_duplicate_event_or_comment_ids_are_ambiguous():
    with pytest.raises(RuntimeBoundaryError):
        select([event(), event(at="2026-09-22T11:00:00Z")], [comment()])
    with pytest.raises(RuntimeBoundaryError):
        select([event()], [comment(), comment(at="2026-09-22T09:30:00Z")])


@pytest.mark.parametrize("rows", [None, {}, "private context", [None]])
def test_malformed_pages_are_fail_closed(rows):
    with pytest.raises(RuntimeBoundaryError):
        select(rows, [comment()])
    with pytest.raises(RuntimeBoundaryError):
        select([event()], rows)


def test_unrelated_labels_do_not_choose_the_boundary():
    unrelated = event(3, at="2026-09-22T11:00:00Z", actor="outsider")
    unrelated["label"] = {"name": "other-label"}
    assert select([event(), unrelated], [comment()]).label_event_id == 1


@pytest.mark.parametrize("page", ["events", "comments"])
def test_github_http_follows_link_next_pages(page, monkeypatch):
    """§1.1 / §5.3: inventory and continue admission must consume every page."""
    from ydbdoc_review_ng.runtime_continue import select_continue_trigger
    from ydbdoc_review_ng.runtime_github import GitHubHTTP

    calls: list[str] = []

    class Response:
        def __init__(self, body: object, link: str = "") -> None:
            self.headers = {"Link": link}
            self._body = json.dumps(body).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return self._body

    def urlopen(request, timeout):
        calls.append(request.full_url)
        if "page=2" in request.full_url:
            if page == "events":
                return Response([])
            return Response([])
        link = (
            f'<https://api.github.com/repos/ydb-platform/ydb/issues/42/{page}'
            f'?per_page=100&page=2>; rel="next"'
        )
        if page == "events":
            return Response([event()], link)
        return Response([comment()], link)

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    http = GitHubHTTP("test-token", "test-token")

    def transport(method, path, payload):
        if page == "comments" and "/events?" in path:
            return [event()]
        if page == "events" and "/comments?" in path:
            return [comment()]
        return http(method, path, payload)

    result = select_continue_trigger(GitHubBackend(transport), 42, frozenset({"writer"}))
    assert result.comment_id == 2
    assert any("page=2" in url for url in calls)
