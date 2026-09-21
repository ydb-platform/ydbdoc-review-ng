"""Domain and wire-boundary errors."""

__all__ = [
    "DomainError",
    "InvariantViolation",
    "MalformedPayload",
    "SerializationError",
    "UnknownDomainType",
    "UnsupportedSchemaVersion",
]


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
