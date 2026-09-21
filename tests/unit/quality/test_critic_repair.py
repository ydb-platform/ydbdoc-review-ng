from __future__ import annotations

import json

import pytest

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


def critic_json(verdict: str, findings: list[dict[str, object]]) -> str:
    return json.dumps({"verdict": verdict, "findings": findings}, ensure_ascii=False)


def finding(
    *,
    repairable: bool,
    snippet: str,
    line: int,
    field_ids: list[str] | None = None,
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
    assert PATH.value in executor.calls[0].prompt


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
    executor = FakeExecutor(
        critic_json(
            "RED", [finding(repairable=True, snippet="Установка YDB", line=1, field_ids=[field_id])]
        ),
        json.dumps({field_id: "Новая установка YDB"}),
        critic_json("RED", [finding(repairable=False, snippet="YDB", line=1)]),
    )
    result = review(executor)
    assert result.accepted_maps[0].as_dict() == {**values, field_id: "Новая установка YDB"}
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
    events: list[str] = []
    executor = FakeExecutor(
        primary,
        json.dumps({field_id: repaired_text}, ensure_ascii=False),
        critic_json("GREEN", []),
        events=events,
    )

    def before_final_critic(candidate: bytes) -> None:
        assert candidate == expected
        events.append("repair_validated_and_published")

    expected_values = dict(values)
    expected_values[field_id] = repaired_text
    expected = assemble_candidate(SOURCE, plan, request, expected_values)

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
    assert events == ["critic", "repair", "repair_validated_and_published", "final_critic"]
    assert expected.decode() in executor.calls[2].prompt
    assert target.decode() not in executor.calls[2].prompt


def test_repair_derives_current_field_values_from_actual_target() -> None:
    plan, request, _values, target = prepared()
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
        json.dumps({field_id: repaired_text}, ensure_ascii=False),
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
        json.dumps({description_id: "Corrected description"}),
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
    _plan, request, _values, _target = prepared()
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
        json.dumps({safe_id: repaired}),
        critic_json("RED", [finding(repairable=False, snippet="Установка", line=1)]),
    )

    result = review(executor)

    assert result.repair_attempted and result.repair_applied
    repair_request = executor.calls[1]
    schema = mutable_json(repair_request.schema)
    assert type(schema) is dict
    assert schema["required"] == [safe_id]
    assert unsafe_id not in repair_request.prompt
    assert SOURCE.decode() in repair_request.prompt
    assert "Прочитайте" in repair_request.prompt
    assert "Перевод пропускает обязательное условие." in repair_request.prompt
    assert request.fields[1].text in repair_request.prompt
    assert all(item.token in repair_request.prompt for item in request.fields[1].placeholders)
    assert SOURCE.decode() not in repr(repair_request)
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
    plan, request, _values, target = prepared()
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
        json.dumps({field_id: repaired_text}, ensure_ascii=False),
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


def test_invalid_placeholder_repair_retains_original_and_reports_assembly_error() -> None:
    _plan, request, _values, target = prepared()
    field_id = request.fields[1].field_id
    executor = FakeExecutor(
        critic_json(
            "RED",
            [finding(repairable=True, snippet="Прочитайте", line=3, field_ids=[field_id])],
        ),
        json.dumps({field_id: "Исправление без обязательных placeholders"}),
        critic_json("RED", [finding(repairable=False, snippet="Прочитайте", line=3)]),
    )

    result = review(executor)

    assert result.repair_error is RepairErrorReason.ASSEMBLY_FAILED
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
    assert result.repair_error is RepairErrorReason.ASSEMBLY_FAILED
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
    assert SOURCE.decode() not in repr(built)
    assert target.decode() not in repr(built)


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
