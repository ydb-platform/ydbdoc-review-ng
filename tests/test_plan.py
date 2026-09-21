import pytest

from pr_translation_smoke.contract import ContractError
from pr_translation_smoke.plan import build_plan


SOURCE = """## Заголовок {#anchor}

Используйте {{ ydb-short-name }} и [`StartTls`](page.md#starttls).

```yaml
title: Не переводить
```

#|
|| Параметр | Описание ||
|| external_idp_config.issuer
| Адрес провайдера `issuer`.
    ||
|#
"""


def test_plan_round_trips_source_and_never_extracts_fenced_code():
    plan = build_plan(SOURCE)

    identity_map = {field.field_id: field.model_text for field in plan.fields}
    assert plan.assemble(identity_map) == SOURCE
    assert all("Не переводить" not in field.model_text for field in plan.fields)


def test_plan_exposes_inline_technical_fragments_as_source_references():
    plan = build_plan(SOURCE)
    field = next(field for field in plan.fields if "Используйте" in field.model_text)

    assert "{{ ydb-short-name }}" not in field.model_text
    assert "page.md#starttls" not in field.model_text
    assert len(field.atoms) == 2
    assert all(atom.token in field.model_text for atom in field.atoms)


def test_assembly_restores_exact_source_atoms_around_translation():
    plan = build_plan(SOURCE)
    translations = {field.field_id: field.model_text for field in plan.fields}
    target = next(field for field in plan.fields if "Используйте" in field.model_text)
    translations[target.field_id] = target.model_text.replace(
        "Используйте", "Use"
    ).replace(" и ", " and ")

    candidate = plan.assemble(translations)

    assert "Use {{ ydb-short-name }} and [`StartTls`](page.md#starttls)." in candidate


def test_assembly_rejects_reordered_source_references():
    plan = build_plan(SOURCE)
    translations = {field.field_id: field.model_text for field in plan.fields}
    target = next(field for field in plan.fields if "Используйте" in field.model_text)
    first, second = target.atoms[0].token, target.atoms[1].token
    translations[target.field_id] = target.model_text.replace(first, "TEMP", 1).replace(
        second, first, 1
    ).replace("TEMP", second, 1)

    with pytest.raises(ContractError, match="source reference order"):
        plan.assemble(translations)


def test_assembly_allows_reordered_references_only_for_explicit_fields():
    plan = build_plan(SOURCE)
    translations = {field.field_id: field.model_text for field in plan.fields}
    target = next(field for field in plan.fields if "Используйте" in field.model_text)
    first, second = target.atoms[0].token, target.atoms[1].token
    translations[target.field_id] = target.model_text.replace(first, "TEMP", 1).replace(
        second, first, 1
    ).replace("TEMP", second, 1)

    candidate = plan.assemble(
        translations, reorderable_fields={target.field_id}
    )

    assert "[`StartTls`](page.md#starttls)" in candidate
    assert "{{ ydb-short-name }}" in candidate


def test_plan_translates_yfm_table_header_and_description_but_not_parameter_name():
    plan = build_plan(SOURCE)
    texts = [field.model_text for field in plan.fields]

    assert any("Параметр" in text and "Описание" in text for text in texts)
    assert any("Адрес провайдера" in text for text in texts)
    assert all("external_idp_config.issuer" not in text for text in texts)
