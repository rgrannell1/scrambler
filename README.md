# scrambler

Compile a constrained JSON-Schema **dialect** into a Kùzu (ryugraph) graph schema — node and
relationship table DDL — and matching runtime **dataclasses**, from one source document.

A *schema document* is a JSON Schema restricted to the dialect (`dialect.json`): the subset
that lowers, by construction, to a property-graph schema. The dialect is the contract — a
document valid against it is compilable, and the compilers in `compile.py` are the other face
of that contract (anything the dialect admits must lower).

```python
import scrambler

schema = scrambler.load_schema("my.schema.json")

# 1. Kùzu DDL — CREATE NODE/REL TABLE statements, in dependency order
for ddl in scrambler.schema_ddls(schema):
    conn.execute(ddl)

# 2. Runtime dataclasses for any node type
Widget = scrambler.dataclass_for("Widget", schema)
row = scrambler.encode_row("Widget", schema, Widget(id="w1", label="hi"))

# 3. Validate a document against the dialect
import jsonschema
jsonschema.Draft202012Validator(scrambler.dialect()).validate(schema)
```

Each `$defs` entry carries an `x-kuzu` annotation: `table: "node"` (+ `primaryKey`, optional
`identity`) or `table: "rel"` (+ `pairs`). Field types lower through codecs (string/int/float/
bool, native lists, maps, and tagged unions with a numeric projection).

## Develop

```sh
uv sync
uv run pytest
uv run ruff check
```

Pure stdlib at runtime; `ryugraph` and `jsonschema` are dev-only (for the tests that execute
the generated DDL and validate against the dialect).

## Licence

MIT Róisín Grannell
