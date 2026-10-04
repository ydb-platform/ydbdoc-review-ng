"""Ordinary in-memory remote-service boundaries for installed runtime smoke."""

import base64
import json
import re
from decimal import Decimal

from tests.support.tool_critic_scripts import finish_only, patch_read_finish


def raw_translation_source(prompt):
    if "\nSegments: " in prompt:
        encoded = prompt.split("\nSegments: ", 1)[1].split("\n\n", 1)[0]
        return "".join(json.loads(encoded).values())
    if "=== SOURCE ===\n" in prompt:
        start = prompt.index("=== SOURCE ===\n") + len("=== SOURCE ===\n")
        end = prompt.index("\n=== END SOURCE ===", start)
        return prompt[start:end]
    marker = "<AUTHORITATIVE_SOURCE_"
    if marker in prompt:
        start = prompt.index("\n", prompt.index(marker)) + 1
        end = prompt.index("</AUTHORITATIVE_SOURCE_", start)
        return prompt[start:end]
    source = prompt.split("\n\n", 1)[1]
    for marker in ("\n\nOperator context:\n", "\n\nImportant correction:\n"):
        source = source.split(marker, 1)[0]
    return source


def translation_segments(prompt, word="Translated", *, preserve_suffix=False):
    encoded = prompt.split("\nSegments: ", 1)[1].split("\n\n", 1)[0]
    return {
        key: rewrite_markdown(value, word, preserve_suffix=preserve_suffix)
        for key, value in json.loads(encoded).items()
    }


def request_prompt(body):
    """Read either native Yandex or OpenAI-compatible message payloads."""
    message = body["messages"][-1]
    return message.get("text", message.get("content", ""))


def request_schema(body):
    """Read structured-output schemas from native and OpenAI payloads."""
    if body.get("jsonSchema") is not None:
        return body["jsonSchema"]
    response_format = body.get("response_format")
    if response_format is None:
        return None
    return {"schema": response_format["json_schema"]["schema"]}


def toc_string_translations(prompt, schema):
    """Closed JSON ID-map for TOC visible strings (§3 DeepSeek path)."""
    string_ids = list(schema["properties"]["strings"]["properties"])
    by_id = {}
    if "\nInput:\n" in prompt:
        raw = prompt.split("\nInput:\n", 1)[1]
        raw = raw.split("\n\nImportant", 1)[0].split("\n<PREVIOUS_RESPONSE>", 1)[0]
        try:
            payload = json.loads(raw)
            by_id = {
                item["id"]: item["text"]
                for item in payload.get("strings", ())
                if type(item) is dict and type(item.get("id")) is str
            }
        except (TypeError, ValueError, json.JSONDecodeError):
            by_id = {}
    return {
        "strings": {
            key: ("EN " + by_id[key]) if key in by_id else f"Translated {key}"
            for key in string_ids
        }
    }


def classification_response(prompt, *, direction="ru_to_en", decisions=None):
    """Direction-only model payload. Python mirrors Git ops separately."""
    inventory = json.loads(prompt.split("\nInventory: ", 1)[1].split("\n\n", 1)[0])
    actions = []
    chosen = direction
    for item in inventory["files"]:
        key = item["path"].split("/core/", 1)[-1]
        verdict = None if decisions is None else decisions.get(key)
        action = "toc_delta" if item["path"].endswith(".yaml") else "page"
        if verdict == "complete_pair":
            action = "none"
        elif verdict in {"ru_to_en", "en_to_ru"}:
            chosen = verdict
        target_locale = "en" if chosen in {"ru_to_en", None} else "ru"
        if item["path"].startswith(f"ydb/docs/{target_locale}/"):
            action = "none"
        actions.append(action)
    required = any(action != "none" for action in actions)
    return {
        "translation_required": required,
        "direction": chosen if required else None,
        "reason": "Source PR classification.",
    }


def seed_inventory_preimages(rows, before, after):
    """Give legacy scenario fixtures explicit source versions for their PR diff."""
    for row in rows:
        if row["status"] != "added":
            old_path = row.get("previous_filename", row["filename"])
            before.setdefault(old_path, after.get(row["filename"], b"# Removed source preimage\n"))


def replace_response_text(response_body, text):
    """Replace assistant text in either provider response envelope."""
    data = json.loads(response_body)
    if "choices" in data:
        data["choices"][0]["message"]["content"] = text
    else:
        data["result"]["alternatives"][0]["message"]["text"] = text
    return json.dumps(data).encode()


def response_text(response_body):
    data = json.loads(response_body)
    if "choices" in data:
        return data["choices"][0]["message"]["content"]
    return data["result"]["alternatives"][0]["message"]["text"]


def raw_repair_context(prompt, tag):
    return prompt.split(f"<{tag}>\n", 1)[1].split(f"</{tag}>", 1)[0]


def raw_translation_draft(prompt):
    marker = "<TRANSLATION_DRAFT_"
    start = prompt.index(">\n", prompt.index(marker)) + 2
    end = prompt.index("</TRANSLATION_DRAFT_", start)
    return prompt[start:end]


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
        self.source_comments = []
        self.branch_head = None
        self.pr_exists = False
        self.jobs = {}
        self.checkpoints = {}
        self.files = {
            "ydb/docs/ru/core/page.md": b"# Source\n",
            "ydb/docs/en/core/page.md": b"# Old\n",
        }
        self.blob = None
        self.blobs: dict[str, bytes] = {}
        self.tree: list[dict] = []
        # Thin pipeline: no tool-critic. Queue is arbiter-only (legacy tests may
        # still prepend {"files": ...}; model() skips those for verdict calls).
        self.semantic_responses = [
            {"verdict": "GREEN", "findings": []},
        ]
        self._tool_queue: list = []
        self._commit_n = 0

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
        if "/jobs`" in statement:
            if "SELECT" in statement:
                row = self.jobs.get(parameters["job_id"])
                return [] if row is None else [row]
            self.jobs.setdefault(parameters["job_id"], {}).update(parameters)
            return []
        if "/continuations`" in statement:
            if "UPSERT" in statement:
                self.checkpoints[parameters["continuation_id"]] = dict(parameters)
            elif "UPDATE" in statement:
                row = self.checkpoints[parameters["continuation_id"]]
                if "SET consumed_by_job_id" in statement:
                    row["consumed_by_job_id"] = parameters["new_consumed_by_job_id"]
                elif "SET status = 'open'" in statement:
                    row["status"] = "open"
                else:
                    row["status"] = "closed"
            elif "continuation_id" in parameters:
                row = self.checkpoints.get(parameters["continuation_id"])
                return [] if row is None else [row]
            else:
                return list(self.checkpoints.values())
            return []
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
                "base": {
                    "ref": "main",
                    "sha": self.base,
                    "repo": {"full_name": "ydb-platform/ydb"},
                },
                "changed_files": 1,
            }
        if path == "/pulls/43":
            return {
                "changed_files": len([name for name in self.files if name.startswith("ydb/docs/en/")]),
                "head": {
                    "sha": self.translated,
                    "ref": "translation/pr-42",
                    "repo": {"full_name": "ydb-platform/ydb"},
                },
                "base": {
                    "ref": "main",
                    "sha": self.base,
                    "repo": {"full_name": "ydb-platform/ydb"},
                },
                "body": "<!-- ydbdoc-source-pr:42 -->\n<!-- ydbdoc-source-sha:"
                + self.source
                + " -->",
            }
        if path == "/pulls/42/files?per_page=100":
            return [{"status": "modified", "filename": "ydb/docs/ru/core/page.md"}]
        if path == "/pulls/43/files?per_page=100":
            return [{"status": "modified", "filename": name} for name in self.files
                    if name.startswith("ydb/docs/en/")]
        if path == "/git/ref/heads/main":
            return {"object": {"sha": self.base}}
        if path.startswith("/git/ref/heads/translation"):
            return None if self.branch_head is None else {"object": {"sha": self.branch_head}}
        if method == "DELETE" and path.startswith("/git/refs/heads/translation"):
            self.branch_head = None
            return None
        if method == "DELETE" and "/labels/" in path:
            return None
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
            return {"tree": {"sha": "c" * 40}, "parents": [{"sha": self.base}]}
        if path == "/git/blobs":
            self.blob = base64.b64decode(payload["content"])
            sha = f"{len(self.blobs) + 0xC0:040x}"
            self.blobs[sha] = self.blob
            return {"sha": sha}
        if path == "/git/trees":
            self.tree = list(payload.get("tree") or ())
            return {"sha": "d" * 40}
        if path == "/git/commits":
            # Unique tip SHAs: GitHubBackend caches contents by commit; reusing one
            # SHA after critic push would keep stale draft bytes forever.
            self._commit_n += 1
            self.translated = f"{0xE000 + self._commit_n:040x}"
            return {"sha": self.translated}
        if path == "/git/refs" or path.startswith("/git/refs/heads/"):
            self.branch_head = payload["sha"]
            for item in self.tree:
                path_name = item.get("path")
                sha = item.get("sha")
                if not isinstance(path_name, str):
                    continue
                if sha is None:
                    self.files.pop(path_name, None)
                elif sha in self.blobs:
                    self.files[path_name] = self.blobs[sha]
                elif self.blob is not None and path_name.endswith("page.md"):
                    self.files[path_name] = self.blob
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
        if path == "/issues/42/comments?per_page=100":
            return self.source_comments
        if path == "/issues/43/comments":
            self.comments.append(
                {
                    "id": 7,
                    "user": {"id": 42, "type": "User", "login": "pat-publisher"},
                    "body": payload["body"],
                }
            )
            return {"id": 7}
        if path == "/issues/42/comments":
            comment_id = 800 + len(self.source_comments)
            self.source_comments.append(
                {
                    "id": comment_id,
                    "user": {"id": 42, "type": "User", "login": "pat-publisher"},
                    "body": payload["body"],
                }
            )
            return {"id": comment_id}
        if path.startswith("/issues/comments/") and method == "PATCH":
            comment_id = int(path.rsplit("/", 1)[-1])
            for rows in (self.comments, self.source_comments):
                for row in rows:
                    if row["id"] == comment_id:
                        row["body"] = payload["body"]
                        return {}
            raise AssertionError((method, path, "unknown_comment_id"))
        raise AssertionError((method, path))

    def model(self, request):
        from ydbdoc_review_ng.models import HttpResponse

        body = json.loads(request.body)
        if body.get("tools"):
            return self._critic_tool_http(body)

        schema = request_schema(body)
        if schema is None:
            prompt = request_prompt(body)
            if prompt.startswith("Repair"):
                self.events.append(("REPAIR", "markdown"))
                text = rewrite_markdown(raw_repair_context(prompt, "current-target"), "Corrected")
            elif "<TRANSLATION_DRAFT_" in prompt:
                self.events.append(("CRITIC", ("raw_markdown",)))
                text = raw_translation_draft(prompt)
            else:
                self.events.append(("MODEL", ("raw_markdown",)))
                text = translated_markdown(prompt)
        else:
            properties = schema["schema"]["properties"]
            self.events.append(("CLASSIFIER" if "translation_required" in properties else "MODEL",
                                tuple(properties)))
            prompt = request_prompt(body)
            if "translation_required" in properties:
                values = classification_response(prompt)
            elif "strings" in properties and type(properties["strings"]) is dict:
                values = toc_string_translations(prompt, schema["schema"])
            elif properties and all(key.startswith("segment_") for key in properties):
                values = translation_segments(prompt)
            elif set(properties) in ({"files"}, {"verdict", "findings"}):
                assert self.semantic_responses, "unexpected extra semantic model call"
                # Thin cutover: discard queued critic {"files": ...} payloads.
                while self.semantic_responses and set(
                    self.semantic_responses[0]
                ) == {"files"}:
                    self.semantic_responses.pop(0)
                assert self.semantic_responses, "unexpected extra semantic model call"
                values = self.semantic_responses.pop(0)
            else:
                values = {key: "Translated" for key in properties}
            text = json.dumps(values)
        if "model" in body:
            payload = {
                "model": "test-model",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": text},
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }
        else:
            payload = {
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
        return HttpResponse(200, json.dumps(payload).encode(), Decimal("0.01"))

    def _on_critic_tool_session(self, body: dict) -> None:
        """Hook once per critic tool session (developer+user). Subclasses record roles."""

    def _critic_files_for_chunk(
        self, drafts: dict[str, str | None], body: dict
    ) -> dict[str, str] | None:
        """Optional path→reviewed text for this chunk. None uses semantic_responses."""
        return None

    def _critic_tool_http(self, body: dict):
        from ydbdoc_review_ng.models import HttpResponse

        roles = [
            message.get("role")
            for message in body.get("messages", [])
            if isinstance(message, dict)
        ]
        if roles == ["developer", "user"] or roles == ["user"]:
            # New critic session: drop leftover turns from a failed attempt.
            self._tool_queue = []
            self._on_critic_tool_session(body)
        if not self._tool_queue:
            self._refill_tool_queue(body)
        assert self._tool_queue, "unexpected critic tool call without queued turns"
        result = self._tool_queue.pop(0)
        self.events.append(("CRITIC", ("tools",)))
        tool_calls = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in result.tool_calls
        ]
        payload = {
            "model": "test-model",
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": tool_calls,
                    },
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        return HttpResponse(200, json.dumps(payload).encode(), Decimal("0.01"))

    def _enqueue_reviewed_files(
        self, drafts: dict[str, str | None], reviewed: dict[str, str]
    ) -> None:
        if not reviewed:
            self._tool_queue.extend([finish_only()])
            return
        for path, text in reviewed.items():
            draft_text = drafts.get(path)
            draft = b"" if draft_text is None else draft_text.encode("utf-8")
            reviewed_b = text.encode("utf-8") if isinstance(text, str) else b""
            self._tool_queue.extend(patch_read_finish(path, draft, reviewed_b))

    def _refill_tool_queue(self, body: dict) -> None:
        drafts = translation_pr_files_from_body(body)
        custom = self._critic_files_for_chunk(drafts, body)
        if custom is not None:
            self._enqueue_reviewed_files(drafts, custom)
            return
        assert self.semantic_responses, "unexpected extra semantic model call"
        values = self.semantic_responses[0]
        if "verdict" in values:
            # Critic tool loop should not see arbiter payloads.
            raise AssertionError("critic tools requested but next semantic response is arbiter")
        files = values.get("files")
        assert isinstance(files, dict), "critic semantic response must contain files"
        requested = set(drafts)
        matched = {path: text for path, text in files.items() if path in requested}
        leftover = {path: text for path, text in files.items() if path not in requested}
        if leftover:
            values["files"] = leftover
        else:
            self.semantic_responses.pop(0)
        if not matched and not drafts:
            self._tool_queue.extend([finish_only()])
            return
        if not matched:
            # No scripted correction for this chunk: no-op finish keeps draft.
            self._tool_queue.extend([finish_only()])
            return
        matched_text = {
            path: (text if isinstance(text, str) else "") for path, text in matched.items()
        }
        self._enqueue_reviewed_files(drafts, matched_text)


def translation_pr_files_from_body(body: dict) -> dict[str, str | None]:
    for message in body.get("messages", []):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, str) or "<translation-pr-files>" not in content:
            continue
        block = content.split("<translation-pr-files>\n", 1)[1].split(
            "\n</translation-pr-files>", 1
        )[0]
        rendered = json.loads(block)
        out: dict[str, str | None] = {}
        for path, text in rendered.items():
            if text is None:
                out[path] = None
            elif isinstance(text, str):
                out[path] = text
        return out
    return {}


def source_pr_files_from_body(body: dict) -> dict[str, str | None]:
    for message in body.get("messages", []):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, str) or "<source-pr-files>" not in content:
            continue
        block = content.split("<source-pr-files>\n", 1)[1].split("\n</source-pr-files>", 1)[0]
        rendered = json.loads(block)
        out: dict[str, str | None] = {}
        for path, text in rendered.items():
            if text is None:
                out[path] = None
            elif isinstance(text, str):
                out[path] = text
        return out
    return {}


class InstalledContinueServices(RuntimeServices):
    """The same HTTP/YDB boundaries with a review checkpoint for installed smoke."""

    def __init__(self):
        super().__init__()
        self.jobs = {}
        self.checkpoints = {}
        self.continuing = False
        self.semantic_responses = [
            {"files": {"ydb/docs/en/core/page.md": "# Translated\n"}},
            {"verdict": "GREEN", "findings": []},
            {"files": {"ydb/docs/en/core/page.md": "# Translated\n"}},
            {
                "verdict": "RED",
                "findings": [
                    {
                        "reason": "Meaning requires operator context.",
                        "expected_correction": "Confirm the intended source meaning.",
                        "searchable_snippet": "Translated",
                        "target_path": "ydb/docs/en/core/page.md",
                    }
                ],
            },
            {"files": {"ydb/docs/en/core/page.md": "# Translated\n"}},
            {"verdict": "GREEN", "findings": []},
        ]

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
