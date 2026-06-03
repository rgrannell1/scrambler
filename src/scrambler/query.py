"""Cypher views: pure templates that render query strings from their arguments."""

import re
from collections.abc import Sequence

BEGIN_TRANSACTION = "BEGIN TRANSACTION"
COMMIT = "COMMIT"
ROLLBACK = "ROLLBACK"


def create_node_table(label: str, columns: Sequence[tuple[str, str]], primary_key: str) -> str:
    """CREATE NODE TABLE from (name, Kùzu-type) column specs and a primary-key column."""
    cols = ", ".join(f"{name} {kuzu}" for name, kuzu in columns)
    return f"CREATE NODE TABLE IF NOT EXISTS {label}({cols}, PRIMARY KEY({primary_key}))"


def create_rel_table(label: str, pairs: Sequence[tuple[str, str]]) -> str:
    """CREATE REL TABLE from its (from-label, to-label) endpoint pairs."""

    rendered = ", ".join(f"FROM {source} TO {target}" for source, target in pairs)
    return f"CREATE REL TABLE IF NOT EXISTS {label}({rendered})"


def merge_node(label: str, primary_key: str, assignments: Sequence[tuple[str, str]]) -> str:
    """Idempotent MERGE keyed on primary_key, SETting each (column, binding) assignment.
    A node whose only column is its primary key has nothing to SET — emit a bare MERGE.
    """

    match = f"MERGE (n:{label} {{{primary_key}: ${primary_key}}})"
    sets = ", ".join(f"n.{name} = {binding}" for name, binding in assignments)
    return f"{match} SET {sets}" if sets else match


def unwind_rows(statement: str) -> str:
    """Batch a single-row statement: rewrite each $param to row.param under an UNWIND."""

    body = re.sub(r"\$(\w+)", r"row.\1", statement)
    return f"UNWIND $rows AS row {body}"


def match_by_key(label: str, primary_key: str, columns: Sequence[str]) -> str:
    """MATCH one node by its $key primary-key value, RETURNing the given columns."""

    returns = ", ".join(f"n.{name}" for name in columns)
    return f"MATCH (n:{label} {{{primary_key}: $key}}) RETURN {returns}"


def match_all(label: str, columns: Sequence[str], terms: Sequence[str] = ()) -> str:
    """MATCH every node of a type (optionally filtered by terms), RETURNing the given columns."""

    returns = ", ".join(f"n.{name}" for name in columns)
    return f"MATCH (n:{label}){where_clause(terms)} RETURN {returns}"


def detach_delete(label: str) -> str:
    """Delete every node of a type and its relationships."""

    return f"MATCH (n:{label}) DETACH DELETE n"


def equality(name: str) -> str:
    """A `n.col = $col` filter term."""

    return f"n.{name} = ${name}"


def is_null(name: str) -> str:
    """A `n.col IS NULL` filter term."""

    return f"n.{name} IS NULL"


def where_clause(terms: Sequence[str]) -> str:
    """Join filter terms into a ` WHERE … AND …` clause; '' when there are none."""

    return " WHERE " + " AND ".join(terms) if terms else ""
