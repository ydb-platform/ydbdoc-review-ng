import json

from pr_translation_smoke.plan import Field
from pr_translation_smoke.contract import ContractError
from pr_translation_smoke.runner import (
    build_prompt,
    chunk_fields,
    complete_validated_with_retries,
    complete_with_fallback,
)


def _field(field_id: str, text: str) -> Field:
    return Field(field_id, 0, len(text), text, text, ())


def test_chunking_keeps_fields_whole_and_respects_both_limits():
    fields = [_field("f1", "aaaa"), _field("f2", "bbbb"), _field("f3", "cccc")]

    chunks = chunk_fields(fields, max_source_chars=8, max_fields=2)

    assert [[field.field_id for field in chunk] for chunk in chunks] == [
        ["f1", "f2"],
        ["f3"],
    ]


def test_prompt_contains_only_requested_source_fields():
    prompt = build_prompt("docs/page.md", [_field("f1", "Первое"), _field("f2", "Второе")])
    payload = json.loads(prompt.split("\n", 1)[1])

    assert payload == {
        "source_path": "docs/page.md",
        "source_locale": "ru",
        "target_locale": "en",
        "fields": {"f1": "Первое", "f2": "Второе"},
    }


def test_timeout_switches_once_to_fallback_model():
    calls = []

    def fake_complete(**kwargs):
        calls.append(kwargs["model_uri"])
        if kwargs["model_uri"] == "primary":
            raise TimeoutError("slow")
        return "{}", {"choices": []}

    raw, response, model_used = complete_with_fallback(
        primary_model="primary",
        fallback_model="fallback",
        complete_fn=fake_complete,
        request_kwargs={},
    )

    assert (raw, response, model_used) == ("{}", {"choices": []}, "fallback")
    assert calls == ["primary", "fallback"]


def test_contract_failure_is_retried_until_response_validates():
    calls = []

    def fake_request():
        calls.append(None)
        if len(calls) == 1:
            return "bad", {"attempt": 1}, "model"
        return "good", {"attempt": 2}, "model"

    def validate(raw):
        if raw == "bad":
            raise ContractError("invalid JSON")
        return {"f1": "Good"}

    validated, response, model_used, failures = complete_validated_with_retries(
        request_fn=fake_request,
        validate_fn=validate,
        max_attempts=3,
    )

    assert validated == {"f1": "Good"}
    assert response == {"attempt": 2}
    assert model_used == "model"
    assert failures == [{"attempt": 1, "error": "invalid JSON", "response": {"attempt": 1}}]


def test_contract_failure_stops_after_bounded_attempts():
    def fail_request():
        raise ContractError("truncated")

    with __import__("pytest").raises(ContractError, match="after 2 attempts"):
        complete_validated_with_retries(
            request_fn=fail_request,
            validate_fn=lambda raw: raw,
            max_attempts=2,
        )
