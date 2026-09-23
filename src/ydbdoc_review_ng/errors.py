"""Domain and wire-boundary errors."""

__all__ = [
    "DomainError",
    "InvariantViolation",
    "MalformedPayload",
    "SafeDiagnosticError",
    "SerializationError",
    "UnknownDomainType",
    "UnsupportedSchemaVersion",
]

class SafeDiagnosticError(RuntimeError):
    """An internal, payload-free diagnostic code safe for operator output."""

    def __init__(self, code: str, /) -> None:
        if (
            type(code) is not str
            or not code
            or len(code) > 80
            or not code.isascii()
            or not code.isidentifier()
            or not code.islower()
            or not code[0].isalpha()
        ):
            raise ValueError("invalid safe diagnostic code")
        self.code = code
        super().__init__(code)


class DomainError(ValueError):
    """Base class for errors in the domain contract."""


class InvariantViolation(DomainError):
    """A domain value was constructed with an invalid field."""


class SerializationError(DomainError):
    """Base class for wire serialization errors."""


class UnsupportedSchemaVersion(SerializationError):
    """The payload does not declare the one supported schema version."""


class UnknownDomainType(SerializationError):
    """The Python domain type or wire type tag is unsupported."""


class MalformedPayload(SerializationError):
    """The payload does not conform to its declared wire schema."""
