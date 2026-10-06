"""T07 witnesses at the installed HTTP and persistence boundaries."""

import base64
import json
from decimal import Decimal

import pytest
from _runtime_services import RuntimeServices, request_prompt, request_schema

from ydbdoc_review_ng.application import TranslateWorkflowInput, WorkflowError
from ydbdoc_review_ng.domain import GitSha
from ydbdoc_review_ng.models import HttpResponse
from ydbdoc_review_ng.runtime import create_runtime

PAGE = "ydb/docs/ru/core/page.md"
TARGET = "ydb/docs/en/core/page.md"
IMAGE = "ydb/docs/ru/core/new.png"
OLD_IMAGE = "ydb/docs/ru/core/old.png"


class ClassifierServices(RuntimeServices):
    def __init__(self, rows, answers):
        super().__init__()
        self.rows, self.answers = rows, answers
        self.source_pr_body = None
        self.requests = []
        self.before = {
            PAGE: b"# Before\n\nComplete old text.\n",
            OLD_IMAGE: b"\x89PNG\x00old",
            TARGET: b"# Previous target\n",
        }
        self.after = {
            PAGE: b"# After\n\nComplete new text.\n",
            TARGET: b"# Target\n",
            IMAGE: b"\x89PNG\x00new",
        }
        self.files.update(self.after)

    def github(self, method, path, payload):
        relative = path.removeprefix("/repos/ydb-platform/ydb")
        if relative == "/pulls/42/files?per_page=100":
            return self.rows
        if relative.startswith("/contents/"):
            name, ref = relative[10:].split("?ref=", 1)
            content = (self.before if ref == self.base else self.after).get(name)
            return (
                None
                if content is None
                else {
                    "type": "file",
                    "encoding": "base64",
                    "content": base64.b64encode(content).decode(),
                }
            )
        result = super().github(method, path, payload)
        if relative == "/pulls/42":
            result["changed_files"] = len(self.rows)
            if self.source_pr_body is not None:
                result["body"] = self.source_pr_body
        return result

    def model(self, request):

        body = json.loads(request.body)
        if body.get("tools"):
            roles = [
                message.get("role")
                for message in body.get("messages", [])
                if isinstance(message, dict)
            ]
            if roles == ["developer", "user"] or roles == ["user"]:
                self.requests.append(request)
                if self.answers and isinstance(self.answers[0], str):
                    data = json.loads(self.answers[0])
                    if "files" in data:
                        self.answers.pop(0)
                        self.semantic_responses = [data] + [
                            item
                            for item in self.semantic_responses
                            if "verdict" in item
                        ]
            return super().model(request)
        self.requests.append(request)
        answer = self.answers.pop(0)
        if type(answer) is int:
            return HttpResponse(answer, b"{}", None)
        return HttpResponse(
            200,
            json.dumps(
                {
                    "model": "test-model",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": answer,
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                }
            ).encode(),
            Decimal("0.01"),
        )


def answer(rows, *, required=False, direction=None):
    del rows
    return json.dumps(
        {
            "translation_required": required,
            "direction": direction,
            "reason": "Changes are already reflected in both locales.",
        }
    )


def runtime(services):
    return create_runtime(
        environment={
            "GITHUB_ACTOR": "m",
            "YDBDOC_ALLOWED_ACTORS": "m",
            "YANDEX_API_KEY": "offline",
            "YANDEX_FOLDER_ID": "offline",
        },
        ydb_executor=services,
        github_transport=services.github,
        model_transport=services.model,
    )


@pytest.mark.parametrize(
    "rows",
    [
        [{"filename": PAGE, "status": "modified"}],
        [{"filename": PAGE, "status": "modified"}, {"filename": TARGET, "status": "modified"}],
        [{"filename": IMAGE, "status": "renamed", "previous_filename": OLD_IMAGE, "changes": 2}],
    ],
)
def test_every_inventory_is_classified_once_and_noop_is_reported_idempotently(rows):
    services = ClassifierServices(rows, [answer(rows), answer(rows)])
    for _ in range(2):
        runtime(services).doc_translate(
            TranslateWorkflowInput(42, GitSha(services.source), Decimal(10))
        )
    assert len(services.requests) == 2
    assert len(services.source_comments) == 1
    assert "перевод не требуется" in services.source_comments[0]["body"].lower()
    assert not services.pr_exists and services.branch_head is None
    # §5.1 may DELETE the previous translation ref; §0 removes the trigger label.
    assert not any(
        method in {"POST", "PATCH"} and "/git/" in path for method, path in services.events
    )


def test_bilingual_inventory_skips_translate_even_when_classifier_says_required():
    """#55240/#55243: author already changed RU and EN; do not re-translate."""
    rows = [
        {"filename": PAGE, "status": "modified"},
        {"filename": TARGET, "status": "modified"},
    ]
    services = ClassifierServices(rows, [answer(rows, required=True, direction="ru_to_en")])
    runtime(services).doc_translate(
        TranslateWorkflowInput(42, GitSha(services.source), Decimal(10))
    )
    assert len(services.requests) == 1
    assert "translation_required" in request_schema(json.loads(services.requests[0].body))["schema"][
        "properties"
    ]
    assert "перевод не требуется" in services.source_comments[0]["body"].lower()
    assert not services.pr_exists and services.branch_head is None


def test_existing_translation_pr_skips_without_direction_call():
    """#55244: doc_translate on an already-translated PR must not nest another PR."""
    rows = [{"filename": PAGE, "status": "modified"}]
    services = ClassifierServices(rows, [])
    services.source_pr_body = (
        "<!-- ydbdoc-source-pr:53033 -->\n<!-- ydbdoc-source-sha:" + "a" * 40 + " -->\n"
    )
    runtime(services).doc_translate(
        TranslateWorkflowInput(42, GitSha(services.source), Decimal(10))
    )
    assert services.requests == []
    assert "перевод не требуется" in services.source_comments[0]["body"].lower()
    assert "уже является переводом" in services.source_comments[0]["body"].lower()
    assert not services.pr_exists and services.branch_head is None


def test_classifier_reads_complete_pinned_before_after_and_binary_metadata():
    rows = [
        {"filename": PAGE, "status": "modified"},
        {"filename": IMAGE, "status": "renamed", "previous_filename": OLD_IMAGE, "changes": 2},
    ]
    services = ClassifierServices(rows, [answer(rows)])
    runtime(services).doc_translate(
        TranslateWorkflowInput(42, GitSha(services.source), Decimal(10))
    )
    body = json.loads(services.requests[0].body)
    prompt = request_prompt(body)
    payload = json.loads(prompt.split("\nInventory: ", 1)[1].split("\n\n", 1)[0])
    files = {row["path"]: row for row in payload["files"]}
    assert len(payload["files"]) == 2
    assert files[PAGE]["operation"] == "modify"
    assert files[PAGE]["before"]["text"] == "# Before\n\nComplete old text.\n"
    assert files[PAGE]["after"]["text"] == "# After\n\nComplete new text.\n"
    assert files[IMAGE]["old_path"] == OLD_IMAGE and files[IMAGE]["new_path"] == IMAGE
    assert files[IMAGE]["before"] == {
        "kind": "binary",
        "size": 8,
        "sha256": "04542ebe95646d84b002474c94faaa8ccadb316de0e25082bdb11c75e123b400",
    }
    assert files[IMAGE]["after"]["kind"] == "binary"
    assert files[IMAGE]["mapping"]["ru_to_en"] == "ydb/docs/en/core/new.png"
    assert payload["before_sha"] == services.base and payload["after_sha"] == services.source
    assert request_schema(body)["schema"]["additionalProperties"] is False


@pytest.mark.parametrize("responses", [[503, 503], ["not JSON", "{}"], [503, "{}"]])
def test_classifier_exhaustion_retries_same_request_once_and_updates_one_comment(responses):
    rows = [{"filename": PAGE, "status": "modified"}]
    services = ClassifierServices(rows, list(responses) * 2)
    for _ in range(2):
        with pytest.raises(WorkflowError, match="inventory_classifier_failed"):
            runtime(services).doc_translate(
                TranslateWorkflowInput(42, GitSha(services.source), Decimal(10))
            )
    assert len(services.requests) == 4
    assert len({request.body for request in services.requests}) == 1
    assert len(services.source_comments) == 1
    assert "классифика" in services.source_comments[0]["body"].lower()
    assert not services.pr_exists and services.branch_head is None
    assert services.audit[-1]["status"] == "failed"


def test_classifier_malformed_response_retries_identically_and_recovers():
    rows = [{"filename": PAGE, "status": "modified"}]
    services = ClassifierServices(rows, ["{}", answer(rows)])
    runtime(services).doc_translate(
        TranslateWorkflowInput(42, GitSha(services.source), Decimal(10))
    )
    assert len(services.requests) == 2
    assert services.requests[0].body == services.requests[1].body
    assert "перевод не требуется" in services.source_comments[0]["body"].lower()


def test_classifier_includes_added_and_deleted_non_markdown_paths():
    rows = [
        {"filename": "README.txt", "status": "added"},
        {"filename": "shared/manual.pdf", "status": "removed"},
    ]
    services = ClassifierServices(rows, [answer(rows)])
    services.after["README.txt"] = b"Complete added text.\n"
    services.before["shared/manual.pdf"] = b"\x00binary-pdf"
    runtime(services).doc_translate(
        TranslateWorkflowInput(42, GitSha(services.source), Decimal(10))
    )
    prompt = request_prompt(json.loads(services.requests[0].body))
    files = json.loads(prompt.split("\nInventory: ", 1)[1])["files"]
    assert len(files) == 2
    assert files[0]["old_path"] is None and files[0]["before"] is None
    assert files[0]["after"]["text"] == "Complete added text.\n"
    assert files[1]["new_path"] is None and files[1]["after"] is None
    assert files[1]["before"]["kind"] == "binary"
    assert "binary-pdf" not in prompt


def test_ascii_pdf_is_binary_metadata_not_model_text():
    rows = [{"filename": "shared/manual.pdf", "status": "modified"}]
    services = ClassifierServices(rows, [answer(rows)])
    services.before["shared/manual.pdf"] = b"%PDF-1.4\nOld PDF body.\n"
    services.after["shared/manual.pdf"] = b"%PDF-1.4\nNew PDF body.\n"
    runtime(services).doc_translate(
        TranslateWorkflowInput(42, GitSha(services.source), Decimal(10))
    )
    prompt = request_prompt(json.loads(services.requests[0].body))
    row = json.loads(prompt.split("\nInventory: ", 1)[1])["files"][0]
    assert row["before"]["kind"] == row["after"]["kind"] == "binary"
    assert "%PDF" not in prompt


@pytest.mark.parametrize("version", ["before", "after"])
def test_missing_required_inventory_version_fails_before_classifier(version):
    rows = [{"filename": PAGE, "status": "modified"}]
    services = ClassifierServices(rows, [answer(rows)])
    getattr(services, version).pop(PAGE)
    with pytest.raises(WorkflowError, match="source_inventory_version_missing"):
        runtime(services).doc_translate(
            TranslateWorkflowInput(42, GitSha(services.source), Decimal(10))
        )
    assert services.requests == [] and not services.pr_exists


def test_required_resource_is_classified_and_mirrored_without_markdown() -> None:
    rows = [{"filename": IMAGE, "status": "renamed", "previous_filename": OLD_IMAGE, "changes": 0}]
    services = ClassifierServices(
        rows,
        [
            answer(rows, required=True, direction="ru_to_en"),
            json.dumps({"files": {}}),
            json.dumps({"verdict": "GREEN", "findings": []}),
        ],
    )
    result = runtime(services).doc_translate(
        TranslateWorkflowInput(42, GitSha(services.source), Decimal(10))
    )
    assert len(services.requests) >= 1
    assert result.verdict.value == "GREEN"
    assert services.pr_exists or services.branch_head is not None


@pytest.mark.parametrize("second_page", ["complete", "missing", "duplicate"])
def test_complete_inventory_paginates_and_rejects_inconsistent_pages_before_models(second_page):
    rows = [
        {"filename": f"shared/file-{index:03}.txt", "status": "modified"} for index in range(101)
    ]

    class Services(ClassifierServices):
        def github(self, method, path, payload):
            if path.endswith("/pulls/42/files?per_page=100"):
                return self.rows[:100]
            if path.endswith("/pulls/42/files?per_page=100&page=2"):
                return (
                    []
                    if second_page == "missing"
                    else (self.rows[:1] if second_page == "duplicate" else self.rows[100:])
                )
            return super().github(method, path, payload)

    services = Services(rows, [answer(rows)])
    services.before.update({row["filename"]: b"Before text" for row in rows})
    services.after.update({row["filename"]: b"After text" for row in rows})
    request = TranslateWorkflowInput(42, GitSha(services.source), Decimal(10))
    if second_page != "complete":
        with pytest.raises(WorkflowError):
            runtime(services).doc_translate(request)
        assert services.requests == [] and services.source_comments == []
        return
    runtime(services).doc_translate(request)
    prompt = request_prompt(json.loads(services.requests[0].body))
    files = json.loads(prompt.split("\nInventory: ", 1)[1])["files"]
    assert len(files) == 101 and files[-1]["path"] == "shared/file-100.txt"
    assert files[-1]["before"]["text"] == "Before text"
    assert files[-1]["after"]["text"] == "After text"


def test_budget_rejection_precedes_classifier():
    from ydbdoc_review_ng.persistence import DailyBudgetExceeded

    services = ClassifierServices([{"filename": PAGE, "status": "modified"}], [])
    with pytest.raises(DailyBudgetExceeded):
        runtime(services).doc_translate(
            TranslateWorkflowInput(42, GitSha(services.source), Decimal(0))
        )
    assert services.requests == [] and services.source_comments == []


def test_selected_direction_translates_in_same_job_without_another_classifier():
    services = RuntimeServices()
    runtime(services).doc_translate(
        TranslateWorkflowInput(42, GitSha(services.source), Decimal(10))
    )
    assert services.pr_exists
    assert len([event for event in services.events if event[0] == "CLASSIFIER"]) == 1
    assert services.files[TARGET] == b"# Translated\n"


@pytest.mark.parametrize("mixed", [False, True])
def test_verify_uses_frozen_target_inventory_without_classifier(mixed):
    from ydbdoc_review_ng.application import VerifyWorkflowInput

    class Services(RuntimeServices):
        def github(self, method, path, payload):
            value = super().github(method, path, payload)
            if mixed and path.endswith("/pulls/42/files?per_page=100"):
                return value + [{"filename": TARGET, "status": "modified"}]
            if mixed and path.endswith("/pulls/42"):
                value["changed_files"] = 2
            return value

    services = Services()
    services.branch_head, services.pr_exists = services.translated, True
    runtime(services).doc_verify(
        VerifyWorkflowInput(43, GitSha(services.source), GitSha(services.translated))
    )
    assert not any(event[0] == "CLASSIFIER" for event in services.events)
    assert any("/pulls/43/files?" in path for method, path in services.events if method == "GET")


def test_verify_rejects_target_movement_while_freezing_inventory():
    from ydbdoc_review_ng.application import VerifyWorkflowInput

    class Services(RuntimeServices):
        def github(self, method, path, payload):
            value = super().github(method, path, payload)
            if path.endswith("/pulls/43/files?per_page=100"):
                self.translated = "f" * 40
            return value

    services = Services()
    services.branch_head, services.pr_exists = services.translated, True
    with pytest.raises(WorkflowError, match="verification_head_mismatch"):
        runtime(services).doc_verify(
            VerifyWorkflowInput(43, GitSha(services.source), GitSha(services.translated))
        )
    assert not any(event[0] in {"CLASSIFIER", "MODEL"} for event in services.events)


def test_verify_rejects_duplicate_target_inventory_before_models():
    from ydbdoc_review_ng.application import VerifyWorkflowInput

    class Services(RuntimeServices):
        def github(self, method, path, payload):
            value = super().github(method, path, payload)
            if path.endswith("/pulls/43"):
                value["changed_files"] = 2
            if path.endswith("/pulls/43/files?per_page=100"):
                return value + value
            return value

    services = Services()
    services.branch_head, services.pr_exists = services.translated, True
    with pytest.raises(WorkflowError):
        runtime(services).doc_verify(
            VerifyWorkflowInput(43, GitSha(services.source), GitSha(services.translated))
        )
    assert not any(
        event[0] in {"CLASSIFIER", "MODEL", "POST", "PATCH"} for event in services.events
    )
