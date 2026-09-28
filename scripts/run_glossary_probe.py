#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from pr_translation_smoke.glossary_probe import (
    _OPAQUE_TOKEN,
    build_glossary_prompt,
    mask_opaque_fragments,
    restore_opaque_fragments,
    split_markdown_chunks,
)
from pr_translation_smoke.yandex_openai_client import complete_markdown

SOURCE_PATH = "ydb/docs/ru/core/concepts/glossary.md"
DEFAULT_REF = "dd2b2f3bff7e634b043f3a35c378db6510787178"


def fetch_source(ref: str) -> str:
    url = f"https://raw.githubusercontent.com/ydb-platform/ydb/{ref}/{SOURCE_PATH}"
    request = urllib.request.Request(url, headers={"User-Agent": "ydbdoc-review-ng-probe"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode("utf-8")


def required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"required environment variable is not set: {name}")
    return value


def main() -> int:
    if os.environ.get("YDBDOC_LIVE") != "1":
        raise RuntimeError("set YDBDOC_LIVE=1 to authorize the paid model call")
    ref = os.environ.get("YDBDOC_GLOSSARY_REF", DEFAULT_REF)
    model = os.environ.get("YDBDOC_MODEL_TRANSLATE", "deepseek-v4-flash")
    source = fetch_source(ref)
    masked, fragments = mask_opaque_fragments(source)
    chunks = split_markdown_chunks(
        masked, max_chars=int(os.environ.get("YDBDOC_GLOSSARY_CHUNK_CHARS", "12000"))
    )
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output = Path("artifacts") / "glossary-probe" / run_id
    output.mkdir(parents=True, exist_ok=False)
    (output / "source.md").write_text(source, encoding="utf-8")
    (output / "masked.md").write_text(masked, encoding="utf-8")
    responses: list[str] = []
    raw_responses: list[dict[str, object]] = []
    prompts: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        chunk_tokens = set(_OPAQUE_TOKEN.findall(chunk))
        chunk_fragments = tuple(item for item in fragments if item.token in chunk_tokens)
        prompt = build_glossary_prompt(
            source_path=f"{SOURCE_PATH} (chunk {index}/{len(chunks)})",
            source_locale="ru",
            target_locale="en",
            masked_markdown=chunk,
        )
        last_error: ValueError | None = None
        for attempt in range(1, 3):
            current_prompt = prompt
            if last_error is not None:
                current_prompt += (
                    "\n\nYour previous answer was rejected. Return the same complete chunk "
                    "and do not lose these tokens: "
                    + ", ".join(item.token for item in chunk_fragments)
                    + "."
                )
            response, raw_response = complete_markdown(
                api_key=required_env("YC_API_KEY"),
                folder_id=required_env("YC_FOLDER_ID"),
                model_uri=model,
                prompt=current_prompt,
                max_tokens=int(os.environ.get("YDBDOC_GLOSSARY_MAX_TOKENS", "24000")),
                timeout_seconds=300,
            )
            chunk_root = output / "chunks"
            chunk_root.mkdir(exist_ok=True)
            (chunk_root / f"{index:02d}-{attempt:02d}.prompt.txt").write_text(
                current_prompt, encoding="utf-8"
            )
            (chunk_root / f"{index:02d}-{attempt:02d}.response.md").write_text(
                response, encoding="utf-8"
            )
            (chunk_root / f"{index:02d}-{attempt:02d}.response.json").write_text(
                json.dumps(raw_response, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            try:
                restore_opaque_fragments(response, chunk_fragments)
            except ValueError as error:
                last_error = error
                if attempt == 2:
                    (output / "failure.txt").write_text(str(error) + "\n", encoding="utf-8")
                    raise
                continue
            responses.append(response)
            raw_responses.append(raw_response)
            prompts.append(current_prompt)
            break
    restored_chunks = [
        restore_opaque_fragments(response, tuple(item for item in fragments if item.token in set(_OPAQUE_TOKEN.findall(chunk))))
        for chunk, response in zip(chunks, responses, strict=True)
    ]
    restored = "".join(restored_chunks)
    (output / "prompt.txt").write_text("\n\n".join(prompts), encoding="utf-8")
    (output / "response.md").write_text("\n\n".join(responses), encoding="utf-8")
    (output / "restored.md").write_text(restored, encoding="utf-8")
    (output / "response.json").write_text(
        json.dumps(raw_responses, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output / "metadata.json").write_text(
        json.dumps(
            {
                "source_path": SOURCE_PATH,
                "source_ref": ref,
                "model": model,
                "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                "opaque_fragments": len(fragments),
                "chunks": len(chunks),
                "source_chars": len(source),
                "response_chars": sum(len(item) for item in responses),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
