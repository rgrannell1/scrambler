"""scrambler — compile a JSON-Schema dialect into a Kùzu schema and runtime dataclasses.

A schema document is a JSON Schema constrained to the *dialect* (dialect.json): the subset
that lowers, by construction, to Kùzu node/relationship-table DDL and matching dataclasses.
The compilers live in `compile`; `dialect()` returns the meta-schema to validate against.
"""

from scrambler.compile import (
    codec_for,
    dataclass_for,
    dialect,
    dialect_path,
    encode_mapping,
    encode_row,
    identity_fields,
    load_schema,
    merge_for,
    node_ddl,
    node_fields,
    node_labels,
    rel_ddl,
    schema_ddls,
)

__all__ = [
    "codec_for",
    "dataclass_for",
    "dialect",
    "dialect_path",
    "encode_mapping",
    "encode_row",
    "identity_fields",
    "load_schema",
    "merge_for",
    "node_ddl",
    "node_fields",
    "node_labels",
    "rel_ddl",
    "schema_ddls",
]
