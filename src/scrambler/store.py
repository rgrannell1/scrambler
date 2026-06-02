"""The Scrambler facade: one object wrapping a connection and a defined schema document.

The compile layer (schema_ddls, dataclass_for, merge_for, encode_row) is a set of pure
functions that each take a (label, document) pair. This module bundles them behind a small
stateful interface so a consumer opens a connection and adopts a schema once, then derives
dataclasses and writes rows without re-threading the document and a stringly-typed label
through every call. The compilers stay pure; this is the only module that touches a live
connection or validates a document, and it raises clear errors when a label is not a node.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any, Self

import jsonschema
import ryugraph

from scrambler.compile import (
    check_schema,
    column_names,
    dataclass_for,
    decode_row,
    dialect,
    encode_mapping,
    equality_filter,
    merge_for,
    merge_many_for,
    node_labels,
    record_schema,
    schema_ddls,
)
from scrambler.errors import NotDefinedError, QueryError, RecordError, UnknownLabelError

type SchemaDocument = dict[str, Any]
type ConnectionSource = ryugraph.Connection | ryugraph.Database | str | Path
type Opened = tuple[ryugraph.Connection, ryugraph.Database | None, bool, bool]


def open_source(source: ConnectionSource) -> Opened:
    """(connection, database, owns_connection, owns_database) for a connection, database, or path.

    scrambler owns — and so close()s — only what it creates: nothing for a borrowed connection,
    the connection for a borrowed database, both for a path it opens itself.
    """
    if isinstance(source, ryugraph.Connection):
        return source, None, False, False
    if isinstance(source, ryugraph.Database):
        return ryugraph.Connection(source), source, True, False
    if isinstance(source, (str, Path)):
        database = ryugraph.Database(str(source))
        return ryugraph.Connection(database), database, True, True
    raise TypeError(
        f"Scrambler needs a Connection, Database, or path; got {type(source).__name__}."
    )


def run(connection: ryugraph.Connection, statement: str, parameters: dict, action: str) -> Any:
    """Execute a statement, translating a ryugraph engine error into a QueryError with context."""
    try:
        return connection.execute(statement, parameters)
    except RuntimeError as error:
        raise QueryError(f"{action} failed: {error}") from error


def validate(schema: SchemaDocument) -> None:
    """Check a document against the bundled dialect, pinning the draft 2020-12 validator.

    The house draft/2023-02 URI isn't in jsonschema's registry, so the validator is pinned
    explicitly rather than resolved from the document's own $schema.
    """
    jsonschema.Draft202012Validator(dialect()).validate(schema)


class _Reader:
    """The `scrambler.read` namespace: schema-aware reads, decoded into dataclasses.

    Holds no state of its own — it borrows the owning Scrambler's connection, document, and
    per-label dataclass cache. A verb-view, not an independent store.
    """

    def __init__(self, store: "Scrambler") -> None:
        self._store = store

    def get(self, label: str, key: Any) -> Any | None:
        """Fetch one node by primary-key value, decoded into its dataclass — None if absent."""
        store = self._store
        document = store.node_or_raise(label)
        primary_key = document["$defs"][label]["x-kuzu"]["primaryKey"]
        columns = column_names(label, document)
        returns = ", ".join(f"n.{name}" for name in columns)
        result = run(store.connection,
                     f"MATCH (n:{label} {{{primary_key}: $key}}) RETURN {returns}",
                     {"key": key}, f"get {label}")
        if not result.has_next():
            return None
        row = dict(zip(columns, result.get_next(), strict=True))
        return store.schema.dataclass(label)(**decode_row(label, document, row))

    def all(self, label: str, where: dict | None = None) -> list[Any]:
        """Every node of a type, each decoded into its dataclass.

        `where` is an optional equality filter (field -> required value, ANDed); its keys must
        be fields of the node type and its values are encoded through their codecs to match the
        stored form. Pass it to scope the read — a bare all() spans every stored node of the type.
        """
        store = self._store
        document = store.node_or_raise(label)
        columns = column_names(label, document)
        clause, params = equality_filter(label, document, where or {})
        returns = ", ".join(f"n.{name}" for name in columns)
        rows = run(store.connection, f"MATCH (n:{label}){clause} RETURN {returns}",
                   params, f"read {label}").get_all()
        cls = store.schema.dataclass(label)
        return [cls(**decode_row(label, document, dict(zip(columns, row, strict=True))))
                for row in rows]


class _Writer:
    """The `scrambler.write` namespace: MERGE nodes through their codecs.

    Borrows the owning Scrambler's connection, document guard, and validators; owns no state.
    """

    def __init__(self, store: "Scrambler") -> None:
        self._store = store

    def insert_mapping(self, label: str, values: dict) -> None:
        """MERGE one node from a field->value mapping — the generic write kernel.

        Use this when the primary key is computed externally, or the record's class name
        differs from the node label. `values` must cover every field of the node type. Every
        codec, including a native MAP column, supplies its own Cypher binding, so a node with
        any admitted field type writes through this one path.
        """
        store = self._store
        document = store.node_or_raise(label)
        if store.validating:
            store.schema.validate_record(label, values)
        statement = merge_for(label, document)
        parameters = encode_mapping(label, document, values)
        run(store.connection, statement, parameters, f"insert into {label}")

    def insert_many(self, label: str, rows: list[dict]) -> None:
        """MERGE many nodes of one type in a single batched UNWIND statement.

        Each row is a field->value mapping (as insert_mapping takes); far faster than a loop
        for bulk loads. An empty `rows` is a no-op. Validated per row when validate=True.
        """
        if not rows:
            return
        store = self._store
        document = store.node_or_raise(label)
        if store.validating:
            for row in rows:
                store.schema.validate_record(label, row)
        parameters = {"rows": [encode_mapping(label, document, row) for row in rows]}
        run(store.connection, merge_many_for(label, document), parameters,
            f"bulk insert into {label}")

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
        store = self._store
        document = store.defined_document()
        for label in node_labels(document):
            run(store.connection, f"MATCH (n:{label}) DETACH DELETE n", {}, f"clear {label}")


class _Schema:
    """The `scrambler.schema` namespace: derive per-label artifacts from the adopted document.

    Caches (dataclasses, validators) live on the owning Scrambler; this view reads and fills
    them. `define` stays on the facade — it is once-only setup, not a repeated query.
    """

    def __init__(self, store: "Scrambler") -> None:
        self._store = store

    @property
    def labels(self) -> tuple[str, ...]:
        """The node-table labels in the defined schema, in document order."""
        return node_labels(self._store.defined_document())

    def dataclass(self, label: str) -> type:
        """The runtime dataclass for a node type; raises if `label` isn't a node in the schema.

        Cached per label, so every record of a type — built here or read back by read.get/all —
        shares one class, and instances compare equal and pass isinstance against it.
        """
        store = self._store
        document = store.node_or_raise(label)
        if label not in store.dataclasses:
            store.dataclasses[label] = dataclass_for(label, document)
        return store.dataclasses[label]

    def validate_record(self, label: str, values: dict) -> None:
        """Check `values` against the node type's JSON Schema (type, enum, required, no extras).

        Raises RecordError on the first violation. The compiled validator is cached per label.
        This runs automatically on writes when the store was opened with validate=True.
        """
        store = self._store
        document = store.node_or_raise(label)
        if label not in store.validators:
            store.validators[label] = jsonschema.Draft202012Validator(
                record_schema(label, document))
        try:
            store.validators[label].validate(values)
        except jsonschema.ValidationError as error:
            raise RecordError(f"invalid {label} record: {error.message}") from error


class Scrambler:
    """A connection plus a defined schema: define once, then derive dataclasses and write rows.

    Operations are grouped into namespace facades beneath this instance: reads through
    `scrambler.read` (get, all), writes through `scrambler.write` (insert, insert_mapping,
    insert_many, clear), and schema-derived artifacts through `scrambler.schema` (dataclass,
    labels, validate_record). The instance itself owns the connection, the adopted document,
    and the per-label caches; the namespaces borrow them. `define`, `transaction`, and the
    context-manager lifecycle stay on the facade.
    """

    def __init__(self, source: ConnectionSource, *, validate: bool = False) -> None:
        self.connection, self.database, self.owns_connection, self.owns_database = open_source(source)
        self.validating = validate
        self.document: SchemaDocument | None = None
        self.dataclasses: dict[str, type] = {}
        self.validators: dict[str, jsonschema.protocols.Validator] = {}
        self.read = _Reader(self)
        self.write = _Writer(self)
        self.schema = _Schema(self)

    def define(self, schema: SchemaDocument, *, clear: bool = False) -> Self:
        """Validate a document, create its node and relationship tables, and adopt it.

        Tables are created with IF NOT EXISTS, so re-defining is safe; clear=True then empties
        every node table (DETACH DELETE) so the store starts from a known-empty state. The
        document is checked for shape (the dialect) and for lowering semantics (check_schema).
        """
        validate(schema)
        check_schema(schema)
        for statement in schema_ddls(schema):
            run(self.connection, statement, {}, "create schema")
        self.document = schema
        if clear:
            self.write.clear()
        return self

    def defined_document(self) -> SchemaDocument:
        """The adopted document, or a clear error if define() hasn't run yet."""
        if self.document is None:
            raise NotDefinedError("No schema defined; call define(schema) first.")
        return self.document

    def node_or_raise(self, label: str) -> SchemaDocument:
        """The document, guaranteed to define node type `label`; else a clear error."""
        document = self.defined_document()
        labels = node_labels(document)
        if label not in labels:
            available = ", ".join(labels) or "(none)"
            raise UnknownLabelError(
                f"{label!r} is not a node type in the schema; available: {available}."
            )
        return document

    @contextmanager
    def transaction(self) -> Iterator[Self]:
        """Run a block atomically: COMMIT on clean exit, ROLLBACK if it raises.

        Writes inside the block (insert, insert_many, …) commit together or not at all.
        """
        run(self.connection, "BEGIN TRANSACTION", {}, "begin transaction")
        try:
            yield self
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        run(self.connection, "COMMIT", {}, "commit")

    def close(self) -> None:
        """Close the connection (and database) scrambler opened; a no-op for borrowed handles."""
        if self.owns_connection:
            self.connection.close()
        if self.owns_database and self.database is not None:
            self.database.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exception: object) -> None:
        self.close()
