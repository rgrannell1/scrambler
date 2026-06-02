# scrambler

[![CI](https://github.com/rgrannell1/scrambler/actions/workflows/ci.yml/badge.svg)](https://github.com/rgrannell1/scrambler/actions/workflows/ci.yml)

Compile a JSON schema dialect into a Kùzu graph schema, and matching dataclasses.

## Usage

Write one schema document (a JSON Schema constrained to the *dialect*), then drive it through
the `Scrambler` facade — it holds a connection and the defined schema so you never thread a
`(label, document)` pair around by hand. Operations live in three namespaces on the instance:
`schema` (derived artifacts), `write` (merges), and `read` (decoded reads).

```python
import scrambler

scram = scrambler.Scrambler("graph.db").define(my_schema)

# A runtime dataclass for a Cypher node.
Widget = scram.schema.dataclass("Widget")

# +++ Merge +++
scram.write.insert(Widget(id="w1", label="hello"))
scram.write.insert_many("Widget", [
    {"id": "w2", "label": "world"},
    {"id": "w3", "label": "again"},
])

# +++ Read +++
scram.read.get("Widget", "w1")
scram.read.all("Widget")                    
scram.read.all("Widget", where={"label": "hello"}) 

# Writes can be grouped into an atomic transaction.
with scram.transaction():
    scram.write.insert(Widget(id="w4", label="batched"))
```

## Develop

```sh
uv sync
uv run pytest
uv run ruff check
```

## Licence

MIT License

Copyright (c) 2026 Róisín Grannell

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
