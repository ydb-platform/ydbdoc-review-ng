"""Payload-free, line-oriented diagnostics for rare live workflow runs."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager

from ydbdoc_review_ng.errors import SafeDiagnosticError

_TOKEN = re.compile(r"[a-z][a-z0-9_]*")
_ALLOWED_STATUSES = frozenset({"start", "ok", "fail", "retry"})
_ALLOWED_DETAILS = frozenset(
    {
        "accepted_total",
        "article",
        "attempt",
        "attempts_total",
        "code",
        "document_index",
        "documents_total",
        "endpoint",
        "entries_total",
        "error_type",
        "field_index",
        "fields_total",
        "inventory_files",
        "job_id",
        "locale_pairs",
        "method",
        "mode",
        "model_role",
        "pr_number",
        "rows",
        "scopes_total",
        "stage",
    }
)


def _token(name: str, value: object) -> str:
    if type(value) is not str or _TOKEN.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase diagnostic token")
    return value


def _details(values: Mapping[str, object]) -> dict[str, bool | int | str | None]:
    if not set(values) <= _ALLOWED_DETAILS:
        raise TypeError("trace metadata key is not allowlisted")
    result: dict[str, bool | int | str | None] = {}
    for key, value in values.items():
        if value is None or type(value) in {bool, int}:
            result[key] = value  # type: ignore[assignment]
            continue
        if type(value) is not str or len(value) > 200 or any(
            character in value for character in "\r\n\x00"
        ):
            raise ValueError("trace metadata value is not a bounded single-line scalar")
        result[key] = value
    return result


def write_trace(
    component: str,
    operation: str,
    status: str,
    /,
    **details: object,
) -> None:
    """Write one JSON event containing only explicitly allowlisted metadata."""
    event: dict[str, object] = {
        "component": _token("component", component),
        "operation": _token("operation", operation),
        "status": status,
        **_details(details),
    }
    if status not in _ALLOWED_STATUSES:
        raise ValueError("unsupported trace status")
    print(
        "YDBDOC_TRACE " + json.dumps(event, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
        file=sys.stderr,
        flush=True,
    )


@contextmanager
def traced(
    component: str,
    operation: str,
    /,
    **details: object,
) -> Iterator[None]:
    """Trace one boundary without exposing exception messages or payloads."""
    write_trace(component, operation, "start", **details)
    try:
        yield
    except Exception as error:
        code = error.code if isinstance(error, SafeDiagnosticError) else "unexpected_exception"
        write_trace(
            component,
            operation,
            "fail",
            **details,
            code=code,
            error_type=type(error).__name__,
        )
        raise
    write_trace(component, operation, "ok", **details)
