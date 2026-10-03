"""Production-path witnesses for tip lifecycle bugs on 56d6bff.

Canon: REQUIREMENTS_RU.md §5 / §5.2 / §5.3 / §7.

Bug 1: repeated verify RED must not leave two open checkpoints that make
`/ydbdoc continue` fail with continue_checkpoint_missing_or_ambiguous.

Bug 2: neutralizing a zero-commit source RED into «Актуальный…» must keep
idempotent markers (or otherwise remain updatable) so a later clean translate
leaves only one current source link to the latest translation PR.

Bug 3: successful doc_verify must reconcile leftover source-only RED QA when a
translation PR already exists (§7), not only DOC_TRANSLATE / DOC_CONTINUE.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest
from _runtime_services import raw_repair_context, request_prompt, request_schema
from test_checkpoint_capture import CaptureServices, ENV
from test_continue_translation import ContinueServices

from ydbdoc_review_ng import application
from ydbdoc_review_ng.application import VerifyWorkflowInput
from ydbdoc_review_ng.domain import GitSha, Mode
from ydbdoc_review_ng.models import HttpResponse
from ydbdoc_review_ng.publication import GitPublicationAdapter, PublicationContext
from ydbdoc_review_ng.quality import CriticResult, Finding, QualityReviewResult, Verdict
from ydbdoc_review_ng.reporting import (
    Comment,
    QAReporter,
    ReportContext,
    QA_MARKER,
    TRANSLATION_LINK_MARKER,
)
from ydbdoc_review_ng.runtime import create_runtime

RU = "ydb/docs/ru/core/"
EN = "ydb/docs/en/core/"
TOC = "toc.yaml"
LINK_MARKER = TRANSLATION_LINK_MARKER


def _runtime(services):
    return create_runtime(
        environment=ENV,
        ydb_executor=services,
        github_transport=services.github,
        model_transport=services.model,
    )


class _RoutedComments:
    """Per-PR comment store with unique ids and PATCH-by-id (realistic §7)."""

    def __init__(self, services: CaptureServices, *, prs: tuple[int, ...] = (42, 43)):
        self._services = services
        self._next_id = 900
        self.by_pr: dict[int, list[dict]] = {pr: [] for pr in prs}
        self.fail_source_patch = False
        self.source_patch_attempts = 0

    def install(self) -> None:
        services = self._services
        original = services.github

        def github(method, path, payload):
            relative = path.removeprefix("/repos/ydb-platform/ydb")
            if relative.endswith("/comments?per_page=100"):
                pr = None
                for candidate in self.by_pr:
                    if f"/issues/{candidate}/" in relative:
                        pr = candidate
                        break
                if pr is not None:
                    if getattr(services, "continuing", False) and hasattr(
                        services, "commands"
                    ):
                        # Operator `/ydbdoc continue` must be visible on the
                        # trigger PR (source or translation).
                        return list(services.commands) + list(self.by_pr[pr])
                    return list(self.by_pr[pr])
            for pr in self.by_pr:
                if method == "POST" and relative == f"/issues/{pr}/comments":
                    self._next_id += 1
                    comment = {
                        "id": self._next_id,
                        "user": {"id": 42, "type": "User", "login": "pat-publisher"},
                        "body": payload["body"],
                        "created_at": "2026-09-21T12:00:00Z",
                        "updated_at": "2026-09-21T12:00:00Z",
                    }
                    self.by_pr[pr].append(comment)
                    if pr == 42:
                        services.source_comments = list(self.by_pr[42])
                    elif pr == 43:
                        services.comments = list(self.by_pr[43])
                    return {"id": comment["id"]}
            if method == "PATCH" and relative.startswith("/issues/comments/"):
                comment_id = int(relative.rsplit("/", 1)[-1])
                for pr, rows in self.by_pr.items():
                    for row in rows:
                        if row["id"] == comment_id:
                            if pr == 42 and self.fail_source_patch:
                                self.source_patch_attempts += 1
                                raise OSError("source comment patch temporarily unavailable")
                            row["body"] = payload["body"]
                            row["updated_at"] = "2026-09-21T13:00:00Z"
                            if pr == 42:
                                services.source_comments = list(self.by_pr[42])
                            elif pr == 43:
                                services.comments = list(self.by_pr[43])
                            return {}
                raise AssertionError(f"unknown comment id {comment_id}")
            return original(method, path, payload)

        services.github = github


def _red_finding(path: str = EN + "page.md") -> dict:
    return {
        "target_path": path,
        "searchable_snippet": "Translated",
        "reason": "Meaning still incomplete for publication.",
        "expected_correction": "Restore the missing meaning from the source.",
    }


@pytest.mark.parametrize("continue_pr", [42, 43])
def test_repeated_verify_red_keeps_continue_unambiguous(continue_pr: int) -> None:
    """§5.2/§5.3: second verify RED must supersede the prior open checkpoint."""

    class Witness(ContinueServices):
        def __init__(self):
            super().__init__(names=("page",), stop=None)
            self.phase = "translate"

        def model(self, request):

            body = json.loads(request.body)
            if body.get("tools"):
                return super().model(request)
            schema = request_schema(body)
            if schema is None:
                return super().model(request)
            props = schema["schema"]["properties"]
            if "translation_required" in props:
                return super().model(request)
            if "files" in props:
                self.roles.append("critic")
                files = json.loads(
                    raw_repair_context(request_prompt(body), "translation-pr-files")
                )
                text = json.dumps({"files": files})
            elif "findings" in props:
                self.roles.append("arbiter")
                if self.phase.startswith("verify"):
                    text = json.dumps({"verdict": "RED", "findings": [_red_finding()]})
                else:
                    text = json.dumps({"verdict": "GREEN", "findings": []})
            else:
                return super().model(request)
            payload = {
                "model": "t",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": text},
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
            return HttpResponse(200, json.dumps(payload).encode(), Decimal(".01"))

    services = Witness()
    comments = _RoutedComments(services)
    comments.install()

    first = services.translate()
    assert first.verdict is Verdict.GREEN, first.verdict
    head = services.branch_head
    assert head is not None
    assert not any(row.get("status") == "open" for row in services.rows.values())

    services.phase = "verify_1"
    services.roles.clear()
    red1 = _runtime(services).doc_verify(
        VerifyWorkflowInput(43, GitSha(services.source), GitSha(head))
    )
    assert red1.verdict is Verdict.RED, red1.verdict
    open_after_first = [
        row for row in services.rows.values() if row.get("status") == "open"
    ]
    assert len(open_after_first) == 1, open_after_first

    # Critic may push; keep verify against the live translation head.
    head2 = services.branch_head or head
    services.phase = "verify_2"
    services.roles.clear()
    red2 = _runtime(services).doc_verify(
        VerifyWorkflowInput(43, GitSha(services.source), GitSha(head2))
    )
    assert red2.verdict is Verdict.RED, red2.verdict
    open_after_second = [
        row for row in services.rows.values() if row.get("status") == "open"
    ]
    assert len(open_after_second) == 1, (
        "second verify RED must leave exactly one open checkpoint; "
        f"got {len(open_after_second)}: {open_after_second!r}"
    )

    services.continuing = True
    services.phase = "continue_green"
    services.roles.clear()
    # Continue from source PR or translation PR must both resolve the same lineage.
    result = services.resume(continue_pr)
    assert result.verdict is Verdict.GREEN, result.verdict
    assert all(row.get("status") != "open" for row in services.rows.values())


def test_stale_unmarked_aktualny_is_updated_on_next_translation_link() -> None:
    """§7: rewritten zero-commit RED must stay findable so later links stay current."""

    class PerPrBackend:
        def __init__(self) -> None:
            self.comments_by_pr: dict[int, list[Comment]] = {
                42: [
                    Comment(
                        1,
                        True,
                        (
                            "Перевод этого PR: "
                            "https://github.com/ydb-platform/ydb/pull/40\n"
                            f"{LINK_MARKER}"
                        ),
                    ),
                    Comment(
                        2,
                        True,
                        "🔴 RED\n\nНужен /ydbdoc continue.\n" + QA_MARKER,
                    ),
                ]
            }
            self.pr_number = 44

        def list_comments(self, pr_number):
            return tuple(self.comments_by_pr.get(pr_number, ()))

        def create_comment(self, pr_number, body):
            comments = self.comments_by_pr.setdefault(pr_number, [])
            comments.append(Comment(pr_number * 100 + len(comments), True, body))

        def update_comment(self, pr_number, comment_id, body):
            comments = self.comments_by_pr[pr_number]
            index = next(i for i, comment in enumerate(comments) if comment.id == comment_id)
            comments[index] = Comment(comment_id, True, body)

        def find_pr(self, repository, branch, base):
            return self.pr_number

        def create_pr(self, context, sha):
            return self.pr_number

        def update_pr(self, pr_number, context, sha):
            return None

        def head(self, _branch):
            return GitSha("c" * 40)

        def checks(self, _commit_sha):
            return ()

        def commit(self, context, plan):
            return GitSha("c" * 40)

        def push(self, context, sha):
            return None

    backend = PerPrBackend()
    commit = GitSha("c" * 40)
    context = PublicationContext(
        "ydb-platform/ydb",
        "translation/pr-42",
        "main",
        "main",
        commit,
        branch_must_exist=True,
        expected_branch_head=commit,
    )
    publisher = GitPublicationAdapter(
        backend,
        lambda *_args: None,
        lambda *_args: None,
    )
    publisher.context = context
    publisher.noop = False
    publisher.pr_number = 44
    publisher._published_snapshot = object()  # noqa: SLF001 - production adapter field

    reporter = QAReporter(
        backend,
        publisher,
        lambda: ReportContext(
            GitSha("a" * 40),
            commit,
            Decimal("1.00"),
            source_pr_number=42,
        ),
    )
    red = CriticResult(
        Verdict.RED,
        (
            Finding(
                True,
                "Still red",
                "Fix it",
                "Translated",
                EN + "page.md",
                1,
            ),
        ),
    )
    review = QualityReviewResult(b"{}", None, b"{}", red, red, False, False, None)

    # Continue publishes QA on translation PR #44 and rewrites source comments.
    reporter.update_current_pr(
        mode=Mode.DOC_CONTINUE,
        pr_number=44,
        branch=context.branch,
        commit_sha=commit,
        review=review,
    )
    source_bodies = [c.body for c in backend.comments_by_pr[42]]
    assert any("/pull/44" in body for body in source_bodies)
    # After rewrite, every source comment that still mentions a translation PR
    # must remain marker-addressable for the next clean translate.
    for body in source_bodies:
        if "/pull/" in body:
            assert LINK_MARKER in body, (
                "neutralized/converted source comments must keep "
                f"{LINK_MARKER!r}; got {body!r}"
            )

    # Later clean translate creates PR #45 and must leave only current #45 links.
    backend.pr_number = 45
    publisher.pr_number = 45
    green_critic = CriticResult(Verdict.GREEN, ())
    green = QualityReviewResult(
        b"{}", None, b"{}", green_critic, green_critic, False, False, None
    )
    reporter.update_current_pr(
        mode=Mode.DOC_TRANSLATE,
        pr_number=45,
        branch=context.branch,
        commit_sha=commit,
        review=green,
    )
    source_bodies = [c.body for c in backend.comments_by_pr[42]]
    assert any("/pull/45" in body and LINK_MARKER in body for body in source_bodies), source_bodies
    stale = [body for body in source_bodies if "/pull/44" in body]
    assert not stale, (
        "stale unmarked/marked link to previous translation PR must not remain; "
        f"got {stale!r} among {source_bodies!r}"
    )


def test_doc_verify_reconciles_leftover_source_red_when_translation_pr_exists() -> None:
    """§5.2/§7: verify success must replace obsolete source-only current RED with link."""

    class Witness(ContinueServices):
        def __init__(self):
            super().__init__(names=(), stop="rename_red")
            self.phase = "translate"
            self.corrected = "items:\n- href: page.md\n  name: Corrected New\n"

        def _critic_files_for_chunk(self, drafts, body):
            files = super()._critic_files_for_chunk(drafts, body)
            if self.phase in {"continue", "verify"} and EN + TOC in files:
                files[EN + TOC] = self.corrected
            return files

        def model(self, request):

            body = json.loads(request.body)
            if body.get("tools"):
                return super().model(request)
            schema = request_schema(body)
            if schema is None:
                return super().model(request)
            props = schema["schema"]["properties"]
            if "translation_required" in props or "strings" in props:
                return super().model(request)
            if "findings" in props:
                self.roles.append("arbiter")
                if self.phase == "continue":
                    # Translation PR gets a successful semantic color; source PATCH
                    # will fail afterward so the job still aborts mid-report.
                    text = json.dumps({"verdict": "GREEN", "findings": []})
                elif self.phase == "verify":
                    text = json.dumps({"verdict": "YELLOW", "findings": [
                        {
                            "target_path": EN + TOC,
                            "searchable_snippet": "Corrected New",
                            "reason": "Minor wording polish remains.",
                            "expected_correction": "Use the glossary label.",
                        }
                    ]})
                else:
                    text = json.dumps({"verdict": "GREEN", "findings": []})
            else:
                return super().model(request)
            payload = {
                "model": "t",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": text},
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
            return HttpResponse(200, json.dumps(payload).encode(), Decimal(".01"))

    services = Witness()
    comments = _RoutedComments(services)
    comments.install()
    services.changes = [{"status": "modified", "filename": RU + TOC}]
    before_ru = b"items:\n- href: page.md\n  name: Old\n"
    after_ru = b"items:\n- href: page.md\n  name: New\n"
    already_en = b"items:\n- href: page.md\n  name: EN New\n"
    for tree in [services.files, *services.snapshots.values()]:
        tree[RU + TOC] = before_ru
        tree[EN + TOC] = already_en
        tree.pop(RU + "page.md", None)
        tree.pop(EN + "page.md", None)
    services.snapshots[services.source][RU + TOC] = after_ru
    services.snapshots[services.base][RU + TOC] = before_ru
    services.snapshots[services.base][EN + TOC] = already_en

    first = services.translate()
    assert first.verdict is Verdict.RED
    assert services.branch_head is None
    assert any(
        QA_MARKER in c["body"] and c["body"].startswith("🔴") for c in comments.by_pr[42]
    )

    # Continue creates the translation PR + QA, then source PATCH fails → job error,
    # leaving obsolete source RED while translation PR already exists.
    services.continuing = True
    services.phase = "continue"
    services.stop = None
    services.roles.clear()
    comments.fail_source_patch = True
    with pytest.raises(application.WorkflowError):
        services.resume()
    comments.fail_source_patch = False
    assert services.branch_head is not None
    assert comments.by_pr[43], "translation PR must already have QA despite source PATCH failure"
    assert any(
        QA_MARKER in c["body"] and c["body"].startswith("🔴") for c in comments.by_pr[42]
    ), "source RED must still be present after partial report failure"
    assert not any(LINK_MARKER in c["body"] for c in comments.by_pr[42]), (
        "source must not yet have a translation link after failed source reconcile"
    )

    # Close leftover continue checkpoint: human runs doc_verify on the translation PR.
    for row in services.rows.values():
        if row.get("status") == "open":
            row["status"] = "closed"
    head = services.branch_head
    assert head is not None
    services.continuing = False
    services.phase = "verify"
    services.roles.clear()
    verified = _runtime(services).doc_verify(
        VerifyWorkflowInput(43, GitSha(services.source), GitSha(head))
    )
    assert verified.verdict in {Verdict.GREEN, Verdict.YELLOW}, verified.verdict
    translation_qa = [c["body"] for c in comments.by_pr[43] if QA_MARKER in c["body"]]
    assert translation_qa and translation_qa[-1].startswith(("🟢", "🟡")), translation_qa
    assert any(LINK_MARKER in c["body"] and "/pull/43" in c["body"] for c in comments.by_pr[42]), (
        "doc_verify must publish §7 source link to the existing translation PR; "
        f"got {[c['body'] for c in comments.by_pr[42]]!r}"
    )
    assert not any(
        QA_MARKER in c["body"] and c["body"].startswith("🔴") for c in comments.by_pr[42]
    ), "obsolete source-only RED current QA must be replaced/removed by doc_verify"
