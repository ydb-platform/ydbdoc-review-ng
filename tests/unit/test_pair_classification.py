from __future__ import annotations

import json

import pytest

from ydbdoc_review_ng import pair_classification as pc
from ydbdoc_review_ng.continuation import normalize_source_inventory
from ydbdoc_review_ng.direction import InventoryFile
from ydbdoc_review_ng.domain import GitSha, Locale, RepoPath, RepositoryId, SnapshotRef
from ydbdoc_review_ng.locales import (
    LocalePairInventory,
    LocaleRoots,
    PairFileState,
    PairKey,
    SnapshotLocaleFile,
)
from ydbdoc_review_ng.models.types import ModelCallResult

BEFORE = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a" * 40))
AFTER = SnapshotRef(BEFORE.repository, GitSha("b" * 40))


def fixture(ru_old, ru_new, en_old, en_new, key="page.md"):
    roots = LocaleRoots(RepoPath("ru"), RepoPath("en"))
    pair_key = PairKey(RepoPath(key))
    files = []
    for locale, old, new in [("ru", ru_old, ru_new), ("en", en_old, en_new)]:
        if old != new:
            change = normalize_source_inventory(
                [
                    {
                        "filename": f"{locale}/{key}",
                        "status": "added" if old is None else "modified",
                        "changes": 1,
                    }
                ]
            ).files[0]
            files.append(InventoryFile(change, old, new, None, None))
    pair = LocalePairInventory(
        roots,
        pair_key,
        SnapshotLocaleFile(roots, Locale.RU, pair_key, RepoPath(f"ru/{key}"), AFTER, ru_new),
        SnapshotLocaleFile(roots, Locale.EN, pair_key, RepoPath(f"en/{key}"), AFTER, en_new),
        (),
        PairFileState.BOTH_PRESENT if en_new is not None else PairFileState.RU_ONLY,
    )
    return tuple(files), pair


def response(action, key="page.md"):
    return ModelCallResult(
        json.dumps({"key": key, "action": action, "reason": "Semantic evidence"}), None, ()
    )


def test_compact_request_preserves_complete_changed_blocks_and_counterpart():
    untouched = ("Unrelated paragraph.\n\n" * 2000).encode()
    files, pair = fixture(
        b"# H\n\nOld paragraph.\n\n" + untouched,
        b"# H\n\nChanged paragraph.\nSecond line.\n\n" + untouched,
        b"# Heading\n\nCounterpart current paragraph.\n\n" + untouched,
        b"# Heading\n\nCounterpart current paragraph.\n\n" + untouched,
    )
    request = pc.pair_request(files, pair, BEFORE, AFTER, "model")
    assert len(request.prompt.encode()) < 12000
    assert "Changed paragraph.\\nSecond line." in request.prompt
    assert "Counterpart current paragraph." in request.prompt
    assert request.max_output_tokens == 4096
    assert request.schema["additionalProperties"] is False


@pytest.mark.parametrize(
    "body",
    [
        "{}",
        '{"key":"page.md","action":"skip","reason":"x"}',
        '{"key":"other.md","action":"no_translation","reason":"x"}',
        '{"key":"page.md","action":"no_translation","reason":" "}',
        '{"key":"page.md","action":"no_translation","reason":"x","extra":1}',
        '{"key":"page.md","action":"conflict","action":"no_translation","reason":"x"}',
    ],
)
def test_strict_parser_rejects_malformed_classification(body):
    with pytest.raises(ValueError):
        pc.parse_pair_response(body, "page.md")


def test_bilingual_changes_are_classified_and_can_already_be_synchronized():
    files, pair = fixture(b"Old", b"New", b"Old EN", b"New EN")
    requests = []

    def invoke(request):
        requests.append(request)
        return response("no_translation")

    decisions = pc.classify_pairs(files, (pair,), BEFORE, AFTER, "model", invoke)
    assert decisions == (pc.PairDecision("page.md", "no_translation", "Semantic evidence"),)
    assert len(requests) == 1
    assert "already synchronized" in requests[0].prompt


def test_need_context_retries_once_then_preserves_checkpoint():
    files, pair = fixture(b"# H\n\nOld\n\nOther", b"# H\n\nNew\n\nOther", b"EN", b"EN")
    requests = []

    def invoke(request):
        requests.append(request)
        return response("need_context")

    result = pc.classify_pairs(files, (pair,), BEFORE, AFTER, "model", invoke)
    assert result[0].action == "need_context"
    assert len(requests) == 2
    assert "expanded_sections" in requests[1].prompt


def test_oversized_indivisible_block_is_checkpoint_not_truncated_model_input():
    files, pair = fixture(b"old", b"x" * 70000, b"EN", b"EN")

    def forbidden(request):
        pytest.fail("Oversized incomplete context must not be sent")

    result = pc.classify_pairs(files, (pair,), BEFORE, AFTER, "model", forbidden)
    assert result[0].action == "need_context"
    assert "bound" in result[0].reason


def test_invalid_model_output_never_silently_skips():
    files, pair = fixture(b"Old", b"New", b"EN", b"EN")
    result = pc.classify_pairs(
        files, (pair,), BEFORE, AFTER, "model", lambda _: ModelCallResult("{}", None, ())
    )
    assert result[0].action == "conflict"


@pytest.mark.parametrize(
    "block",
    [
        "```sql\nselect 1;\n\nselect 2;\n```",
        "- first\n\n  continuation\n- second",
        "| A | B |\n|---|---|\n| 1 | 2 |",
    ],
)
def test_complete_fence_list_and_table_are_retained(block):
    files, pair = fixture(
        ("# H\n\n" + block).encode(),
        ("# H\n\n" + block.replace("1", "9").replace("first", "updated")).encode(),
        b"EN",
        b"EN",
    )
    request = pc.pair_request(files, pair, BEFORE, AFTER, "model")
    payload = json.loads(request.prompt.split("\nPair evidence: ", 1)[1])
    expected = block.split("\n- second")[0]
    assert any(chunk["text"] == expected for chunk in payload["ru"]["before_blocks"])


def test_new_page_content_is_sent_once_and_in_full():
    files, pair = fixture(None, b"# New\n\nUnique full page.", None, None)
    request = pc.pair_request(files, pair, BEFORE, AFTER, "model")
    assert request.prompt.count("Unique full page.") == 1


def test_counterpart_anchor_finds_shifted_matching_block():
    prefix = b"# Other\n\n" + b"Unrelated\n\n" * 20
    files, pair = fixture(
        b"# H\n\n`setting` was 1.",
        b"# H\n\n`setting` is 2.",
        prefix + b"# Target\n\n`setting` is already 2.",
        prefix + b"# Target\n\n`setting` is already 2.",
    )
    request = pc.pair_request(files, pair, BEFORE, AFTER, "model")
    assert "`setting` is already 2." in request.prompt


def test_mixed_pair_decisions_are_independent():
    files_a, pair_a = fixture(b"Old", b"New", b"EN", b"EN", key="a.md")
    files_b, pair_b = fixture(b"RU", b"RU", b"Old", b"New", key="b.md")
    answers = iter([response("ru_to_en", "a.md"), response("en_to_ru", "b.md")])
    result = pc.classify_pairs(
        files_a + files_b, (pair_a, pair_b), BEFORE, AFTER, "model", lambda _: next(answers)
    )
    assert [(d.key, d.action) for d in result] == [("a.md", "ru_to_en"), ("b.md", "en_to_ru")]


# Exact changed paragraphs from ydb-platform/ydb PR #52478.
PR52478 = {
    "en_before": "Regardless of the [AUTO_PARTITIONING_PARTITION_SIZE_MB](#auto_partitioning_by_size) parameter value, {{ ydb-short-name }} performs partition splitting and merging based on a size of 2000 MB. At the same time, {{ ydb-short-name }} does not restrict user settings and works in parallel with them. For example, if you set the [partition size](#auto_partitioning_by_size) to 100 MB and the [limit](#auto_partitioning_max_partitions_count) to 10 partitions, the table stops splitting when it reaches 10 partitions of 100 MB each, and partition sizes begin to grow. When any partition exceeds 2000 MB, it splits, and the table has 11 partitions. If the size of two adjacent partitions drops to 1000 MB, they merge, and the table has 10 partitions again.",
    "en_after": "Regardless of the [AUTO_PARTITIONING_PARTITION_SIZE_MB](#auto_partitioning_by_size) parameter value, {{ ydb-short-name }} forcibly splits partitions whose size exceeds the [force_shard_split_data_size](../../../reference/configuration/immediate_controls_config.md) parameter (2 GiB by default). At the same time, {{ ydb-short-name }} does not restrict user settings and works in parallel with them. For example, if you set the [partition size](#auto_partitioning_by_size) to 100 MiB and the [limit](#auto_partitioning_max_partitions_count) to 10 partitions, the table stops splitting when it reaches 10 partitions of 100 MiB each, and partition sizes begin to grow. When any partition exceeds the `force_shard_split_data_size` parameter value, it splits, and the table has 11 partitions. If the combined size of two adjacent partitions drops to 50% of the [AUTO_PARTITIONING_PARTITION_SIZE_MB](#auto_partitioning_by_size) parameter value, they merge, and the table has 10 partitions again.",
    "ru_before": "Независимо от значения параметра [AUTO_PARTITIONING_PARTITION_SIZE_MB](#auto_partitioning_by_size), {{ ydb-short-name }} выполняет разделение и объединение партиций, ориентируясь на размер в 2000 MB. При этом {{ ydb-short-name }} не ограничивает пользовательские настройки, а работает параллельно с ними. Например, если задать [размер партиции](#auto_partitioning_by_size) 100 MB и [ограничение](#auto_partitioning_max_partitions_count) на 10 партиций, то, когда у таблицы станет 10 партиций по 100 MB, она перестанет делиться, и размер партиций начнет увеличиваться. Когда размер какой-либо партиции превысит 2000 MB, она разделится, и в таблице станет 11 партиций. Если размер двух соседних партиций уменьшится до 1000 MB, они объединятся, и в таблице снова станет 10 партиций.",
    "ru_after": "Независимо от значения параметра [AUTO_PARTITIONING_PARTITION_SIZE_MB](#auto_partitioning_by_size), {{ ydb-short-name }} принудительно разделяет партиции, размер которых превышает параметр [force_shard_split_data_size](../../../reference/configuration/immediate_controls_config.md) (по умолчанию 2 ГиБ). При этом {{ ydb-short-name }} не ограничивает пользовательские настройки, а работает параллельно с ними. Например, если задать [размер партиции](#auto_partitioning_by_size) 100 МиБ и [ограничение](#auto_partitioning_max_partitions_count) на 10 партиций, то, когда у таблицы станет 10 партиций по 100 МиБ, она перестанет делиться, и размер партиций начнет увеличиваться. Когда размер какой-либо партиции превысит значение параметра `force_shard_split_data_size`, она разделится, и в таблице станет 11 партиций. Если суммарный размер двух соседних партиций уменьшится до 50% от значения параметра [AUTO_PARTITIONING_PARTITION_SIZE_MB](#auto_partitioning_by_size), они объединятся, и в таблице снова станет 10 партиций.",
}


def test_pr52478_synchronized_paragraphs_scripted_provider():
    files, pair = fixture(
        *(PR52478[k].encode() for k in ("ru_before", "ru_after", "en_before", "en_after"))
    )
    requests = []

    def invoke(request):
        requests.append(request)
        payload = json.loads(request.prompt.split("\nPair evidence: ", 1)[1])
        assert payload["ru"]["after_blocks"][0]["text"] == PR52478["ru_after"]
        assert payload["en"]["after_blocks"][0]["text"] == PR52478["en_after"]
        return response("no_translation")

    result = pc.classify_pairs(files, (pair,), BEFORE, AFTER, "model", invoke)
    assert result[0].action == "no_translation"
    assert len(requests) == 1
    assert len(requests[0].prompt.encode()) < 20000


def test_removed_paragraph_anchors_find_shifted_counterpart():
    prefix = b"# Other\n\n" + b"Unrelated\n\n" * 20
    counterpart = prefix + b"# Target\n\n`removed_setting` is enabled."
    files, pair = fixture(
        b"# H\n\n`removed_setting` is enabled.\n\nRemaining.",
        b"# H\n\nRemaining.",
        counterpart,
        counterpart,
    )
    request = pc.pair_request(files, pair, BEFORE, AFTER, "model")
    assert (
        "`removed_setting` is enabled."
        in json.loads(request.prompt.split("\nPair evidence: ", 1)[1])["en"]["current_blocks"][-1][
            "text"
        ]
    )


def test_malformed_response_retries_same_compact_request_and_recovers(capsys):
    files, pair = fixture(b"Old", b"New", b"EN", b"EN")
    requests = []
    responses = iter([ModelCallResult("{}", None, ()), response("ru_to_en")])

    def invoke(request):
        requests.append(request)
        return next(responses)

    decisions = pc.classify_pairs(files, (pair,), BEFORE, AFTER, "model", invoke)
    assert decisions[0].action == "ru_to_en"
    assert len(requests) == 2 and requests[0] == requests[1]
    events = [
        json.loads(line.removeprefix("YDBDOC_TRACE "))
        for line in capsys.readouterr().err.splitlines()
    ]
    assert all(event["article"] == "page.md" for event in events)


def test_large_list_change_keeps_complete_item_without_unrelated_items():
    items = [f"- Unrelated entry {i}: " + "background " * 80 for i in range(150)]
    old = ("# Release\n\n" + "\n".join(items) + "\n\n").encode()
    new = old.replace(b"Unrelated entry 75:", b"Changed entry 75:")
    files, pair = fixture(old, new, old, new)
    request = pc.pair_request(files, pair, BEFORE, AFTER, "model")
    payload = json.loads(request.prompt.split("\nPair evidence: ", 1)[1])
    assert payload["ru"]["before_blocks"][0]["text"] == items[75]
    assert payload["ru"]["after_blocks"][0]["text"] == items[75].replace("Unrelated", "Changed")
    assert "Unrelated entry 0:" not in request.prompt


def test_large_list_item_keeps_nested_list_and_fenced_continuation():
    lead = "- Other entry " + "background " * 500 + "\n"
    item = "- Main item\n\n  continuation\n  - child\n\n  ```text\n- fence content\n  ```"
    blocks = pc._blocks(lead + item + "\n- Last item\n")
    assert any(block.text == item for block in blocks)


def batching_fixture():
    old = "# Changes\n\n" + "\n\n".join(
        f"- `setting_{i}` old " + "background " * 70 for i in range(12)
    )
    new = (
        old.replace("`setting_1` old", "`setting_1` new")
        .replace("- `setting_4` old " + "background " * 70, "")
        .replace("`setting_9` old", "`setting_9` new")
    )
    new += "\n\n- `added` new " + "background " * 70
    return fixture(old.encode(), new.encode(), old.encode(), new.encode())


def test_batches_cover_every_delta_once_including_insert_and_delete(monkeypatch):
    monkeypatch.setattr(pc, "MAX_REQUEST_BYTES", 14000)
    files, pair = batching_fixture()
    requests = []

    def invoke(request):
        requests.append(request)
        assert len(request.prompt.encode()) <= pc.MAX_REQUEST_BYTES
        return response("no_translation")

    decisions = pc.classify_pairs(files, (pair,), BEFORE, AFTER, "model", invoke)
    assert decisions[0].action == "no_translation"
    assert len(requests) > 1
    payloads = [json.loads(r.prompt.split("\nPair evidence: ", 1)[1]) for r in requests]
    for locale, item in zip(("ru", "en"), files):
        expected = pc._delta(item.before.decode(), item.after.decode())
        seen = [d for payload in payloads for d in payload[locale]["delta"]]
        assert sorted(seen, key=lambda d: d["old_start"]) == expected
        removed = [line for delta in seen for line in delta["removed"] if line.strip()]
        added = [line for delta in seen for line in delta["added"] if line.strip()]
        assert [line.split("`")[1] for line in removed] == ["setting_1", "setting_4", "setting_9"]
        assert [line.split("`")[1] for line in added] == ["setting_1", "setting_9", "added"]
        assert all(" old " in line for line in removed)
        assert all(" new " in line for line in added)
        for payload in payloads:
            side = payload[locale]
            for delta in side["delta"]:
                for kind, lines in [
                    ("before_blocks", delta["removed"]),
                    ("after_blocks", delta["added"]),
                ]:
                    text = "\n".join(b["text"] for b in side[kind])
                    assert all(not line.strip() or line in text for line in lines)


@pytest.mark.parametrize(
    "actions,expected",
    [
        (["no_translation"], "no_translation"),
        (["ru_to_en", "no_translation"], "ru_to_en"),
        (["ru_to_en", "en_to_ru"], "conflict"),
        (["no_translation", "conflict"], "conflict"),
        (["need_context"], "need_context"),
    ],
)
def test_batch_decisions_are_aggregated_conservatively(monkeypatch, actions, expected):
    monkeypatch.setattr(pc, "MAX_REQUEST_BYTES", 14000)
    files, pair = batching_fixture()
    calls = []

    def invoke(request):
        action = actions[min(len(calls), len(actions) - 1)]
        calls.append(request)
        return response(action)

    result = pc.classify_pairs(files, (pair,), BEFORE, AFTER, "model", invoke)
    assert len(calls) > 1
    assert result[0].action == expected


def test_list_item_fence_with_trailing_text_does_not_close():
    text = "- Main\n  ```text\n  ```not-a-close\n- inside fence\n  ```\n- Next"
    block = pc._Block(0, 7, text, ("# H",), 0)
    items = pc._list_items(block)
    assert [item.text for item in items] == [text.rsplit("\n- Next", 1)[0], "- Next"]
