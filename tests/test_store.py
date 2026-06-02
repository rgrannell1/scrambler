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


def test_insert_then_get_round_trips_a_record_through_the_facade():
    """Proves a record written with insert reads back equal via get — no raw Cypher needed."""
    scram = scrambler.Scrambler(temp_db_path()).define(FIXTURE)
    widget = scram.dataclass("Widget")
    scram.insert(widget(id="w1", label="hello"))
    assert scram.get("Widget", "w1") == widget(id="w1", label="hello")


def test_get_returns_none_for_a_missing_node():
    """Proves get yields None when no node has the given primary-key value."""
    scram = scrambler.Scrambler(temp_db_path()).define(FIXTURE)
    assert scram.get("Widget", "nope") is None


def test_all_returns_every_node_decoded():
    """Proves all returns one decoded record per stored node of the type."""
    scram = scrambler.Scrambler(temp_db_path()).define(FIXTURE)
    widget = scram.dataclass("Widget")
    scram.insert(widget(id="w1", label="a"))
    scram.insert(widget(id="w2", label="b"))
    assert set(scram.all("Widget")) == {widget(id="w1", label="a"), widget(id="w2", label="b")}


def test_all_with_where_filters_by_equality():
    """Proves all(label, where=...) returns only nodes whose columns equal the given values."""
    scram = scrambler.Scrambler(temp_db_path()).define(FIXTURE)
    widget = scram.dataclass("Widget")
    scram.insert(widget(id="w1", label="keep"))
    scram.insert(widget(id="w2", label="drop"))
    assert scram.all("Widget", {"label": "keep"}) == [widget(id="w1", label="keep")]


def test_all_with_unknown_filter_field_raises():
    """Proves a where key that isn't a field fails fast (no silent match, no Cypher injection)."""
    scram = scrambler.Scrambler(temp_db_path()).define(FIXTURE)
    with pytest.raises(ValueError, match=r"no field.*missing.*available"):
        scram.all("Widget", {"missing": "x"})


def test_all_where_filters_a_json_encoded_column_by_value():
    """Proves where on a JSON-encoded column matches the stored encoding, not the raw value."""
    encoded = {
        "$schema": "https://json-schema.org/draft/2023-02/schema",
        "$id": "https://scrambler/store-encoded",
        "description": "A node with a JSON-encoded list column.",
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
                "description": "A doc with a json list.",
                "properties": {
                    "id": {"$ref": "#/$defs/nodeId"},
                    "tags": {"$ref": "#/$defs/tags"},
                },
                "x-kuzu": {"table": "node", "primaryKey": "id"},
            },
        },
    }
    scram = scrambler.Scrambler(temp_db_path()).define(encoded)
    doc = scram.dataclass("Doc")
    scram.insert(doc(id="d1", tags=["a", "b"]))
    scram.insert(doc(id="d2", tags=["c"]))
    assert scram.all("Doc", {"tags": ["a", "b"]}) == [doc(id="d1", tags=["a", "b"])]


def test_all_where_rejects_a_non_equality_filterable_column():
    """Proves filtering on a native MAP column fails fast rather than crashing inside Kùzu."""
    scram = scrambler.Scrambler(temp_db_path()).define(MAPS)
    with pytest.raises(ValueError, match=r"not equality-filterable"):
        scram.all("Tally", {"counts": {"a": 1}})


def test_records_share_the_cached_dataclass():
    """Proves dataclass/get share one cached type, so records compare equal and isinstance works."""
    scram = scrambler.Scrambler(temp_db_path()).define(FIXTURE)
    widget = scram.dataclass("Widget")
    scram.insert(widget(id="w1", label="hello"))
    assert isinstance(scram.get("Widget", "w1"), widget)
    assert scram.dataclass("Widget") is widget


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
