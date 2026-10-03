#!/usr/bin/env python3
"""Live spike: Markdown-out translation with placeholders, no JSON segment map.

Not production CI. Set YDBDOC_LIVE=1 and Yandex credentials.

  export YDBDOC_LIVE=1
  export YANDEX_API_KEY=...   # or YANDEX_CLOUD_API_KEY_DOC_REVIEW
  export YANDEX_FOLDER_ID=... # or YANDEX_CLOUD_FOLDER_DOC_REVIEW
  python scripts/probe_markdown_placeholder_translate_live.py
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ydbdoc_review_ng.domain import (
    GitSha,
    ModelRole,
    RepoPath,
    RepositoryId,
    SnapshotRef,
)
from ydbdoc_review_ng.models import (
    AttemptResult,
    ExecutionConfig,
    ModelRequest,
    UrllibTransport,
    YandexCredentials,
    YandexOpenAIClient,
)
from ydbdoc_review_ng.parser.markdown import build_markdown_plan
from ydbdoc_review_ng.translation.document import (
    DocumentTranslationError,
    build_document_correction_note,
    build_document_prompt,
    prepare_document,
    restore_document,
)
from ydbdoc_review_ng.translation.language import validate_translated_prose

SOURCE = """\
# Представления {#predstavleniya}

{% include [intro](_includes/view-intro.md) %}

В документации YDB представления позволяют читать результат сохранённого запроса как таблицу. Администратор включает их настройкой `enable_views` в [динамической конфигурации](./devops/configuration-management/configuration-v1/dynamic-config#updating-dynamic-configuration).

```yaml
# Включить представления
enable_views: true
```

```yql
SELECT * FROM my_view;
```
"""

INCLUDE = "{% include [intro](_includes/view-intro.md) %}"
DEST = (
    "./devops/configuration-management/configuration-v1/"
    "dynamic-config#updating-dynamic-configuration"
)
YAML_KEY = "enable_views: true"
YQL = "SELECT * FROM my_view;"
_CYRILLIC_HEADING = re.compile(r"^#\s+.*[А-Яа-яЁё]")


def _map_aliases() -> None:
    if not os.environ.get("YANDEX_API_KEY", "").strip():
        alias = os.environ.get("YANDEX_CLOUD_API_KEY_DOC_REVIEW", "").strip()
        if alias:
            os.environ["YANDEX_API_KEY"] = alias
    if not os.environ.get("YANDEX_FOLDER_ID", "").strip():
        alias = os.environ.get("YANDEX_CLOUD_FOLDER_DOC_REVIEW", "").strip()
        if alias:
            os.environ["YANDEX_FOLDER_ID"] = alias


def _fail(message: str) -> None:
    raise SystemExit(f"FAIL: {message}")


def main() -> None:
    if os.environ.get("YDBDOC_LIVE", "").strip() != "1":
        raise SystemExit("set YDBDOC_LIVE=1")
    _map_aliases()
    key = os.environ.get("YANDEX_API_KEY", "").strip()
    folder = os.environ.get("YANDEX_FOLDER_ID", "").strip()
    if not key or not folder:
        raise SystemExit("need YANDEX_API_KEY and YANDEX_FOLDER_ID")

    source = SOURCE.encode("utf-8")
    path = RepoPath("ydb/docs/ru/core/concepts/datamodel/view-sample.md")
    plan = build_markdown_plan(
        SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a" * 40)),
        path,
        source,
    )
    prepared = prepare_document(source, plan)
    chunk = prepared.chunks[0]
    print(f"placeholders={len(prepared.placeholders)}", flush=True)
    print("--- prepared markdown ---", flush=True)
    print(chunk.text, flush=True)
    print("--- end prepared ---", flush=True)
    if INCLUDE in chunk.text:
        _fail("include leaked into model-visible markdown")
    if DEST in chunk.text:
        _fail("link destination leaked into model-visible markdown")
    if YQL in chunk.text:
        _fail("yql fence leaked into model-visible markdown")
    if not chunk.placeholders:
        _fail("expected protected placeholders")

    recorded: list[AttemptResult] = []

    def persist(attempt: AttemptResult) -> None:
        recorded.append(attempt)
        print(
            f"attempt status={attempt.status.value} http={attempt.http_status} "
            f"in={attempt.usage.input_tokens} out={attempt.usage.output_tokens}",
            flush=True,
        )

    timeout = float(os.environ.get("YDBDOC_MODEL_HTTP_TIMEOUT_SECONDS", "180"))
    client = YandexOpenAIClient(
        YandexCredentials(key, folder),
        UrllibTransport(),
        persist,
        execution=ExecutionConfig(max_attempts=2),
        timeout_seconds=timeout,
    )

    missing: tuple[str, ...] = ()
    previous: str | None = None
    response_text: str | None = None
    for attempt in (1, 2):
        prompt = build_document_prompt(
            chunk,
            "ru",
            "en",
            correction=attempt == 2,
            correction_note=(
                None
                if attempt == 1
                else build_document_correction_note(
                    source, chunk, prepared.placeholders, missing
                )
            ),
            previous_response=previous,
        )
        request = ModelRequest(
            ModelRole.TRANSLATE,
            "deepseek-v4-flash",
            prompt,
            None,
            developer_prompt=None,
            max_output_tokens=2048,
        )
        if request.schema is not None:
            _fail("markdown-out request must not set a JSON schema")
        print(
            f"request attempt={attempt} prompt_chars={len(prompt)} schema={request.schema}",
            flush=True,
        )
        result = client.invoke(request)
        if not result.success or not result.text:
            _fail(f"model call failed: {result.failure}")
        if result.text.lstrip().startswith("{") and '"document_chunk"' in result.text:
            _fail("model returned a JSON segment map instead of Markdown")
        previous = result.text
        print("--- model output ---", flush=True)
        print(result.text, flush=True)
        print("--- end model output ---", flush=True)
        try:
            validate_translated_prose(chunk, result.text, "ru", "en")
            restored = restore_document(source, plan, prepared, (result.text,))
        except DocumentTranslationError as error:
            if attempt == 1 and "placeholder_mismatch" in str(error):
                expected = tuple(item.token for item in prepared.placeholders)
                missing = tuple(token for token in expected if token not in result.text)
                print(f"retry after {error} missing={missing}", flush=True)
                continue
            _fail(str(error))
        response_text = result.text
        break
    else:
        _fail("translator retry exhausted")
    assert restored is not None
    assert response_text is not None

    out_dir = ROOT / "artifacts" / "markdown_placeholder_translate"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "prepared.md").write_text(chunk.text, encoding="utf-8")
    (out_dir / "model.md").write_text(response_text, encoding="utf-8")
    (out_dir / "restored.md").write_text(restored.decode("utf-8"), encoding="utf-8")

    text = restored.decode("utf-8")
    print("--- restored english ---", flush=True)
    print(text, flush=True)
    print("--- end restored ---", flush=True)

    if "[[YDBDOC_" in text:
        _fail("placeholders leaked into restored markdown")
    if INCLUDE not in text:
        _fail("include was not restored")
    if DEST not in text:
        _fail("link destination was not restored")
    if YAML_KEY not in text:
        _fail("yaml key was not restored")
    if YQL not in text:
        _fail("yql fence was not restored")
    if "{#predstavleniya}" not in text:
        _fail("explicit heading anchor was not restored")
    heading = next((line for line in text.splitlines() if line.startswith("# ")), "")
    if _CYRILLIC_HEADING.match(heading):
        _fail(f"heading still Russian: {heading}")
    words = [part for part in heading.lstrip("# ").split("{", 1)[0].split() if part]
    if words and not all(word[:1].isupper() or not word[0].isalpha() for word in words):
        _fail(f"heading is not Title Case: {heading}")
    lowered = text.lower()
    if "table" not in lowered and "view" not in lowered:
        _fail("translated prose missing expected YDB meaning")
    print("PASS markdown placeholder translate", flush=True)


if __name__ == "__main__":
    main()
