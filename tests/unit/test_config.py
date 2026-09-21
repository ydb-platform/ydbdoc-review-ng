from dataclasses import FrozenInstanceError

import pytest

from ydbdoc_review_ng.config import (
    ACTIONS_VARIABLE_NAMES,
    ALLOWED_ACTORS_VARIABLE,
    DAILY_BUDGET_RUB_VARIABLE,
    MAX_DEPENDENCY_FILES_VARIABLE,
    MAX_SOURCE_CHARACTERS_VARIABLE,
    InvalidActionsVariable,
    MissingActionsVariable,
    RawActionsVariables,
    load_actions_variables,
)
from ydbdoc_review_ng.errors import InvariantViolation

EXPECTED_NAMES_AND_FIELDS = (
    ("YDBDOC_MAX_DEPENDENCY_FILES_PER_ARTICLE", "max_dependency_files_per_article"),
    ("YDBDOC_MAX_SOURCE_CHARACTERS", "max_source_characters"),
    ("YDBDOC_ALLOWED_ACTORS", "allowed_actors"),
    ("YDBDOC_DAILY_BUDGET_RUB", "daily_budget_rub"),
)


def valid_values() -> dict[str, object]:
    return {
        "YDBDOC_MAX_DEPENDENCY_FILES_PER_ARTICLE": " 17 ",
        "YDBDOC_MAX_SOURCE_CHARACTERS": "20000",
        "YDBDOC_ALLOWED_ACTORS": " alice,Боб ",
        "YDBDOC_DAILY_BUDGET_RUB": " 12.340 ",
        "UNRELATED_SECRET": "secret-canary",
    }


def test_exact_four_variable_mapping_preserves_raw_strings() -> None:
    assert MAX_DEPENDENCY_FILES_VARIABLE == "YDBDOC_MAX_DEPENDENCY_FILES_PER_ARTICLE"
    assert MAX_SOURCE_CHARACTERS_VARIABLE == "YDBDOC_MAX_SOURCE_CHARACTERS"
    assert ALLOWED_ACTORS_VARIABLE == "YDBDOC_ALLOWED_ACTORS"
    assert DAILY_BUDGET_RUB_VARIABLE == "YDBDOC_DAILY_BUDGET_RUB"
    assert ACTIONS_VARIABLE_NAMES == (
        "YDBDOC_MAX_DEPENDENCY_FILES_PER_ARTICLE",
        "YDBDOC_MAX_SOURCE_CHARACTERS",
        "YDBDOC_ALLOWED_ACTORS",
        "YDBDOC_DAILY_BUDGET_RUB",
    )
    result = load_actions_variables(valid_values())
    assert result.max_dependency_files_per_article == " 17 "
    assert result.max_source_characters == "20000"
    assert result.allowed_actors == " alice,Боб "
    assert result.daily_budget_rub == " 12.340 "


@pytest.mark.parametrize("variable_name,field_name", EXPECTED_NAMES_AND_FIELDS)
def test_missing_variable_is_typed_and_non_secret(variable_name: str, field_name: str) -> None:
    values = valid_values()
    del values[variable_name]
    with pytest.raises(MissingActionsVariable) as caught:
        load_actions_variables(values)
    error = caught.value
    assert error.variable_name == variable_name
    assert error.expectation == "a required Actions variable"
    assert variable_name in str(error)
    assert "secret-canary" not in str(error)
    assert field_name not in str(error)


@pytest.mark.parametrize("bad", [None, 1, True, "", " \t\r\n", pytest.param(type("S", (str,), {})("canary"), id="str-subclass")])
@pytest.mark.parametrize("variable_name,field_name", EXPECTED_NAMES_AND_FIELDS)
def test_invalid_variable_is_typed_and_non_secret(
    variable_name: str, field_name: str, bad: object
) -> None:
    values = valid_values()
    values[variable_name] = bad
    with pytest.raises(InvalidActionsVariable) as caught:
        load_actions_variables(values)
    error = caught.value
    assert error.variable_name == variable_name
    assert error.expectation == "an exact non-blank string"
    rendered = str(error) + repr(error)
    assert variable_name in rendered
    assert "secret-canary" not in rendered
    assert repr(bad) not in rendered


def test_raw_actions_variables_is_frozen() -> None:
    value = RawActionsVariables("1", "2", "actor", "3")
    with pytest.raises(FrozenInstanceError):
        value.allowed_actors = "other"  # type: ignore[misc]


class StringSubclass(str):
    pass


@pytest.mark.parametrize("bad", [None, 1, True, "", " \t\r\n", StringSubclass("canary")])
@pytest.mark.parametrize("field_name", [field for _, field in EXPECTED_NAMES_AND_FIELDS])
def test_raw_actions_variables_direct_construction_rejects_every_invalid_field(
    field_name: str, bad: object
) -> None:
    kwargs: dict[str, object] = {
        "max_dependency_files_per_article": "1",
        "max_source_characters": "2",
        "allowed_actors": "actor",
        "daily_budget_rub": "3",
    }
    kwargs[field_name] = bad
    with pytest.raises(InvariantViolation, match=rf"RawActionsVariables\.{field_name}"):
        RawActionsVariables(**kwargs)  # type: ignore[arg-type]


def test_no_alias_or_default_is_accepted() -> None:
    values = valid_values()
    del values["YDBDOC_MAX_SOURCE_CHARACTERS"]
    values["ydbdoc_max_source_characters"] = "20000"
    with pytest.raises(MissingActionsVariable):
        load_actions_variables(values)
