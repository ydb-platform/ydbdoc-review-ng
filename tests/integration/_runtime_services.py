"""Ordinary in-memory remote-service boundaries for installed runtime smoke."""

import base64
import json
import re
from decimal import Decimal


def raw_translation_source(prompt):
    marker = "<AUTHORITATIVE_SOURCE_"
    if marker in prompt:
        start = prompt.index("\n", prompt.index(marker)) + 1
        end = prompt.index("</AUTHORITATIVE_SOURCE_", start)
        return prompt[start:end]
    source = prompt.split("\n\n", 1)[1]
    for marker in ("\n\nOperator context:\n", "\n\nImportant correction:\n"):
        source = source.split(marker, 1)[0]
    return source


def raw_repair_context(prompt, tag):
    return prompt.split(f"<{tag}>\n", 1)[1].split(f"</{tag}>", 1)[0]


def rewrite_markdown(source, word="Translated", *, preserve_suffix=False):
    def heading(match):
        text = match.group(2)
        if preserve_suffix and text.startswith("Source"):
            text = word + text.removeprefix("Source")
        else:
            text = word
        return match.group(1) + text

    translated = re.sub(r"(?m)^( {0,3}#{1,6}[ \t]+)([^\r\n]+)", heading, source)
    return re.sub(r"(?m)^Source[^\r\n]*$", word, translated)


def translated_markdown(prompt, word="Translated", *, preserve_suffix=False):
    return rewrite_markdown(raw_translation_source(prompt), word, preserve_suffix=preserve_suffix)


class RuntimeServices:
    """Remote service responses, not a substitute workflow or content adapter."""

    source = "a" * 40
    base = "b" * 40
    translated = "e" * 40

    def __init__(self):
        self.events = []
        self.audit = []
        self.comments = []
        self.branch_head = None
        self.pr_exists = False
        self.files = {
            "ydb/docs/ru/core/page.md": b"# Source\n",
            "ydb/docs/en/core/page.md": b"# Old\n",
        }
        self.blob = None

    def execute(self, statement, parameters):
        self.audit.append(dict(parameters))
        if "a.target_path AS target_path" in statement:
            job_ids = {
                row["job_id"]
                for row in self.audit
                if row.get("source_sha") == parameters["source_sha"] and "mode" in row
            }
            return [
                {
                    "target_path": row.get("target_path"),
                    "role": row["role"],
                    "cost_rub": row.get("cost_rub"),
                }
                for row in self.audit
                if "attempt_id" in row and row.get("job_id") in job_ids
            ]
        if "SUM" in statement:
            return [{"total_cost_rub": Decimal(0)}]
        return []

    def github(self, method, path, payload):
        self.events.append((method, path))
        path = path.removeprefix("/repos/ydb-platform/ydb")
        if path == "/user":
            return {"id": 42, "type": "User", "login": "pat-publisher"}
        if path == "/pulls/42":
            return {
                "merged": False,
                "head": {
                    "sha": self.source,
                    "ref": "source",
                    "repo": {"full_name": "ydb-platform/ydb"},
                },
                "base": {"ref": "main", "repo": {"full_name": "ydb-platform/ydb"}},
                "changed_files": 1,
            }
        if path == "/pulls/43":
            return {
                "head": {
                    "sha": self.translated,
                    "ref": "translation/pr-42",
                    "repo": {"full_name": "ydb-platform/ydb"},
                },
                "base": {"ref": "main", "repo": {"full_name": "ydb-platform/ydb"}},
                "body": "<!-- ydbdoc-source-pr:42 -->\n<!-- ydbdoc-source-sha:"
                + self.source
                + " -->",
            }
        if path == "/pulls/42/files?per_page=100":
            return [{"status": "modified", "filename": "ydb/docs/ru/core/page.md"}]
        if path == "/git/ref/heads/main":
            return {"object": {"sha": self.base}}
        if path.startswith("/git/ref/heads/translation"):
            return None if self.branch_head is None else {"object": {"sha": self.branch_head}}
        if path.startswith("/contents/"):
            name = path[10:].split("?")[0]
            content = self.files.get(name)
            return (
                None
                if content is None
                else {
                    "type": "file",
                    "encoding": "base64",
                    "content": base64.b64encode(content).decode(),
                }
            )
        if method == "GET" and path.startswith("/git/commits/"):
            return {"tree": {"sha": "c" * 40}}
        if path == "/git/blobs":
            self.blob = base64.b64decode(payload["content"])
            return {"sha": "c" * 40}
        if path == "/git/trees":
            return {"sha": "d" * 40}
        if path == "/git/commits":
            return {"sha": self.translated}
        if path == "/git/refs" or path.startswith("/git/refs/heads/"):
            self.branch_head = payload["sha"]
            self.files["ydb/docs/en/core/page.md"] = self.blob
            return {}
        if path.startswith("/commits/") and "/check-runs?" in path:
            return {
                "total_count": 2,
                "check_runs": [
                    {
                        "name": name,
                        "head_sha": self.translated,
                        "status": "completed",
                        "conclusion": "success",
                    }
                    for name in ("doc_verify", "build-docs")
                ],
            }
        if path.startswith("/pulls?"):
            return [{"number": 43}] if self.pr_exists else []
        if path == "/pulls" and method == "POST":
            assert "ydbdoc-source-pr:42" in payload["body"]
            self.pr_exists = True
            return {"number": 43}
        if path == "/issues/43/comments?per_page=100":
            return self.comments
        if path == "/issues/43/comments":
            self.comments.append(
                {
                    "id": 7,
                    "user": {"id": 42, "type": "User", "login": "pat-publisher"},
                    "body": payload["body"],
                }
            )
            return {"id": 7}
        if path == "/issues/comments/7":
            self.comments[0]["body"] = payload["body"]
            return {}
        raise AssertionError((method, path))

    def model(self, request):
        from ydbdoc_review_ng.models import HttpResponse

        body = json.loads(request.body)
        schema = body.get("jsonSchema")
        if schema is None:
            prompt = body["messages"][-1]["text"]
            if prompt.startswith("Repair"):
                self.events.append(("REPAIR", "markdown"))
                text = rewrite_markdown(raw_repair_context(prompt, "current-target"), "Corrected")
            else:
                self.events.append(("MODEL", ("raw_markdown",)))
                text = translated_markdown(prompt)
        else:
            properties = schema["schema"]["properties"]
            self.events.append(("MODEL", tuple(properties)))
            if "verdict" in properties:
                values = {"verdict": "GREEN", "findings": []}
                if "corrected_markdown" in properties:
                    prompt = body["messages"][-1]["text"]
                    values["corrected_markdown"] = raw_repair_context(
                        prompt, "final-target"
                    )
            else:
                values = {key: "Translated" for key in properties}
            text = json.dumps(values)
        return HttpResponse(
            200,
            json.dumps(
                {
                    "result": {
                        "alternatives": [
                            {
                                "status": "ALTERNATIVE_STATUS_FINAL",
                                "message": {"role": "assistant", "text": text},
                            }
                        ],
                        "usage": {"inputTextTokens": "10", "completionTokens": "5"},
                    }
                }
            ).encode(),
            Decimal("0.01"),
        )


class InstalledContinueServices(RuntimeServices):
    """The same HTTP/YDB boundaries with a review checkpoint for installed smoke."""

    def __init__(self):
        super().__init__()
        self.jobs = {}
        self.checkpoints = {}
        self.stop_review = False
        self.continuing = False

    def execute(self, statement, parameters):
        if "a.target_path AS target_path" in statement:
            return super().execute(statement, parameters)
        if "/jobs`" in statement:
            if "SELECT" in statement:
                row = self.jobs.get(parameters["job_id"])
                return [] if row is None else [row]
            self.jobs.setdefault(parameters["job_id"], {}).update(parameters)
        if "/continuations`" in statement:
            if "UPSERT" in statement:
                self.checkpoints[parameters["continuation_id"]] = dict(parameters)
            elif "UPDATE" in statement:
                row = self.checkpoints[parameters["continuation_id"]]
                if "SET consumed_by_job_id" in statement:
                    assert all(
                        row.get(key) == value
                        for key, value in parameters.items()
                        if key != "new_consumed_by_job_id"
                    )
                    row["consumed_by_job_id"] = parameters["new_consumed_by_job_id"]
                elif "SET status = 'open'" in statement:
                    assert all(row.get(key) == value for key, value in parameters.items())
                    row["status"] = "open"
                else:
                    row["status"] = "closed"
            elif "continuation_id" in parameters:
                row = self.checkpoints.get(parameters["continuation_id"])
                return [] if row is None else [row]
            else:
                return list(self.checkpoints.values())
            return []
        return super().execute(statement, parameters)

    def github(self, method, path, payload):
        if path.endswith("/events?per_page=100"):
            return [
                {
                    "id": 100,
                    "event": "labeled",
                    "label": {"name": "doc_continue"},
                    "actor": {"login": "maintainer"},
                    "created_at": "2026-09-21T11:00:00Z",
                }
            ]
        response = super().github(method, path, payload)
        if path.endswith("/pulls/42"):
            response.update(number=42, body="")
        if path.endswith("/pulls/43"):
            response["number"] = 43
        if self.continuing and method == "GET" and path.endswith("/comments?per_page=100"):
            return [
                {
                    "id": 99,
                    "user": {"login": "maintainer"},
                    "created_at": "2026-09-21T10:00:00Z",
                    "updated_at": "2026-09-21T10:00:00Z",
                    "body": "/ydbdoc continue\nKeep the authoritative source meaning.\n",
                }
            ] + [
                {
                    **comment,
                    "created_at": "2026-09-21T12:00:00Z",
                    "updated_at": "2026-09-21T12:00:00Z",
                }
                for comment in response
            ]
        return response

    def model(self, request):
        from ydbdoc_review_ng.models import HttpResponse

        response = super().model(request)
        schema = json.loads(request.body).get("jsonSchema")
        if self.stop_review and schema is not None and "verdict" in schema["schema"]["properties"]:
            properties = schema["schema"]["properties"]
            values = {
                "verdict": "RED",
                "findings": [
                    {
                        "repairable": False,
                        "reason": "Meaning requires operator context.",
                        "expected_correction": "Confirm the intended source meaning.",
                        "searchable_snippet": "Translated",
                        "target_path": "ydb/docs/en/core/page.md",
                        "target_line": 1,
                    }
                ],
            }
            if "corrected_markdown" in properties:
                prompt = json.loads(request.body)["messages"][-1]["text"]
                values["corrected_markdown"] = raw_repair_context(prompt, "final-target")
            body = json.loads(response.body)
            body["result"]["alternatives"][0]["message"]["text"] = json.dumps(values)
            return HttpResponse(200, json.dumps(body).encode(), Decimal("0.01"))
        return response
