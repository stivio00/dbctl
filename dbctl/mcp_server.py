"""``dbctl mcp serve`` — expose the registries as MCP (Model Context
Protocol) tools so AI agents can operate databases through dbctl's
declared, safety-gated operations instead of raw connections.

Six tools:

* ``list_connections`` — connection catalog (metadata only, no secrets)
* ``list_operations``   — operation catalog with parameter schemas + SQL
* ``get_schema``        — SQLAlchemy-inspector introspection: dialect (db
  type / driver / server version), tables with columns + primary keys,
  foreign-key links, indexes, views. The raw material for authoring new
  operations with an LLM.
* ``draft_operation``   — validate an LLM-authored operation (YAML) and
  persist it as a *draft* under ``<config dir>/drafts/`` for human
  review; drafts never activate on their own.
* ``run_operation``     — run one declared single-scope operation; DML is
  dry-run by default and only commits with ``apply=true`` on a server
  started with ``--allow-write`` (per-connection ``read_only`` and
  ``allowed_operations`` always apply)
* ``health``            — open a connection and run its healthcheck

Every run goes through the same audit log as the CLI, with
``actor="mcp"`` (overridable via ``--actor``). Result payloads carry a
``status`` and, on failure, the CLI's stable ``exit_code`` semantics
(1 SQL, 2 unknown name/param, 5 health, 6 safety gate) so agents can
branch on failure reason.

Requires the optional ``mcp`` package: ``pip install 'dbctl[mcp]'``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from dbctl.catalog import connections_catalog, operations_catalog, redact_params

if TYPE_CHECKING:
    from dbctl.config import Connection, Operation

MAX_ROWS_DEFAULT = 50
MAX_ROWS_CAP = 1000


def _load_registries(profile: str | None) -> tuple[dict[str, Connection], dict[str, Operation]]:
    """Loader-resilient registries without any console output (a stdio MCP
    server must keep stdout clean for the protocol stream)."""
    from dbctl.connections import ConnectionsFileError
    from dbctl.connections import load as load_connections
    from dbctl.operations import OperationsFileError
    from dbctl.operations import load as load_operations

    try:
        conns = load_connections(profile=profile)
    except ConnectionsFileError as e:
        conns = e.valid
    except Exception:  # noqa: BLE001 - YAML parse error, IO, etc.
        conns = {}
    try:
        ops = load_operations(profile=profile)
    except OperationsFileError as e:
        ops = e.valid
    except Exception:  # noqa: BLE001
        ops = {}
    return conns, ops


def _err(msg: str, code: int) -> dict[str, Any]:
    return {"status": "error", "error": msg, "exit_code": code}


def _ok(
    *,
    operation: str,
    connection: str,
    status: str,
    rows: list[dict[str, Any]] | None = None,
    rows_affected: int | None = None,
    duration_ms: float = 0.0,
    run_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": status,
        "operation": operation,
        "connection": connection,
        "duration_ms": round(duration_ms, 1),
    }
    if run_id:
        out["run_id"] = run_id
    if rows is not None:
        out["rows"] = rows
    if rows_affected is not None:
        out["rows_affected"] = rows_affected
    if extra:
        out.update(extra)
    return out


def run_single(
    *,
    profile: str | None,
    allow_write: bool,
    actor: str,
    connection: str,
    operation: str,
    params: dict[str, Any] | None,
    apply: bool,
    max_rows: int = MAX_ROWS_DEFAULT,
) -> dict[str, Any]:
    """Non-interactive single-operation runner (the MCP counterpart of the
    CLI's ``_execute_single``). Never prompts; returns structured dicts.

    Policy differences from the CLI, forced by non-interactivity:

    * DML without ``apply`` is a dry-run that returns the resolved SQL.
    * ``apply`` on a confirm-gated operation is rejected unless the
      server was started with ``--allow-write`` (exit_code 6, audited).
    """
    from dbctl.audit import append
    from dbctl.connections import resolve
    from dbctl.execute import bind_params, format_sql, render
    from dbctl.multi import opened

    conns, ops = _load_registries(profile)
    try:
        canonical, conn = resolve(connection, conns)
    except KeyError as e:
        return _err(str(e), 2)
    from dbctl.operations import UnknownOperationError

    if operation not in ops:
        return _err(str(UnknownOperationError(operation, list(ops))), 2)
    op = ops[operation]
    if op.scope.value != "single":
        return _err(
            f"operation {operation!r} is multi-scope; run it via the CLI: dbctl {operation} ...",
            2,
        )

    is_dml = op.mode.value in {"execute", "upsert", "script"}
    read_only = conn.safety.read_only and is_dml

    if read_only:
        return _err(f"connection {canonical!r} is read-only; cannot run {operation!r}", 6)
    if conn.safety.allowed_operations and operation not in conn.safety.allowed_operations:
        return _err(f"operation {operation!r} not allowed on {canonical!r}", 6)

    try:
        bound = bind_params(op, params or {})
    except ValueError as e:
        return _err(str(e), 2)

    def _audit(status: str, **kw: Any) -> str:
        return append(
            profile=profile,
            connection=canonical,
            operation=operation,
            params=bound,
            mode=op.mode.value,
            status=status,
            actor=actor,
            redact={p.name for p in op.parameters if p.type.value == "secret"},
            **kw,
        )

    # Agent-facing policy (stricter than the CLI on purpose): ANY DML is a
    # dry-run preview unless the caller passes apply=true AND the server
    # was started with --allow-write. Op-level `confirm: false` still
    # executes immediately in the CLI/TUI, but over MCP the default is
    # always preview-first.
    if is_dml and not apply:
        rid = _audit("dry-run")
        return _ok(
            operation=operation,
            connection=canonical,
            status="dry-run",
            run_id=rid,
            extra={
                "sql": format_sql(op, bound),
                "params": redact_params(op, bound),
                "note": "DML is dry-run by default; pass apply=true to commit",
            },
        )
    if is_dml and apply and not allow_write:
        rid = _audit("blocked")
        return _ok(
            operation=operation,
            connection=canonical,
            status="blocked",
            run_id=rid,
            extra={
                "error": (
                    "write blocked: the dbctl MCP server was not started with --allow-write; "
                    "the run was audited and nothing was committed"
                ),
                "exit_code": 6,
            },
        )

    started = time.monotonic()
    try:
        with opened(canonical, conn) as oc:
            from dbctl.db import healthcheck

            ok, _ms, msg = healthcheck(oc.engine, conn.healthcheck.query, conn.healthcheck.timeout_seconds)
            if not ok:
                return _err(f"healthcheck failed for {canonical}: {msg}", 5)
            with oc.engine.begin() as sa_conn:
                res = render(sa_conn, op, bound)
    except Exception as e:  # noqa: BLE001 - surface any DB failure as payload
        from dbctl.db import fmt_db_error

        rid = _audit("error", duration_ms=(time.monotonic() - started) * 1000)
        out = _err(fmt_db_error(e), 1)
        out["run_id"] = rid
        out["connection"] = canonical
        out["operation"] = operation
        return out

    rid = _audit("ok", rows_affected=res.rows_affected, duration_ms=res.latency_ms)
    capped = min(max(1, max_rows), MAX_ROWS_CAP)
    extra: dict[str, Any] = {}
    if res.rows is not None:
        extra["row_count"] = len(res.rows)
        if len(res.rows) > capped:
            extra["rows_truncated"] = True
    return _ok(
        operation=operation,
        connection=canonical,
        status="ok",
        rows=None if res.rows is None else res.rows[:capped],
        rows_affected=res.rows_affected,
        duration_ms=res.latency_ms,
        run_id=rid,
        extra=extra or None,
    )


def health_connection(*, profile: str | None, connection: str) -> dict[str, Any]:
    """Open a connection (tunnel + engine) and run its healthcheck."""
    from dbctl.connections import resolve
    from dbctl.db import healthcheck
    from dbctl.multi import opened

    conns, _ = _load_registries(profile)
    try:
        canonical, conn = resolve(connection, conns)
    except KeyError as e:
        return _err(str(e), 2)
    try:
        with opened(canonical, conn) as oc:
            ok, ms, msg = healthcheck(oc.engine, conn.healthcheck.query, conn.healthcheck.timeout_seconds)
    except Exception as e:  # noqa: BLE001
        from dbctl.db import fmt_db_error

        return _err(fmt_db_error(e), 3)
    if not ok:
        return {
            "status": "fail",
            "connection": canonical,
            "latency_ms": round(ms, 1),
            "message": msg,
            "exit_code": 5,
        }
    return {
        "status": "ok",
        "connection": canonical,
        "latency_ms": round(ms, 1),
        "message": msg,
    }


# --------------------------------------------------------------------------- #
# schema introspection (SQLAlchemy inspector) — feeds operation authoring
# --------------------------------------------------------------------------- #
# Dialect-aware authoring guide: powers the get_schema → draft_operation
# workflow, so an LLM can write dialect-correct, parameterized operations
# from the inspector facts (tables, keys, links, indexes, views, db type).
_COMMON_SQL_HINTS = [
    "SQL is parameterized: write $name placeholders, declare each under parameters: — never inline values",
    "fetch / fetch_one modes are for SELECT (they render rows); execute is for DML, reports rows_affected",
    "one focused statement per `sql:` block keeps dry-run previews honest",
]

_DIALECT_SQL_HINTS: dict[str, list[str]] = {
    "postgresql": [
        "pagination: LIMIT n OFFSET m",
        "upsert: INSERT ... ON CONFLICT (key) DO UPDATE SET ...",
        "RETURNING * after INSERT/UPDATE/DELETE to get affected rows back",
        "dates: NOW() - INTERVAL '7 days'",
    ],
    "mysql": [
        "pagination: LIMIT n OFFSET m",
        "upsert: INSERT ... ON DUPLICATE KEY UPDATE col = VALUES(col)",
        "no RETURNING — follow an INSERT with a SELECT when rows are needed",
        "dates: NOW() - INTERVAL 7 DAY",
    ],
    "sqlite": [
        "pagination: LIMIT n OFFSET m",
        "upsert: INSERT ... ON CONFLICT (key) DO UPDATE SET ... (3.24+)",
        "typing is dynamic — CAST(x AS INTEGER) when a numeric compare matters",
        "dates: datetime('now', '-7 days')",
    ],
    "mssql": [
        "pagination: SELECT TOP n ... (or OFFSET n ROWS FETCH NEXT m ROWS ONLY with ORDER BY)",
        "upsert: MERGE INTO t USING (...) s ON t.k = s.k WHEN MATCHED THEN UPDATE "
        "... WHEN NOT MATCHED THEN INSERT ...",
        "OUTPUT inserted./deleted. instead of RETURNING",
        "dates: DATEADD(day, -7, GETDATE())",
    ],
}

_EXAMPLE_OPERATION_YAML = """\
my-op:
  description: "One sentence an LLM router can match on"
  scope: single
  mode: fetch
  parameters:
    - { name: floor, type: integer, required: true, position: 1 }
  sql: "SELECT name FROM users WHERE credits >= $floor"
"""


def authoring_guide(dialect_name: str) -> dict[str, Any]:
    """Authoring context for LLMs: the $name placeholder rule, an
    operations.yaml entry shape, and SQL hints keyed off the engine's
    dialect (db type)."""
    return {
        "placeholder_style": "$name in sql, declared in parameters: — dbctl binds them, never interpolates",
        "modes": (
            "single scope: fetch | fetch_one | execute | script | upsert; "
            "multi scope: compare | diff | copy | sync | validate | replay"
        ),
        "example_entry_yaml": _EXAMPLE_OPERATION_YAML,
        "dialect_hints": _COMMON_SQL_HINTS + _DIALECT_SQL_HINTS.get(dialect_name, []),
    }


def schema_payload(
    *,
    profile: str | None,
    connection: str,
    table: str | None = None,
    max_tables: int = 100,
) -> dict[str, Any]:
    """Inspector-based schema dump for one connection: dialect (db type +
    driver + server version), tables with columns / primary keys /
    foreign-key links / indexes, and views. Read-only catalog lookups —
    the same inspector the TUI's schema browser uses."""
    from dbctl.connections import resolve
    from dbctl.multi import opened
    from dbctl.ui.schema import (
        list_columns,
        list_foreign_keys,
        list_indexes,
        list_schemas,
        list_tables,
        list_views,
    )

    conns, _ = _load_registries(profile)
    try:
        canonical, conn = resolve(connection, conns)
    except KeyError as e:
        return _err(str(e), 2)
    try:
        with opened(canonical, conn) as oc:
            engine = oc.engine
            try:
                schemas = list_schemas(engine)
            except Exception:  # noqa: BLE001
                schemas = []
            dialect: dict[str, Any] = {
                "name": engine.dialect.name,
                "driver": engine.url.get_driver_name(),
                "database": engine.url.database or "",
            }
            try:
                vinfo = engine.dialect.server_version_info
                if vinfo:
                    dialect["server_version"] = ".".join(str(p) for p in vinfo)
            except Exception:  # noqa: BLE001 - version unknown on some drivers
                pass

            targets = (
                [(s, list_tables(engine, s)) for s in schemas] if schemas else [(None, list_tables(engine))]
            )
            pairs: list[tuple[str | None, str]] = []
            for schema, names in targets:
                if table is not None:
                    names = [n for n in names if n == table]
                pairs.extend((schema, n) for n in names)
            if table is not None and not pairs:
                return _err(f"table {table!r} not found on {canonical!r}", 2)

            capped = max(1, min(max_tables, MAX_ROWS_CAP))
            truncated = len(pairs) > capped
            tables_out: list[dict[str, Any]] = []
            for schema, t in pairs[:capped]:
                tables_out.append(
                    {
                        "schema": schema,
                        "name": t,
                        "columns": [
                            {
                                "name": c.name,
                                "type": c.type,
                                "nullable": c.nullable,
                                "primary_key": c.primary_key,
                            }
                            for c in list_columns(engine, t, schema)
                        ],
                        "foreign_keys": [
                            {
                                "columns": fk.columns,
                                "ref_table": fk.ref_table,
                                "ref_columns": fk.ref_columns,
                            }
                            for fk in list_foreign_keys(engine, t, schema)
                        ],
                        "indexes": [
                            {"name": ix.name, "columns": ix.columns, "unique": ix.unique}
                            for ix in list_indexes(engine, t, schema)
                        ],
                    }
                )
            views_out: list[dict[str, Any]] = []
            for schema, _names in targets:
                for v in list_views(engine, schema):
                    views_out.append(
                        {
                            "schema": schema,
                            "name": v,
                            "columns": [c.name for c in list_columns(engine, v, schema)],
                        }
                    )
            views_out = views_out[:capped]
    except Exception as e:  # noqa: BLE001
        from dbctl.db import fmt_db_error

        return _err(fmt_db_error(e), 1)

    out: dict[str, Any] = {
        "status": "ok",
        "connection": canonical,
        "dialect": dialect,
        "authoring": authoring_guide(dialect["name"]),
        "tables": tables_out,
        "views": views_out,
        "table_count": len(pairs),
    }
    if truncated:
        out["tables_truncated"] = True
        out["note"] = f"more than {capped} tables; raise max_tables or pass table= to focus"
    return out


def draft_operation_yaml(*, profile: str | None, operation_yaml: str) -> dict[str, Any]:
    """Validate an LLM-authored operation (YAML text) and persist it as a
    draft for human review under ``<config dir>/drafts/``. Drafts are
    never active — the human moves them into operations.yaml."""
    import re

    import yaml as _yaml

    from dbctl.config import Operation, resolve_config_dir
    from dbctl.operations import _first_validation_msg

    try:
        raw = _yaml.safe_load(operation_yaml)
    except _yaml.YAMLError as e:
        return _err(f"YAML parse error: {e}", 2)
    if not isinstance(raw, dict):
        return _err("operation YAML must be a mapping of name -> operation fields", 2)
    # accept a wrapped `operations:` envelope — either a proper nested mapping
    # or a null header followed by an unindented body (common LLM emission)
    if "operations" in raw:
        if isinstance(raw["operations"], dict) and len(raw) == 1:
            raw = raw["operations"]
        elif raw["operations"] is None:
            del raw["operations"]
    if len(raw) != 1:
        return _err("draft exactly one operation: a mapping with a single name key", 2)
    name, body = next(iter(raw.items()))
    if not isinstance(name, str) or not isinstance(body, dict):
        return _err("operation YAML must map a name to a mapping of fields", 2)
    try:
        op = Operation.model_validate(body)
    except Exception as e:  # noqa: BLE001 - pydantic validation
        return _err(f"invalid operation {name!r}: {_first_validation_msg(e)}", 2)

    warnings: list[str] = []
    placeholders = set(re.findall(r"\$([a-zA-Z_][a-zA-Z0-9_]*)", op.sql or ""))
    for role_sql in (op.queries or {}).values():
        placeholders |= set(re.findall(r"\$([a-zA-Z_][a-zA-Z0-9_]*)", role_sql))
    declared = {p.name for p in op.parameters}
    if unknown := sorted(placeholders - declared):
        # Undeclared placeholders fail at bind time — reject the draft.
        return _err(
            f"SQL references undeclared params: {', '.join(unknown)} — declare them "
            "in `parameters:` or fix the SQL",
            2,
        )
    if unused := sorted(declared - placeholders):
        warnings.append(f"declared params never referenced in SQL: {', '.join(unused)}")

    drafts_dir = resolve_config_dir(profile) / "drafts"
    drafts_dir.mkdir(parents=True, exist_ok=True)
    path = drafts_dir / f"{name}.yaml"
    header = (
        "# drafted via `dbctl mcp draft_operation` — review, then move into\n"
        "# operations.yaml to activate (it then becomes a CLI command + MCP tool).\n"
    )
    path.write_text(
        header + _yaml.safe_dump({name: op.model_dump(mode="json")}, sort_keys=False),
        encoding="utf-8",
    )
    return {
        "status": "ok",
        "name": name,
        "scope": op.scope.value,
        "mode": op.mode.value,
        "draft_path": str(path),
        "warnings": warnings,
        "note": (
            "draft saved (NOT active). Review it, then append its content to "
            "operations.yaml and re-run `dbctl operations list` to confirm."
        ),
    }


def build_server(
    *,
    profile: str | None = None,
    allow_write: bool = False,
    actor: str = "mcp",
) -> Any:
    """Assemble the MCP server (FastMCP in mcp 1.x, MCPServer in 2.x —
    same decorator API)."""
    try:
        from mcp.server.fastmcp import FastMCP as ServerCls  # mcp 1.x
    except ImportError:
        from mcp.server.mcpserver import MCPServer as ServerCls  # mcp 2.x rename

    mcp = ServerCls(
        "dbctl",
        instructions=(
            "Operate databases through dbctl's declared, safety-gated operations. "
            "Workflow: call list_connections and list_operations first; then run_operation "
            "with the operation's declared parameter names. DML (execute/upsert/script) is "
            "dry-run by default — it returns the resolved SQL and commits nothing; pass "
            "apply=true only when the user explicitly asked to write, and expect status "
            "'blocked' when the server disallows writes. fetch/fetch_one operations return "
            "rows directly. To author NEW operations: call get_schema to inspect a "
            "connection (tables, columns, keys, foreign-key links, indexes, views, "
            "dialect) — its `authoring` block carries the $name placeholder rules, an "
            "example entry shape and dialect-specific SQL hints; write the entry, then "
            "draft_operation to validate + persist it as a draft for the human to "
            "review. status 'error' payloads carry the CLI's exit_code semantics."
        ),
    )

    @mcp.tool()
    def list_connections() -> dict[str, Any]:
        """List configured dbctl connections (metadata only, never credentials)."""
        conns, _ = _load_registries(profile)
        return {"connections": connections_catalog(conns)}

    @mcp.tool()
    def list_operations() -> dict[str, Any]:
        """List declared dbctl operations with their parameters, modes and SQL."""
        _, ops = _load_registries(profile)
        return {"operations": operations_catalog(ops, include_sql=True)}

    @mcp.tool()
    def get_schema(
        connection: str,
        table: str | None = None,
        max_tables: int = 100,
    ) -> dict[str, Any]:
        """Introspect a connection's schema via the SQLAlchemy inspector.

        Returns the dialect (db type, driver, server version), tables with
        columns (name/type/nullable/primary_key), foreign-key links
        (columns -> ref_table(ref_columns)), indexes (unique flag), and
        views — plus an `authoring` guide (placeholder rules, an
        operations.yaml entry shape, dialect-specific SQL hints) so new
        operations can be drafted from these facts. Pass ``table`` to
        focus on one table. ``max_tables`` caps the response (default 100).
        """
        return schema_payload(profile=profile, connection=connection, table=table, max_tables=max_tables)

    @mcp.tool()
    def draft_operation(operation_yaml: str) -> dict[str, Any]:
        """Validate an authored operation (YAML text) and save it as a draft.

        Accepts a single ``{name: {operation fields}}`` mapping (or the
        full ``operations:`` wrapper). Validates against the Operation
        schema (typos fail loudly), cross-checks $placeholders vs declared
        parameters, and writes ``<config dir>/drafts/<name>.yaml`` for
        human review — drafts are never active until moved into
        operations.yaml.
        """
        return draft_operation_yaml(profile=profile, operation_yaml=operation_yaml)

    @mcp.tool()
    def run_operation(
        connection: str,
        operation: str,
        params: dict[str, Any] | None = None,
        apply: bool = False,
        max_rows: int = MAX_ROWS_DEFAULT,
    ) -> dict[str, Any]:
        """Run one declared single-scope operation against a connection.

        ``params`` uses the operation's declared parameter names (see
        list_operations). DML is dry-run by default; ``apply=true``
        commits but is blocked unless the server runs with --allow-write.
        ``max_rows`` caps fetch results (default 50).
        """
        return run_single(
            profile=profile,
            allow_write=allow_write,
            actor=actor,
            connection=connection,
            operation=operation,
            params=params,
            apply=apply,
            max_rows=max_rows,
        )

    @mcp.tool()
    def health(connection: str) -> dict[str, Any]:
        """Open a connection (tunnel + engine) and run its healthcheck."""
        return health_connection(profile=profile, connection=connection)

    return mcp


__all__ = [
    "build_server",
    "run_single",
    "health_connection",
    "schema_payload",
    "draft_operation_yaml",
]
