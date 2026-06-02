"""The scrambler exception hierarchy."""


class ScramblerError(Exception):
    """Base class for every error scrambler raises."""


class SchemaError(ScramblerError, ValueError):
    """A schema document is invalid, or admits a construct that can't be lowered faithfully."""


class NotDefinedError(ScramblerError, RuntimeError):
    """A store operation ran before define() adopted a schema."""


class UnknownLabelError(ScramblerError, ValueError):
    """A label is not a node type in the defined schema."""


class RecordError(ScramblerError, ValueError):
    """A record or filter doesn't conform to the node type's schema."""


class QueryError(ScramblerError, RuntimeError):
    """A Cypher statement failed in the graph engine."""
