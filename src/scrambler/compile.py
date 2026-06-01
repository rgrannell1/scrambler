"""Compilers: a Layer-2 JSON Schema document -> Kùzu DDL and runtime dataclasses.

Consumes a schema document whose $defs hold node types (x-kuzu.table == "node"),
relationship types (== "rel"), and reusable field types. Every admitted construct
lowers via a codec constructor from schema_dsl, so the Layer-1 dialect (dialect.json)
and these compilers stay two faces of one contract: anything the dialect admits must
lower here.
"""

import json
from dataclasses import field as dc_field
from dataclasses import make_dataclass
from pathlib import Path
from typing import Any

from scrambler.schema_dsl import (
    _MISSING,
    Bool,
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
    return defs[schema["$ref"].split("/")[-1]] if "$ref" in schema else schema


def codec_for(schema: dict, defs: dict):
    """Map a property schema (possibly a $ref) to a codec constructor (name -> Codec)."""
    target = resolve(schema, defs)
    x_kuzu = target.get("x-kuzu", {})
    if x_kuzu.get("codec") == "scalar":
        return Scalar
    if x_kuzu.get("codec") == "union":
        return NativeUnion(x_kuzu["members"], x_kuzu["projection"])
    declared = target.get("type")
    nullable = isinstance(declared, list) and "null" in declared
    base_type = (next(t for t in declared if t != "null")
                 if isinstance(declared, list) else declared)
    if base_type == "array":
        kind = (ListStr if x_kuzu.get("encode") == "json"
                else NativeList(element_kuzu(target, defs)))
    elif base_type == "object":
        kind = (NativeMap("STRING", value_kuzu(target, defs))
                if x_kuzu.get("codec") == "map" else Json)
    else:
        kind = SCALAR_CODECS[base_type]
    return Opt(kind) if nullable else kind


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
    fields: list[tuple[str, dict]] = []
    for parent in node.get("allOf", ()):
        fields.extend(resolve(parent, defs).get("properties", {}).items())
    fields.extend(node.get("properties", {}).items())
    return fields


def node_ddl(label: str, node: dict, defs: dict) -> str:
    """CREATE NODE TABLE for one node type (canonical columns then projections)."""
    columns = []
    for name, schema in node_fields(node, defs):
        codec = codec_for(schema, defs)(name)
        columns.extend(f"{column.name} {column.kuzu}"
                       for column in (*codec.columns, *codec.projections))
    primary_key = node["x-kuzu"]["primaryKey"]
    return (f"CREATE NODE TABLE IF NOT EXISTS {label}"
            f"({', '.join(columns)}, PRIMARY KEY({primary_key}))")


def merge_for(label: str, document: dict) -> str:
    """The idempotent MERGE statement for one node type, generated from its columns."""
    defs = document["$defs"]
    node = defs[label]
    columns = []
    for name, schema in node_fields(node, defs):
        codec = codec_for(schema, defs)(name)
        columns.extend((*codec.columns, *codec.projections))
    primary_key = node["x-kuzu"]["primaryKey"]
    sets = ", ".join(f"n.{column.name} = ${column.name}"
                     for column in columns if column.name != primary_key)
    return f"MERGE (n:{label} {{{primary_key}: ${primary_key}}}) SET {sets}"


def encode_mapping(label: str, document: dict, values: dict) -> dict:
    """Encode a full field->value mapping into a parameters dict (columns + projections)."""
    defs = document["$defs"]
    params: dict = {}
    for name, schema in node_fields(defs[label], defs):
        codec = codec_for(schema, defs)(name)
        params.update(codec.encode(values[name]))
        params.update(codec.project(values[name]))
    return params


def identity_fields(label: str, document: dict) -> list[str]:
    """The fields whose values form a node's id (x-kuzu.identity)."""
    return document["$defs"][label]["x-kuzu"]["identity"]


def rel_ddl(label: str, rel: dict) -> str:
    """CREATE REL TABLE for one relationship type (its FROM/TO pairs)."""
    pairs = ", ".join(f"FROM {source} TO {target}" for source, target in rel["x-kuzu"]["pairs"])
    return f"CREATE REL TABLE IF NOT EXISTS {label}({pairs})"


def is_kind(definition: dict, kind: str) -> bool:
    return definition.get("x-kuzu", {}).get("table") == kind


def schema_ddls(document: dict) -> list[str]:
    """Every node table then every relationship table, in document order."""
    defs = document["$defs"]
    nodes = [node_ddl(label, d, defs) for label, d in defs.items() if is_kind(d, "node")]
    rels = [rel_ddl(label, d) for label, d in defs.items() if is_kind(d, "rel")]
    return nodes + rels


def node_labels(document: dict) -> tuple[str, ...]:
    """The node-table labels, in document order (the source for clear_package)."""
    return tuple(label for label, d in document["$defs"].items() if is_kind(d, "node"))


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
            defaulted.append((name, annotation, dc_field(default=default)))
    cls = make_dataclass(label, [*plain, *defaulted], frozen=True)
    cls.__doc__ = node.get("description", "")
    return cls


def encode_row(label: str, document: dict, record: Any) -> dict:
    """Stored-record -> parameters dict (canonical columns + projections)."""
    defs = document["$defs"]
    row: dict = {}
    for name, schema in node_fields(defs[label], defs):
        codec, value = codec_for(schema, defs)(name), getattr(record, name)
        row.update(codec.encode(value))
        row.update(codec.project(value))
    return row
