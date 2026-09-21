import json

from pr_translation_smoke.single_field_runner import run_single_field_live


def test_live_runner_translates_each_field_in_an_independent_call(tmp_path, monkeypatch):
    monkeypatch.setenv("YDBDOC_LIVE", "1")
    monkeypatch.setenv("YC_API_KEY", "key")
    monkeypatch.setenv("YC_FOLDER_ID", "folder")
    monkeypatch.setenv("YDBDOC_MODEL_TRANSLATE", "model")
    calls = []

    def fake_complete(**kwargs):
        payload = json.loads(kwargs["prompt"].split("\n", 1)[1])
        calls.append(payload)
        field_id, source = next(iter(payload["translate_only"].items()))
        translated = {"Первое": "First", "Второе": "Second"}[source]
        raw = json.dumps({field_id: translated})
        response = {
            "result": {
                "alternatives": [
                    {
                        "status": "ALTERNATIVE_STATUS_FINAL",
                        "message": {"role": "assistant", "text": raw},
                    }
                ],
                "usage": {"completionTokensDetails": {"reasoningTokens": "0"}},
            }
        }
        return raw, response

    output_root = run_single_field_live(
        tmp_path,
        source_paths=("docs/a.md",),
        fetch_fn=lambda path: "Первое\nВторое\n",
        complete_fn=fake_complete,
        workers=2,
    )

    candidate = (output_root / "candidate/docs/a.md").read_text(encoding="utf-8")
    responses = sorted((output_root / "responses").glob("*.json"))

    assert candidate == "First\nSecond\n"
    assert len(calls) == 2
    assert all(len(call["translate_only"]) == 1 for call in calls)
    assert len(responses) == 2
    assert all(
        json.loads(path.read_text())["response"]["result"]["usage"]
        ["completionTokensDetails"]["reasoningTokens"]
        == "0"
        for path in responses
    )


def test_saved_prompt_is_the_exact_compact_prompt_sent_to_model(tmp_path, monkeypatch):
    monkeypatch.setenv("YDBDOC_LIVE", "1")
    monkeypatch.setenv("YC_API_KEY", "key")
    monkeypatch.setenv("YC_FOLDER_ID", "folder")
    monkeypatch.setenv("YDBDOC_MODEL_TRANSLATE", "model")
    sent_prompts = []

    def fake_complete(**kwargs):
        sent_prompts.append(kwargs["prompt"])
        payload = json.loads(kwargs["prompt"].split("\n", 1)[1])
        field_id = next(iter(payload["translate_only"]))
        reference = __import__("re").search(
            r"__REF_1_[A-Za-z0-9_]+__", payload["translate_only"][field_id]
        ).group()
        raw = json.dumps({field_id: f"First {reference}"})
        return raw, {"result": {"usage": {}}}

    output_root = run_single_field_live(
        tmp_path,
        source_paths=("docs/a.md",),
        fetch_fn=lambda path: "Первое `code`\n",
        complete_fn=fake_complete,
        workers=1,
    )

    saved = json.loads(next((output_root / "responses").glob("*.json")).read_text())

    assert "__REF_1_CODE_code__" in sent_prompts[0]
    assert saved["prompt"] == sent_prompts[0]


def test_runner_retries_one_rejected_model_response_and_records_it(tmp_path, monkeypatch):
    monkeypatch.setenv("YDBDOC_LIVE", "1")
    monkeypatch.setenv("YC_API_KEY", "key")
    monkeypatch.setenv("YC_FOLDER_ID", "folder")
    monkeypatch.setenv("YDBDOC_MODEL_TRANSLATE", "model")
    calls = 0

    def fake_complete(**kwargs):
        nonlocal calls
        calls += 1
        payload = json.loads(kwargs["prompt"].split("\n", 1)[1])
        field_id = next(iter(payload["translate_only"]))
        value = "Не переведено" if calls == 1 else "Translated"
        raw = json.dumps({field_id: value}, ensure_ascii=False)
        return raw, {"result": {"usage": {}}}

    output_root = run_single_field_live(
        tmp_path,
        source_paths=("docs/a.md",),
        fetch_fn=lambda path: "Первое\n",
        complete_fn=fake_complete,
        workers=1,
    )
    saved = json.loads(next((output_root / "responses").glob("*.json")).read_text())

    assert calls == 2
    assert saved["translation"] == "Translated"
    assert saved["failed_attempts"] == [
        {"attempt": 1, "error": "Cyrillic prose remains in field_0001"}
    ]


def test_runner_uses_reasoning_free_fallback_after_two_primary_rejections(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("YDBDOC_LIVE", "1")
    monkeypatch.setenv("YC_API_KEY", "key")
    monkeypatch.setenv("YC_FOLDER_ID", "folder")
    monkeypatch.setenv("YDBDOC_MODEL_TRANSLATE", "primary")
    monkeypatch.setenv("YDBDOC_MODEL_FALLBACK", "fallback")
    models = []

    def fake_complete(**kwargs):
        models.append(kwargs["model_uri"])
        payload = json.loads(kwargs["prompt"].split("\n", 1)[1])
        field_id = next(iter(payload["translate_only"]))
        value = "Не переведено" if kwargs["model_uri"] == "primary" else "Translated"
        raw = json.dumps({field_id: value}, ensure_ascii=False)
        return raw, {"result": {"usage": {}}}

    output_root = run_single_field_live(
        tmp_path,
        source_paths=("docs/a.md",),
        fetch_fn=lambda path: "Первое\n",
        complete_fn=fake_complete,
        fallback_complete_fn=fake_complete,
        workers=1,
    )
    saved = json.loads(next((output_root / "responses").glob("*.json")).read_text())

    assert models == ["primary", "primary", "fallback"]
    assert saved["strategy"] == "fallback-model"


def test_runner_allows_structurally_safe_reordering_after_strict_models_fail(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("YDBDOC_LIVE", "1")
    monkeypatch.setenv("YC_API_KEY", "key")
    monkeypatch.setenv("YC_FOLDER_ID", "folder")
    monkeypatch.setenv("YDBDOC_MODEL_TRANSLATE", "primary")
    monkeypatch.setenv("YDBDOC_MODEL_FALLBACK", "fallback")
    calls = []

    def fake_complete(**kwargs):
        calls.append((kwargs["model_uri"], "may reorder" in kwargs["prompt"]))
        payload = json.loads(kwargs["prompt"].split("\n", 1)[1])
        field_id, source = next(iter(payload["translate_only"].items()))
        refs = __import__("re").findall(r"__REF_\d+_[A-Za-z0-9_]+__", source)
        raw = json.dumps({field_id: f"Use {refs[1]} before {refs[0]}"})
        return raw, {"result": {"usage": {}}}

    output_root = run_single_field_live(
        tmp_path,
        source_paths=("docs/a.md",),
        fetch_fn=lambda path: "Используйте `one` и `two`\n",
        complete_fn=fake_complete,
        fallback_complete_fn=fake_complete,
        workers=1,
    )
    saved = json.loads(next((output_root / "responses").glob("*.json")).read_text())
    candidate = (output_root / "candidate/docs/a.md").read_text()

    assert calls == [
        ("primary", False),
        ("primary", False),
        ("fallback", False),
        ("primary", True),
    ]
    assert saved["strategy"] == "relaxed-reference-order"
    assert candidate == "Use `two` before `one`\n"
