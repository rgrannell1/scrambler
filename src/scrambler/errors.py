"""The scrambler exception hierarchy.

Every error scrambler raises derives from ScramblerError, so a caller can catch the whole
family with one `except`. Each also derives from the built-in exception it most resembles
(ValueError, RuntimeError), so existing `except ValueError`/`except RuntimeError` handlers and
intuitions keep working — the scrambler types only narrow them.
"""


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
