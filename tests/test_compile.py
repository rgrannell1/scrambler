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
