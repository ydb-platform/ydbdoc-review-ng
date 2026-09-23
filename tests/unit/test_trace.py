from __future__ import annotations

import json

import pytest

from ydbdoc_review_ng.trace import traced, write_trace


def test_trace_is_one_machine_readable_line_with_only_allowlisted_metadata(capsys) -> None:
    write_trace(
        "translation",
        "field",
        "start",
        article="ydb/docs/en/core/page.md",
        document_index=2,
        documents_total=3,
        field_index=7,
        fields_total=11,
    )

    line = capsys.readouterr().err
    assert line.startswith("YDBDOC_TRACE ")
    assert json.loads(line.removeprefix("YDBDOC_TRACE ")) == {
        "article": "ydb/docs/en/core/page.md",
        "component": "translation",
        "document_index": 2,
        "documents_total": 3,
        "field_index": 7,
        "fields_total": 11,
        "operation": "field",
        "status": "start",
    }
    assert line.count("\n") == 1


@pytest.mark.parametrize(
    ("details", "value"),
    [
        ({"token": "secret-value"}, "secret-value"),
        ({"article": "safe.md\nSECRET"}, "SECRET"),
        ({"code": "x" * 201}, "x" * 201),
    ],
)
def test_trace_rejects_unapproved_or_unsafe_metadata_without_printing_it(
    details: dict[str, object], value: str, capsys
) -> None:
    with pytest.raises((TypeError, ValueError)):
        write_trace("workflow", "failure", "fail", **details)

    assert value not in capsys.readouterr().err


def test_traced_reports_safe_exception_class_without_its_message(capsys) -> None:
    with (
        pytest.raises(RuntimeError, match="PRIVATE_MODEL_RESPONSE"),
        traced("prepare", "translate_documents", documents_total=2),
    ):
        raise RuntimeError("PRIVATE_MODEL_RESPONSE")

    output = capsys.readouterr().err
    lines = [
        json.loads(line.removeprefix("YDBDOC_TRACE ")) for line in output.splitlines()
    ]
    assert lines == [
        {
            "component": "prepare",
            "documents_total": 2,
            "operation": "translate_documents",
            "status": "start",
        },
        {
            "code": "unexpected_exception",
            "component": "prepare",
            "documents_total": 2,
            "error_type": "RuntimeError",
            "operation": "translate_documents",
            "status": "fail",
        },
    ]
    assert "PRIVATE_MODEL_RESPONSE" not in output
