"""The pieces a codec is built from: physical columns, the value<->column operations, and the
numeric-projection helpers."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from scrambler.errors import RecordError
from scrambler.protocols import Decode, Encode, Project, Value


@dataclass(frozen=True)
class Column:
    """A physical Kùzu column, plus how its value is supplied in a write."""

    name: str
    kuzu: str  # STRING | INT64 | DOUBLE | BOOLEAN | MAP(...) | ...
    bind: str | None = None
    params: tuple[str, ...] = ()

    def binding(self) -> str:
        """The Cypher expression supplying this column's value in a SET/CREATE."""
        return self.bind or f"${self.name}"

    def param_names(self) -> tuple[str, ...]:
        """The bound-parameter names this column's binding consumes."""
        return self.params or (self.name,)


def bind_value(name: str, prepare: Callable[[Value], Any] = lambda value: value) -> Encode:
    """Encode straight: bind prepare(value) to the column's own parameter."""
    
    return lambda value: {name: prepare(value)}


def read_column(name: str) -> Decode:
    """Decode straight: read the value back from its column."""
    
    return lambda row: row[name]


def json_encode(name: str, prepare: Callable[[Value], Any] = lambda value: value) -> Encode:
    """Encode via JSON: store prepare(value) as a JSON string in the column."""
    
    return lambda value: {name: json.dumps(prepare(value))}


def json_decode(name: str) -> Decode:
    """Decode via JSON: parse the column's JSON string back to a value."""
    
    return lambda row: json.loads(row[name])


def is_metric(value: Any) -> bool:
    """Whether a scalar value has a place in the numeric range-query index.

    Only int/float qualify; bools are flags, not metrics, and are excluded even though
    they're castable to a double. A value that isn't a metric is projected as NULL.
    """
    
    return not isinstance(value, bool) and isinstance(value, int | float)


def to_double(value: float | int) -> float:
    """A numeric value as a DOUBLE; raises if it's too large to represent."""
    
    try:
        return float(value)
    except OverflowError as error:
        raise RecordError(f"{value!r} is too large to store in a DOUBLE column") from error


def numeric_projection(value: Any) -> float | None:
    """A scalar value's entry in the numeric projection column: the double, or None when
    it isn't a metric."""
    
    return to_double(value) if is_metric(value) else None


def project_numeric(projection: str) -> Project:
    """Project the value's entry into the numeric range-query column."""
    
    return lambda value: {projection: numeric_projection(value)}
