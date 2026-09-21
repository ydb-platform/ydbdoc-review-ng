from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contract import ContractError, build_field_map_schema, parse_and_validate_field_map
from .plan import Field, TranslationPlan, build_plan
from .yandex_client import complete, extract_response_text


PR_NUMBER = 51079
HEAD_SHA = "5aab6d0e65926540eff571d03a018a492face7e1"
SOURCE_PATHS = (
    "ydb/docs/ru/core/reference/configuration/auth_config.md",
    "ydb/docs/ru/core/security/authentication.md",
)


def chunk_fields(
    fields: list[Field] | tuple[Field, ...],
    *,
    max_source_chars: int = 10_000,
    max_fields: int = 35,
) -> list[list[Field]]:
    chunks: list[list[Field]] = []
    current: list[Field] = []
    current_chars = 0
    for field in fields:
        size = len(field.model_text)
        if size > max_source_chars:
            raise ContractError(f"field {field.field_id} exceeds the chunk character limit")
        if current and (current_chars + size > max_source_chars or len(current) >= max_fields):
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(field)
        current_chars += size
    if current:
        chunks.append(current)
    return chunks


def build_prompt(source_path: str, fields: list[Field]) -> str:
    payload = {
        "source_path": source_path,
        "source_locale": "ru",
        "target_locale": "en",
        "fields": {field.field_id: field.model_text for field in fields},
    }
    return (
        "Translate all values in this source-field object. Return only the schema-bound map.\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    )


def complete_with_fallback(
    *,
    primary_model: str,
    fallback_model: str,
    complete_fn: Any,
    request_kwargs: dict[str, Any],
) -> tuple[str, dict[str, Any], str]:
    try:
        raw, response = complete_fn(model_uri=primary_model, **request_kwargs)
        return raw, response, primary_model
    except TimeoutError:
        raw, response = complete_fn(model_uri=fallback_model, **request_kwargs)
        return raw, response, fallback_model


def complete_validated_with_retries(
    *,
    request_fn: Any,
    validate_fn: Any,
    max_attempts: int = 3,
) -> tuple[dict[str, str], dict[str, Any], str, list[dict[str, Any]]]:
    if max_attempts < 1:
        raise ValueError("max_attempts must be positive")
    failures: list[dict[str, Any]] = []
    last_error: ContractError | None = None
    for attempt in range(1, max_attempts + 1):
        response: dict[str, Any] | None = None
        try:
            raw_text, response, model_used = request_fn()
            validated = validate_fn(raw_text)
            return validated, response, model_used, failures
        except ContractError as error:
            last_error = error
            failure: dict[str, Any] = {"attempt": attempt, "error": str(error)}
            if response is not None:
                failure["response"] = response
            failures.append(failure)
    raise ContractError(
        f"model response failed validation after {max_attempts} attempts: {last_error}"
    ) from last_error


def fetch_source(path: str, sha: str = HEAD_SHA) -> str:
    url = f"https://raw.githubusercontent.com/ydb-platform/ydb/{sha}/{path}"
    request = urllib.request.Request(url, headers={"User-Agent": "ydbdoc-review-ng-smoke"})
    with urllib.request.urlopen(request, timeout=60) as response:
        raw = response.read()
    return raw.decode("utf-8")


def _write_text(root: Path, relative: Path, text: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _write_json(root: Path, relative: Path, value: Any) -> None:
    _write_text(root, relative, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _required_env(name: str, *fallback_names: str) -> str:
    for candidate in (name, *fallback_names):
        value = os.environ.get(candidate)
        if value:
            return value
    raise RuntimeError(f"required environment variable is not set: {name}")


def _plan_record(plan: TranslationPlan) -> dict[str, Any]:
    return {
        "source_sha256": __import__("hashlib").sha256(plan.source.encode("utf-8")).hexdigest(),
        "fields": [asdict(field) for field in plan.fields],
    }


def run_live(output_parent: Path, *, resume_root: Path | None = None) -> Path:
    if os.environ.get("YDBDOC_LIVE") != "1":
        raise RuntimeError("set YDBDOC_LIVE=1 to authorize the paid live model calls")

    api_key = _required_env("YC_API_KEY", "YDBDOC_YC_API_KEY")
    folder_id = _required_env("YC_FOLDER_ID")
    model_uri = _required_env("YDBDOC_MODEL_TRANSLATE")
    fallback_model = os.environ.get("YDBDOC_MODEL_FALLBACK", "yandexgpt-5.1")
    experimental_recovery = os.environ.get("YDBDOC_EXPERIMENTAL_RECOVERY") == "1"
    if resume_root is None:
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        output_root = output_parent / f"pr-{PR_NUMBER}" / run_id
        output_root.mkdir(parents=True, exist_ok=False)
    else:
        output_root = resume_root
        if not output_root.is_dir():
            raise RuntimeError(f"resume directory does not exist: {output_root}")

    manifest: dict[str, Any] = {
        "pr": PR_NUMBER,
        "head_sha": HEAD_SHA,
        "source_locale": "ru",
        "target_locale": "en",
        "source_paths": list(SOURCE_PATHS),
        "model_uri": model_uri,
        "fallback_model_uri": fallback_model,
        "files": [],
    }

    for file_index, source_path in enumerate(SOURCE_PATHS, start=1):
        source = fetch_source(source_path)
        plan = build_plan(source)
        if not plan.fields:
            raise ContractError(f"no translatable fields found in {source_path}")
        identity = {field.field_id: field.model_text for field in plan.fields}
        if plan.assemble(identity) != source:
            raise ContractError(f"plan does not round-trip {source_path}")

        _write_text(output_root, Path("source") / source_path, source)
        _write_json(output_root, Path("plans") / f"{file_index:02d}.json", _plan_record(plan))

        translations: dict[str, str] = {}
        chunks = chunk_fields(plan.fields, max_source_chars=2_000, max_fields=5)
        file_record = {
            "path": source_path,
            "field_count": len(plan.fields),
            "chunk_count": len(chunks),
            "chunks": [],
        }
        for chunk_index, chunk in enumerate(chunks, start=1):
            field_ids = [field.field_id for field in chunk]
            schema = build_field_map_schema(
                field_ids,
                required_tokens={
                    field.field_id: [atom.token for atom in field.atoms]
                    for field in chunk
                },
            )
            prompt = build_prompt(source_path, chunk)
            response_path = Path("responses") / f"{file_index:02d}-{chunk_index:02d}.json"
            absolute_response_path = output_root / response_path
            reused = absolute_response_path.is_file()
            if reused:
                saved = json.loads(absolute_response_path.read_text(encoding="utf-8"))
                if saved.get("field_ids") != field_ids:
                    raise ContractError(f"saved response fields do not match {response_path}")
                response = saved["response"]
                model_used = saved.get("model_used", model_uri)
                if saved.get("translations") is not None:
                    raw_text = json.dumps(saved["translations"], ensure_ascii=False)
                else:
                    raw_text = extract_response_text(response)
                validated = parse_and_validate_field_map(raw_text, field_ids)
                TranslationPlan(source=source, fields=tuple(chunk)).assemble(validated)
                failures = saved.get("failed_attempts", [])
            else:
                def request_chunk() -> tuple[str, dict[str, Any], str]:
                    return complete_with_fallback(
                        primary_model=model_uri,
                        fallback_model=fallback_model,
                        complete_fn=complete,
                        request_kwargs={
                            "api_key": api_key,
                            "folder_id": folder_id,
                            "prompt": prompt,
                            "schema": schema,
                            "max_tokens": 6_000,
                            "timeout_seconds": 45,
                        },
                    )

                def validate_chunk(raw_text: str) -> dict[str, str]:
                    result = parse_and_validate_field_map(raw_text, field_ids)
                    TranslationPlan(source=source, fields=tuple(chunk)).assemble(result)
                    return result

                try:
                    validated, response, model_used, failures = complete_validated_with_retries(
                        request_fn=request_chunk,
                        validate_fn=validate_chunk,
                        max_attempts=3 if experimental_recovery else 1,
                    )
                except ContractError as group_error:
                    if not experimental_recovery:
                        raise
                    validated = {}
                    split_responses = []
                    failures = [{"group_error": str(group_error), "split_into_fields": True}]
                    models_used = []
                    for field in chunk:
                        single_ids = [field.field_id]
                        single_schema = build_field_map_schema(
                            single_ids,
                            required_tokens={
                                field.field_id: [atom.token for atom in field.atoms]
                            },
                        )
                        single_prompt = build_prompt(source_path, [field])

                        def request_field() -> tuple[str, dict[str, Any], str]:
                            return complete_with_fallback(
                                primary_model=model_uri,
                                fallback_model=fallback_model,
                                complete_fn=complete,
                                request_kwargs={
                                    "api_key": api_key,
                                    "folder_id": folder_id,
                                    "prompt": single_prompt,
                                    "schema": single_schema,
                                    "max_tokens": 6_000,
                                    "timeout_seconds": 45,
                                },
                            )

                        def validate_field(raw_text: str) -> dict[str, str]:
                            result = parse_and_validate_field_map(raw_text, single_ids)
                            TranslationPlan(source=source, fields=(field,)).assemble(result)
                            return result

                        result, single_response, single_model, single_failures = (
                            complete_validated_with_retries(
                                request_fn=request_field,
                                validate_fn=validate_field,
                                max_attempts=3,
                            )
                        )
                        validated.update(result)
                        models_used.append(single_model)
                        split_responses.append(
                            {
                                "field_ids": single_ids,
                                "model_used": single_model,
                                "failed_attempts": single_failures,
                                "response": single_response,
                            }
                        )
                    response = {"split": True, "parts": split_responses}
                    model_used = ",".join(dict.fromkeys(models_used))
            translations.update(validated)
            if not reused:
                _write_json(
                    output_root,
                    response_path,
                    {
                        "field_ids": field_ids,
                        "prompt": prompt,
                        "model_used": model_used,
                        "failed_attempts": failures,
                        "translations": validated if response.get("split") else None,
                        "response": response,
                    },
                )
            file_record["chunks"].append(
                {
                    "index": chunk_index,
                    "field_ids": field_ids,
                    "response_file": str(response_path),
                    "model_used": model_used,
                    "reused": reused,
                    "usage": response.get("result", {}).get("usage", {}),
                }
            )
            print(
                f"translated file {file_index}/{len(SOURCE_PATHS)}, "
                f"chunk {chunk_index}/{len(chunks)}",
                flush=True,
            )

        candidate = plan.assemble(translations)
        candidate_path = Path("candidate") / source_path.replace("/ru/", "/en/")
        _write_text(output_root, candidate_path, candidate)
        file_record["candidate_path"] = str(candidate_path)
        manifest["files"].append(file_record)

    _write_json(output_root, Path("manifest.json"), manifest)
    return output_root
