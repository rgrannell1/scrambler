"""Codec layer: the value <-> Kùzu-column primitives the schema compiler lowers to."""
# ruff: noqa: N802

from dataclasses import dataclass
from typing import Any

from scrambler.constants import KUZU_TYPE
from scrambler.errors import SchemaError
from scrambler.parts import (
    Column,
    bind_value,
    json_decode,
    json_encode,
    project_numeric,
    read_column,
)
from scrambler.protocols import CodecFactory, Decode, Encode, Project

_MISSING: Any = object()


@dataclass(frozen=True, slots=True)
class Codec:
    """A python value <-> storage columns, with a round-trip guarantee.

    `columns` carry the bijection; `projections` are derived, write-only columns
    (computed by `project`, never consulted by `decode`)."""

    py_type: Any
    columns: tuple[Column, ...]
    encode: Encode
    decode: Decode
    projections: tuple[Column, ...] = ()
    project: Project = lambda _value: {}


def Str(name: str) -> Codec:
    return Codec(str, (Column(name, "STRING"),), bind_value(name), read_column(name))


def Int(name: str) -> Codec:
    return Codec(int, (Column(name, "INT64"),), bind_value(name), read_column(name))


def Float(name: str) -> Codec:
    return Codec(float, (Column(name, "DOUBLE"),), bind_value(name), read_column(name))


def Bool(name: str) -> Codec:
    return Codec(bool, (Column(name, "BOOLEAN"),), bind_value(name), read_column(name))


def Opt(inner: CodecFactory) -> CodecFactory:
    """Make a type nullable: None when every backing column is NULL."""

    def make(name: str) -> Codec:
        codec = inner(name)
        columns = codec.columns
        nulls = {param: None for col in columns for param in col.param_names()}

        return Codec(
            codec.py_type | None, columns,
            encode=lambda v: codec.encode(v) if v is not None else nulls,
            decode=lambda r: None if all(r[c.name] is None for c in columns)
            else codec.decode(r),
            # Forward the inner codec's projections; project() already maps None -> None.
            projections=codec.projections,
            project=codec.project,
        )

    return make


def ListStr(name: str) -> Codec:
    """list[str] <-> STRING via JSON — survives commas inside elements."""

    return Codec(list[str], (Column(name, "STRING"),),
                 json_encode(name, list), json_decode(name))


def NativeList(element_kuzu: str) -> CodecFactory:
    """A native Kùzu LIST (`<element>[]`); binds and round-trips as a python list."""

    def make(name: str) -> Codec:
        return Codec(list, (Column(name, f"{element_kuzu}[]"),),
                     bind_value(name, list), read_column(name))
    return make


def Json(name: str) -> Codec:
    """dict <-> STRING via JSON — the escape hatch for arbitrary/heterogeneous data."""

    return Codec(dict, (Column(name, "STRING"),),
                 json_encode(name, lambda value: value or {}), json_decode(name))


def NativeMap(key_kuzu: str, value_kuzu: str) -> CodecFactory:
    """A native Kùzu MAP. A dict binds as a STRUCT, not a MAP, so the column is written with
    `map($name_keys, $name_values)`"""

    def make(name: str) -> Codec:
        keys, values = f"{name}_keys", f"{name}_values"
        # CAST the parameter lists to their element types so an empty map still resolves a
        # MAP type — `map([], [])` over untyped params leaves Kùzu with an unresolved ANY.

        bind = f"map(CAST(${keys} AS {key_kuzu}[]), CAST(${values} AS {value_kuzu}[]))"
        column = Column(name, f"MAP({key_kuzu}, {value_kuzu})", bind=bind, params=(keys, values))

        def encode(value: dict) -> dict:
            pairs = value or {}
            return {keys: list(pairs.keys()), values: list(pairs.values())}

        return Codec(dict, (column,), encode=encode, decode=read_column(name))

    return make


def Scalar(name: str, projection: str = "num_value") -> Codec:
    """A union scalar (float|int|str|bool) stored JSON-canonical, plus a numeric
    projection column for range queries"""

    return Codec(
        float | int | str | bool, (Column(name, "STRING"),),
        encode=json_encode(name), decode=json_decode(name),
        projections=(Column(projection, "DOUBLE"),),
        project=project_numeric(projection),
    )


def base_kuzu_type(kuzu: str) -> str:
    """The bare type name of a Kùzu type — drops any `(...)` parameters and normalises case.

    Raises SchemaError if the string isn't a recognisable `NAME` or `NAME(...)` type token."""

    match = KUZU_TYPE.fullmatch(kuzu)
    if match is None:
        raise SchemaError(f"not a Kùzu type: {kuzu!r}")

    return match.group(1).upper()


def union_has_string_member(members: dict) -> bool:
    """Whether a native UNION includes a STRING member."""

    return any(base_kuzu_type(kuzu) == "STRING" for kuzu in members.values())


def NativeUnion(members: dict, projection: str) -> CodecFactory:
    """A native Kùzu UNION over named variants; binds/returns the python value directly."""

    union_ddl = ", ".join(f"{tag} {kuzu}" for tag, kuzu in members.items())

    def make(name: str) -> Codec:
        return Codec(
            float | int | str | bool, (Column(name, f"UNION({union_ddl})"),),
            encode=bind_value(name), decode=read_column(name),
            projections=(Column(projection, "DOUBLE"),),
            project=project_numeric(projection),
        )

    return make
