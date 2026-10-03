# P0 probe: DeepSeek tool_calls on YC OpenAI endpoint

**Result: PASS** (2026-10-03)

Probe script: `scripts/probe_deepseek_tools.py` (`YDBDOC_LIVE=1`).

## What was proven

| Check | Result |
|---|---|
| HTTP 200 with `tools` + `tool_choice=required` | yes |
| Assistant returns OpenAI-style `tool_calls[]` | yes |
| `finish_reason` on tool turn | `tool_calls` |
| `message.content` on tool turn | `null` |
| Multi-turn: append assistant + `role=tool` → next completion | HTTP 200, `finish_reason=stop` |
| Cost (usage) | turn1 ~375 tokens; turn2 ~130 tokens |

## Environment used

| Field | Value |
|---|---|
| Endpoint | `https://ai.api.cloud.yandex.net/v1/chat/completions` |
| Model | `deepseek-v4-flash` |
| Model URI | `gpt://b1gj42mhlf663vd7slc2/deepseek-v4-flash` |
| Creds source | shell `YANDEX_CLOUD_API_KEY_DOC_REVIEW` + `YANDEX_CLOUD_FOLDER_DOC_REVIEW` |
| Production `YANDEX_API_KEY` / `YANDEX_FOLDER_ID` in this shell | **absent** (smoke/doc-review aliases used; documented non-runtime) |

No secrets recorded here.

## clients.py landmine (confirmed live)

Turn1 shape matches the plan warning:

- `finish_reason == "tool_calls"`
- `content is null`
- `tool_calls` present with `id` + `function.name` + `function.arguments`

Current `_semantic_error` / `_parse_openai` would classify that as `NON_FINAL`
(and/or empty-text paths) if tools were wired without P1c parser changes.
P1c must treat this as a success intermediate.

## Go / no-go

**GO for P1.** Provider supports OpenAI-style tools multi-turn on the production
endpoint shape. No fake tool protocol needed. Schema tweaks not forced by probe
(simple `echo` function worked; critic tools from §4.1 remain the canon).
