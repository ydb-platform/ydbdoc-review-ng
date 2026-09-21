from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from .contract import ContractError
from .plan import Field, TranslationPlan, build_plan
from .runner import (
    HEAD_SHA,
    PR_NUMBER,
    SOURCE_PATHS,
    _plan_record,
    _required_env,
    _write_json,
    _write_text,
    fetch_source,
)
from .single_field import (
    build_single_field_prompt,
    compact_model_field,
    translate_field,
    validate_single_field_translation,
)
from .yandex_client import complete
from .yandex_openai_client import complete as complete_openai


def _response_path(file_index: int, field: Field) -> Path:
    return Path("responses") / f"{file_index:02d}-{field.field_id}.json"


def run_single_field_live(
    output_parent: Path,
    *,
    source_paths: tuple[str, ...] = SOURCE_PATHS,
    fetch_fn: Callable[[str], str] = fetch_source,
    complete_fn: Any = complete,
    fallback_complete_fn: Any = complete_openai,
    workers: int = 4,
    resume_root: Path | None = None,
) -> Path:
    if os.environ.get("YDBDOC_LIVE") != "1":
        raise RuntimeError("set YDBDOC_LIVE=1 to authorize the paid live model calls")
    if workers < 1:
        raise ValueError("workers must be positive")

    api_key = _required_env("YC_API_KEY", "YDBDOC_YC_API_KEY")
    folder_id = _required_env("YC_FOLDER_ID")
    model_uri = _required_env("YDBDOC_MODEL_TRANSLATE")
    fallback_model_uri = os.environ.get("YDBDOC_MODEL_FALLBACK", "deepseek-v4-flash")
    if resume_root is None:
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-single")
        output_root = output_parent / f"pr-{PR_NUMBER}" / run_id
        output_root.mkdir(parents=True, exist_ok=False)
    else:
        output_root = resume_root
        if not output_root.is_dir():
            raise RuntimeError(f"resume directory does not exist: {output_root}")

    manifest: dict[str, Any] = {
        "pr": PR_NUMBER,
        "head_sha": HEAD_SHA,
        "algorithm": "one-field-per-call",
        "model_uri": model_uri,
        "fallback_model_uri": fallback_model_uri,
        "reasoning": "DISABLED",
        "files": [],
    }

    for file_index, source_path in enumerate(source_paths, start=1):
        source = fetch_fn(source_path)
        plan = build_plan(source)
        identity = {field.field_id: field.model_text for field in plan.fields}
        if not plan.fields or plan.assemble(identity) != source:
            raise ContractError(f"invalid translation plan for {source_path}")

        _write_text(output_root, Path("source") / source_path, source)
        _write_json(output_root, Path("plans") / f"{file_index:02d}.json", _plan_record(plan))

        translations: dict[str, str] = {}
        reorderable_fields: set[str] = set()
        pending: list[Field] = []
        for field in plan.fields:
            relative = _response_path(file_index, field)
            saved_path = output_root / relative
            if saved_path.is_file():
                saved = json.loads(saved_path.read_text(encoding="utf-8"))
                if saved.get("field_id") != field.field_id:
                    raise ContractError(f"saved response field mismatch: {relative}")
                if saved.get("strategy") == "atom-segments":
                    pending.append(field)
                    continue
                translated = validate_single_field_translation(
                    field, saved["translation"]
                )
                allow_reordering = saved.get("strategy") == "relaxed-reference-order"
                TranslationPlan(source=source, fields=(field,)).assemble(
                    {field.field_id: translated},
                    reorderable_fields={field.field_id} if allow_reordering else None,
                )
                translations[field.field_id] = translated
                if allow_reordering:
                    reorderable_fields.add(field.field_id)
            else:
                pending.append(field)

        def translate_pending(
            field: Field,
        ) -> tuple[Field, str, dict[str, Any], str, list[dict[str, Any]], str]:
            prompt = build_single_field_prompt(
                source_path=source_path,
                field=compact_model_field(field),
                before="",
                after="",
            )
            failed_attempts: list[dict[str, Any]] = []
            for attempt in (1, 2):
                try:
                    translated, response = translate_field(
                        source_path=source_path,
                        source=source,
                        field=field,
                        api_key=api_key,
                        folder_id=folder_id,
                        model_uri=model_uri,
                        complete_fn=complete_fn,
                    )
                    return field, translated, response, prompt, failed_attempts, "primary"
                except ContractError as error:
                    failed_attempts.append({"attempt": attempt, "error": str(error)})
            try:
                translated, response = translate_field(
                    source_path=source_path,
                    source=source,
                    field=field,
                    api_key=api_key,
                    folder_id=folder_id,
                    model_uri=fallback_model_uri,
                    complete_fn=fallback_complete_fn,
                )
                return (
                    field,
                    translated,
                    response,
                    prompt,
                    failed_attempts,
                    "fallback-model",
                )
            except ContractError as error:
                failed_attempts.append({"strategy": "fallback-model", "error": str(error)})
            if not field.atoms:
                raise ContractError(f"all translation strategies failed for {field.field_id}")
            relaxed_prompt = build_single_field_prompt(
                source_path=source_path,
                field=compact_model_field(field),
                before="",
                after="",
                allow_reference_reordering=True,
            )
            translated, response = translate_field(
                source_path=source_path,
                source=source,
                field=field,
                api_key=api_key,
                folder_id=folder_id,
                model_uri=model_uri,
                complete_fn=complete_fn,
                allow_reference_reordering=True,
            )
            return (
                field,
                translated,
                response,
                relaxed_prompt,
                failed_attempts,
                "relaxed-reference-order",
            )

        errors: list[str] = []
        completed_count = len(plan.fields) - len(pending)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(translate_pending, field): field for field in pending}
            for future in as_completed(futures):
                field = futures[future]
                try:
                    field, translated, response, prompt, failed_attempts, strategy = (
                        future.result()
                    )
                except Exception as error:
                    errors.append(f"{field.field_id}: {error}")
                    continue
                translations[field.field_id] = translated
                if strategy == "relaxed-reference-order":
                    reorderable_fields.add(field.field_id)
                _write_json(
                    output_root,
                    _response_path(file_index, field),
                    {
                        "field_id": field.field_id,
                        "prompt": prompt,
                            "translation": translated,
                            "failed_attempts": failed_attempts,
                            "strategy": strategy,
                            "response": response,
                    },
                )
                completed_count += 1
                print(
                    f"translated file {file_index}/{len(source_paths)}, "
                    f"field {completed_count}/{len(plan.fields)}",
                    flush=True,
                )

        if errors:
            _write_json(
                output_root,
                Path("errors") / f"{file_index:02d}.json",
                {"path": source_path, "errors": errors},
            )
            raise ContractError(
                f"{len(errors)} single-field calls failed for {source_path}; "
                f"resume from {output_root}"
            )

        candidate = plan.assemble(
            translations, reorderable_fields=reorderable_fields
        )
        candidate_path = Path("candidate") / source_path.replace("/ru/", "/en/")
        _write_text(output_root, candidate_path, candidate)
        manifest["files"].append(
            {
                "path": source_path,
                "field_count": len(plan.fields),
                "reorderable_fields": sorted(reorderable_fields),
                "candidate_path": str(candidate_path),
            }
        )

    _write_json(output_root, Path("manifest.json"), manifest)
    return output_root
