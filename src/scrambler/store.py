"""The Scrambler facade: one object wrapping a connection and a defined schema document.

The compile layer (schema_ddls, dataclass_for, merge_for, encode_row) is a set of pure
functions that each take a (label, document) pair. This module bundles them behind a small
stateful interface so a consumer opens a connection and adopts a schema once, then derives
dataclasses and writes rows without re-threading the document and a stringly-typed label
through every call. The compilers stay pure; this is the only module that touches a live
connection or validates a document, and it raises clear errors when a label is not a node.
"""

from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any, Self

import jsonschema
import ryugraph

from scrambler.compile import (
    check_schema,
    dataclass_for,
    dialect,
    encode_mapping,
    merge_for,
    node_labels,
    schema_ddls,
)

type SchemaDocument = dict[str, Any]
type ConnectionSource = ryugraph.Connection | ryugraph.Database | str | Path


def connection_from(source: ConnectionSource) -> ryugraph.Connection:
    """A live connection from an existing connection, a database, or a path to open."""
    if isinstance(source, ryugraph.Connection):
        return source
    if isinstance(source, ryugraph.Database):
        return ryugraph.Connection(source)
    if isinstance(source, (str, Path)):
        return ryugraph.Connection(ryugraph.Database(str(source)))
    raise TypeError(
        f"Scrambler needs a Connection, Database, or path; got {type(source).__name__}."
    )


def validate(schema: SchemaDocument) -> None:
    """Check a document against the bundled dialect, pinning the draft 2020-12 validator.

    The house draft/2023-02 URI isn't in jsonschema's registry, so the validator is pinned
    explicitly rather than resolved from the document's own $schema.
    """
    jsonschema.Draft202012Validator(dialect()).validate(schema)


class Scrambler:
    """A connection plus a defined schema: define once, then derive dataclasses and write rows."""

    def __init__(self, source: ConnectionSource) -> None:
        self.connection = connection_from(source)
        self.document: SchemaDocument | None = None

    def define(self, schema: SchemaDocument, *, clear: bool = False) -> Self:
        """Validate a document, create its node and relationship tables, and adopt it.

        Tables are created with IF NOT EXISTS, so re-defining is safe; clear=True then empties
        every node table (DETACH DELETE) so the store starts from a known-empty state. The
        document is checked for shape (the dialect) and for lowering semantics (check_schema).
        """
        validate(schema)
        check_schema(schema)
        for statement in schema_ddls(schema):
            self.connection.execute(statement)
        self.document = schema
        if clear:
            self.clear()
        return self

    def defined_document(self) -> SchemaDocument:
        """The adopted document, or a clear error if define() hasn't run yet."""
        if self.document is None:
            raise RuntimeError("No schema defined; call define(schema) first.")
        return self.document

    def node_or_raise(self, label: str) -> SchemaDocument:
        """The document, guaranteed to define node type `label`; else a clear error."""
        document = self.defined_document()
        labels = node_labels(document)
        if label not in labels:
            available = ", ".join(labels) or "(none)"
            raise ValueError(
                f"{label!r} is not a node type in the schema; available: {available}."
            )
        return document

    def dataclass(self, label: str) -> type:
        """The runtime dataclass for a node type; raises if `label` isn't a node in the schema."""
        document = self.node_or_raise(label)
        return dataclass_for(label, document)

    def insert_mapping(self, label: str, values: dict) -> None:
        """MERGE one node from a field->value mapping — the generic write kernel.

        Use this when the primary key is computed externally, or the record's class name
        differs from the node label. `values` must cover every field of the node type. Every
        codec, including a native MAP column, supplies its own Cypher binding, so a node with
        any admitted field type writes through this one path.
        """
        document = self.node_or_raise(label)
        statement = merge_for(label, document)
        parameters = encode_mapping(label, document, values)
        self.connection.execute(statement, parameters)

    def insert(self, record: Any) -> None:
        """MERGE one node record — the convenience wrapper over insert_mapping.

        The record's dataclass name is the node label and its attributes are the field values;
        reach for insert_mapping(label, values) when the id is computed externally or the
        record class name differs from the label.
        """
        values = {field.name: getattr(record, field.name) for field in dataclass_fields(record)}
        self.insert_mapping(type(record).__name__, values)

    def clear(self) -> None:
        """Empty every node table; DETACH DELETE removes the nodes and their relationships."""
        document = self.defined_document()
        for label in node_labels(document):
            self.connection.execute(f"MATCH (n:{label}) DETACH DELETE n")

    @property
    def labels(self) -> tuple[str, ...]:
        """The node-table labels in the defined schema, in document order."""
        return node_labels(self.defined_document())
