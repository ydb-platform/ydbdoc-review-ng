"""Production-path witnesses for tip residuals after A/B/C at 4f4a393.

Canon: REQUIREMENTS_RU.md §1.2/§3.2/§4.2/§5.2/§7.

Bug 1: intentional already-absent TOC delete must survive doc_verify scope
restore; must not re-enter as required null → force RED.

Bug 2: after multi-continue creates a translation PR and finishes GREEN, the
obsolete source-only RED QA (zero-commit) must not remain current on source PR.
"""

from __future__ import annotations

import json
from decimal import Decimal

from _runtime_services import raw_repair_context, request_prompt, request_schema
from test_checkpoint_capture import CaptureServices, ENV
from test_continue_translation import ContinueServices

from ydbdoc_review_ng.application import VerifyWorkflowInput
from ydbdoc_review_ng.domain import GitSha
from ydbdoc_review_ng.models import HttpResponse
from ydbdoc_review_ng.quality import Verdict
from ydbdoc_review_ng.runtime import create_runtime

RU = "ydb/docs/ru/core/"
EN = "ydb/docs/en/core/"
TOC = "toc.yaml"
QA_MARKER = "<!-- ydbdoc-current-qa -->"
LINK_MARKER = "<!-- ydbdoc-translation-pr -->"


def _runtime(services):
    return create_runtime(
        environment=ENV,
        ydb_executor=services,
        github_transport=services.github,
        model_transport=services.model,
    )


class _RoutedComments:
    """Per-PR comment store with unique ids and PATCH-by-id (realistic §7)."""

    def __init__(self, services: CaptureServices):
        self._services = services
        self._next_id = 900
        self.by_pr: dict[int, list[dict]] = {42: [], 43: []}

    def install(self) -> None:
        services = self._services
        original = services.github

        def github(method, path, payload):
            relative = path.removeprefix("/repos/ydb-platform/ydb")
            if relative.endswith("/comments?per_page=100"):
                pr = 43 if "/issues/43/" in relative else 42 if "/issues/42/" in relative else None
                if pr is not None:
                    # ContinueServices prepends operator commands on source PR.
                    if (
                        getattr(services, "continuing", False)
                        and pr == 42
                        and hasattr(services, "commands")
                    ):
                        return list(services.commands) + list(self.by_pr[pr])
                    return list(self.by_pr[pr])
            if method == "POST" and relative in {"/issues/42/comments", "/issues/43/comments"}:
                pr = 42 if relative.startswith("/issues/42/") else 43
                self._next_id += 1
                comment = {
                    "id": self._next_id,
                    "user": {"id": 42, "type": "User", "login": "pat-publisher"},
                    "body": payload["body"],
                    "created_at": "2026-09-21T12:00:00Z",
                    "updated_at": "2026-09-21T12:00:00Z",
                }
                self.by_pr[pr].append(comment)
                # Keep legacy buckets in sync for older assertions.
                if pr == 42:
                    services.source_comments = list(self.by_pr[42])
                else:
                    services.comments = list(self.by_pr[43])
                return {"id": comment["id"]}
            if method == "PATCH" and relative.startswith("/issues/comments/"):
                comment_id = int(relative.rsplit("/", 1)[-1])
                for pr, rows in self.by_pr.items():
                    for row in rows:
                        if row["id"] == comment_id:
                            row["body"] = payload["body"]
                            row["updated_at"] = "2026-09-21T13:00:00Z"
                            if pr == 42:
                                services.source_comments = list(self.by_pr[42])
                            else:
                                services.comments = list(self.by_pr[43])
                            return {}
                raise AssertionError(f"unknown comment id {comment_id}")
            return original(method, path, payload)

        services.github = github


def test_verify_already_absent_intentional_toc_delete_keeps_green() -> None:
    """§1.2/§5.2: verify must not re-require an already-absent deleted target TOC."""

    class Witness(CaptureServices):
        def __init__(self):
            super().__init__(names=("page",), stop=None)
            self.critic_targets: list[dict] = []

        def model(self, request):
            body = json.loads(request.body)
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
                self.critic_targets.append(dict(files))
                # Echo current translation-PR files; do not invent a TOC.
                text = json.dumps({"files": files})
            elif "findings" in props:
                self.roles.append("arbiter")
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
    # Source deletes TOC + edits page; EN TOC never existed on base.
    services.changes = [
        {"status": "removed", "filename": RU + TOC},
        {"status": "modified", "filename": RU + "page.md"},
    ]
    for tree in [services.files, *services.snapshots.values()]:
        tree[RU + TOC] = "items:\n- name: До\n  href: page.md\n".encode()
        tree.pop(EN + TOC, None)
        tree[RU + "page.md"] = "# До правки\n".encode()
        tree[EN + "page.md"] = b"# Old EN\n"
    services.snapshots[services.source] = {
        k: v for k, v in services.snapshots[services.source].items() if k != RU + TOC
    }
    services.snapshots[services.source][RU + "page.md"] = "# После правки\n".encode()

    translated = services.translate()
    assert translated.verdict is Verdict.GREEN, translated.verdict
    assert services.files.get(EN + TOC) is None
    assert EN + TOC not in (services.snapshots.get(services.branch_head) or {})
    assert not any(row.get("status") == "open" for row in services.rows.values())

    services.roles.clear()
    services.critic_targets.clear()
    head = services.branch_head
    assert head is not None
    verified = _runtime(services).doc_verify(
        VerifyWorkflowInput(43, GitSha(services.source), GitSha(head))
    )
    assert verified.verdict is Verdict.GREEN, (
        f"verify must keep arbiter GREEN for already-absent intentional TOC delete; "
        f"got {verified.verdict}"
    )
    assert not any(row.get("status") == "open" for row in services.rows.values()), (
        "false RED must not open a review checkpoint"
    )
    for files in services.critic_targets:
        assert EN + TOC not in files or files.get(EN + TOC) is not None, (
            "intentionally absent TOC must not be a required null in critic map"
        )
    qa = [c["body"] for c in comments.by_pr[43] if QA_MARKER in c.get("body", "")]
    assert qa and qa[-1].startswith("🟢"), qa
    assert EN + TOC not in qa[-1]


def test_verify_already_absent_intentional_toc_delete_yellow_no_checkpoint() -> None:
    """§4.2/§5.2: YELLOW on the article must not be force-upgraded by absent TOC."""

    class Witness(CaptureServices):
        def __init__(self):
            super().__init__(names=("page",), stop=None)
            self.phase = "translate"

        def model(self, request):
            body = json.loads(request.body)
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
                if self.phase == "verify":
                    files = json.loads(
                        raw_repair_context(request_prompt(body), "translation-pr-files")
                    )
                    page = files.get(EN + "page.md") or ""
                    snippet = page.splitlines()[0] if page else "Translated"
                    finding = {
                        "target_path": EN + "page.md",
                        "searchable_snippet": snippet,
                        "reason": "Minor terminology issue.",
                        "expected_correction": "Align the term with the glossary.",
                    }
                    text = json.dumps({"verdict": "YELLOW", "findings": [finding]})
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
    services.changes = [
        {"status": "removed", "filename": RU + TOC},
        {"status": "modified", "filename": RU + "page.md"},
    ]
    for tree in [services.files, *services.snapshots.values()]:
        tree[RU + TOC] = "items:\n- name: До\n  href: page.md\n".encode()
        tree.pop(EN + TOC, None)
        tree[RU + "page.md"] = "# До\n".encode()
        tree[EN + "page.md"] = b"# Old\n"
    services.snapshots[services.source] = {
        k: v for k, v in services.snapshots[services.source].items() if k != RU + TOC
    }
    services.snapshots[services.source][RU + "page.md"] = "# После\n".encode()

    assert services.translate().verdict is Verdict.GREEN
    head = services.branch_head
    assert head is not None
    services.phase = "verify"
    services.roles.clear()
    verified = _runtime(services).doc_verify(
        VerifyWorkflowInput(43, GitSha(services.source), GitSha(head))
    )
    assert verified.verdict is Verdict.YELLOW, verified.verdict
    assert not any(row.get("status") == "open" for row in services.rows.values())


def test_multi_continue_removes_stale_source_zero_commit_red_qa() -> None:
    """§7: after translation PR exists, obsolete source-only RED QA must not linger."""

    class Witness(ContinueServices):
        def __init__(self):
            # TOC-only inventory; target already holds translated delta → zero commits.
            super().__init__(names=(), stop="rename_red")
            self.phase = "translate"
            self.corrected = "items:\n- href: page.md\n  name: Corrected New\n"

        def model(self, request):
            body = json.loads(request.body)
            schema = request_schema(body)
            if schema is None:
                return super().model(request)
            props = schema["schema"]["properties"]
            if "translation_required" in props:
                return super().model(request)
            if "strings" in props:
                return super().model(request)
            if "files" in props:
                self.roles.append("critic")
                files = json.loads(
                    raw_repair_context(request_prompt(body), "translation-pr-files")
                )
                if self.phase == "continue_red":
                    files[EN + TOC] = self.corrected
                text = json.dumps({"files": files})
            elif "findings" in props:
                self.roles.append("arbiter")
                if self.phase == "continue_red":
                    text = json.dumps(
                        {
                            "verdict": "RED",
                            "findings": [
                                {
                                    "target_path": EN + TOC,
                                    "searchable_snippet": "Corrected New",
                                    "reason": "Wording still needs an editorial pass.",
                                    "expected_correction": "Use the approved navigation label.",
                                }
                            ],
                        }
                    )
                else:
                    # translate + final continue: arbiter GREEN (zero-commit forces RED
                    # only on the first translate publish path).
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
    assert services.commits == 0
    source_qa = [c for c in comments.by_pr[42] if QA_MARKER in c["body"]]
    assert len(source_qa) == 1 and source_qa[0]["body"].startswith("🔴"), source_qa
    assert any(row.get("status") == "open" for row in services.rows.values())

    services.continuing = True
    services.phase = "continue_red"
    services.stop = None
    services.roles.clear()
    mid = services.resume()
    assert mid.verdict is Verdict.RED
    assert services.branch_head is not None
    assert services.commits >= 1
    assert comments.by_pr[43], "translation PR must receive QA"
    assert any(LINK_MARKER in c["body"] for c in comments.by_pr[42])
    # §7: as soon as the translation PR exists and receives QA, obsolete
    # source-only current-QA RED must already be gone.
    assert not any(
        QA_MARKER in c["body"] and c["body"].startswith("🔴") for c in comments.by_pr[42]
    ), "creating the translation PR must clear source-only current RED QA"

    services.phase = "continue_green"
    services.roles.clear()
    final = services.resume()
    assert final.verdict is Verdict.GREEN, final.verdict
    assert all(row.get("status") != "open" for row in services.rows.values())
    translation_qa = [c["body"] for c in comments.by_pr[43] if QA_MARKER in c["body"]]
    assert translation_qa and translation_qa[-1].startswith("🟢"), translation_qa
    stale = [
        c["body"]
        for c in comments.by_pr[42]
        if QA_MARKER in c["body"] and c["body"].startswith("🔴")
    ]
    assert not stale, (
        "source PR must not keep obsolete zero-commit RED as current QA; "
        f"got {stale!r}"
    )
    assert any(LINK_MARKER in c["body"] for c in comments.by_pr[42])
    assert not any(QA_MARKER in c["body"] for c in comments.by_pr[42]), (
        "source PR must not retain a current-QA marker after translation PR exists"
    )
