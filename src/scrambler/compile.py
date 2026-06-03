"""Compilers: a Layer-2 JSON Schema document -> Kùzu DDL and runtime dataclasses."""

import json
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import field as dc_field, make_dataclass
from pathlib import Path
from typing import Any

from scrambler import query
from scrambler.errors import RecordError, SchemaError
from scrambler.protocols import CodecFactory
from scrambler.schema_dsl import (
    _MISSING,
    Bool,
    Codec,
    Float,
    Int,
    Json,
    ListStr,
    NativeList,
    NativeMap,
    NativeUnion,
    Opt,
    Scalar,
    Str,
    union_has_string_member,
)

# JSON Schema scalar `type` -> codec constructor.
SCALAR_CODECS = {"string": Str, "integer": Int, "number": Float, "boolean": Bool}


def load_schema(path: str | Path) -> dict:
    """Read a Layer-2 schema document from disk."""
    return json.loads(Path(path).read_text())


def dialect_path() -> Path:
    """Path to the bundled dialect meta-schema (the language a schema must conform to)."""
    return Path(__file__).parent / "dialect.json"


def dialect() -> dict:
    """The dialect meta-schema as a dict — validate a schema document against this."""
    return json.loads(dialect_path().read_text())


def resolve(schema: dict, defs: dict) -> dict:
    """Follow a single $ref into $defs; pass other schemas through unchanged."""
    if "$ref" not in schema:
        return schema
    return defs[schema["$ref"].split("/")[-1]]


def codec_for(schema: dict, defs: dict) -> CodecFactory:
    """Map a property schema (possibly a $ref) to a codec factory (name -> Codec)."""
    target = resolve(schema, defs)
    x_kuzu = target.get("x-kuzu", {})
    codec = x_kuzu.get("codec")

    if codec == "scalar":
        # Bind a field-derived projection name so two scalar fields don't collide
        # on a shared default column (x-kuzu.projection overrides it when given).
        return lambda name: Scalar(name, x_kuzu.get("projection") or f"{name}_num")

    if codec == "union":
        return NativeUnion(x_kuzu["members"], x_kuzu["projection"])

    declared = target.get("type")
    base_type = (next(t for t in declared if t != "null")
                 if isinstance(declared, list) else declared)
    if base_type == "array":
        kind = (ListStr if x_kuzu.get("encode") == "json"
                else NativeList(element_kuzu(target, defs)))
    elif base_type == "object":
        kind = (NativeMap("STRING", value_kuzu(target, defs))
                if codec == "map" else Json)
    else:
        kind = SCALAR_CODECS[base_type]
    return Opt(kind) if isinstance(declared, list) and "null" in declared else kind


def element_kuzu(array_schema: dict, defs: dict) -> str:
    """The Kùzu type of an array's items (resolved from its $ref)."""

    items = resolve(array_schema["items"], defs)
    return codec_for(items, defs)("_").columns[0].kuzu


def value_kuzu(object_schema: dict, defs: dict) -> str:
    """The Kùzu type of a map's values (resolved from additionalProperties)."""

    value = resolve(object_schema["additionalProperties"], defs)
    return codec_for(value, defs)("_").columns[0].kuzu


def node_fields(node: dict, defs: dict) -> list[tuple[str, dict]]:
    """A node's (name, schema) fields: allOf base fragments first, then own properties."""

    fields: dict[str, dict] = {}
    for parent in node.get("allOf", ()):
        fields.update(resolve(parent, defs).get("properties", {}))
    fields.update(node.get("properties", {}))
    return list(fields.items())


def bound_codecs(node: dict, defs: dict) -> Iterator[tuple[str, Codec]]:
    """Each field's (name, bound codec) — node_fields with codec_for applied."""
    for name, schema in node_fields(node, defs):
        yield name, codec_for(schema, defs)(name)


def node_ddl(label: str, node: dict, defs: dict) -> str:
    """CREATE NODE TABLE for one node type (canonical columns then projections)."""

    columns = []
    for _name, codec in bound_codecs(node, defs):
        columns.extend((column.name, column.kuzu)
                       for column in (*codec.columns, *codec.projections))
    primary_key = node["x-kuzu"]["primaryKey"]
    return query.create_node_table(label, columns, primary_key)


def merge_for(label: str, document: dict) -> str:
    """The idempotent MERGE statement for one node type, generated from its columns."""
    
    defs = document["$defs"]
    node = defs[label]
    columns = []
    for _name, codec in bound_codecs(node, defs):
        columns.extend((*codec.columns, *codec.projections))
    primary_key = node["x-kuzu"]["primaryKey"]
    assignments = [(column.name, column.binding())
                   for column in columns if column.name != primary_key]
    return query.merge_node(label, primary_key, assignments)


def merge_many_for(label: str, document: dict) -> str:
    """The UNWIND batch form of merge_for: one MERGE per row of a bound `$rows` list.

    Each `$param` in the single-row statement becomes `row.param`, so the same encoded mapping
    that feeds merge_for (columns and projections, native-MAP CASTs and all) feeds each row.
    """
    return query.unwind_rows(merge_for(label, document))


def encode_mapping(label: str, document: dict, values: dict) -> dict:
    """Encode a full field->value mapping into a parameters dict (columns + projections)."""
    defs = document["$defs"]
    params: dict = {}
    for name, codec in bound_codecs(defs[label], defs):
        params.update(codec.encode(values[name]))
        params.update(codec.project(values[name]))
    return params


def identity_fields(label: str, document: dict) -> list[str]:
    """The fields whose values form a node's id (x-kuzu.identity)."""
    return document["$defs"][label]["x-kuzu"]["identity"]


def rel_ddl(label: str, rel: dict) -> str:
    """CREATE REL TABLE for one relationship type (its FROM/TO pairs)."""
    return query.create_rel_table(label, rel["x-kuzu"]["pairs"])


def is_kind(definition: dict, kind: str) -> bool:
    return definition.get("x-kuzu", {}).get("table") == kind


def check_schema(document: dict) -> None:
    """Reject a document the dialect admits structurally but that can't round-trip faithfully.

    The dialect (a JSON Schema) checks shape; this checks lowering semantics. Currently it
    rejects a native UNION with a STRING member, since a string that looks like another
    member's literal coerces into that member and loses its type on read — use the JSON scalar
    codec (x-kuzu.codec='scalar') for a union that includes strings. Raises ValueError on the
    first violation; returns None when the document is sound.
    """
    for label, definition in document["$defs"].items():
        x_kuzu = definition.get("x-kuzu", {})
        if x_kuzu.get("codec") == "union" and union_has_string_member(x_kuzu["members"]):
            raise SchemaError(
                f"{label!r} is a native UNION with a STRING member, which can't round-trip: "
                f"numeric- or boolean-looking strings coerce into the other member. Use the "
                f"JSON scalar codec (x-kuzu.codec='scalar') for a union that includes strings."
            )


def record_schema(label: str, document: dict) -> dict:
    """The JSON Schema a node type's record must satisfy: its $defs entry plus the document's
    $defs, so the property `$ref`s (and their enum/type/required constraints) resolve."""
    defs = document["$defs"]
    return {**defs[label], "$defs": defs}


def schema_ddls(document: dict) -> list[str]:
    """Every node table then every relationship table, in document order."""
    defs = document["$defs"]
    nodes = [node_ddl(label, d, defs) for label, d in defs.items() if is_kind(d, "node")]
    rels = [rel_ddl(label, d) for label, d in defs.items() if is_kind(d, "rel")]
    return nodes + rels


def node_labels(document: dict) -> tuple[str, ...]:
    """The node-table labels, in document order (the source for clear_package)."""
    return tuple(label for label, d in document["$defs"].items() if is_kind(d, "node"))


def _field_spec(default: Any):
    """A dataclass field spec for a default; mutable list/dict defaults need a factory."""
    if isinstance(default, (list, dict)):
        # Deep-copy so instances never share the same mutable default object.
        return dc_field(default_factory=lambda value=default: deepcopy(value))
    return dc_field(default=default)


def dataclass_for(label: str, document: dict) -> type:
    """Compile the runtime stored-record dataclass for one node type."""
    defs = document["$defs"]
    node = defs[label]
    plain, defaulted = [], []
    for name, schema in node_fields(node, defs):
        annotation = codec_for(schema, defs)(name).py_type
        default = schema.get("default", _MISSING)
        if default is _MISSING:
            plain.append((name, annotation))
        else:
            defaulted.append((name, annotation, _field_spec(default)))
    cls = make_dataclass(label, [*plain, *defaulted], frozen=True)
    cls.__doc__ = node.get("description", "")
    return cls


def encode_row(label: str, document: dict, record: Any) -> dict:
    """Stored-record -> parameters dict (canonical columns + projections)."""
    defs = document["$defs"]
    row: dict = {}
    for name, codec in bound_codecs(defs[label], defs):
        value = getattr(record, name)
        row.update(codec.encode(value))
        row.update(codec.project(value))
    return row


def decode_row(label: str, document: dict, row: dict) -> dict:
    """Stored row (column name -> value) -> field name -> python value: the mirror of encode_row.

    Reads only each codec's canonical columns; derived projection columns are write-only and
    never consulted, so extra keys in `row` (e.g. a projection) are ignored.
    """
    defs = document["$defs"]
    values: dict = {}
    for name, codec in bound_codecs(defs[label], defs):
        cells = {column.name: row[column.name] for column in codec.columns}
        values[name] = codec.decode(cells)
    return values


def decode_into(label: str, document: dict, row: dict) -> Any:
    """Stored row -> an instance of dataclass_for(label): the read twin of dataclass_for.

    A fresh class is compiled per call (as dataclass_for does), so instances from separate
    calls compare unequal even with equal fields — compare by field value, not by ==.
    """
    return dataclass_for(label, document)(**decode_row(label, document, row))


def column_names(label: str, document: dict) -> list[str]:
    """The canonical (bijection) column names of a node type, in field order.

    A reader RETURNs exactly these columns and feeds the row to decode_row/decode_into;
    derived projection columns are write-only and excluded.
    """
    defs = document["$defs"]
    names: list[str] = []
    for _name, codec in bound_codecs(defs[label], defs):
        names.extend(column.name for column in codec.columns)
    return names


def equality_term(key: str, value: Any, field_schema: dict, defs: dict) -> tuple[str, dict]:
    """One filter term — `n.col = $col` (or `n.col IS NULL`) — and its parameter, for a where key.

    The value is encoded through the field's codec, so it compares against the column's *stored*
    representation. Raises if the field has no single `=` form: more than one column, or a column
    built by a Cypher expression rather than a plain bound parameter (a native MAP).
    """
    codec = codec_for(field_schema, defs)(key)
    columns = codec.columns
    if len(columns) != 1 or columns[0].bind is not None:
        raise RecordError(
            f"field {key!r} is not equality-filterable (no single `=` form); filter it in Cypher.")
    name = columns[0].name
    encoded = codec.encode(value)[name]
    if encoded is None:
        return query.is_null(name), {}
    return query.equality(name), {name: encoded}


def equality_filter(label: str, document: dict, where: dict) -> tuple[list[str], dict]:
    """The `n.col = $col` filter terms plus their encoded parameters for an equality filter.

    Each key is a field of the node type; its value is encoded through that field's codec so the
    comparison matches the stored representation (a JSON-encoded or scalar column compares against
    its encoded string, not the raw python value). Empty `where` yields ([], {}). The caller
    renders the terms into a WHERE clause (query.match_all / query.where_clause).
    """
    if not where:
        return [], {}
    defs = document["$defs"]
    fields = dict(node_fields(defs[label], defs))
    unknown = [key for key in where if key not in fields]
    if unknown:
        available = ", ".join(fields) or "(none)"
        raise RecordError(
            f"{label} has no field(s) {', '.join(unknown)} to filter on; available: {available}.")
    terms, params = [], {}
    for key, value in where.items():
        term, term_params = equality_term(key, value, fields[key], defs)
        terms.append(term)
        params.update(term_params)
    return terms, params
