import sys
from datetime import UTC, datetime
from types import SimpleNamespace

from ydbdoc_review_ng.runtime_ydb import SDKExecutor, parameter_types


def test_non_null_job_and_continuation_parameters_have_explicit_sdk_types() -> None:
    assert parameter_types(
        {
            "source_sha": None,
            "job_id": "job-1",
            "source_pr": 42,
            "trigger_pr": 52,
            "state": b"{}",
            "created_at": object(),
            "target_sha": None,
            "consumed_by_job_id": None,
        }
    ) == {
        "source_sha": "Utf8?",
        "job_id": "Utf8",
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


def test_sdk_executor_stops_pool_and_driver_once(monkeypatch) -> None:
    stopped = []
    pool = SimpleNamespace(
        execute_with_retries=lambda query, params: [],
        stop=lambda: stopped.append("pool"),
    )
    driver = SimpleNamespace(
        wait=lambda **kwargs: None,
        stop=lambda: stopped.append("driver"),
    )
    sdk = SimpleNamespace(
        Driver=lambda **kwargs: driver,
        QuerySessionPool=lambda supplied: pool,
        AccessTokenCredentials=lambda token: object(),
    )
    monkeypatch.setitem(sys.modules, "ydb", sdk)
    executor = SDKExecutor("grpcs://example.test", "/database", "secret")
    executor.execute("SELECT 1", {})

    executor.close()
    executor.close()

    assert stopped == ["pool", "driver"]


def test_create_runtime_shuts_down_its_owned_ydb_executor_once(monkeypatch) -> None:
    import ydbdoc_review_ng.runtime as runtime_module

    stopped = []

    class Executor:
        def __init__(self, endpoint, database, token, service_account_key):
            pass

        def execute(self, statement, parameters):
            return []

        def close(self):
            stopped.append("executor")

    monkeypatch.setattr(runtime_module, "SDKExecutor", Executor)
    runtime = runtime_module.create_runtime(environment={})

    runtime.shutdown()
    runtime.shutdown()

    assert stopped == ["executor"]
