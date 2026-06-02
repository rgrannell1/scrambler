# Plan: complete the read side (and maybe edge I/O)

Status after the first round (commit `0b22aae`): the write/decode asymmetry is closed.
`insert_mapping`, `decode_row`, and `decode_into` landed; the per-field codec accessor (old
plan B) and the `define(check=…)` opt-out (old plan D) were deliberately declined, and that
was right — both would have leaked or weakened the round-trip guarantee.

There is **no active duplication left** in the dreamfish consumer. Everything that touches a
codec now routes through scrambler (the two drifted reimplementations — the `Property`
overflow guard and the `Entity` MAP `CAST` — are gone, living only in the codecs). So what
follows is **not** "the consumer copied scrambler." It's "scrambler doesn't offer X, so the
consumer built X generically." These are symmetry gaps, and declining them (as with B/D) is a
legitimate choice to keep scrambler scoped as a schema compiler + write facade.

## 1. A schema-aware read/drain — the strongest candidate

The round-trip is still half-open in practice. scrambler has:

- **write**: `insert_mapping(label, values)`
- **decode**: `decode_row(label, document, row)` / `decode_into(label, document, row)`
- **read/drain**: *nothing*

So a consumer can decode a row but must hand-roll the loop that *produces* the row. In
dreamfish that loop is `Backend.table/rows/dicts/scalar`:

```python
result = conn.execute(cypher, params)
columns = result.get_column_names()
while result.has_next():
    drained.append(result.get_next())
```

This is pure ryugraph drain boilerplate — no domain in it. The natural capstone is a
**`Scrambler.select(label, where=…) -> list[record]`** that drains *and* `decode_into`s each
row: the exact mirror of `insert_mapping`. Then a consumer can write a node and read it back
as a dataclass without ever touching `connection.execute`.

*Why it belongs:* `decode_into` already committed scrambler to a schema-aware read side; right
now it's slightly orphaned because scrambler hands you no rows to feed it. `select` finishes
the write→read→decode triangle.

*Reservation to weigh:* this expands scrambler from "schema compiler + write facade" toward
"query wrapper." Two ways to scope it:
- **Narrow (preferred):** only the *schema-aware* form — `select(label, …)` returning decoded
  records (and maybe `get(label, id)`), which is genuinely scrambler's domain because it
  decodes by schema. Leave arbitrary-Cypher draining out.
- **Broad:** also expose bare `query`/`rows`/`scalar` drains. Lower value — that's generic
  ryugraph, not schema-aware — and the consumer still wraps it for its own concerns (dreamfish
  adds `$package` auto-binding on top regardless), so the bare drain saves little.

Recommend the narrow form, or nothing.

## 2. Edge I/O — symmetric but a real feature, easy to defer

scrambler *compiles* relationship tables (`rel_ddl`, the FROM/TO pairs) but the facade can't
**write, count, or read an edge** — `labels` is node-only, there is no `insert_edge`. dreamfish
fills the gap with generic plumbing:

```python
EdgeStore.merge_edge(rel, (src_label, src_id), (dst_label, dst_id))  # MATCH both by id, MERGE the rel
EdgeStore.count(rel)                                                 # MATCH ()-[r:rel]->() RETURN count(r)
```

An `insert_edge(rel, src_id, dst_id)` would be symmetric with `insert_mapping`.

*Why it's weaker than #1:* it's an **unbuilt feature**, not a leak — the consumer isn't
drifting, just supplying something absent. And it has real design surface: a rel table can
declare multiple FROM/TO pairs, so the API must decide whether the caller names the endpoint
labels (as dreamfish does) or scrambler infers the pair from the ids. That's a design call,
not a mechanical lift. Defer unless a second consumer wants it.

## Explicitly staying in the consumer (not scrambler's job)

- `$package` auto-binding and the partition-filtered `clear_package`. Partitioning is a
  dreamfish convention; scrambler's plain `clear()` is the right primitive and should **not**
  grow a `where=` predicate.
- Structural-edge helpers (`IN_FILE` / `CONTAINS` / `DESCRIBES`) and `merge_set` (linker glue)
  — application graph semantics, not schema compilation.
- Package-qualified id construction (`package::part::part`) — domain. scrambler's job ends at
  `identity_fields` telling the consumer *which* fields form the id.

## Recommendation

If anything: do **#1 narrow** (`select(label, …)` → decoded records), because it completes the
read/write/decode triangle and retires the orphaned-`decode_into` feel. **#2** is nice
symmetry but a genuine feature with design choices — wait for a real need. Taking **neither**
is also fully defensible: nothing on the consumer side is broken anymore, only ergonomics are
left on the table.

## Suggested order

1. `select(label, where=…) -> list[record]` (drain + `decode_into`), plus maybe
   `get(label, id) -> record | None`. Additive, schema-aware, no behaviour change.
2. (Optional, on demand) `insert_edge(rel, src_id, dst_id)` + `count_edges(rel)` — settle the
   FROM/TO-pair selection question first.
