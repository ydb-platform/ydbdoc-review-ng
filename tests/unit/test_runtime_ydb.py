import sys
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from ydbdoc_review_ng.runtime_ydb import SDKExecutor, parameter_types


@pytest.mark.parametrize(
    ("source_sha", "expected_type"),
    [
        ("a" * 40, "Utf8"),
        (None, "Utf8?"),
    ],
)
def test_parameter_types_binds_source_sha_by_nullability(source_sha, expected_type) -> None:
    assert parameter_types({"source_sha": source_sha}) == {"source_sha": expected_type}


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


@pytest.mark.parametrize(
    ("job_id", "declaration", "value_type"),
    [
        ("job-1", "DECLARE $job_id AS Utf8;", "Utf8"),
        (None, "DECLARE $job_id AS Utf8?;", "Optional<Utf8>"),
    ],
)
def test_sdk_executor_binds_job_id_by_nullability(
    monkeypatch, job_id, declaration, value_type
) -> None:
    calls = []
    pool = SimpleNamespace(
        execute_with_retries=lambda query, params: calls.append((query, params)) or []
    )
    sdk = SimpleNamespace(
        Driver=lambda **kwargs: SimpleNamespace(wait=lambda **kwargs: None),
        QuerySessionPool=lambda driver: pool,
        AccessTokenCredentials=lambda token: object(),
        PrimitiveType=SimpleNamespace(Utf8="Utf8"),
        OptionalType=lambda item: f"Optional<{item}>",
        TypedValue=lambda value, item_type: (value, item_type),
    )
    monkeypatch.setitem(sys.modules, "ydb", sdk)

    SDKExecutor("grpcs://example.test", "/database", "secret").execute(
        "SELECT $job_id", {"job_id": job_id}
    )

    assert declaration in calls[0][0]
    assert calls[0][1] == {"$job_id": (job_id, value_type)}


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


def test_create_runtime_composes_distinct_github_read_and_mutation_credentials(monkeypatch) -> None:
    import ydbdoc_review_ng.runtime as runtime_module

    credentials = []

    def github_http(read_token, mutation_token):
        credentials.append((read_token, mutation_token))
        return lambda method, path, payload: None

    executor = SimpleNamespace(execute=lambda statement, parameters: [], close=lambda: None)
    monkeypatch.setattr(runtime_module, "GitHubHTTP", github_http)

    runtime = runtime_module.create_runtime(
        environment={
            "YDBDOC_GITHUB_READ_TOKEN": "read-token",
            "YDB_GH_TOKEN": "mutation-token",
        },
        ydb_executor=executor,
    )

    assert credentials == [("read-token", "mutation-token")]
    runtime.shutdown()


def test_create_runtime_runs_diplodoc_after_candidate_validation(monkeypatch, tmp_path) -> None:
    import ydbdoc_review_ng.runtime as runtime_module
    import ydbdoc_review_ng.runtime_content as content_module

    events = []
    captured = {}

    def validate_content(self, snapshot, candidate, plan):
        events.append("candidate")

    def make_diplodoc(docs_root):
        assert docs_root == tmp_path / "ydb/docs"
        return lambda plan: events.append("diplodoc")

    def make_publisher(github, build_plan, validate_plan):
        captured["validate_plan"] = validate_plan
        return SimpleNamespace()

    executor = SimpleNamespace(execute=lambda statement, parameters: [], close=lambda: None)
    monkeypatch.setattr(content_module.RuntimeContent, "validate_plan", validate_content)
    monkeypatch.setattr(runtime_module, "DiplodocBuildValidator", make_diplodoc)
    monkeypatch.setattr(runtime_module, "GitPublicationAdapter", make_publisher)

    runtime = runtime_module.create_runtime(
        environment={"YDBDOC_DOCS_ROOT": str(tmp_path / "ydb/docs")},
        ydb_executor=executor,
    )
    captured["validate_plan"](object(), object(), object())

    assert events == ["candidate", "diplodoc"]
    runtime.shutdown()
