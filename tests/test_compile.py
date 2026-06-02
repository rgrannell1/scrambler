"""Tests for the scrambler compilers and the dialect meta-schema.

Proves a document valid against the dialect compiles to Kùzu DDL the engine accepts, and
that the generated dataclasses round-trip through storage encoding. Uses a small fixture
schema so the tests stand alone (no dependency on any particular consumer's schema).
"""

import tempfile
from pathlib import Path

import jsonschema
import ryugraph

import scrambler

# A minimal Layer-2 schema: one node type (string id + label) and one self-relationship.
FIXTURE = {
    "$schema": "https://json-schema.org/draft/2023-02/schema",
    "$id": "https://scrambler/test",
    "description": "A fixture schema for the compiler tests.",
    "$defs": {
        "nodeId": {"type": "string", "description": "A node id."},
        "label": {"type": "string", "description": "A display label."},
        "Widget": {
            "type": "object",
            "additionalProperties": False,
            "description": "A widget node.",
            "properties": {
                "id": {"$ref": "#/$defs/nodeId"},
                "label": {"$ref": "#/$defs/label"},
            },
            "required": ["id", "label"],
            "x-kuzu": {"table": "node", "primaryKey": "id"},
        },
        "LINKS": {
            "type": "object",
            "additionalProperties": False,
            "description": "A widget links to another widget.",
            "properties": {},
            "x-kuzu": {"table": "rel", "pairs": [["Widget", "Widget"]]},
        },
    },
}


def test_dialect_is_itself_valid_json_schema():
    """Proves the bundled dialect meta-schema is a well-formed JSON Schema document."""
    jsonschema.Draft202012Validator.check_schema(scrambler.dialect())


def test_fixture_validates_against_dialect():
    """Proves a document built to the dialect is a legal schema (compilable by construction)."""
    # Pin the validator: the house-style draft/2023-02 URI isn't in jsonschema's registry.
    jsonschema.Draft202012Validator(scrambler.dialect()).validate(FIXTURE)


def test_compiled_ddl_is_accepted_by_kuzu():
    """Proves every generated CREATE statement is valid Kùzu the engine executes."""
    db = ryugraph.Database(str(Path(tempfile.mkdtemp()) / "probe"))
    conn = ryugraph.Connection(db)
    for statement in scrambler.schema_ddls(FIXTURE):
        conn.execute(statement)
    assert scrambler.node_labels(FIXTURE) == ("Widget",)


def test_node_record_round_trips_through_storage():
    """Proves a compiled dataclass encodes to a storage row unchanged."""
    widget = scrambler.dataclass_for("Widget", FIXTURE)
    record = widget(id="w1", label="hello")
    row = scrambler.encode_row("Widget", FIXTURE, record)
    assert row == {"id": "w1", "label": "hello"}


def test_decode_row_inverts_encode_row():
    """Proves decode_row recovers a record's field values from the row encode_row produced."""
    widget = scrambler.dataclass_for("Widget", FIXTURE)
    row = scrambler.encode_row("Widget", FIXTURE, widget(id="w1", label="hello"))
    assert scrambler.decode_row("Widget", FIXTURE, row) == {"id": "w1", "label": "hello"}


def test_decode_into_rebuilds_the_dataclass_instance():
    """Proves decode_into turns a stored row back into a dataclass_for instance."""
    record = scrambler.decode_into("Widget", FIXTURE, {"id": "w1", "label": "hello"})
    assert (record.id, record.label) == ("w1", "hello")


# A node with two scalar-codec fields — each must get its own projection column.
SCALARS = {
    "$schema": "https://json-schema.org/draft/2023-02/schema",
    "$id": "https://scrambler/scalars",
    "$defs": {
        "nodeId": {"type": "string", "description": "A node id."},
        "metric": {
            "oneOf": [{"type": "number"}, {"type": "string"}],
            "description": "A scalar metric.",
            "x-kuzu": {"codec": "scalar"},
        },
        "Gauge": {
            "type": "object",
            "additionalProperties": False,
            "description": "Two scalar metrics on one node.",
            "properties": {
                "id": {"$ref": "#/$defs/nodeId"},
                "a": {"$ref": "#/$defs/metric"},
                "b": {"$ref": "#/$defs/metric"},
            },
            "x-kuzu": {"table": "node", "primaryKey": "id"},
        },
    },
}


def test_scalar_fields_get_distinct_projection_columns():
    """Two scalar-codec fields must not collide on a shared 'num_value' projection column."""
    ddl = scrambler.node_ddl("Gauge", SCALARS["$defs"]["Gauge"], SCALARS["$defs"])
    assert "a_num DOUBLE" in ddl
    assert "b_num DOUBLE" in ddl


# A node whose only column is its primary key, and a base fragment a node overrides.
EDGES = {
    "$schema": "https://json-schema.org/draft/2023-02/schema",
    "$id": "https://scrambler/edges",
    "$defs": {
        "nodeId": {"type": "string", "description": "A node id."},
        "label": {"type": "string", "description": "A display label."},
        "named": {
            "type": "object",
            "additionalProperties": False,
            "description": "A base fragment carrying a label.",
            "properties": {"label": {"$ref": "#/$defs/label"}},
            "x-kuzu": {"role": "base"},
        },
        "Tag": {
            "type": "object",
            "additionalProperties": False,
            "description": "A node with only a primary key.",
            "properties": {"id": {"$ref": "#/$defs/nodeId"}},
            "x-kuzu": {"table": "node", "primaryKey": "id"},
        },
        "Item": {
            "type": "object",
            "additionalProperties": False,
            "description": "A node that re-declares its base fragment's label field.",
            "allOf": [{"$ref": "#/$defs/named"}],
            "properties": {
                "id": {"$ref": "#/$defs/nodeId"},
                "label": {"$ref": "#/$defs/label"},
            },
            "x-kuzu": {"table": "node", "primaryKey": "id"},
        },
    },
}


def test_merge_for_primary_key_only_node_omits_set():
    """A node with no non-PK columns yields a bare MERGE, not a trailing empty SET."""
    assert scrambler.merge_for("Tag", EDGES) == "MERGE (n:Tag {id: $id})"


def test_own_property_overrides_base_field():
    """A field re-declared over an allOf base fragment yields one column, not a duplicate."""
    ddl = scrambler.node_ddl("Item", EDGES["$defs"]["Item"], EDGES["$defs"])
    assert ddl.count("label STRING") == 1


# A node with a mutable (list) default supplied at the property's ref usage.
DEFAULTS = {
    "$schema": "https://json-schema.org/draft/2023-02/schema",
    "$id": "https://scrambler/defaults",
    "$defs": {
        "nodeId": {"type": "string", "description": "A node id."},
        "label": {"type": "string", "description": "A display label."},
        "tags": {
            "type": "array",
            "items": {"$ref": "#/$defs/label"},
            "description": "A JSON-encoded list of tags.",
            "x-kuzu": {"encode": "json"},
        },
        "Doc": {
            "type": "object",
            "additionalProperties": False,
            "description": "A node with a mutable default.",
            "properties": {
                "id": {"$ref": "#/$defs/nodeId"},
                "tags": {"$ref": "#/$defs/tags", "default": []},
            },
            "x-kuzu": {"table": "node", "primaryKey": "id"},
        },
    },
}


def test_mutable_default_does_not_crash_and_is_not_shared():
    """A list/dict default compiles via a factory; two instances must not share the object."""
    doc = scrambler.dataclass_for("Doc", DEFAULTS)
    first, second = doc(id="a"), doc(id="b")
    first.tags.append("x")
    assert second.tags == []
