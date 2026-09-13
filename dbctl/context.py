"""``dbctl context`` — render an LLM-ready markdown context pack.

Two shapes:

* **global** (no connection argument): the connections catalog (metadata
  only, never credentials) plus the operations catalog with each
  operation's parameters and SQL. Paste this into any chat to give a
  model full working knowledge of a dbctl installation.

* **per-connection** (``dbctl context <conn>``): the global pack plus a
  live schema introspection (schemas → tables → columns) via the same
  read-only SQLAlchemy inspector the TUI's schema browser uses, plus the
  connection's declared info queries.

Output is plain markdown on stdout (or ``--output PATH``), deterministic
(no timestamps) so it can be diffed and cached.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

    from dbctl.config import Connection, Operation


# --------------------------------------------------------------------------- #
# connections / operations sections
# --------------------------------------------------------------------------- #
def _connections_md(conns: dict[str, Connection]) -> list[str]:
    lines: list[str] = ["## Connections", ""]
    if not conns:
        return lines + ["(none configured)", ""]
    lines += [
        "| name | aliases | type | driver | database | read-only | description |",
        "|---|---|---|---|---|---|---|",
    ]
    for name in sorted(conns):
        c = conns[name]
        lines.append(
            "| {name} | {aliases} | {type} | {driver} | {db} | {ro} | {desc} |".format(
                name=name,
                aliases=", ".join(c.aliases) or "-",
                type=c.type.value,
                driver=c.driver or ("(from url)" if c.url else "-"),
                db=c.database or "-",
                ro="yes" if c.safety.read_only else "no",
                desc=(c.description or "-").replace("|", "\\|"),
            )
        )
    lines.append("")
    return lines


def _param_sig(op: Operation) -> str:
    if not op.parameters:
        return "none"
    parts: list[str] = []
    positional = sorted((p for p in op.parameters if p.position is not None), key=lambda p: p.position or 0)
    for p in positional:
        parts.append(_param_cell(p, positional=True))
    for p in op.parameters:
        if p.position is None:
            parts.append(_param_cell(p, positional=False))
    return "; ".join(parts)


def _param_cell(p: Any, *, positional: bool) -> str:
    bits = [f"`{p.name}` ({p.type.value}"]
    if p.required:
        bits.append("required")
    elif p.default is not None:
        bits.append(f"default {p.default!r}")
    if p.choices:
        bits.append(f"one of {','.join(p.choices)}")
    if p.position is not None and positional:
        bits.append(f"position {p.position}")
    if p.description:
        bits.append(p.description)
    return ", ".join(bits) + ")"


def _operations_md(ops: dict[str, Operation], *, include_sql: bool) -> list[str]:
    lines: list[str] = ["## Operations", ""]
    if not ops:
        return lines + ["(none configured)", ""]
    for name in sorted(ops):
        op = ops[name]
        title = op.description or name
        lines.append(f"### `{name}` — {title}")
        lines.append("")
        meta = [f"scope: {op.scope.value}", f"mode: {op.mode.value}"]
        if op.roles:
            meta.append(f"roles: {', '.join(op.roles)}")
        if op.output:
            meta.append(f"output: {op.output.value}")
        lines.append(f"* {meta[0]}; " + "; ".join(meta[1:]))
        lines.append(f"* parameters: {_param_sig(op)}")
        if op.sql and include_sql:
            lines.append("")
            lines.append("```sql")
            lines.append(op.sql.strip())
            lines.append("```")
        elif op.queries:
            lines.append("")
            for role, sql in op.queries.items():
                lines.append(f"**{role}**:")
                lines.append("```sql")
                lines.append(sql.strip())
                lines.append("```")
        lines.append("")
    return lines


# --------------------------------------------------------------------------- #
# live schema section (per-connection only)
# --------------------------------------------------------------------------- #
def _schema_md(engine: Engine) -> list[str]:
    from dbctl.ui.schema import (
        list_columns,
        list_foreign_keys,
        list_indexes,
        list_schemas,
        list_tables,
        list_views,
    )

    lines: list[str] = ["### Schema", ""]
    try:
        schemas = list_schemas(engine)
    except Exception:  # noqa: BLE001 - inspector not supported by this dialect
        return lines + ["(schema introspection not supported by this driver)", ""]
    targets: list[tuple[str | None, list[str]]] = []
    if schemas:
        for s in schemas:
            targets.append((s, list_tables(engine, s)))
    else:
        targets.append((None, list_tables(engine)))

    rendered = False
    for schema, tables in targets:
        views = list_views(engine, schema)
        if not tables and not views:
            continue
        rendered = True
        heading = f"#### {schema}" if schema else "#### (default schema)"
        lines += [heading, ""]
        for t in tables:
            lines.append(f"**{t}**")
            lines.append("")
            lines.append("| column | type | nullable | key |")
            lines.append("|---|---|---|---|")
            for col in list_columns(engine, t, schema):
                key = "PK" if col.primary_key else ""
                lines.append(f"| {col.name} | {col.type} | {'YES' if col.nullable else 'NO'} | {key} |")
            for fk in list_foreign_keys(engine, t, schema):
                lines.append(f"| link | {fk.render()} | | |")
            for ix in list_indexes(engine, t, schema):
                uniq = " UNIQUE" if ix.unique else ""
                lines.append(f"| index | {ix.name} ({', '.join(ix.columns)}){uniq} | | |")
            lines.append("")
        for v in views:
            cols = ", ".join(c.name for c in list_columns(engine, v, schema))
            lines.append(f"**view** `{v}` ({cols})")
            lines.append("")
    if not rendered:
        lines.append("(no tables found)")
        lines.append("")
    return lines


def _connection_md(name: str, conn: Connection, engine: Engine | None) -> list[str]:
    lines = [f"## Connection: `{name}`", ""]
    ro = "yes" if conn.safety.read_only else "no"
    allow = ", ".join(conn.safety.allowed_operations) or "(all)"
    lines.append(
        f"* type: {conn.type.value}; driver: {conn.driver or '(from url)'}; database: {conn.database or '-'}"
    )
    lines.append(f"* safety: read-only={ro}; allowed_operations={allow}")
    if conn.info:
        lines.append("")
        lines.append("Declared info queries:")
        lines.append("")
        for q in conn.info:
            lines.append(f"**{q.name}** — {q.description or '(no description)'}")
            lines.append("")
            lines.append("```sql")
            lines.append(q.query.strip())
            lines.append("```")
    if engine is not None:
        lines += _schema_md(engine)
    lines.append("")
    return lines


# --------------------------------------------------------------------------- #
# top-level builder
# --------------------------------------------------------------------------- #
def build_context(
    conns: dict[str, Connection],
    ops: dict[str, Operation],
    *,
    conn_name: str | None = None,
    conn: Connection | None = None,
    engine: Engine | None = None,
    include_sql: bool = True,
) -> str:
    """Assemble the markdown pack. When ``conn_name``/``conn`` are given a
    per-connection section (schema via ``engine`` when available) is
    appended to the global catalogs."""
    lines: list[str] = ["# dbctl context", ""]
    lines.append(
        "Configured dbctl installation: connections and operations are declared in"
        " `connections.yaml` / `operations.yaml`. Operations run as parameterized SQL"
        " with bind params (`$name` placeholders); DML is dry-run by default until"
        ' `--apply`. Use `dbctl <conn> <op> [params]` to run, `dbctl ask "..."` to'
        " route natural language, `dbctl mcp serve` to expose these as MCP tools."
    )
    lines.append("")
    lines += _connections_md(conns)
    lines += _operations_md(ops, include_sql=include_sql)
    if conn_name and conn is not None:
        lines += _connection_md(conn_name, conn, engine)
    return "\n".join(lines).rstrip() + "\n"


__all__ = ["build_context"]
