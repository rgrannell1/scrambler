"""Property-based round-trip tests for every codec, exercised against a real Kùzu database.

Proves the storage round trip holds in reality, not merely the in-memory encode/decode law:
hypothesis drives each codec type — and one node combining all of them — with random values,
each value is written through Scrambler.insert, read back, and decoded; the recovered value
must equal the value stored. Schemas are generated per codec so the suite stands alone.
"""

from collections import namedtuple
from functools import cache

import hypothesis.strategies as st
import pytest
import ryugraph
from hypothesis import given, settings

import scrambler

DRAFT = "https://json-schema.org/draft/2023-02/schema"
# Each store is its own in-memory database, capped at 1 GiB: a Kùzu database otherwise reserves
# an 8 TiB virtual mmap, and the ~17 kept open across these cases would exhaust the address
# space (it fails at ~15). The cap costs nothing — these fixtures hold a handful of rows.
MAX_DB_SIZE = 2**30


def build_store(document: dict) -> scrambler.Scrambler:
    """A Scrambler over a fresh, size-capped in-memory database for the given schema."""
    database = ryugraph.Database(":memory:", max_db_size=MAX_DB_SIZE)
    return scrambler.Scrambler(database).define(document)


# Value strategies. Native INT64/DOUBLE columns are bounded to their Kùzu domains; text avoids
# control and surrogate code points so it survives binding into a native STRING column.
INT64 = st.integers(min_value=-(2**63), max_value=2**63 - 1)
TEXT = st.text(st.characters(codec="utf-8", exclude_categories=("Cc", "Cs")))
REAL = st.floats(allow_nan=False, allow_infinity=False)
JSON_LEAF = st.none() | st.booleans() | INT64 | REAL | TEXT
JSON_VALUE = st.recursive(
    JSON_LEAF,
    lambda children: st.lists(children) | st.dictionaries(TEXT, children),
    max_leaves=8,
)
JSON_OBJECT = st.dictionaries(TEXT, JSON_VALUE)
# A union scalar (JSON codec) admits any of float|int|str|bool; a native UNION must be
# string-free (a STRING member can't round-trip — check_schema rejects it), so it is declared
# over INT64|DOUBLE members and admits int|float, which Kùzu keeps in distinct members.
SCALAR = st.one_of(st.integers(), REAL, TEXT, st.booleans())
UNION = st.one_of(INT64, REAL)

# Shared element/variant field types referenced by the array, map, and union cases below.
ELEMS = {
    "str_elem": {"type": "string", "description": "a string element."},
    "int_elem": {"type": "integer", "description": "an integer element."},
    "num_elem": {"type": "number", "description": "a number element."},
}
SCALAR_UNION_ELEMS = {"num_elem": ELEMS["num_elem"], "str_elem": ELEMS["str_elem"]}
NATIVE_UNION_ELEMS = {"num_elem": ELEMS["num_elem"], "int_elem": ELEMS["int_elem"]}


def scalar_type(name: str) -> dict:
    """A scalar field-type fragment for a JSON Schema scalar type name."""
    return {"type": name, "description": "a scalar field."}


def nullable_type(name: str) -> dict:
    """A nullable scalar field-type fragment (the type unioned with null)."""
    return {"type": [name, "null"], "description": "an optional scalar field."}


def array_type(items_ref: str, *, json: bool) -> dict:
    """An array field type over an element $ref, native or JSON-encoded."""
    fragment = {
        "type": "array",
        "items": {"$ref": f"#/$defs/{items_ref}"},
        "description": "a list field.",
    }
    if json:
        fragment["x-kuzu"] = {"encode": "json"}
    return fragment


def map_type(values_ref: str, *, native: bool) -> dict:
    """An object field type over a value $ref, a native MAP or a JSON object."""
    fragment = {
        "type": "object",
        "additionalProperties": {"$ref": f"#/$defs/{values_ref}"},
        "description": "a map field.",
    }
    if native:
        fragment["x-kuzu"] = {"codec": "map"}
    return fragment


def scalar_union_type() -> dict:
    """A JSON-scalar union over number|string — faithful for any float|int|str|bool value."""
    return {
        "oneOf": [{"$ref": "#/$defs/num_elem"}, {"$ref": "#/$defs/str_elem"}],
        "description": "a json scalar union field.",
        "x-kuzu": {"codec": "scalar"},
    }


def native_union_type() -> dict:
    """A native UNION over number|integer — string-free, so its INT64|DOUBLE members round-trip."""
    return {
        "oneOf": [{"$ref": "#/$defs/num_elem"}, {"$ref": "#/$defs/int_elem"}],
        "description": "a native union field.",
        "x-kuzu": {"codec": "union", "members": {"i": "INT64", "d": "DOUBLE"},
                   "projection": "u_num"},
    }


Case = namedtuple("Case", "name value_def extra strategy")

# One case per codec the dialect admits: scalars, their nullable forms, JSON and native lists,
# JSON and native maps, and the union scalar in both its JSON and native lowerings.
CASES = [
    Case("string", scalar_type("string"), {}, TEXT),
    Case("integer", scalar_type("integer"), {}, INT64),
    Case("number", scalar_type("number"), {}, REAL),
    Case("boolean", scalar_type("boolean"), {}, st.booleans()),
    Case("nullable_string", nullable_type("string"), {}, st.none() | TEXT),
    Case("nullable_integer", nullable_type("integer"), {}, st.none() | INT64),
    Case("nullable_number", nullable_type("number"), {}, st.none() | REAL),
    Case("nullable_boolean", nullable_type("boolean"), {}, st.none() | st.booleans()),
    Case("json_list", array_type("str_elem", json=True), {"str_elem": ELEMS["str_elem"]},
         st.lists(TEXT)),
    Case("native_list_int", array_type("int_elem", json=False), {"int_elem": ELEMS["int_elem"]},
         st.lists(INT64)),
    Case("native_list_str", array_type("str_elem", json=False), {"str_elem": ELEMS["str_elem"]},
         st.lists(TEXT)),
    Case("json_object", map_type("str_elem", native=False), {"str_elem": ELEMS["str_elem"]},
         JSON_OBJECT),
    Case("native_map_int", map_type("int_elem", native=True), {"int_elem": ELEMS["int_elem"]},
         st.dictionaries(TEXT, INT64)),
    Case("native_map_str", map_type("str_elem", native=True), {"str_elem": ELEMS["str_elem"]},
         st.dictionaries(TEXT, TEXT)),
    Case("scalar_union", scalar_union_type(), SCALAR_UNION_ELEMS, SCALAR),
    Case("native_union", native_union_type(), NATIVE_UNION_ELEMS, UNION),
]
CASE_BY_NAME = {case.name: case for case in CASES}


def node_schema(properties: dict, defs: dict) -> dict:
    """A one-node ('Rec') schema: a string id plus the given properties and their type defs."""
    all_defs = {"nodeId": {"type": "string", "description": "a node id."}, **defs}
    all_defs["Rec"] = {
        "type": "object",
        "additionalProperties": False,
        "description": "the record under test.",
        "properties": {"id": {"$ref": "#/$defs/nodeId"}, **properties},
        "x-kuzu": {"table": "node", "primaryKey": "id"},
    }
    return {"$schema": DRAFT, "$id": "https://scrambler/fuzz",
            "description": "a fuzz fixture.", "$defs": all_defs}


def store_round_trip(scram: scrambler.Scrambler, document: dict, values: dict) -> dict:
    """Insert {field: value} as one record, then read each field back through its own codec.

    Reads only the canonical columns (the bijection); derived projection columns sit outside
    the round trip. The returned dict should equal `values` exactly.
    """
    scram.write.insert(scram.schema.dataclass("Rec")(id="k", **values))
    defs = document["$defs"]
    properties = defs["Rec"]["properties"]
    read = {}
    for field in values:
        codec = scrambler.codec_for(properties[field], defs)(field)
        names = [column.name for column in codec.columns]
        returns = ", ".join(f"n.{name}" for name in names)
        row = scram.connection.execute(f"MATCH (n:Rec {{id: 'k'}}) RETURN {returns}").get_all()[0]
        read[field] = codec.decode(dict(zip(names, row, strict=True)))
    return read


@cache
def case_store(name: str) -> tuple[scrambler.Scrambler, dict]:
    """A Scrambler over the one-field schema for a case, built once and reused across examples."""
    case = CASE_BY_NAME[name]
    document = node_schema(
        {"value": {"$ref": "#/$defs/value"}}, {"value": case.value_def, **case.extra}
    )
    return build_store(document), document


@pytest.mark.parametrize("name", list(CASE_BY_NAME))
@given(data=st.data())
@settings(max_examples=100, deadline=None)
def test_codec_round_trips_through_storage(name, data):
    """Proves each codec's random value survives a real Kùzu write and reads back unchanged."""
    scram, document = case_store(name)
    value = data.draw(CASE_BY_NAME[name].strategy)
    assert store_round_trip(scram, document, {"value": value}) == {"value": value}


def test_define_rejects_a_native_union_with_a_string_member():
    """Proves define() refuses a native UNION that includes a STRING member, before any DDL runs.

    Such a union can't round-trip (a numeric- or boolean-looking string coerces into the other
    member), so it is rejected at definition rather than silently losing types; the error
    points at the JSON scalar codec, which preserves the value's exact type.
    """
    union = {
        "oneOf": [{"$ref": "#/$defs/num_elem"}, {"$ref": "#/$defs/str_elem"}],
        "description": "an unfaithful native union field.",
        "x-kuzu": {"codec": "union", "members": {"d": "DOUBLE", "s": "STRING"},
                   "projection": "u_num"},
    }
    document = node_schema({"value": {"$ref": "#/$defs/value"}},
                           {**SCALAR_UNION_ELEMS, "value": union})
    with pytest.raises(ValueError, match=r"STRING member.*scalar"):
        build_store(document)


def combination_schema() -> dict:
    """One node carrying a field of every codec at once — the cross-type combination fixture."""
    properties, defs = {}, {}
    for case in CASES:
        properties[f"v_{case.name}"] = {"$ref": f"#/$defs/{case.name}"}
        defs[case.name] = case.value_def
        defs.update(case.extra)
    return node_schema(properties, defs)


COMBINATION = combination_schema()
COMBINATION_STRATEGY = st.fixed_dictionaries(
    {f"v_{case.name}": case.strategy for case in CASES}
)


@cache
def combination_store() -> scrambler.Scrambler:
    """A Scrambler over the all-codecs combination schema, built once."""
    return build_store(COMBINATION)


@given(values=COMBINATION_STRATEGY)
@settings(max_examples=60, deadline=None)
def test_combination_node_round_trips_every_field(values):
    """Proves a node combining every codec type round-trips all fields together in one write."""
    assert store_round_trip(combination_store(), COMBINATION, values) == values
