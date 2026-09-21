import json
import re

import pytest

from pr_translation_smoke.contract import ContractError
from pr_translation_smoke.plan import Field, SourceAtom
from pr_translation_smoke.single_field import (
    build_single_field_prompt,
    build_single_field_schema,
    compact_model_field,
    context_for_field,
    restore_compact_references,
    split_prose_segments,
    translate_field_by_segments,
    translate_field,
    validate_single_field_translation,
)


def _field(text: str = "Исходный текст") -> Field:
    return Field("field_0007", 10, 10 + len(text), text, text, ())


def test_prompt_has_one_output_field_and_separate_read_only_context():
    prompt = build_single_field_prompt(
        source_path="docs/page.md",
        field=_field(),
        before="Заголовок раздела",
        after="Следующее предложение",
    )

    payload = json.loads(prompt.split("\n", 1)[1])

    assert payload == {
        "source_path": "docs/page.md",
        "source_locale": "ru",
        "target_locale": "en",
        "translate_only": {"field_0007": "Исходный текст"},
        "context_only_do_not_translate": {
            "before": "Заголовок раздела",
            "after": "Следующее предложение",
        },
    }


def test_nonempty_source_rejects_empty_translation():
    with pytest.raises(ContractError, match="empty translation"):
        validate_single_field_translation(_field(), "")


def test_translation_rejects_untranslated_cyrillic_prose():
    with pytest.raises(ContractError, match="Cyrillic"):
        validate_single_field_translation(_field(), "Partly переведено")


@pytest.mark.parametrize("bad", ["Text\x02broken", "Text\u200bbroken"])
def test_translation_rejects_control_and_zero_width_characters(bad):
    with pytest.raises(ContractError, match="forbidden character"):
        validate_single_field_translation(_field(), bad)


def test_translation_accepts_english_text_with_protected_tokens():
    field = _field("Откройте <S100_12> для настройки")

    assert (
        validate_single_field_translation(field, "Open <S100_12> to configure")
        == "Open <S100_12> to configure"
    )


def test_context_excludes_the_translated_field():
    source = "before Исходный текст after"
    field = Field("field_0007", 7, 21, "Исходный текст", "Исходный текст", ())

    before, after = context_for_field(source, field, window=100)

    assert before == "before "
    assert after == " after"


def test_translate_field_requests_and_accepts_exactly_one_id():
    source = "prefix Исходный текст suffix"
    field = Field("field_0007", 7, 21, "Исходный текст", "Исходный текст", ())
    captured = {}

    def fake_complete(**kwargs):
        captured.update(kwargs)
        return '{"field_0007":"English text"}', {"result": {"usage": {}}}

    translated, response = translate_field(
        source_path="docs/page.md",
        source=source,
        field=field,
        api_key="key",
        folder_id="folder",
        model_uri="model",
        complete_fn=fake_complete,
    )

    assert translated == "English text"
    assert response == {"result": {"usage": {}}}
    prompt_payload = json.loads(captured["prompt"].split("\n", 1)[1])
    assert prompt_payload["context_only_do_not_translate"] == {
        "before": "",
        "after": "",
    }
    assert captured["schema"] == {
        "type": "object",
        "properties": {
            "field_0007": {
                "type": "string",
                "pattern": r"^[^<>\u0000-\u001F\u007F\u200B-\u200F\u2060\uFEFF]*$",
            }
        },
        "required": ["field_0007"],
        "additionalProperties": False,
    }


def test_compact_references_are_positional_and_restore_source_tokens():
    atoms = (
        SourceAtom("<S100_12>", "`one`", 100, 112, "a"),
        SourceAtom("<S120_5>", "`two`", 120, 125, "b"),
    )
    field = Field(
        "field_0007",
        0,
        10,
        "Откройте `one` и `two`",
        "Откройте <S100_12> и <S120_5>",
        atoms,
    )

    compact = compact_model_field(field)

    assert compact.model_text == (
        "Откройте __REF_1_CODE_one__ и __REF_2_CODE_two__"
    )
    assert (
        restore_compact_references(
            field, "Open __REF_1_CODE_one__ and __REF_2_CODE_two__"
        )
        == "Open <S100_12> and <S120_5>"
    )


def test_compact_references_reject_reordering():
    atoms = (
        SourceAtom("<S100_12>", "`one`", 100, 112, "a"),
        SourceAtom("<S120_5>", "`two`", 120, 125, "b"),
    )
    field = Field("field_0007", 0, 1, "x", "<S100_12><S120_5>", atoms)

    with pytest.raises(ContractError, match="order"):
        restore_compact_references(
            field, "__REF_2_CODE_two__ then __REF_1_CODE_one__"
        )


def test_compact_references_allow_explicit_safe_reordering():
    atoms = (
        SourceAtom("<S100_12>", "`one`", 100, 112, "a"),
        SourceAtom("<S120_5>", "`two`", 120, 125, "b"),
    )
    field = Field("field_0007", 0, 1, "x", "<S100_12><S120_5>", atoms)

    assert restore_compact_references(
        field,
        "__REF_2_CODE_two__ then __REF_1_CODE_one__",
        require_order=False,
    ) == "<S120_5> then <S100_12>"


def test_compact_references_reject_broken_link_pair_when_reordering():
    atoms = (
        SourceAtom("<S1_1>", "[", 1, 2, "a"),
        SourceAtom("<S2_2>", "](a)", 2, 4, "b"),
        SourceAtom("<S4_1>", "[", 4, 5, "c"),
        SourceAtom("<S5_2>", "](b)", 5, 7, "d"),
    )
    field = Field("field_0007", 0, 1, "x", "".join(a.token for a in atoms), atoms)

    with pytest.raises(ContractError, match="link pair"):
        restore_compact_references(
            field,
            "__REF_1_LINK_OPEN__a__REF_4_LINK_CLOSE__ "
            "__REF_3_LINK_OPEN__b__REF_2_LINK_CLOSE__",
            require_order=False,
        )


def test_compact_references_parse_link_text_between_adjacent_tokens():
    atoms = (
        SourceAtom("<S1_1>", "[", 1, 2, "a"),
        SourceAtom("<S9_9>", "](url)", 9, 18, "b"),
    )
    field = Field("field_0007", 0, 1, "x", "<S1_1>databases<S9_9>", atoms)

    assert restore_compact_references(
        field, "__REF_1_LINK_OPEN__databases__REF_2_LINK_CLOSE__"
    ) == "<S1_1>databases<S9_9>"


def test_single_field_schema_rejects_invisible_characters_around_references():
    atom = SourceAtom("<S100_12>", "`one`", 100, 112, "a")
    field = Field("field_0007", 0, 1, "x", "x <S100_12>", (atom,))
    pattern = build_single_field_schema(field)["properties"]["field_0007"]["pattern"]

    assert re.fullmatch(pattern, "Open __REF_1_CODE_one__ now")
    assert not re.fullmatch(pattern, "Open\x02 __REF_1_CODE_one__ now")
    assert not re.fullmatch(pattern, "Open __REF_1_CODE_one__\u200b now")


def test_compound_code_link_reference_uses_short_stable_hint():
    atom = SourceAtom(
        "<S1_20>",
        "`allowed_clock_skew`](../configuration.md#auth)",
        1,
        21,
        "a",
    )
    field = Field("field_0007", 0, 1, "x", "<S1_20>", (atom,))

    assert compact_model_field(field).model_text == (
        "__REF_1_CODE_LINK_allowed_clock_skew__"
    )


def test_split_prose_segments_keeps_source_atoms_between_parts():
    atoms = (
        SourceAtom("<S1_1>", "`one`", 1, 2, "a"),
        SourceAtom("<S2_1>", "`two`", 2, 3, "b"),
    )
    field = Field("field_0007", 0, 1, "x", "До <S1_1> между <S2_1> после", atoms)

    assert split_prose_segments(field) == ["До ", " между ", " после"]


def test_segment_translation_interleaves_immutable_source_references():
    atoms = (
        SourceAtom("<S1_1>", "`one`", 1, 2, "a"),
        SourceAtom("<S2_1>", "`two`", 2, 3, "b"),
    )
    field = Field("field_0007", 0, 1, "x", "До <S1_1> между <S2_1> после", atoms)
    translations = {"До ": "Before ", " между ": " between ", " после": " after"}

    def fake_complete(**kwargs):
        payload = json.loads(kwargs["prompt"].split("\n", 1)[1])
        field_id, source = next(iter(payload["translate_only"].items()))
        raw = json.dumps({field_id: translations[source]})
        return raw, {"result": {"usage": {}}}

    translated, responses = translate_field_by_segments(
        source_path="docs/page.md",
        field=field,
        api_key="key",
        folder_id="folder",
        model_uri="model",
        complete_fn=fake_complete,
    )

    assert translated == "Before <S1_1> between <S2_1> after"
    assert len(responses) == 3
