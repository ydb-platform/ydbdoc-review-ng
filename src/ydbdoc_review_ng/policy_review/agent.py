"""Run the versioned pr-review skill with bounded, read-only GitHub tools."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any, cast
from urllib.parse import quote

from ydbdoc_review_ng.domain import ModelRole
from ydbdoc_review_ng.models import ModelRequest
from ydbdoc_review_ng.models.configuration import DEEPSEEK_PRICE, PRODUCTION_MODEL
from ydbdoc_review_ng.models.types import FrozenJson
from ydbdoc_review_ng.policy_review.github import ReviewGitHub
from ydbdoc_review_ng.policy_review.model import BudgetedPolicyModel
from ydbdoc_review_ng.policy_review.types import (
    RULE_FILES,
    RULE_ROOT,
    ReviewDiff,
    ReviewError,
    repo_path,
)

SKILL = Path(__file__).parent / "skill"
MAX_TURNS = 24
MAX_CALLS = 96
MAX_WIRE = 512 * 1024
MAX_OUTPUT_TOKENS = 8192
HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
RESOURCE_NAMES = ("writing-policy", "placement", "public-map", "feature-flags")


def _function(name: str, description: str, properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "function", "function": {"name": name, "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": list(properties), "additionalProperties": False}}}


STRING = {"type": "string"}
INTEGER = {"type": "integer", "minimum": 1}
SOURCE = {"path": STRING, "revision": {"enum": ["base", "head"], "type": "string"}}
FINDING = {"path": STRING, "line": INTEGER, "problem": STRING, "proof": STRING, "correction": STRING}
TOOLS = [
    _function("read_file", "Read a repository file at HEAD or base tip, inclusive line window (1–120 lines).",
              {**SOURCE, "start_line": INTEGER, "end_line": INTEGER}),
    _function("search", "Find a literal in one explicit repository path and return numbered matches.",
              {**SOURCE, "literal": STRING}),
    _function("read_resource", "Read a complete trusted skill reference or the ordered writing policy.",
              {"name": {"type": "string", "enum": list(RESOURCE_NAMES)}}),
    _function("finish_review", "Return the human review. State scope/result, checks and named gaps.",
              {"summary": {**STRING, "description": "One short result paragraph only. No headings, finding list or coverage report here."}, "findings": {"type": "array", "items": {
                  "type": "object", "properties": FINDING, "required": list(FINDING),
                  "additionalProperties": False}}, "checked": {
                   **STRING, "description": "Brief factual checks. Name only exact targets actually inspected. Use changed_file_count; never infer all-target coverage."},
               "gaps": {"type": "array", "items": {**STRING, "description":
                   "An unavailable verification only. Do not include unrelated pre-existing defects."}}}),
]


def historical(path: str) -> bool:
    return any(part.casefold() == "releases" or
               re.match(r"^(?:change[-_]?logs?|release[-_]?notes?|(?:version[-_]?)?history|whats[-_]?new)(?:[._-]|$)",
                        part, re.IGNORECASE) for part in PurePosixPath(path).parts)


def patch_lines(patch: str) -> Iterator[tuple[str, int, int, str]]:
    old = new = 0
    for row in patch.splitlines():
        match = HUNK.match(row)
        if match:
            old, new = map(int, match.groups())
        elif row and row[0] in "+- ":
            yield row[0], old, new, row[1:]
            old += row[0] != "+"
            new += row[0] != "-"


def _historical_change(row: dict[str, object]) -> bool:
    return any(historical(str(row.get(key, ""))) for key in ("filename", "previous_filename"))


def public_changes(files: list[dict[str, object]]) -> list[dict[str, object]]:
    changes = []
    for row in files:
        item = dict(row)
        repo_path(item["filename"])
        if "previous_filename" in item:
            repo_path(item["previous_filename"])
        patch = item.get("patch")
        if not isinstance(patch, str):
            item["gap"] = "GitHub supplied no text patch."
        else:
            counts = {"+": 0, "-": 0}
            for kind, _, _, _ in patch_lines(patch):
                if kind in counts:
                    counts[kind] += 1
            if any(k in item and item[k] != counts[sign]
                   for k, sign in (("additions", "+"), ("deletions", "-"))):
                item["gap"] = "Patch does not cover GitHub's reported change counts."
            if _historical_change(item):
                if item.get("status") in {"added", "removed"} or sum(counts.values()) > 100:
                    item["gap"] = "Historical document exceeds changed-entry scope."
                    item.pop("patch", None)
                else:
                    item["patch"] = "\n".join(f"{kind}{new if kind == '+' else old}: {line}"
                                              for kind, old, new, line in patch_lines(patch)
                                              if kind in "+-")
        if isinstance(patch, str) and "patch" in item:
            item["head_changed_lines"] = [new for kind, _, new, _ in patch_lines(patch) if kind == "+"]
        changes.append(item)
    return changes


def read_source(github: ReviewGitHub, changes: list[dict[str, object]], head: str, base: str,
                tool: str, args: dict[str, Any]) -> dict[str, Any]:
    path = repo_path(args["path"])
    revision = {"head": head, "base": base}[args["revision"]]
    if tool == "read_file":
        start, end = args["start_line"], args["end_line"]
        if type(start) is not int or type(end) is not int or not 1 <= start <= end < start + 120:
            raise ReviewError("invalid_line_window")
    else:
        literal = args["literal"]
        if not isinstance(literal, str) or not 1 <= len(literal) <= 256:
            raise ReviewError("invalid_literal")
    changed = next((r for r in changes if path in (r["filename"], r.get("previous_filename"))), {})
    scope = "line_window" if tool == "read_file" else "entire_file"
    if historical(path) or _historical_change(changed):
        scope = "changed_lines_only"
        if revision != head or changed.get("filename") != path or changed.get("status") in {"added", "removed"}:
            raise ReviewError("historical_read_outside_changed_entry")
        lines = {new: line for kind, _, new, line in patch_lines(str(changed.get("patch", "")))
                 if kind == "+"}
        if len(lines) > 100 or (tool == "read_file" and
                                not all(n in lines for n in range(start, end + 1))):
            raise ReviewError("historical_read_outside_changed_entry")
        source = lines.items()
    else:
        body = github.read_text(path, revision, required=False)
        if body is None:
            return {"path": path, "revision": revision, "missing": True}
        source = dict(enumerate(body.splitlines(), 1)).items()
    result: list[tuple[int, str]] = []
    truncated = False
    for number, line in source:
        if (start <= number <= end if tool == "read_file" else literal in line):
            if len(result) >= (120 if tool == "read_file" else 20):
                truncated = True
                break
            result.append((number, line))
    reply = {"path": path, "revision": revision, "scope": scope,
             "lines": result, "truncated": truncated}
    if scope == "changed_lines_only":
        reply["coverage_notice"] = (
            "Only added/modified lines of this PR were searched/read, not the entire file. "
            "An empty result says nothing about other entries. Historical content is deliberately withheld.")
    if len(json.dumps(reply, ensure_ascii=False).encode()) > 16_384:
        raise ReviewError("result_too_large_request_smaller_window")
    return reply


def resource(github: ReviewGitHub, revision: str, name: str) -> dict[str, object]:
    if name not in RESOURCE_NAMES:
        raise ReviewError("unknown_resource")
    if name != "writing-policy":
        return {"name": name, "text": (SKILL / (name + ".md")).read_text(encoding="utf-8")}
    paths = [RULE_ROOT + f for f in ("DOCUMENTATION_POLICY.md", *RULE_FILES)]
    files = {p: github.read_text(p, revision, required=False) for p in paths}
    entry = files[paths[0]]
    if entry is not None:
        declared = re.findall(r"(?m)^\d+\.\s+`([^`\n]+\.md)`", entry)
        if tuple(declared) != RULE_FILES:
            raise ReviewError("unsupported_policy_order")
    return {"revision": revision, "legacy_fallback": entry is None, "files": files,
            "missing": [p for p, body in files.items() if body is None]}


def _string(value: object) -> str:
    if type(value) is not str or not value.strip() or len(value) > 12000:
        raise ReviewError("invalid_report_text")
    return value


def finish(diff: ReviewDiff, args: dict[str, Any], gaps: list[str]) -> dict[str, object]:
    parts = ["## Результат ревью", _string(args["summary"])]
    findings = args["findings"]
    if not isinstance(findings, list) or len(findings) > 40 or not isinstance(args["gaps"], list):
        raise ReviewError("invalid_report")
    for finding in findings:
        path, line = repo_path(finding["path"]), finding["line"]
        row = next((r for r in diff.files if r["filename"] == path), {})
        if type(line) is not int or line < 1 or not any(
            new == line and kind != "-" for kind, _, new, _ in patch_lines(str(row.get("patch", "")))
        ):
            raise ReviewError("finding_outside_head_hunk")
        url = f"https://github.com/ydb-platform/ydb/blob/{diff.head_sha}/{quote(path)}#L{line}"
        parts.append(f"### [{path}:{line}]({url})\n\n" + _string(finding["problem"])
                     + "\n\nПруф: " + _string(finding["proof"])
                     + "\n\nИсправление:\n\n" + _string(finding["correction"]))
    if not findings:
        parts.append("Замечаний нет.")
    parts.extend(["### Что проверено", _string(args["checked"])])
    limitations = [*map(_string, args["gaps"]), *gaps]
    if limitations:
        parts.extend(["### Что осталось непроверенным", "\n".join("- " + g for g in limitations)])
    markdown = "\n\n".join(parts)
    if len(markdown) > 50000:
        raise ReviewError("report_too_large")
    return {"status": "incomplete" if limitations else "findings" if findings else "clean",
            "markdown": markdown, "findings": findings}


def review_pr(diff: ReviewDiff, github: ReviewGitHub, model: BudgetedPolicyModel) -> dict[str, object]:
    changes = public_changes(diff.files)
    gaps = [f"{r['filename']}: {r['gap']}" for r in changes if "gap" in r]
    data = {**diff.metadata, "head_sha": diff.head_sha, "base_tip_sha": diff.base_sha,
            "writing_policy_sha": diff.rules_sha, "changed_file_count": len(diff.files), "changes": changes}
    bundled = [name for name in RESOURCE_NAMES if name != "writing-policy"]
    instructions = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    instructions += "\n\nThe following trusted skill references are already loaded. Read writing-policy via its tool.\n"
    for name in bundled:
        instructions += "\n## Bundled skill reference: " + name + "\n" + (SKILL / (name + ".md")).read_text(encoding="utf-8")
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": instructions},
        {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
    ]
    calls = 0
    loaded: set[str] = set(bundled)
    failed_calls: dict[str, int] = {}
    repeated_calls: dict[str, int] = {}
    for turn_number in range(MAX_TURNS):
        request = ModelRequest(ModelRole.CRITIC, PRODUCTION_MODEL, "pr-review", None,
                               messages=cast(FrozenJson, messages), tools=cast(FrozenJson, TOOLS),
                               tool_choice="auto", max_output_tokens=MAX_OUTPUT_TOKENS)
        try:
            wire = model.prepare_request(request)
        except ValueError:
            raise ReviewError("model_context_limit_exceeded") from None
        if len(wire.body) > MAX_WIRE or wire.max_tokens < MAX_OUTPUT_TOKENS:
            raise ReviewError("model_context_limit_exceeded")
        upper_bound = (Decimal(wire.input_tokens) * DEEPSEEK_PRICE.input_token_rub
                       + Decimal(wire.max_tokens) * DEEPSEEK_PRICE.output_token_rub)
        available = model.budget.limit - model.budget.spent - model.budget.reserved
        if turn_number == MAX_TURNS - 1 or available < upper_bound * Decimal("1.5"):
            # Finish while one bounded answer still fits. The normal reservation remains mandatory.
            gaps.append("Дальнейшее чтение остановлено для завершения отчёта в пределах лимита.")
            messages.append({"role": "user", "content": (
                "Return the final review now using the evidence already read. No more tools are available. "
                "Include concrete proven findings and explicitly name checks left unverified. "
                "Do not infer that all scenarios were checked from this forced completion. "
                "A link is verified only if its actual destination and fragment were read, not inferred from "
                "other links. Respect each search response scope; absence in changed_lines_only is not "
                "absence in the file. Keep the report concise and omit unproved coverage claims.")})
            request = ModelRequest(ModelRole.CRITIC, PRODUCTION_MODEL, "pr-review", None,
                                   messages=cast(FrozenJson, messages), max_output_tokens=MAX_OUTPUT_TOKENS)
            wire = model.prepare_request(request)
            if len(wire.body) > MAX_WIRE or wire.max_tokens < MAX_OUTPUT_TOKENS:
                raise ReviewError("model_context_limit_exceeded")
            print("pr-review finalizing within run budget", flush=True)
        turn = model.invoke(request)
        if turn.status != "ok":
            raise ReviewError(turn.status)
        if not turn.tool_calls:
            if not turn.text or not turn.text.strip() or len(turn.text) > 50000:
                raise ReviewError("invalid_report_text")
            missing_resources = ["Ресурс не проверен: " + x for x in sorted(set(RESOURCE_NAMES) - loaded)]
            limitations = [*gaps, *missing_resources]
            markdown = turn.text
            if limitations:
                markdown += "\n\n### Что осталось непроверенным\n" + "\n".join("- " + g for g in limitations)
            print("pr-review final Markdown received", flush=True)
            return {"status": "incomplete" if limitations else "reviewed",
                    "markdown": markdown, "findings": []}
        calls += len(turn.tool_calls)
        if calls > MAX_CALLS:
            raise ReviewError("review_tool_limit")
        messages.append({"role": "assistant", "content": turn.text, "tool_calls": [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
            for c in turn.tool_calls]})
        if turn.reasoning_content is not None:
            # Provider continuation state stays private, never in reports or trace logs.
            messages[-1]["reasoning_content"] = turn.reasoning_content
        for call in turn.tool_calls:
            args = {}
            try:
                args = json.loads(call.arguments)
                if not isinstance(args, dict):
                    raise TypeError("invalid_arguments")
                if call.name == "finish_review":
                    if len(turn.tool_calls) != 1:
                        raise ReviewError("finish_must_be_alone")
                    missing = set(RESOURCE_NAMES) - loaded
                    print("pr-review finish_review", flush=True)
                    return finish(diff, args, gaps + ["Ресурс не проверен: " + x for x in sorted(missing)])
                if call.name == "read_resource":
                    reply = resource(github, diff.rules_sha, args["name"])
                    if len(json.dumps(reply, ensure_ascii=False).encode()) > 65536:
                        raise ReviewError("resource_too_large")
                    loaded.add(args["name"])
                    gaps.extend("Файл политики отсутствует: " + str(p) for p in cast(list[str], reply.get("missing", [])))
                elif call.name in {"read_file", "search"}:
                    reply = read_source(github, diff.files, diff.head_sha, diff.base_sha, call.name, args)
                else:
                    raise ReviewError("unknown_tool")
            except (ValueError, KeyError, TypeError, ReviewError) as error:
                # Return a bounded diagnostic; do not forward exceptions containing untrusted arguments.
                reply = {"error": error.code if isinstance(error, ReviewError) else "invalid_tool_arguments_or_bounds"}
                if not isinstance(args, dict):
                    args = {}
            coordinates = {k: args[k] for k in ("name", "path", "revision", "start_line", "end_line") if k in args}
            reply_lines = reply.get("lines")
            print("pr-review tool", repr(call.name), json.dumps(coordinates, ensure_ascii=False),
                  "error=" + str(reply.get("error")), "missing=" + str(reply.get("missing")),
                  "lines=" + str(len(reply_lines) if isinstance(reply_lines, list) else 0),
                  "scope=" + str(reply.get("scope")), "truncated=" + str(reply.get("truncated")), flush=True)
            key = call.name + ":" + json.dumps(args, sort_keys=True)
            repeated_calls[key] = repeated_calls.get(key, 0) + 1
            if repeated_calls[key] >= 3:
                raise ReviewError("repeated_tool_call")
            if reply.get("error") or reply.get("missing"):
                failed_calls[key] = failed_calls.get(key, 0) + 1
                if failed_calls[key] >= 2:
                    raise ReviewError("repeated_tool_failure")
            messages.append({"role": "tool", "tool_call_id": call.id,
                             "content": json.dumps(reply, ensure_ascii=False)})
    raise ReviewError("review_turn_limit")
