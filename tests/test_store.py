"""Tests for the Scrambler facade — the connection + defined-schema wrapper.

Proves the facade creates the schema, hands back working dataclasses, writes records back to
the graph, reports tidy errors for unknown labels, and clears existing data on demand. Uses a
small self-contained fixture so the tests don't depend on any consumer's schema.
"""

import tempfile
from pathlib import Path

import pytest
import ryugraph

import scrambler

# One node type (string id + label) and one self-relationship — enough to exercise the facade.
FIXTURE = {
    "$schema": "https://json-schema.org/draft/2023-02/schema",
    "$id": "https://scrambler/store-test",
    "description": "A fixture schema for the Scrambler facade tests.",
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


def temp_db_path() -> str:
    """A fresh on-disk database path for one test."""
    return str(Path(tempfile.mkdtemp()) / "probe")


def widget_rows(scram: scrambler.Scrambler) -> list:
    """Every (id, label) pair currently stored on the Widget table."""
    return scram.connection.execute("MATCH (w:Widget) RETURN w.id, w.label").get_all()


def test_define_creates_the_schema_and_reports_its_node_labels():
    """Proves define() runs the DDL so the declared node labels are then queryable."""
    scram = scrambler.Scrambler(temp_db_path()).define(FIXTURE)
    assert scram.labels == ("Widget",)


def test_define_accepts_an_existing_connection():
    """Proves the constructor takes a live connection, not only a path."""
    connection = ryugraph.Connection(ryugraph.Database(temp_db_path()))
    scram = scrambler.Scrambler(connection).define(FIXTURE)
    assert scram.labels == ("Widget",)


def test_insert_round_trips_a_dataclass_record_into_the_graph():
    """Proves a record built from the compiled dataclass is stored and read back unchanged."""
    scram = scrambler.Scrambler(temp_db_path()).define(FIXTURE)
    widget = scram.dataclass("Widget")
    scram.insert(widget(id="w1", label="hello"))
    assert widget_rows(scram) == [["w1", "hello"]]


def test_insert_mapping_writes_a_node_from_an_explicit_label_and_values():
    """Proves insert_mapping writes a node from a (label, values) pair — no dataclass needed."""
    scram = scrambler.Scrambler(temp_db_path()).define(FIXTURE)
    scram.insert_mapping("Widget", {"id": "w1", "label": "hello"})
    assert widget_rows(scram) == [["w1", "hello"]]


def test_insert_mapping_rejects_an_unknown_label():
    """Proves insert_mapping fails fast when the label isn't a node type in the schema."""
    scram = scrambler.Scrambler(temp_db_path()).define(FIXTURE)
    with pytest.raises(ValueError, match=r"not a node type.*Widget"):
        scram.insert_mapping("Gadget", {"id": "g1"})


def test_dataclass_for_unknown_label_raises_with_available_labels():
    """Proves an unknown label fails fast with a message naming the real node labels."""
    scram = scrambler.Scrambler(temp_db_path()).define(FIXTURE)
    with pytest.raises(ValueError, match=r"not a node type.*Widget"):
        scram.dataclass("Gadget")


def test_methods_before_define_raise_a_clear_error():
    """Proves using the store before define() points the caller at define()."""
    scram = scrambler.Scrambler(temp_db_path())
    with pytest.raises(RuntimeError, match="call define"):
        scram.dataclass("Widget")


# A node carrying a native Kùzu MAP column — the codec that can't bind a dict directly.
MAPS = {
    "$schema": "https://json-schema.org/draft/2023-02/schema",
    "$id": "https://scrambler/store-maps",
    "description": "A node with a native map column.",
    "$defs": {
        "nodeId": {"type": "string", "description": "A node id."},
        "count": {"type": "integer", "description": "A tally."},
        "counts": {
            "type": "object",
            "additionalProperties": {"$ref": "#/$defs/count"},
            "description": "A string-keyed map of counts.",
            "x-kuzu": {"codec": "map"},
        },
        "Tally": {
            "type": "object",
            "additionalProperties": False,
            "description": "A node with a native map column.",
            "properties": {
                "id": {"$ref": "#/$defs/nodeId"},
                "counts": {"$ref": "#/$defs/counts"},
            },
            "x-kuzu": {"table": "node", "primaryKey": "id"},
        },
    },
}


def test_insert_round_trips_a_native_map_column():
    """Proves a native MAP node writes via map($keys, $values), not a dict bound as a STRUCT."""
    scram = scrambler.Scrambler(temp_db_path()).define(MAPS)
    tally = scram.dataclass("Tally")
    scram.insert(tally(id="t1", counts={"a": 1, "b": 2}))
    stored = scram.connection.execute("MATCH (n:Tally) RETURN n.counts").get_all()
    assert stored == [[{"a": 1, "b": 2}]]


def test_define_with_clear_empties_existing_node_data():
    """Proves clear=True wipes rows left by a previous define against the same database."""
    path = temp_db_path()
    first = scrambler.Scrambler(path).define(FIXTURE)
    widget = first.dataclass("Widget")
    first.insert(widget(id="w1", label="hello"))
    second = scrambler.Scrambler(path).define(FIXTURE, clear=True)
    assert widget_rows(second) == []
