"""The Scrambler facade: one object wrapping a connection and a defined schema document."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any, Self

import jsonschema
import ryugraph

from scrambler import query
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
    """(connection, database, owns_connection, owns_database) for a connection, database, or
    path."""

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
    """

    jsonschema.Draft202012Validator(dialect()).validate(schema)


class _Reader:
    """The `scrambler.read` namespace: schema-aware reads, decoded into dataclasses."""

    def __init__(self, store: "Scrambler") -> None:
        self._store = store

    def get(self, label: str, key: Any) -> Any | None:
        """Fetch one node by primary-key value, decoded into its dataclass — None if absent."""

        store = self._store
        document = store.node_or_raise(label)
        primary_key = document["$defs"][label]["x-kuzu"]["primaryKey"]
        columns = column_names(label, document)

        statement = query.match_by_key(label, primary_key, columns)
        result = run(store.connection, statement, {"key": key}, f"get {label}")

        if not result.has_next():
            return None
        
        row = dict(zip(columns, result.get_next(), strict=True))
        
        return store.schema.dataclass(label)(**decode_row(label, document, row))

    def all(self, label: str, where: dict | None = None) -> list[Any]:
        """Every node of a type, each decoded into its dataclass."""

        store = self._store
        document = store.node_or_raise(label)
        columns = column_names(label, document)
        terms, params = equality_filter(label, document, where or {})

        statement = query.match_all(label, columns, terms)
        rows = run(store.connection, statement, params, f"read {label}").get_all()
        cls = store.schema.dataclass(label)
        
        return [cls(**decode_row(label, document, dict(zip(columns, row, strict=True))))
                for row in rows]


class _Writer:
    """The `scrambler.write` namespace: MERGE nodes through their codecs"""

    def __init__(self, store: "Scrambler") -> None:
        self._store = store

    def insert_mapping(self, label: str, values: dict) -> None:
        """MERGE one node from a field->value mapping — the generic write kernel.

        Use this when the primary key is computed externally, or the record's class name
        differs from the node label.
        """

        store = self._store
        document = store.node_or_raise(label)
        
        if store.validating:
            store.schema.validate_record(label, values)
        
        statement = merge_for(label, document)
        parameters = encode_mapping(label, document, values)
        
        run(store.connection, statement, parameters, f"insert into {label}")

    def insert_many(self, label: str, rows: list[dict]) -> None:
        """MERGE many nodes of one type in a single batched UNWIND statement."""

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
        """MERGE one node record — the convenience wrapper over insert_mapping."""

        values = {field.name: getattr(record, field.name) for field in dataclass_fields(record)}
        
        self.insert_mapping(type(record).__name__, values)

    def clear(self) -> None:
        """Empty every node table; DETACH DELETE removes the nodes and their relationships."""
        
        store = self._store
        document = store.defined_document()
        
        for label in node_labels(document):
            run(store.connection, query.detach_delete(label), {}, f"clear {label}")


class _Schema:
    """The `scrambler.schema` namespace: derive per-label artifacts from the adopted document"""

    def __init__(self, store: "Scrambler") -> None:
        self._store = store

    def labels(self) -> tuple[str, ...]:
        """The node-table labels in the defined schema, in document order."""

        return node_labels(self._store.defined_document())

    def dataclass(self, label: str) -> type:
        """The runtime dataclass for a node type; raises if `label` isn't a node in the schema."""

        store = self._store
        document = store.node_or_raise(label)
        
        if label not in store.dataclasses:
            store.dataclasses[label] = dataclass_for(label, document)
        
        return store.dataclasses[label]

    def validate_record(self, label: str, values: dict) -> None:
        """Check `values` against the node type's JSON Schema (type, enum, required, no extras)"""
        
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
    """A connection plus a defined schema: define once, then derive dataclasses and write rows."""

    def __init__(self, source: ConnectionSource, *, validate: bool = False) -> None:

        (self.connection, self.database,
         self.owns_connection, self.owns_database) = open_source(source)
        self.validating = validate
        self.document: SchemaDocument | None = None
        self.dataclasses: dict[str, type] = {}
        self.validators: dict[str, jsonschema.protocols.Validator] = {}
        self.read = _Reader(self)
        self.write = _Writer(self)
        self.schema = _Schema(self)

    def define(self, schema: SchemaDocument, *, clear: bool = False) -> Self:
        """Validate a document, create its node and relationship tables, and adopt it."""
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
        """Run a block atomically: COMMIT on clean exit, ROLLBACK if it raises."""

        run(self.connection, query.BEGIN_TRANSACTION, {}, "begin transaction")

        try:
            yield self
        except BaseException:
            self.connection.execute(query.ROLLBACK)
            raise

        run(self.connection, query.COMMIT, {}, "commit")

    def close(self) -> None:
        """Close the connection (and database) scrambler opened; a no-op for borrowed handle."""
        
        if self.owns_connection:
            self.connection.close()
        
        if self.owns_database and self.database is not None:
            self.database.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exception: object) -> None:
        self.close()
