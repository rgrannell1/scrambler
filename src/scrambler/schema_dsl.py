"""Codec layer: the value <-> Kùzu-column primitives the schema compiler lowers to.

@work.md — a `Codec` maps a python value to one-or-more storage columns with a
round-trip guarantee: `columns` carry the bijection, `projections` are derived
query-only columns that sit outside it. The type constructors (Str, Int, Opt,
ListStr, Json, Scalar, …) build a codec bound to a field name; dsl/compile.py picks
one per JSON Schema property. They are capitalised on purpose — they read as types.
"""
# ruff: noqa: N802

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

_MISSING: Any = object()


@dataclass(frozen=True)
class Column:
    """A physical Kùzu column."""

    name: str
    kuzu: str  # STRING | INT64 | DOUBLE | BOOLEAN


@dataclass(frozen=True)
class Codec:
    """A python value <-> storage columns, with a round-trip guarantee.

    `columns` carry the bijection; `projections` are derived, write-only columns
    (computed by `project`, never consulted by `decode`).
    """

    py_type: Any
    columns: tuple[Column, ...]
    encode: Callable[[Any], dict]
    decode: Callable[[dict], Any]
    projections: tuple[Column, ...] = ()
    project: Callable[[Any], dict] = lambda _value: {}

    def roundtrips(self, value: Any) -> bool:
        """The law: encoding then decoding recovers an equal value."""
        return self.decode(self.encode(value)) == value


def Str(name: str) -> Codec:
    return Codec(str, (Column(name, "STRING"),), lambda v: {name: v}, lambda r: r[name])


def Int(name: str) -> Codec:
    return Codec(int, (Column(name, "INT64"),), lambda v: {name: v}, lambda r: r[name])


def Float(name: str) -> Codec:
    return Codec(float, (Column(name, "DOUBLE"),), lambda v: {name: v}, lambda r: r[name])


def Bool(name: str) -> Codec:
    return Codec(bool, (Column(name, "BOOLEAN"),), lambda v: {name: v}, lambda r: r[name])


def Opt(inner: Callable[[str], Codec]) -> Callable[[str], Codec]:
    """Make a type nullable: None when every backing column is NULL."""
    def make(name: str) -> Codec:
        codec = inner(name)
        nulls = {col.name: None for col in codec.columns}
        return Codec(
            codec.py_type | None, codec.columns,
            encode=lambda v: codec.encode(v) if v is not None else nulls,
            decode=lambda r: None if all(r[c.name] is None for c in codec.columns)
            else codec.decode(r),
        )
    return make


def ListStr(name: str) -> Codec:
    """list[str] <-> STRING via JSON — survives commas inside elements."""
    return Codec(list[str], (Column(name, "STRING"),),
                 lambda v: {name: json.dumps(list(v))}, lambda r: json.loads(r[name]))


def NativeList(element_kuzu: str) -> Callable[[str], Codec]:
    """A native Kùzu LIST (`<element>[]`); binds and round-trips as a python list."""
    def make(name: str) -> Codec:
        return Codec(list, (Column(name, f"{element_kuzu}[]"),),
                     lambda v: {name: list(v)}, lambda r: r[name])
    return make


def Json(name: str) -> Codec:
    """dict <-> STRING via JSON — the escape hatch for arbitrary/heterogeneous data."""
    return Codec(dict, (Column(name, "STRING"),),
                 lambda v: {name: json.dumps(v or {})}, lambda r: json.loads(r[name]))


def NativeMap(key_kuzu: str, value_kuzu: str) -> Callable[[str], Codec]:
    """A native Kùzu MAP. Note: a dict binds as a STRUCT, so writers must construct it
    with `map($keys, $values)` rather than a single bound parameter (see insert_entity)."""
    def make(name: str) -> Codec:
        return Codec(dict, (Column(name, f"MAP({key_kuzu}, {value_kuzu})"),),
                     lambda v: {name: v}, lambda r: r[name])
    return make


def Scalar(name: str, projection: str = "num_value") -> Codec:
    """A union scalar (float|int|str|bool) stored JSON-canonical, plus a numeric
    projection column for range queries — the encoded escape hatch. JSON preserves the
    exact type, so the decode side never needs a discriminator."""
    def numeric(v: Any) -> bool:
        return isinstance(v, int | float) and not isinstance(v, bool)
    return Codec(
        float | int | str | bool, (Column(name, "STRING"),),
        encode=lambda v: {name: json.dumps(v)}, decode=lambda r: json.loads(r[name]),
        projections=(Column(projection, "DOUBLE"),),
        project=lambda v: {projection: float(v) if numeric(v) else None},
    )


def NativeUnion(members: dict, projection: str) -> Callable[[str], Codec]:
    """A native Kùzu UNION over named variants; binds/returns the python value directly.

    Union member access can't be range-queried, so a numeric `projection` column is kept
    alongside for `>=`/`<` filters.
    """
    union_ddl = ", ".join(f"{tag} {kuzu}" for tag, kuzu in members.items())

    def numeric(value: Any) -> bool:
        return isinstance(value, int | float) and not isinstance(value, bool)

    def make(name: str) -> Codec:
        return Codec(
            float | int | str | bool, (Column(name, f"UNION({union_ddl})"),),
            encode=lambda v: {name: v}, decode=lambda r: r[name],
            projections=(Column(projection, "DOUBLE"),),
            project=lambda v: {projection: float(v) if numeric(v) else None},
        )
    return make
