"""Lazy YDB query-service executor. SDK import/connection happens at the first audit write."""

from __future__ import annotations

import importlib
from collections.abc import Mapping, Sequence
from typing import Any

from ydbdoc_review_ng.persistence import PersistenceError


def parameter_types(parameters: Mapping[str, object]) -> dict[str, str]:
    types = {
        "pr_number": "Uint64",
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
    def __init__(self, endpoint: str, database: str, token: str) -> None:
        self._endpoint, self._database, self._token = endpoint, database, token
        self._pool: Any = None
        self._driver: Any = None

    def execute(
        self, statement: str, parameters: Mapping[str, object], /
    ) -> Sequence[Mapping[str, object]]:
        try:
            sdk = importlib.import_module("ydb")
            if self._pool is None:
                if not all((self._endpoint, self._database, self._token)):
                    raise PersistenceError("YDB configuration is incomplete")
                self._driver = sdk.Driver(
                    endpoint=self._endpoint,
                    database=self._database,
                    credentials=sdk.AccessTokenCredentials(self._token),
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
            return [dict(row) for result in results for row in result.rows]
        except Exception:  # noqa: BLE001 - SDK diagnostics may contain credentials or audit payloads.
            raise PersistenceError("YDB query execution failed") from None
