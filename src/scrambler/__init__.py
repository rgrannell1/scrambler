"""scrambler — compile a JSON-Schema dialect into a Kùzu schema and runtime dataclasses.

The public entry point is `Scrambler`: open a connection, `define` a schema document, then
derive dataclasses and write rows — it calls the compile layer for you. Those compile-layer
functions live in `scrambler.compile` and stay importable from here for the dreamfish consumer
(which imports them by name), but they are plumbing, not the headline API; new code should use
`Scrambler`, or import the functions explicitly from `scrambler.compile`.
"""

# Re-exported for the dreamfish consumer, which imports these by name. They are the plumbing
# `Scrambler` already calls — kept importable, but deliberately left out of `__all__` below so
# the package advertises one entry point. New code should prefer `scrambler.compile`.
from scrambler.compile import (
    check_schema as check_schema,
    codec_for as codec_for,
    column_names as column_names,
    dataclass_for as dataclass_for,
    decode_into as decode_into,
    decode_row as decode_row,
    dialect as dialect,
    dialect_path as dialect_path,
    encode_mapping as encode_mapping,
    encode_row as encode_row,
    identity_fields as identity_fields,
    load_schema as load_schema,
    merge_for as merge_for,
    node_ddl as node_ddl,
    node_fields as node_fields,
    node_labels as node_labels,
    rel_ddl as rel_ddl,
    schema_ddls as schema_ddls,
)
from scrambler.store import Scrambler

__all__ = ["Scrambler"]
