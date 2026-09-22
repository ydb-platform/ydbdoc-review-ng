"""Lazy YDB query-service executor. SDK import/connection happens at the first audit write."""

from __future__ import annotations

import importlib
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from ydbdoc_review_ng.persistence import PersistenceError

DEFAULT_YDB_ENDPOINT = "grpcs://ydb.serverless.yandexcloud.net:2135"
DEFAULT_YDB_DATABASE = "/ru-central1/b1g7gqj2vnq67gjseuva/etns0641qf73btm7j21k"


def parameter_types(parameters: Mapping[str, object]) -> dict[str, str]:
    types = {
        "pr_number": "Uint64",
        "source_pr": "Uint64",
        "trigger_pr": "Uint64",
        "source_sha": "Utf8?",
        "job_id": "Utf8?" if parameters.get("job_id") is None else "Utf8",
        "consumed_by_job_id": "Utf8?",
        "state": "String",
        "source_inventory": "String",
        "scope_target_paths": "String",
        "request": "String",
        "response": "String?",
        "cost_rub": "Decimal(22,9)?",
        "target_sha": "Utf8?",
        "error": "Utf8?",
    }
    return {
        name: types.get(
            name, "Timestamp" if name.endswith("_at") or name in {"start", "end"} else "Utf8"
        )
        for name in parameters
    }


class SDKExecutor:
    def __init__(
        self, endpoint: str, database: str, token: str, service_account_key: str = ""
    ) -> None:
        self._endpoint, self._database, self._token = endpoint, database, token
        self._service_account_key = service_account_key
        self._pool: Any = None
        self._driver: Any = None

    def close(self) -> None:
        pool, self._pool = self._pool, None
        driver, self._driver = self._driver, None
        try:
            try:
                if pool is not None:
                    pool.stop()
            finally:
                if driver is not None:
                    driver.stop()
        except Exception:  # noqa: BLE001 - SDK diagnostics may contain credentials.
            raise PersistenceError("YDB shutdown failed") from None

    def execute(
        self, statement: str, parameters: Mapping[str, object], /
    ) -> Sequence[Mapping[str, object]]:
        try:
            sdk = importlib.import_module("ydb")
            if self._pool is None:
                if (
                    not self._endpoint
                    or not self._database
                    or not (self._token or self._service_account_key)
                ):
                    raise PersistenceError("YDB configuration is incomplete")
                credentials = (
                    sdk.AccessTokenCredentials(self._token)
                    if self._token
                    else sdk.iam.ServiceAccountCredentials.from_content(self._service_account_key)
                )
                self._driver = sdk.Driver(
                    endpoint=self._endpoint,
                    database=self._database,
                    credentials=credentials,
                )
                self._driver.wait(timeout=30, fail_fast=True)
                self._pool = sdk.QuerySessionPool(self._driver)
            declarations = []
            values = {}
            for name, type_name in parameter_types(parameters).items():
                optional = type_name.endswith("?")
                base = type_name.rstrip("?")
                value_type = (
                    sdk.DecimalType(22, 9)
                    if base.startswith("Decimal")
                    else getattr(sdk.PrimitiveType, base)
                )
                if optional:
                    value_type = sdk.OptionalType(value_type)
                declarations.append(f"DECLARE ${name} AS {type_name};")
                values["$" + name] = sdk.TypedValue(parameters[name], value_type)
            results = self._pool.execute_with_retries(
                "\n".join(declarations) + "\n" + statement, values
            )
            # The SDK decodes native YDB Timestamp values as naive UTC datetimes.
            return [
                {
                    name: value.replace(tzinfo=UTC)
                    if isinstance(value, datetime) and value.tzinfo is None
                    else value
                    for name, value in row.items()
                }
                for result in results
                for row in result.rows
            ]
        except Exception:  # noqa: BLE001 - SDK diagnostics may contain credentials or audit payloads.
            raise PersistenceError("YDB query execution failed") from None
