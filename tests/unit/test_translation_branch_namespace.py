from decimal import Decimal

import pytest

from ydbdoc_review_ng.application import TranslateWorkflowInput
from ydbdoc_review_ng.domain import GitSha
from ydbdoc_review_ng.runtime import RuntimeSource
from ydbdoc_review_ng.runtime_github import GitHubBackend


@pytest.mark.parametrize("legacy_exists", [False, True])
def test_translation_preserves_existing_ref_and_namespaces_new_refs(legacy_exists):
    events = []

    def transport(method, path, payload):
        events.append((method, path))
        if method == "DELETE" and path.endswith("/issues/42/labels/doc_translate"):
            return None
        if method == "GET" and path.endswith("/git/ref/heads/translation/pr-42"):
            return {"object": {"sha": "a" * 40}} if legacy_exists else None
        raise AssertionError((method, path))

    source = RuntimeSource(
        {"GITHUB_ACTOR": "maintainer", "YDBDOC_ALLOWED_ACTORS": "maintainer"},
        GitHubBackend(transport),
    )
    request = TranslateWorkflowInput(42, GitSha("a" * 40), Decimal(10))

    authorization = source.authorize_translate(request)

    assert authorization.branch == (
        "translation/pr-42" if legacy_exists else "robots/docs/translation/pr-42"
    )
    assert not any(method != "GET" and "/git/" in path for method, path in events)
