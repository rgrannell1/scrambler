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
    """A physical Kùzu column, plus how its value is supplied in a write.

    `bind` is the Cypher right-hand side used in a SET/CREATE (default: a bound `$name`
    parameter); `params` names the parameters that right-hand side consumes (default: just
    `name`). Almost every column binds a single same-named parameter; a native MAP is the
    exception — Kùzu binds a dict as a STRUCT, so the column is built with
    `map($name_keys, $name_values)` from two list parameters instead of one dict parameter.
    """

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
        """The law: encoding then decoding recovers an equal value.

        Holds for codecs whose encoded parameters are shaped like the stored row (every scalar
        codec). A native MAP encodes to write-only `_keys`/`_values` parameters that don't
        mirror its stored column, so it round-trips through the database, not in memory — the
        storage tests cover that path.
        """
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
        nulls = {param: None for col in codec.columns for param in col.param_names()}
        return Codec(
            codec.py_type | None, codec.columns,
            encode=lambda v: codec.encode(v) if v is not None else nulls,
            decode=lambda r: None if all(r[c.name] is None for c in codec.columns)
            else codec.decode(r),
            # Forward the inner codec's projections; project() already maps None -> None.
            projections=codec.projections,
            project=codec.project,
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
    """A native Kùzu MAP. A dict binds as a STRUCT, not a MAP, so the column is written with
    `map($name_keys, $name_values)` and `encode` splits the dict into those two list
    parameters; the stored column reads back as a dict, so `decode` is a plain lookup."""
    def make(name: str) -> Codec:
        keys, values = f"{name}_keys", f"{name}_values"
        # CAST the parameter lists to their element types so an empty map still resolves a
        # MAP type — `map([], [])` over untyped params leaves Kùzu with an unresolved ANY.
        bind = f"map(CAST(${keys} AS {key_kuzu}[]), CAST(${values} AS {value_kuzu}[]))"
        column = Column(name, f"MAP({key_kuzu}, {value_kuzu})", bind=bind, params=(keys, values))

        def encode(value: dict) -> dict:
            pairs = value or {}
            return {keys: list(pairs.keys()), values: list(pairs.values())}

        return Codec(dict, (column,), encode=encode, decode=lambda r: r[name])
    return make


def as_double(value: Any) -> float | None:
    """A value as a DOUBLE for the numeric projection column, or None when it has no place
    there: non-numeric, a bool (which isn't a metric), or a magnitude too large for a float."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return float(value)
    except OverflowError:
        return None


def Scalar(name: str, projection: str = "num_value") -> Codec:
    """A union scalar (float|int|str|bool) stored JSON-canonical, plus a numeric
    projection column for range queries — the encoded escape hatch. JSON preserves the
    exact type, so the decode side never needs a discriminator."""
    return Codec(
        float | int | str | bool, (Column(name, "STRING"),),
        encode=lambda v: {name: json.dumps(v)}, decode=lambda r: json.loads(r[name]),
        projections=(Column(projection, "DOUBLE"),),
        project=lambda v: {projection: as_double(v)},
    )


def union_has_string_member(members: dict) -> bool:
    """Whether a native UNION includes a STRING member.

    Kùzu binds a value into the first member it can cast to, so a string that looks like
    another member's literal ('0', 'true') coerces into that member and loses its type on
    read. A union carrying a STRING member therefore can't round-trip; the JSON scalar codec
    can. Numeric and boolean members don't cross-coerce, so a string-free union is faithful.
    """
    return any(kuzu.split("(", 1)[0].strip().upper() == "STRING" for kuzu in members.values())


def NativeUnion(members: dict, projection: str) -> Callable[[str], Codec]:
    """A native Kùzu UNION over named variants; binds/returns the python value directly.

    Union member access can't be range-queried, so a numeric `projection` column is kept
    alongside for `>=`/`<` filters. Members must be string-free (see union_has_string_member).
    """
    union_ddl = ", ".join(f"{tag} {kuzu}" for tag, kuzu in members.items())

    def make(name: str) -> Codec:
        return Codec(
            float | int | str | bool, (Column(name, f"UNION({union_ddl})"),),
            encode=lambda v: {name: v}, decode=lambda r: r[name],
            projections=(Column(projection, "DOUBLE"),),
            project=lambda v: {projection: as_double(v)},
        )
    return make
