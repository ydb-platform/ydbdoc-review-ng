import sys
from datetime import UTC, datetime
from types import SimpleNamespace

from ydbdoc_review_ng.runtime_ydb import SDKExecutor, parameter_types


def test_continuation_and_job_parameters_have_explicit_sdk_types() -> None:
    assert parameter_types(
        {
            "source_sha": None,
            "job_id": None,
            "source_pr": 42,
            "trigger_pr": 52,
            "state": b"{}",
            "created_at": object(),
            "target_sha": None,
            "consumed_by_job_id": None,
        }
    ) == {
        "source_sha": "Utf8?",
        "job_id": "Utf8?",
        "source_pr": "Uint64",
        "trigger_pr": "Uint64",
        "state": "String",
        "created_at": "Timestamp",
        "target_sha": "Utf8?",
        "consumed_by_job_id": "Utf8?",
    }


def test_sdk_naive_utc_timestamp_is_returned_as_aware_utc(monkeypatch) -> None:
    naive = datetime(2026, 9, 21, 9, 17, 2, 123456)  # noqa: DTZ001 - SDK returns naive UTC.
    rows = [{"created_at": naive, "state": b"{}", "target_sha": None}]
    pool = SimpleNamespace(execute_with_retries=lambda query, params: [SimpleNamespace(rows=rows)])
    sdk = SimpleNamespace(
        Driver=lambda **kwargs: SimpleNamespace(wait=lambda **kwargs: None),
        QuerySessionPool=lambda driver: pool,
        AccessTokenCredentials=lambda token: object(),
    )
    monkeypatch.setitem(sys.modules, "ydb", sdk)

    result = SDKExecutor("grpcs://example.test", "/database", "secret").execute("SELECT 1", {})

    assert result == [
        {
            "created_at": datetime(2026, 9, 21, 9, 17, 2, 123456, tzinfo=UTC),
            "state": b"{}",
            "target_sha": None,
        }
    ]
    assert rows[0]["created_at"] is naive
