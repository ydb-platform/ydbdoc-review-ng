from pr_translation_smoke.yandex_openai_client import (
    build_request_payload,
    extract_response_text,
)


def test_openai_payload_disables_reasoning_and_uses_strict_schema():
    schema = {
        "type": "object",
        "properties": {"field_0001": {"type": "string"}},
        "required": ["field_0001"],
        "additionalProperties": False,
    }

    payload = build_request_payload(
        model_uri="gpt://folder/deepseek-v4-flash",
        prompt="translate",
        schema=schema,
        max_tokens=2000,
    )

    assert payload["reasoning_effort"] == "none"
    assert payload["temperature"] == 0
    assert payload["response_format"]["json_schema"]["strict"] is True
    assert payload["response_format"]["json_schema"]["schema"] == schema


def test_openai_response_accepts_only_stop_completion():
    response = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": '{"field_0001":"Text"}'},
            }
        ]
    }

    assert extract_response_text(response) == '{"field_0001":"Text"}'
