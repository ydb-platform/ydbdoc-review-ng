import pytest

from pr_translation_smoke.contract import ContractError
from pr_translation_smoke.yandex_client import (
    build_request_payload,
    extract_response_text,
    normalize_model_uri,
)


def test_request_uses_native_json_schema_and_disables_reasoning():
    schema = {
        "type": "object",
        "properties": {"field_0001": {"type": "string"}},
        "required": ["field_0001"],
        "additionalProperties": False,
    }

    payload = build_request_payload(
        model_uri="gpt://folder/model",
        prompt="translate",
        schema=schema,
        max_tokens=2000,
    )

    assert payload["jsonSchema"] == {"schema": schema}
    assert "jsonObject" not in payload
    assert payload["completionOptions"] == {
        "stream": False,
        "temperature": 0,
        "maxTokens": "2000",
        "reasoningOptions": {"mode": "DISABLED"},
    }


def test_short_model_name_is_expanded_with_folder_id():
    assert normalize_model_uri("yandexgpt-pro", "folder123") == "gpt://folder123/yandexgpt-pro"


def test_full_model_uri_is_kept_unchanged():
    assert (
        normalize_model_uri("gpt://folder123/yandexgpt/latest", "folder123")
        == "gpt://folder123/yandexgpt/latest"
    )


def test_response_accepts_only_final_alternative():
    response = {
        "result": {
            "alternatives": [
                {
                    "message": {"role": "assistant", "text": '{"field_0001":"Text"}'},
                    "status": "ALTERNATIVE_STATUS_FINAL",
                }
            ]
        }
    }

    assert extract_response_text(response) == '{"field_0001":"Text"}'


@pytest.mark.parametrize(
    "status",
    [
        "ALTERNATIVE_STATUS_TRUNCATED_FINAL",
        "ALTERNATIVE_STATUS_CONTENT_FILTER",
    ],
)
def test_response_rejects_incomplete_alternative(status):
    response = {
        "result": {
            "alternatives": [
                {"message": {"role": "assistant", "text": "{}"}, "status": status}
            ]
        }
    }

    with pytest.raises(ContractError, match=status):
        extract_response_text(response)
