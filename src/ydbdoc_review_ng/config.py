"""Raw GitHub Actions variable boundary."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ydbdoc_review_ng.errors import InvariantViolation

__all__ = [
    "ACTIONS_VARIABLE_NAMES",
    "ALLOWED_ACTORS_VARIABLE",
    "DAILY_BUDGET_RUB_VARIABLE",
    "MAX_DEPENDENCY_FILES_VARIABLE",
    "MAX_SOURCE_CHARACTERS_VARIABLE",
    "ConfigurationError",
    "InvalidActionsVariable",
    "MissingActionsVariable",
    "RawActionsVariables",
    "load_actions_variables",
]

MAX_DEPENDENCY_FILES_VARIABLE = "YDBDOC_MAX_DEPENDENCY_FILES_PER_ARTICLE"
MAX_SOURCE_CHARACTERS_VARIABLE = "YDBDOC_MAX_SOURCE_CHARACTERS"
ALLOWED_ACTORS_VARIABLE = "YDBDOC_ALLOWED_ACTORS"
DAILY_BUDGET_RUB_VARIABLE = "YDBDOC_DAILY_BUDGET_RUB"
ACTIONS_VARIABLE_NAMES: tuple[str, str, str, str] = (
    MAX_DEPENDENCY_FILES_VARIABLE,
    MAX_SOURCE_CHARACTERS_VARIABLE,
    ALLOWED_ACTORS_VARIABLE,
    DAILY_BUDGET_RUB_VARIABLE,
)


def _invariant(field_name: str) -> InvariantViolation:
    return InvariantViolation(
        f"RawActionsVariables.{field_name}: expected an exact non-blank string"
    )


@dataclass(frozen=True, slots=True)
class RawActionsVariables:
    max_dependency_files_per_article: str
    max_source_characters: str
    allowed_actors: str
    daily_budget_rub: str

    def __post_init__(self) -> None:
        for field_name in (
            "max_dependency_files_per_article",
            "max_source_characters",
            "allowed_actors",
            "daily_budget_rub",
        ):
            value = getattr(self, field_name)
            if type(value) is not str or not value.strip():
                raise _invariant(field_name)


class ConfigurationError(ValueError):
    """Base class for bounded raw-configuration failures."""


class MissingActionsVariable(ConfigurationError):
    def __init__(self, variable_name: str, expectation: str = "a required Actions variable") -> None:
        self.variable_name = variable_name
        self.expectation = expectation
        super().__init__(f"{variable_name}: expected {expectation}")


class InvalidActionsVariable(ConfigurationError):
    def __init__(self, variable_name: str, expectation: str = "an exact non-blank string") -> None:
        self.variable_name = variable_name
        self.expectation = expectation
        super().__init__(f"{variable_name}: expected {expectation}")


def load_actions_variables(values: Mapping[str, object], /) -> RawActionsVariables:
    """Load the four required variables without interpreting their raw strings."""
    loaded: list[str] = []
    for variable_name in ACTIONS_VARIABLE_NAMES:
        if variable_name not in values:
            raise MissingActionsVariable(variable_name)
        value = values[variable_name]
        if type(value) is not str or not value.strip():
            raise InvalidActionsVariable(variable_name)
        loaded.append(value)
    return RawActionsVariables(*loaded)
