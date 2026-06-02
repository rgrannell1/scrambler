"""The callable shapes of the codec layer, as named protocols."""

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from scrambler.schema_dsl import Codec

type Value = Any              # a python field value a codec round-trips
type Params = dict[str, Any]  # Cypher bound-parameter name -> value, supplied to a write
type Row = dict[str, Any]     # stored column name -> value, returned from a read


class Encode(Protocol):
    """A field value -> the bound params for a codec's canonical (round-tripped) columns."""

    def __call__(self, value: Value) -> Params: ...


class Decode(Protocol):
    """A stored row -> the python field value it round-trips back to."""

    def __call__(self, row: Row) -> Value: ...


class Project(Protocol):
    """A field value -> the bound params for derived, write-only projection columns."""

    def __call__(self, value: Value) -> Params: ...


class CodecFactory(Protocol):
    """Bind a field/column name -> the Codec that lowers that field."""

    def __call__(self, name: str) -> "Codec": ...
