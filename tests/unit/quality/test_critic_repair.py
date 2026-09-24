from __future__ import annotations

import json
from collections.abc import Sequence

import pytest

from ydbdoc_review_ng.continuation import AcceptedMap
from ydbdoc_review_ng.domain import GitSha, Locale, RepoPath, RepositoryId, SnapshotRef
from ydbdoc_review_ng.models import AttemptError, ModelCallResult, ModelRequest
from ydbdoc_review_ng.models.types import mutable_json
from ydbdoc_review_ng.parser.markdown import build_markdown_plan
from ydbdoc_review_ng.plan import SourcePlan
from ydbdoc_review_ng.quality import (
    CriticResponseError,
    CriticResponseErrorReason,
    QualityExecutionError,
    QualityReviewResult,
    RepairErrorReason,
    Verdict,
    build_critic_request,
    parse_critic_response,
    review_translation,
)
from ydbdoc_review_ng.translation import (
    TranslationRequest,
    assemble_candidate,
    build_translation_request,
    prepare_document,
)

SNAPSHOT = SnapshotRef(RepositoryId("ydb-platform/ydb"), GitSha("a" * 40))
PATH = RepoPath("ydb/docs/ru/example.md")
SOURCE_PATH = RepoPath("ydb/docs/en/example.md")
SOURCE = b"# Install YDB\n\nRead [the guide](/docs/guide) before starting.\n"


class FakeExecutor:
    def __init__(self, *responses: str | None, events: list[str] | None = None) -> None:
        self.responses = iter(responses)
        self.calls: list[ModelRequest] = []
        self.events = events

    def invoke(self, request: ModelRequest, /) -> ModelCallResult:
        self.calls.append(request)
        if self.events is not None:
            self.events.append(request.role.value)
        response = next(self.responses)
        if response is None:
            return ModelCallResult(None, self._failure(), ())
        return ModelCallResult(response, None, ())

    @staticmethod
    def _failure() -> AttemptError:
        return AttemptError.TRANSPORT


def prepared() -> tuple[SourcePlan, TranslationRequest, dict[str, str], bytes]:
    plan = build_markdown_plan(SNAPSHOT, SOURCE_PATH, SOURCE)
    request = build_translation_request(SOURCE, plan)
    values = {
        request.fields[0].field_id: "Установка YDB",
        request.fields[1].field_id: request.fields[1]
        .text.replace("Read", "Прочитайте")
        .replace("the guide", "руководство")
        .replace("before starting", "до начала"),
    }
    return plan, request, values, assemble_candidate(SOURCE, plan, request, values)


def raw_document(candidate: bytes, path: RepoPath = PATH) -> str:
    plan = build_markdown_plan(SNAPSHOT, path, candidate)
    return prepare_document(candidate, plan, max_characters=100_000).chunks[0].text


def critic_json(verdict: str, findings: list[dict[str, object]]) -> str:
    return json.dumps({"verdict": verdict, "findings": findings}, ensure_ascii=False)


def finding(
    *,
    repairable: bool,
    snippet: str,
    line: int,
    field_ids: Sequence[object] | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "repairable": repairable,
        "reason": "Перевод пропускает обязательное условие.",
        "expected_correction": "Добавить пропущенное условие без изменения URL.",
        "searchable_snippet": snippet,
        "target_path": PATH.value,
        "target_line": line,
    }
    if field_ids is not None:
        value["field_ids"] = field_ids
    return value


def review(executor: FakeExecutor) -> QualityReviewResult:
    plan, request, _values, target = prepared()
    return review_translation(
        executor,
        model="yandexgpt-5.1/latest",
        source=SOURCE,
        source_plan=plan,
        translation_request=request,
        target=target,
        target_path=PATH,
        source_locale=Locale.EN,
        target_locale=Locale.RU,
    )


def test_green_uses_one_critic_and_does_not_attempt_repair() -> None:
    executor = FakeExecutor(critic_json("GREEN", []))

    result = review(executor)

    assert result.primary.verdict is Verdict.GREEN
    assert result.final.verdict is Verdict.GREEN
    assert result.final is result.primary
    assert not result.repair_attempted
    assert not result.repair_applied
    assert result.original_candidate == result.final_candidate
    assert result.repaired_candidate is None
    assert [call.role.value for call in executor.calls] == ["critic"]
    assert executor.calls[0].target_path == PATH
    assert PATH.value in executor.calls[0].prompt


@pytest.mark.parametrize("invalid_placeholder", [False, True])
def test_full_document_repair_restores_source_fragments_before_exposing_map(invalid_placeholder):
    plan, request, accepted, target = prepared()
    repaired = {
        request.fields[0].field_id: "Установка базы данных YDB",
        request.fields[1].field_id: accepted[request.fields[1].field_id].replace(
            "до начала", "до начала работы"
        ),
    }
    repaired_candidate = assemble_candidate(SOURCE, plan, request, repaired)
    raw_repair = raw_document(repaired_candidate)
    if invalid_placeholder:
        raw_repair += " [[YDBDOC_PROTECTED_9999]]"
    executor = FakeExecutor(
        critic_json(
            "RED",
            [
                finding(
                    repairable=True,
                    snippet="Установка",
                    line=1,
                    field_ids=[request.fields[0].field_id],
                )
            ],
        ),
        raw_repair,
        critic_json("GREEN", []),
    )
    published_maps = []
    result = review_translation(
        executor,
        model="yandexgpt-5.1/latest",
        source=SOURCE,
        source_plan=plan,
        translation_request=request,
        target=target,
        target_path=PATH,
        source_locale=Locale.EN,
        target_locale=Locale.RU,
        accepted_map=AcceptedMap(PATH, tuple(sorted(accepted.items()))),
        full_repair=True,
        operator_context="Private guidance",
        before_repaired_map=published_maps.append,
    )
    repair_prompt = executor.calls[1].prompt
    assert executor.calls[1].schema is None
    assert all(field.field_id not in repair_prompt for field in request.fields)
    assert "<authoritative-source>" in repair_prompt
    assert "<current-target>" in repair_prompt
    assert "/docs/guide" not in repair_prompt
    assert [call.role.value for call in executor.calls] == ["critic", "repair", "final_critic"]
    assert all(call.target_path == PATH for call in executor.calls)
    assert all("Private guidance" in call.prompt for call in executor.calls)
    if invalid_placeholder:
        assert result.repair_error is RepairErrorReason.INVALID_RESPONSE
        assert result.final_candidate == target
        assert published_maps == []
        assert result.accepted_maps == (AcceptedMap(PATH, tuple(sorted(accepted.items()))),)
    else:
        assert result.repair_applied
        assert (
            result.final_candidate
            == (
                "# Установка базы данных YDB\n\n"
                "Прочитайте [руководство](/docs/guide) до начала работы.\n"
            ).encode()
        )
        assert published_maps == [AcceptedMap(PATH, tuple(sorted(repaired.items())))]


def test_repair_transport_failure_is_terminal_before_final_critic() -> None:
    _plan, request, _values, _target = prepared()
    executor = FakeExecutor(
        critic_json(
            "RED",
            [
                finding(
                    repairable=True,
                    snippet="Установка YDB",
                    line=1,
                    field_ids=[request.requested_ids[0]],
                )
            ],
        ),
        None,
        critic_json("RED", [finding(repairable=False, snippet="Установка YDB", line=1)]),
    )
    with pytest.raises(QualityExecutionError, match="repair"):
        review(executor)
    assert [call.role.value for call in executor.calls] == ["critic", "repair"]


def test_quality_returns_final_validated_map_including_repaired_values() -> None:
    plan, request, values, _target = prepared()
    field_id = request.requested_ids[0]
    repaired_values = {**values, field_id: "Новая установка YDB"}
    executor = FakeExecutor(
        critic_json(
            "RED", [finding(repairable=True, snippet="Установка YDB", line=1, field_ids=[field_id])]
        ),
        raw_document(assemble_candidate(SOURCE, plan, request, repaired_values)),
        critic_json("RED", [finding(repairable=False, snippet="YDB", line=1)]),
    )
    result = review(executor)
    assert result.accepted_maps[0].as_dict() == repaired_values
    assert (
        assemble_candidate(SOURCE, plan, request, result.accepted_maps[0].as_dict())
        == result.final_candidate
    )


def test_exhausted_job_repair_allowance_still_reports_other_document_findings() -> None:
    plan, request, _values, target = prepared()
    problem = finding(
        repairable=True, snippet="Установка YDB", line=1, field_ids=[request.requested_ids[0]]
    )
    executor = FakeExecutor(critic_json("RED", [problem]))
    result = review_translation(
        executor,
        model="yandexgpt-5.1/latest",
        source=SOURCE,
        source_plan=plan,
        translation_request=request,
        target=target,
        target_path=PATH,
        source_locale=Locale.EN,
        target_locale=Locale.RU,
        allow_repair=False,
    )
    assert result.final.verdict is Verdict.RED
    assert len(result.final.findings) == 1
    assert not result.repair_attempted
    assert [call.role.value for call in executor.calls] == ["critic"]


def test_repairable_red_repairs_once_and_critics_actual_repaired_candidate() -> None:
    plan, request, values, target = prepared()
    field_id = request.fields[1].field_id
    primary = critic_json(
        "RED",
        [finding(repairable=True, snippet="Прочитайте", line=3, field_ids=[field_id])],
    )
    repaired_text = (
        request.fields[1]
        .text.replace("Read", "Обязательно прочитайте")
        .replace("the guide", "руководство")
        .replace("before starting", "до начала")
    )
    expected_values = dict(values)
    expected_values[field_id] = repaired_text
    expected = assemble_candidate(SOURCE, plan, request, expected_values)
    expected_plan = build_markdown_plan(SNAPSHOT, PATH, expected)
    raw_repair = prepare_document(
        expected, expected_plan, max_characters=100_000
    ).chunks[0].text
    events: list[str] = []
    executor = FakeExecutor(
        primary,
        raw_repair,
        critic_json("GREEN", []),
        events=events,
    )

    def before_final_critic(candidate: bytes) -> None:
        assert candidate == expected
        events.append("repair_validated_and_published")

    result = review_translation(
        executor,
        model="model",
        source=SOURCE,
        source_plan=plan,
        translation_request=request,
        target=target,
        target_path=PATH,
        source_locale=Locale.EN,
        target_locale=Locale.RU,
        before_final_critic=before_final_critic,
    )

    assert result.original_candidate == target
    assert result.repaired_candidate == expected
    assert result.final_candidate == expected
    assert result.repair_attempted and result.repair_applied
    assert result.repair_error is None
    assert result.primary.verdict is Verdict.RED
    assert result.final.verdict is Verdict.GREEN
    assert [call.role.value for call in executor.calls] == ["critic", "repair", "final_critic"]
    assert executor.calls[1].schema is None
    assert field_id not in executor.calls[1].prompt
    assert "<authoritative-source>" in executor.calls[1].prompt
    assert "<current-target>" in executor.calls[1].prompt
    assert "Перевод пропускает обязательное условие." in executor.calls[1].prompt
    assert events == ["critic", "repair", "repair_validated_and_published", "final_critic"]
    assert expected.decode() in executor.calls[2].prompt
    assert target.decode() not in executor.calls[2].prompt


@pytest.mark.parametrize(
    ("source", "translated", "source_locale", "target_locale", "direction"),
    [
        (b"# Install YDB\n", "Установите YDB", Locale.EN, Locale.RU, "en -> ru"),
        (
            "# Установите YDB\n".encode(),
            "Install YDB",
            Locale.RU,
            Locale.EN,
            "ru -> en",
        ),
    ],
)
def test_raw_repair_prompt_uses_actual_translation_direction(
    source: bytes,
    translated: str,
    source_locale: Locale,
    target_locale: Locale,
    direction: str,
) -> None:
    plan = build_markdown_plan(SNAPSHOT, SOURCE_PATH, source)
    request = build_translation_request(source, plan)
    field_id = request.requested_ids[0]
    target = assemble_candidate(source, plan, request, {field_id: translated})
    repaired = assemble_candidate(source, plan, request, {field_id: translated + " correctly"})
    executor = FakeExecutor(
        critic_json(
            "RED",
            [
                finding(
                    repairable=True,
                    snippet=translated,
                    line=1,
                    field_ids=[field_id],
                )
            ],
        ),
        raw_document(repaired),
        critic_json("GREEN", []),
    )

    result = review_translation(
        executor,
        model="model",
        source=source,
        source_plan=plan,
        translation_request=request,
        target=target,
        target_path=PATH,
        source_locale=source_locale,
        target_locale=target_locale,
    )

    assert result.final_candidate == repaired
    assert direction in executor.calls[1].prompt


def test_oversized_repair_uses_minimum_ordered_structural_chunks_within_limit() -> None:
    source = b"\n\n".join(
        f"## Source section {number} " .encode() + (b"detail " * 16)
        for number in range(1, 5)
    ) + b"\n"
    plan = build_markdown_plan(SNAPSHOT, SOURCE_PATH, source)
    request = build_translation_request(source, plan)
    target_values = {
        field.field_id: field.text.replace("Source section", "Target section")
        for field in request.fields
    }
    target = assemble_candidate(source, plan, request, target_values)
    first_id = request.requested_ids[0]
    limit = 1_450

    class ChunkExecutor:
        def __init__(self) -> None:
            self.calls: list[ModelRequest] = []

        def invoke(self, model_request: ModelRequest, /) -> ModelCallResult:
            self.calls.append(model_request)
            if model_request.role.value == "critic":
                return ModelCallResult(
                    critic_json(
                        "RED",
                        [
                            finding(
                                repairable=True,
                                snippet="Target section 1",
                                line=1,
                                field_ids=[first_id],
                            )
                        ],
                    ),
                    None,
                    (),
                )
            if model_request.role.value == "repair":
                current = model_request.prompt.split("<current-target>\n", 1)[1].split(
                    "</current-target>", 1
                )[0]
                return ModelCallResult(current, None, ())
            return ModelCallResult(critic_json("GREEN", []), None, ())

    executor = ChunkExecutor()
    result = review_translation(
        executor,
        model="model",
        source=source,
        source_plan=plan,
        translation_request=request,
        target=target,
        target_path=PATH,
        source_locale=Locale.EN,
        target_locale=Locale.RU,
        max_request_characters=limit,
    )

    repair_calls = [call for call in executor.calls if call.role.value == "repair"]
    assert result.final_candidate == target
    assert len(repair_calls) == 2
    assert all(call.schema is None for call in repair_calls)
    assert all(len(call.prompt) <= limit for call in repair_calls)
    assert "Source section 1" in repair_calls[0].prompt
    assert "Source section 4" in repair_calls[-1].prompt


def test_large_repair_uses_response_safe_chunks_and_restores_exact_target() -> None:
    source = "\n\n".join(
        f"Source section {number:03d} " + "x" * 769 for number in range(140)
    ).encode() + b"\n"
    assert 110_000 < len(source.decode()) < 112_000
    plan = build_markdown_plan(SNAPSHOT, SOURCE_PATH, source)
    request = build_translation_request(source, plan)
    target = assemble_candidate(
        source,
        plan,
        request,
        {
            field.field_id: field.text.replace("Source section", "Target section")
            for field in request.fields
        },
    )
    first_id = request.requested_ids[0]

    class LargeRepairExecutor:
        def __init__(self) -> None:
            self.calls: list[ModelRequest] = []
            self.critic_calls = 0

        def invoke(self, model_request: ModelRequest, /) -> ModelCallResult:
            self.calls.append(model_request)
            if model_request.role.value == "repair":
                current = model_request.prompt.split("<current-target>\n", 1)[1].split(
                    "</current-target>", 1
                )[0]
                return ModelCallResult(current, None, ())
            self.critic_calls += 1
            if self.critic_calls == 1:
                return ModelCallResult(
                    critic_json(
                        "RED",
                        [
                            finding(
                                repairable=True,
                                snippet="Target section 000",
                                line=1,
                                field_ids=[first_id],
                            )
                        ],
                    ),
                    None,
                    (),
                )
            return ModelCallResult(critic_json("GREEN", []), None, ())

    executor = LargeRepairExecutor()
    result = review_translation(
        executor,
        model="model",
        source=source,
        source_plan=plan,
        translation_request=request,
        target=target,
        target_path=PATH,
        source_locale=Locale.EN,
        target_locale=Locale.RU,
        max_request_characters=250_000,
    )

    repair_calls = tuple(call for call in executor.calls if call.role.value == "repair")
    target_chunks = tuple(
        call.prompt.split("<current-target>\n", 1)[1].split("</current-target>", 1)[0]
        for call in repair_calls
    )
    source_chunks = tuple(
        call.prompt.split("<authoritative-source>\n", 1)[1].split(
            "</authoritative-source>", 1
        )[0]
        for call in repair_calls
    )
    assert len(repair_calls) > 1
    assert all(call.max_tokens == 8_000 and call.schema is None for call in repair_calls)
    assert all(max(len(source_chunk), len(target_chunk)) <= 16_000 for source_chunk, target_chunk in zip(source_chunks, target_chunks, strict=True))
    assert all(
        max(len(source_left + source_right), len(target_left + target_right)) > 16_000
        for source_left, source_right, target_left, target_right in zip(
            source_chunks[:-1],
            source_chunks[1:],
            target_chunks[:-1],
            target_chunks[1:],
            strict=True,
        )
    )
    assert "".join(target_chunks).encode() == target
    assert result.final_candidate == target


def test_repair_derives_current_field_values_from_actual_target() -> None:
    plan, request, values, target = prepared()
    field_id = request.fields[1].field_id
    repaired_text = (
        request.fields[1]
        .text.replace("Read", "Прочитайте полностью")
        .replace("the guide", "руководство")
        .replace("before starting", "до начала")
    )
    executor = FakeExecutor(
        critic_json(
            "RED",
            [finding(repairable=True, snippet="Прочитайте", line=3, field_ids=[field_id])],
        ),
        raw_document(
            assemble_candidate(SOURCE, plan, request, {**values, field_id: repaired_text})
        ),
        critic_json("GREEN", []),
    )

    result = review_translation(
        executor,
        model="model",
        source=SOURCE,
        source_plan=plan,
        translation_request=request,
        target=target,
        target_path=PATH,
        source_locale=Locale.EN,
        target_locale=Locale.RU,
    )

    assert result.repair_applied
    assert result.final_candidate.startswith("# Установка YDB\n".encode())
    assert "Прочитайте полностью" in result.final_candidate.decode()


def test_repair_prompt_contains_complete_source_target_and_findings() -> None:
    source_sentinel = ("SOURCE-OUTSIDE-SELECTED-FIELD " * 500).strip()
    target_sentinel = ("TARGET-OUTSIDE-SELECTED-FIELD " * 500).strip()
    source = f"{source_sentinel}\n\nRepair [this field](/docs/selected).\n".encode()
    plan = build_markdown_plan(SNAPSHOT, SOURCE_PATH, source)
    request = build_translation_request(source, plan)
    assert len(request.fields) == 2
    selected = request.fields[1]
    current_translation = selected.text.replace("Repair", "Исправьте").replace(
        "this field", "это поле"
    )
    repaired_translation = current_translation.replace("Исправьте", "Точно исправьте")
    target = assemble_candidate(
        source,
        plan,
        request,
        {
            request.fields[0].field_id: target_sentinel,
            selected.field_id: current_translation,
        },
    )
    executor = FakeExecutor(
        critic_json(
            "RED",
            [
                finding(
                    repairable=True,
                    snippet="Исправьте",
                    line=3,
                    field_ids=[selected.field_id],
                )
            ],
        ),
        raw_document(
            assemble_candidate(
                source,
                plan,
                request,
                {
                    request.fields[0].field_id: target_sentinel,
                    selected.field_id: repaired_translation,
                },
            )
        ),
        critic_json("GREEN", []),
    )

    result = review_translation(
        executor,
        model="model",
        source=source,
        source_plan=plan,
        translation_request=request,
        target=target,
        target_path=PATH,
        source_locale=Locale.EN,
        target_locale=Locale.RU,
        operator_context="Use the operator's exact terminology.",
    )

    critic_prompt = executor.calls[0].prompt
    repair_request = executor.calls[1]
    repair_prompt = repair_request.prompt
    assert result.repair_applied
    assert source.decode() in critic_prompt
    assert target.decode() in critic_prompt
    assert repair_request.schema is None
    assert selected.field_id not in repair_prompt
    assert "Перевод пропускает обязательное условие." in repair_prompt
    assert "Добавить пропущенное условие без изменения URL." in repair_prompt
    assert PATH.value in repair_prompt
    assert "en -> ru" in repair_prompt
    assert "Use the operator's exact terminology." in repair_prompt
    assert source_sentinel in repair_prompt
    assert target_sentinel in repair_prompt
    assert "<authoritative-source>" in repair_prompt
    assert "<current-target>" in repair_prompt


def test_t017_n04_repair_preserves_logical_escaped_title_in_untouched_field() -> None:
    source = b'---\ntitle: "An \\"escaped\\" title"\ndescription: "Old description"\n---\n'
    plan = build_markdown_plan(SNAPSHOT, SOURCE_PATH, source)
    request = build_translation_request(source, plan)
    title_id, description_id = request.requested_ids
    target_values = {
        title_id: 'A "quoted" title',
        description_id: "Translated description",
    }
    target = assemble_candidate(source, plan, request, target_values)
    executor = FakeExecutor(
        critic_json(
            "RED",
            [
                finding(
                    repairable=True,
                    snippet="Translated description",
                    line=3,
                    field_ids=[description_id],
                )
            ],
        ),
        raw_document(
            assemble_candidate(
                source,
                plan,
                request,
                {title_id: 'A "quoted" title', description_id: "Corrected description"},
            )
        ),
        critic_json("GREEN", []),
    )

    result = review_translation(
        executor,
        model="model",
        source=source,
        source_plan=plan,
        translation_request=request,
        target=target,
        target_path=PATH,
        source_locale=Locale.EN,
        target_locale=Locale.RU,
    )

    assert result.repair_applied
    assert result.final_candidate == assemble_candidate(
        source,
        plan,
        request,
        {title_id: 'A "quoted" title', description_id: "Corrected description"},
    )
    assert [call.role.value for call in executor.calls] == ["critic", "repair", "final_critic"]


def test_unrepairable_red_uses_one_critic() -> None:
    executor = FakeExecutor(
        critic_json("RED", [finding(repairable=False, snippet="Прочитайте", line=3)])
    )

    result = review(executor)

    assert result.final.verdict is Verdict.RED
    assert not result.repair_attempted
    assert [call.role.value for call in executor.calls] == ["critic"]


def test_mixed_findings_repair_only_locally_safe_mapped_fields() -> None:
    plan, request, values, _target = prepared()
    safe_id = request.fields[1].field_id
    unsafe_id = request.fields[0].field_id
    primary = critic_json(
        "RED",
        [
            finding(repairable=True, snippet="Прочитайте", line=3, field_ids=[safe_id]),
            finding(repairable=True, snippet="не найдено", line=1, field_ids=[unsafe_id]),
            finding(repairable=False, snippet="Установка", line=1),
        ],
    )
    repaired = (
        request.fields[1]
        .text.replace("Read", "Прочитайте внимательно")
        .replace("the guide", "руководство")
        .replace("before starting", "до начала")
    )
    executor = FakeExecutor(
        primary,
        raw_document(
            assemble_candidate(SOURCE, plan, request, {**values, safe_id: repaired})
        ),
        critic_json("RED", [finding(repairable=False, snippet="Установка", line=1)]),
    )

    result = review(executor)

    assert result.repair_attempted and result.repair_applied
    repair_request = executor.calls[1]
    assert repair_request.schema is None
    assert unsafe_id not in repair_request.prompt
    assert SOURCE.decode() not in repair_request.prompt
    assert "Прочитайте" in repair_request.prompt
    assert "Перевод пропускает обязательное условие." in repair_request.prompt
    assert "<authoritative-source>" in repair_request.prompt
    assert result.final.verdict is Verdict.RED


def test_invalid_repair_retains_original_and_still_runs_one_final_critic() -> None:
    _plan, request, _values, target = prepared()
    field_id = request.fields[1].field_id
    executor = FakeExecutor(
        critic_json(
            "RED",
            [finding(repairable=True, snippet="Прочитайте", line=3, field_ids=[field_id])],
        ),
        '{"unexpected":"value"}',
        critic_json("RED", [finding(repairable=False, snippet="Прочитайте", line=3)]),
    )

    callback_candidates: list[bytes] = []
    plan, request, _values, _target = prepared()
    result = review_translation(
        executor,
        model="model",
        source=SOURCE,
        source_plan=plan,
        translation_request=request,
        target=target,
        target_path=PATH,
        source_locale=Locale.EN,
        target_locale=Locale.RU,
        before_final_critic=callback_candidates.append,
    )

    assert result.repair_attempted and not result.repair_applied
    assert result.repair_error is RepairErrorReason.INVALID_RESPONSE
    assert result.repaired_candidate is None
    assert result.final_candidate == target
    assert result.final.verdict is Verdict.RED
    assert [call.role.value for call in executor.calls] == ["critic", "repair", "final_critic"]
    assert callback_candidates == []
    assert target.decode() in executor.calls[2].prompt


def test_repair_callback_failure_prevents_final_critic() -> None:
    plan, request, values, target = prepared()
    field_id = request.fields[1].field_id
    repaired_text = (
        request.fields[1]
        .text.replace("Read", "Обязательно прочитайте")
        .replace("the guide", "руководство")
        .replace("before starting", "до начала")
    )
    executor = FakeExecutor(
        critic_json(
            "RED",
            [finding(repairable=True, snippet="Прочитайте", line=3, field_ids=[field_id])],
        ),
        raw_document(
            assemble_candidate(SOURCE, plan, request, {**values, field_id: repaired_text})
        ),
        critic_json("GREEN", []),
    )

    def fail_publication(_candidate: bytes) -> None:
        raise RuntimeError("publication failed")

    with pytest.raises(RuntimeError, match="publication failed"):
        review_translation(
            executor,
            model="model",
            source=SOURCE,
            source_plan=plan,
            translation_request=request,
            target=target,
            target_path=PATH,
            source_locale=Locale.EN,
            target_locale=Locale.RU,
            before_final_critic=fail_publication,
        )

    assert [call.role.value for call in executor.calls] == ["critic", "repair"]


def test_invalid_placeholder_repair_retains_original_and_reports_invalid_response() -> None:
    _plan, request, _values, target = prepared()
    field_id = request.fields[1].field_id
    executor = FakeExecutor(
        critic_json(
            "RED",
            [finding(repairable=True, snippet="Прочитайте", line=3, field_ids=[field_id])],
        ),
        "Исправление без обязательных placeholders",
        critic_json("RED", [finding(repairable=False, snippet="Прочитайте", line=3)]),
    )

    result = review(executor)

    assert result.repair_error is RepairErrorReason.INVALID_RESPONSE
    assert result.final_candidate == target
    assert len(executor.calls) == 3


def test_lone_surrogate_repair_retains_original_and_runs_final_critic() -> None:
    source = b"# Install YDB\n"
    plan = build_markdown_plan(SNAPSHOT, SOURCE_PATH, source)
    request = build_translation_request(source, plan)
    field_id = request.fields[0].field_id
    target = assemble_candidate(source, plan, request, {field_id: "Установите YDB"})
    executor = FakeExecutor(
        critic_json(
            "RED",
            [finding(repairable=True, snippet="Установите", line=1, field_ids=[field_id])],
        ),
        f'{{"{field_id}":"\\ud800"}}',
        critic_json("GREEN", []),
    )

    result = review_translation(
        executor,
        model="model",
        source=source,
        source_plan=plan,
        translation_request=request,
        target=target,
        target_path=PATH,
        source_locale=Locale.EN,
        target_locale=Locale.RU,
    )

    assert result.repaired_candidate is None
    assert result.final_candidate == target
    assert result.repair_error is RepairErrorReason.INVALID_RESPONSE
    assert [call.role.value for call in executor.calls] == ["critic", "repair", "final_critic"]


def test_repairable_but_unmapped_red_does_not_guess_a_repair_field() -> None:
    executor = FakeExecutor(
        critic_json("RED", [finding(repairable=True, snippet="Прочитайте", line=3)])
    )

    result = review(executor)

    assert result.final.verdict is Verdict.RED
    assert not result.repair_attempted
    assert len(executor.calls) == 1


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("{", CriticResponseErrorReason.MALFORMED_JSON),
        (
            '{"verdict":"GREEN","verdict":"RED","findings":[]}',
            CriticResponseErrorReason.DUPLICATE_KEY,
        ),
        ('{"verdict":"GREEN","findings":[],"extra":1}', CriticResponseErrorReason.UNEXPECTED_FIELD),
        ('{"verdict":"green","findings":[]}', CriticResponseErrorReason.INVALID_VERDICT),
        ('{"verdict":"GREEN","findings":[{}]}', CriticResponseErrorReason.INVALID_FINDING),
        ('{"verdict":"RED","findings":[]}', CriticResponseErrorReason.INCONSISTENT_RESULT),
        (
            '{"verdict":"GREEN","findings":[{"repairable":false,"reason":"x","expected_correction":"y","searchable_snippet":"z","target_path":"ydb/docs/ru/example.md","target_line":1}]}',
            CriticResponseErrorReason.INCONSISTENT_RESULT,
        ),
    ],
)
def test_critic_parser_rejects_malformed_extra_duplicate_and_inconsistent_results(
    raw: str, reason: CriticResponseErrorReason
) -> None:
    _plan, request, _values, _target = prepared()
    with pytest.raises(CriticResponseError) as caught:
        parse_critic_response(raw, target_path=PATH, requested_ids=request.requested_ids)
    assert caught.value.reason is reason
    assert raw not in str(caught.value)
    assert raw not in repr(caught.value)


@pytest.mark.parametrize(
    "finding_patch",
    [
        {"repairable": 1},
        {"reason": ["not", "text"]},
        {"expected_correction": ""},
        {"searchable_snippet": False},
        {"target_path": "ydb/docs/ru/other.md"},
        {"target_line": True},
        {"field_ids": "not-a-list"},
        {"field_ids": [1]},
        {"field_ids": ["unknown"]},
    ],
)
def test_critic_parser_rejects_wrong_finding_types_and_values(
    finding_patch: dict[str, object],
) -> None:
    _plan, request, _values, _target = prepared()
    item = finding(repairable=True, snippet="Прочитайте", line=3)
    item.update(finding_patch)
    raw = critic_json("RED", [item])

    with pytest.raises(CriticResponseError) as caught:
        parse_critic_response(raw, target_path=PATH, requested_ids=request.requested_ids)

    assert caught.value.reason is CriticResponseErrorReason.INVALID_FINDING
    assert "Прочитайте" not in str(caught.value)


def test_critic_parser_rejects_nested_duplicate_key() -> None:
    _plan, request, _values, _target = prepared()
    raw = (
        '{"verdict":"RED","findings":[{"repairable":true,"reason":"one",'
        '"reason":"two","expected_correction":"fix","searchable_snippet":"x",'
        f'"target_path":"{PATH.value}","target_line":1}}]}}'
    )
    with pytest.raises(CriticResponseError) as caught:
        parse_critic_response(raw, target_path=PATH, requested_ids=request.requested_ids)
    assert caught.value.reason is CriticResponseErrorReason.DUPLICATE_KEY


def test_critic_parser_rejects_repair_ids_on_unrepairable_finding() -> None:
    _plan, request, _values, _target = prepared()
    item = finding(
        repairable=False,
        snippet="Прочитайте",
        line=3,
        field_ids=[request.fields[1].field_id],
    )
    with pytest.raises(CriticResponseError) as caught:
        parse_critic_response(
            critic_json("RED", [item]),
            target_path=PATH,
            requested_ids=request.requested_ids,
        )
    assert caught.value.reason is CriticResponseErrorReason.INVALID_FINDING


@pytest.mark.parametrize("repairable", [False, True])
def test_critic_parser_accepts_empty_ids_when_repair_fields_exist(repairable: bool) -> None:
    _plan, request, _values, _target = prepared()

    result = parse_critic_response(
        critic_json(
            "RED",
            [finding(repairable=repairable, snippet="Прочитайте", line=3, field_ids=[])],
        ),
        target_path=PATH,
        requested_ids=request.requested_ids,
    )

    assert result.findings[0].field_ids == ()


def test_critic_parser_canonicalizes_duplicate_repair_ids_in_first_occurrence_order() -> None:
    _plan, request, _values, _target = prepared()
    first_id, second_id = request.requested_ids

    result = parse_critic_response(
        critic_json(
            "RED",
            [
                finding(
                    repairable=True,
                    snippet="Прочитайте",
                    line=3,
                    field_ids=[second_id, first_id, second_id],
                ),
                finding(
                    repairable=True,
                    snippet="Установка",
                    line=1,
                    field_ids=[first_id, first_id],
                ),
            ],
        ),
        target_path=PATH,
        requested_ids=request.requested_ids,
    )

    assert tuple(finding.field_ids for finding in result.findings) == (
        (second_id, first_id),
        (first_id,),
    )


def test_critic_duplicate_ids_reach_one_raw_document_repair() -> None:
    _plan, request, _values, target = prepared()
    second_id = request.requested_ids[1]
    executor = FakeExecutor(
        critic_json(
            "RED",
            [
                finding(
                    repairable=True,
                    snippet="Прочитайте",
                    line=3,
                    field_ids=[second_id, second_id],
                )
            ],
        ),
        raw_document(target),
        critic_json("GREEN", []),
    )

    result = review(executor)

    assert result.repair_applied
    assert result.primary.findings[0].field_ids == (second_id,)
    assert executor.calls[1].schema is None
    assert second_id not in executor.calls[1].prompt
    assert [call.role.value for call in executor.calls] == ["critic", "repair", "final_critic"]


@pytest.mark.parametrize(
    "field_ids",
    [
        ["unknown", "unknown"],
        [1, 1],
    ],
)
def test_critic_parser_still_rejects_invalid_duplicate_repair_ids(
    field_ids: list[object],
) -> None:
    _plan, request, _values, _target = prepared()
    item = finding(repairable=True, snippet="Прочитайте", line=3, field_ids=field_ids)

    with pytest.raises(CriticResponseError) as caught:
        parse_critic_response(
            critic_json("RED", [item]),
            target_path=PATH,
            requested_ids=request.requested_ids,
        )

    assert caught.value.reason is CriticResponseErrorReason.INVALID_FINDING


def test_critic_request_contains_full_source_target_and_link_purpose_boundary() -> None:
    _plan, request, _values, target = prepared()
    built = build_critic_request(
        model="model",
        source=SOURCE,
        target=target,
        target_path=PATH,
        source_locale=Locale.EN,
        target_locale=Locale.RU,
        requested_ids=request.requested_ids,
    )

    assert SOURCE.decode() in built.prompt
    assert target.decode() in built.prompt
    assert PATH.value in built.prompt
    assert "full meaning" in built.prompt
    assert "completeness" in built.prompt
    assert "terminology" in built.prompt
    assert "untranslated user-facing prose" in built.prompt
    assert "purpose and workability of links in context" in built.prompt
    assert "Do not rewrite URLs" in built.prompt
    assert "navigation resolver" in built.prompt
    assert "Always include field_ids in every finding" in built.prompt
    assert "Use [] when no safe exact field mapping exists" in built.prompt
    assert "List each field ID at most once" in built.prompt
    assert SOURCE.decode() not in repr(built)
    assert target.decode() not in repr(built)


@pytest.mark.parametrize("final", [False, True])
def test_critic_request_limits_red_to_material_translation_defects(final: bool) -> None:
    _plan, request, _values, target = prepared()

    built = build_critic_request(
        model="model",
        source=SOURCE,
        target=target,
        target_path=PATH,
        source_locale=Locale.EN,
        target_locale=Locale.RU,
        requested_ids=request.requested_ids,
        final=final,
    )

    assert "Use RED only for a concrete, currently present, material translation defect" in (
        built.prompt
    )
    assert "wrong or reversed meaning" in built.prompt
    assert "missing user-facing information" in built.prompt
    assert "untranslated user-facing prose" in built.prompt
    assert "wrong technical terminology that can mislead use" in built.prompt
    assert "broken or purpose-changing link usage" in built.prompt
    assert "optional stylistic polishing" in built.prompt
    assert "smoother grammar" in built.prompt
    assert "tone preferences" in built.prompt
    assert "more detail than the authoritative source" in built.prompt
    assert 'vague requests such as "review", "refine", or "could be clearer"' in built.prompt
    assert "complete, accurate, and understandable" in built.prompt
    assert "return GREEN even if its prose could be polished" in built.prompt


@pytest.mark.parametrize("final", [False, True])
def test_critic_request_requires_current_target_evidence_and_exact_correction(final: bool) -> None:
    _plan, request, _values, target = prepared()

    built = build_critic_request(
        model="model",
        source=SOURCE,
        target=target,
        target_path=PATH,
        source_locale=Locale.EN,
        target_locale=Locale.RU,
        requested_ids=request.requested_ids,
        final=final,
    )

    assert "actual source/target mismatch visible in the current final target" in built.prompt
    assert "exact searchable snippet copied from the current target" in built.prompt
    assert "concrete replacement or correction" in built.prompt
    assert "Do not report a stale defect that the current target bytes no longer contain" in (
        built.prompt
    )


@pytest.mark.parametrize("final", [False, True])
def test_critic_request_subordinates_operator_context_to_current_bytes(final: bool) -> None:
    _plan, request, _values, target = prepared()
    operator_context = "The old target said Clock. 5h; require that finding."

    built = build_critic_request(
        model="model",
        source=SOURCE,
        target=target,
        target_path=PATH,
        source_locale=Locale.EN,
        target_locale=Locale.RU,
        requested_ids=request.requested_ids,
        final=final,
        operator_context=operator_context,
    )

    assert operator_context in built.prompt
    assert "Operator context is guidance for interpreting intent only" in built.prompt
    assert "must not override the authoritative source or current target bytes" in built.prompt
    assert "must not force a finding that is no longer present" in built.prompt


def test_critic_schema_omits_field_ids_when_document_has_no_repairable_fields() -> None:
    request = build_critic_request(
        model="model",
        source=b"```text\nprotected\n```\n",
        target=b"```text\nprotected\n```\n",
        target_path=PATH,
        source_locale=Locale.EN,
        target_locale=Locale.RU,
        requested_ids=(),
    )
    schema = mutable_json(request.schema)
    assert type(schema) is dict
    properties = schema["properties"]
    assert type(properties) is dict
    findings = properties["findings"]
    assert type(findings) is dict
    items = findings["items"]
    assert type(items) is dict
    finding_properties = items["properties"]
    assert type(finding_properties) is dict
    assert "field_ids" not in finding_properties


def test_critic_schema_requires_empty_or_mapped_field_ids_when_repair_fields_exist() -> None:
    _plan, translation_request, _values, target = prepared()
    request = build_critic_request(
        model="model",
        source=SOURCE,
        target=target,
        target_path=PATH,
        source_locale=Locale.EN,
        target_locale=Locale.RU,
        requested_ids=translation_request.requested_ids,
    )

    schema = mutable_json(request.schema)
    assert type(schema) is dict
    findings = schema["properties"]["findings"]
    assert type(findings) is dict
    finding = findings["items"]
    assert type(finding) is dict
    assert "field_ids" in finding["required"]
    field_ids = finding["properties"]["field_ids"]
    assert type(field_ids) is dict
    assert "minItems" not in field_ids
    assert field_ids["uniqueItems"] is True


def test_critic_parser_rejects_field_ids_without_repair_fields() -> None:
    with pytest.raises(CriticResponseError) as caught:
        parse_critic_response(
            critic_json(
                "RED",
                [finding(repairable=False, snippet="protected", line=2, field_ids=[])],
            ),
            target_path=PATH,
            requested_ids=(),
        )

    assert caught.value.reason is CriticResponseErrorReason.UNEXPECTED_FIELD


def test_model_failure_and_malformed_critic_are_typed_and_non_echoing() -> None:
    secret = "SECRET-SOURCE-CANARY"
    plan = build_markdown_plan(SNAPSHOT, PATH, (secret + "\n").encode())
    request = build_translation_request((secret + "\n").encode(), plan)
    values = {request.requested_ids[0]: "Секрет"}
    target = assemble_candidate((secret + "\n").encode(), plan, request, values)

    with pytest.raises(QualityExecutionError) as failed:
        review_translation(
            FakeExecutor(None),
            model="model",
            source=(secret + "\n").encode(),
            source_plan=plan,
            translation_request=request,
            target=target,
            target_path=PATH,
            source_locale=Locale.EN,
            target_locale=Locale.RU,
        )
    assert secret not in str(failed.value)
    assert secret not in repr(failed.value)

    with pytest.raises(CriticResponseError) as malformed:
        review(FakeExecutor(secret))
    assert secret not in str(malformed.value)
    assert secret not in repr(malformed.value)
