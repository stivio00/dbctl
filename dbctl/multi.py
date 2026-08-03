"""Multi-connection orchestration: open tunnels, run per-role SQL, join results.

Execution paths here:

* ``run_role``         — original ``diff`` / ``compare`` modes: take a per-role
  SQL block and return rows.
* ``run_copy``         — bulk-copy rows src → trg, table by table, batched.
* ``run_table_counts`` — auto-gen ``SELECT 't' AS t, COUNT(*) AS n`` per table
  for the ``table_counts`` diff strategy (no per-table SQL boilerplate).
* ``run_sync``         — converge one trg table to match src: insert missing,
  update differing, optionally delete extras, keyed by ``sync_spec.key``.
* ``run_validate``     — structural schema diff (columns + types) via
  SQLAlchemy ``inspect()``; emits a per-column mismatch report.
* ``run_replay``       — copy with a per-row Python transform applied before
  writing to trg (reuses ``run_copy`` internals).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import inspect, text

from dbctl.db import build_engine
from dbctl.execute import to_bindparams
from dbctl.tunnels.base import build_tunnel

if TYPE_CHECKING:
    from dbctl.config import Connection, CopySpec, Operation, ReplaySpec, SyncSpec, ValidateSpec


@dataclass
class OpenedConn:
    name: str
    engine: object
    tunnel: object
    duration_ms: float = 0.0


@contextmanager
def opened(name: str, conn: Connection) -> Iterator[OpenedConn]:
    tunnel = build_tunnel(conn)
    tunnel.__enter__()
    try:
        engine = build_engine(conn, tunnel)
        yield OpenedConn(name=name, engine=engine, tunnel=tunnel)
    finally:
        tunnel.__exit__(None, None, None)


def run_role(op: Operation, role: str, opened_conn: OpenedConn, params: dict) -> list[dict]:
    """Run one role's query for a diff/compare multi operation."""
    sql = (op.queries or {}).get(role)
    if sql is None:
        raise RuntimeError(
            f"operation has no query for role {role!r}; "
            f"declared roles: {op.roles}, available: {list((op.queries or {}).keys())}"
        )
    started = time.monotonic()
    with opened_conn.engine.connect() as c:
        rows = [dict(r) for r in c.execute(text(to_bindparams(sql)), params).mappings()]
    opened_conn.duration_ms = (time.monotonic() - started) * 1000
    return rows


# --------------------------------------------------------------------------- #
# table_counts — auto-gen SELECT COUNT(*) per table for a diff op
# --------------------------------------------------------------------------- #
def _table_counts_sql(table: str, engine) -> str:
    # Use the dialect's identifier preparer so quoting works on every DB:
    # Postgres uses `"users"`, MySQL uses `` `users` ``, MSSQL uses `[users]`.
    q = engine.dialect.identifier_preparer.quote(table)
    return f"SELECT {table!r} AS t, COUNT(*) AS n FROM {q}"


def run_table_counts(
    opener_src: OpenedConn,
    opener_trg: OpenedConn,
    tables: list[str],
) -> dict[str, list[dict]]:
    """Run ``SELECT COUNT(*) FROM <t>`` for each declared table on each side."""
    out: dict[str, list[dict]] = {"src": [], "trg": []}
    started = time.monotonic()
    for label, oc in (("src", opener_src), ("trg", opener_trg)):
        with oc.engine.connect() as c:
            for tbl in tables:
                rows = [dict(r) for r in c.execute(text(_table_counts_sql(tbl, oc.engine))).mappings()]
                out[label].extend(rows)
    opener_src.duration_ms = (time.monotonic() - started) * 1000
    return out


# --------------------------------------------------------------------------- #
# copy — bulk-insert src rows into trg, table by table, batched
# --------------------------------------------------------------------------- #
@dataclass
class CopyResult:
    table: str
    src_rows: int
    trg_rows_inserted: int
    skipped_existing: int  # only > 0 when on_conflict=skip
    duration_ms: float
    note: str = ""


@dataclass
class CopyReport:
    results: list[CopyResult] = field(default_factory=list)
    total_ms: float = 0.0


def _introspect_tables(engine) -> list[str]:
    """Return the list of user tables, schema-qualified only when needed.

    Skip system schemas (information_schema, pg_catalog, sys, mysql.*, …).
    A schema is treated as "default" (tables returned bare) when it is one
    of the dialect's conventional defaults (`public`/`dbo`/empty) **or**
    when it equals the engine's own database — MySQL and MariaDB surface
    the current database as a schema name, so without this rule every
    MySQL table would come back as `app.<table>` while Postgres stays
    bare, breaking cross-DB set operations (`validate`, table-counts
    introspection, copy with `tables: null`).
    """
    insp = inspect(engine)
    default_schemas = {"public", "dbo", ""}
    db_name = engine.url.database
    # The configured "database" lives at engine.url.database on every dialect
    # dbctl cares about; if it's set, treat it as default too.
    if db_name:
        default_schemas = default_schemas | {db_name}
    tables: list[str] = []
    for schema in insp.get_schema_names():
        if schema is None or schema.lower() in {
            "information_schema",
            "pg_catalog",
            "sys",
            "mysql",
            "performance_schema",
            "INFORMATION_SCHEMA",
        }:
            continue
        for tbl in insp.get_table_names(schema=schema):
            # Unqualify when in the dialect's default schema.
            if schema in default_schemas:
                tables.append(tbl)
            else:
                tables.append(f"{schema}.{tbl}")
    return sorted(tables)


def _resolve_tables(spec: CopySpec, src_engine) -> list[str]:
    if spec.tables is not None:
        # "*" is the explicit "introspect all" alias
        if spec.tables == ["*"]:
            return _introspect_tables(src_engine)
        return list(spec.tables)
    return _introspect_tables(src_engine)


def _quote_ident(name: str, engine) -> str:
    """Dialect-aware quoting of a (possibly schema-qualified) identifier.

    Postgres → "schema"."table"; MySQL → `schema`.`table`; MSSQL → [schema].[table].
    """
    preparer = engine.dialect.identifier_preparer
    if "." in name:
        return ".".join(preparer.quote(p) for p in name.split("."))
    return preparer.quote(name)


def _convert_value(v):
    """Make src row values acceptable to executemany on trg dialect."""
    return v


def run_copy(
    src: OpenedConn,
    trg: OpenedConn,
    spec: CopySpec,
    *,
    batch_size: int | None = None,
    dry_run: bool = False,
    row_transform: Callable[[dict], dict] | None = None,
) -> CopyReport:
    """Stream rows from ``src`` to ``trg``, table by table, in batches.

    * Each table is read once via ``SELECT * FROM <t>`` (optionally filtered
      by ``spec.where``); columns are pulled from the SQLAlchemy mapping so
      no value re-casting is needed for plain types — BSON/value gaps on the
      trg dialect raise at insert time, which is what we want.
    * Batches are inserted via ``executemany`` of ``batch_size`` rows each
      (default: ``spec.batch_size``).
    * Conflict handling: ``error`` (default) lets the driver raise;
      ``skip`` uses an ``INSERT … ON CONFLICT DO NOTHING``-shaped statement
      built per dialect (postgres native, MySQL ``IGNORE``, SQL Server
      ``WHERE NOT EXISTS``); ``update`` is an upsert; ``truncate`` truncates
      the target table first (bypasses conflict handling entirely).
    * ``row_transform`` (used by ``run_replay``) rewrites each row dict
      in-process before it lands in the insert batch; ``None`` is identity.
    """
    batch = batch_size or spec.batch_size
    report = CopyReport()
    started_all = time.monotonic()
    tables = _resolve_tables(spec, src.engine)

    if not tables:
        report.total_ms = (time.monotonic() - started_all) * 1000
        note = "no tables declared and introspection found no user tables"
        report.results.append(CopyResult("<none>", 0, 0, 0, 0.0, note))
        return report

    for tbl in tables:
        started = time.monotonic()
        where = spec.where.get(tbl) or spec.where.get("*")
        src_sql = f"SELECT * FROM {_quote_ident(tbl, src.engine)}"
        if where:
            src_sql += f" WHERE {where}"

        with src.engine.connect() as sc:
            cursor = sc.execute(text(src_sql))
            keys = list(cursor.keys())
            src_count = 0
            inserted = 0
            skipped = 0
            chunk: list[dict] = []
            for row in cursor.mappings():
                src_count += 1
                rec = {k: _convert_value(row[k]) for k in keys}
                if row_transform is not None:
                    rec = row_transform(rec)
                    if not isinstance(rec, dict):
                        raise RuntimeError(
                            f"row_transform for table {tbl!r} must return a dict, got {type(rec).__name__}"
                        )
                chunk.append(rec)
                if len(chunk) >= batch:
                    n_written = _insert_batch(trg, tbl, keys, chunk, spec, dry_run)
                    inserted += n_written
                    if spec.on_conflict.value == "skip":
                        # Per-batch delta: rows this batch skipped due to
                        # existing PKs. `n_written` is the per-call count
                        # (see `_insert_batch`); `len(chunk)` is the rows
                        # we tried to write this batch.
                        skipped += len(chunk) - n_written
                    chunk = []
            if chunk:
                n_written = _insert_batch(trg, tbl, keys, chunk, spec, dry_run)
                inserted += n_written
                if spec.on_conflict.value == "skip":
                    skipped += len(chunk) - n_written

        report.results.append(
            CopyResult(
                table=tbl,
                src_rows=src_count,
                trg_rows_inserted=0 if dry_run else inserted,
                skipped_existing=skipped,
                duration_ms=(time.monotonic() - started) * 1000,
                note="dry-run" if dry_run else "",
            )
        )
    report.total_ms = (time.monotonic() - started_all) * 1000
    return report


def _truncate_table(c, table: str, engine) -> None:
    """Dialect-aware TRUNCATE: disables FK checks where the dialect supports
    it (MySQL), or uses CASCADE where the dialect supports it (Postgres).
    Falls back to a plain DELETE if TRUNCATE fails (the user can address the
    FK ordering themselves)."""
    table_q = _quote_ident(table, engine)
    dialect = engine.dialect.name
    if dialect in {"mysql", "mariadb"}:
        c.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        c.execute(text(f"TRUNCATE TABLE {table_q}"))
        c.execute(text("SET FOREIGN_KEY_CHECKS=1"))
    elif dialect == "postgresql":
        c.execute(text(f"TRUNCATE TABLE {table_q} RESTART IDENTITY CASCADE"))
    else:
        try:
            c.execute(text(f"TRUNCATE TABLE {table_q}"))
        except Exception:  # noqa: BLE001 - try DELETE fallback
            c.execute(text(f"DELETE FROM {table_q}"))


def _insert_batch(
    trg: OpenedConn,
    table: str,
    columns: list[str],
    rows: list[dict],
    spec: CopySpec,
    dry_run: bool,
) -> int:
    """Insert one batch; return number of rows actually written (0 if dry_run)."""
    if dry_run:
        return 0
    if spec.truncate_first:
        # Truncate once at the start of the first batch for this table.
        # (Idempotent across batches because truncate_first suppresses
        # conflict handling — see __init__ of __run_copy's caller.)
        with trg.engine.begin() as c:
            _truncate_table(c, table, trg.engine)
        spec.truncate_first = False  # only the first batch truncates

    preparer = trg.engine.dialect.identifier_preparer
    cols = ", ".join(preparer.quote(c) for c in columns)
    placeholders = ", ".join(f":{c}" for c in columns)
    dialect = trg.engine.dialect.name
    base_sql = f"INSERT INTO {_quote_ident(table, trg.engine)} ({cols}) VALUES ({placeholders})"

    if spec.on_conflict.value == "skip":
        if dialect == "postgresql":
            base_sql += " ON CONFLICT DO NOTHING"
        elif dialect == "mysql" or dialect == "mariadb":
            base_sql = base_sql.replace("INSERT INTO", "INSERT IGNORE INTO")
        elif dialect == "mssql":
            # No native ON CONFLICT; emulate with NOT EXISTS via MERGE is heavy,
            # so fall back to a per-row NOT EXISTS guard for each batch.
            return _insert_batch_mssql_skip(trg, table, columns, rows)
    elif spec.on_conflict.value == "update":
        if dialect == "postgresql":
            # Without the PK we can't build an ON CONFLICT (col, …). The caller
            # is required to provide a `where` filter that won't produce PK
            # collisions for `update`, OR use `truncate` strategy. Refuse cleanly.
            raise RuntimeError(
                "copy on_conflict=update requires a primary-key-aware dialect "
                "implementation; use on_conflict=truncate or skip instead, or "
                "write per-table custom SQL via `queries`"
            )
        elif dialect in {"mysql", "mariadb"}:
            base_sql = base_sql.replace("INSERT INTO", "INSERT INTO") + " ON DUPLICATE KEY UPDATE id=id"

    with trg.engine.begin() as c:
        r = c.execute(text(base_sql), rows)
    # `rowcount` for `executemany` is the number of rows actually written.
    # For `INSERT IGNORE` / `ON CONFLICT DO NOTHING` it excludes the
    # duplicates the driver skipped; for plain `INSERT` it equals
    # `len(rows)` (or the driver raises on a PK conflict, which propagates
    # as `error`). Some drivers return -1 for "unknown" on `executemany`;
    # in that case fall back to `len(rows)` so the skip tally is conservative
    # (skip = 0) rather than wildly wrong.
    rc = getattr(r, "rowcount", None)
    return rc if isinstance(rc, int) and rc >= 0 else len(rows)


def _insert_batch_mssql_skip(
    trg: OpenedConn,
    table: str,
    columns: list[str],
    rows: list[dict],
) -> int:
    """MSSQL has no native ON CONFLICT; emulate skip by inserting rows one at a
    time guarded by NOT EXISTS on the columns. Slow but correct."""
    preparer = trg.engine.dialect.identifier_preparer
    cols = ", ".join(preparer.quote(c) for c in columns)
    vals = ", ".join(f":{c}" for c in columns)
    table_q = _quote_ident(table, trg.engine)
    not_exists_cols = " AND ".join(f"s.{c}=t.{c}" for c in columns)
    sql = (
        f"INSERT INTO {table_q} ({cols}) "
        f"SELECT {vals} WHERE NOT EXISTS "
        f"(SELECT 1 FROM {table_q} t WHERE {not_exists_cols})"
    )
    inserted = 0
    with trg.engine.begin() as c:
        for row in rows:
            r = c.execute(text(sql), row)
            inserted += r.rowcount or 0
    return inserted


# --------------------------------------------------------------------------- #
# sync — converge one trg table to match src, keyed by sync_spec.key
# --------------------------------------------------------------------------- #
@dataclass
class SyncResult:
    table: str
    src_rows: int
    trg_rows: int
    inserted: int
    updated: int
    deleted: int  # > 0 only when sync_spec.delete_extras
    unchanged: int
    duration_ms: float
    note: str = ""


@dataclass
class SyncReport:
    results: list[SyncResult] = field(default_factory=list)
    total_ms: float = 0.0


def _row_key(row: dict, key: list[str]) -> tuple:
    return tuple(row.get(k) for k in key)


def run_sync(
    src: OpenedConn,
    trg: OpenedConn,
    spec: SyncSpec,
    queries: dict[str, str],
    *,
    dry_run: bool = False,
) -> SyncReport:
    """Converge ``spec.target_table`` on trg to match src.

    Both ``queries.src`` and ``queries.trg`` must return the same column
    shape (the key columns plus every non-key column to write). The src and
    trg result sets are diffed in Python by ``spec.key``; trg is then
    written with INSERT (new keys), UPDATE (changed non-key cols), and —
    only when ``spec.delete_extras`` — DELETE (trg keys absent from src).

    All writes are batched via ``executemany`` of ``spec.batch_size`` rows.
    Dry-run computes the diff and reports counts without writing.
    """
    report = SyncReport()
    started_all = time.monotonic()
    table = spec.target_table

    src_sql = queries.get("src")
    trg_sql = queries.get("trg")
    if not src_sql or not trg_sql:
        raise RuntimeError("sync requires queries.src (SELECT) and queries.trg (SELECT)")

    with src.engine.connect() as sc:
        src_rows = [dict(r) for r in sc.execute(text(to_bindparams(src_sql))).mappings()]
    with trg.engine.connect() as tc:
        trg_rows = [dict(r) for r in tc.execute(text(to_bindparams(trg_sql))).mappings()]

    if not src_rows and not trg_rows:
        report.results.append(SyncResult(table, 0, 0, 0, 0, 0, 0, 0.0))
        report.total_ms = (time.monotonic() - started_all) * 1000
        return report

    # Column universe = union of both sides, key first; both sides must share
    # at least the key columns. Missing non-key cols on a side are treated as
    # NULL for diff purposes (so INSERTs fill them with the src value or
    # omit them from the UPDATE SET clause).
    src_keys = set(src_rows[0].keys()) if src_rows else set()
    trg_keys = set(trg_rows[0].keys()) if trg_rows else set()
    all_cols = list(src_keys | trg_keys)
    # Stable ordering: key cols first (in declared order), then the rest alpha.
    key_set = set(spec.key)
    non_key_cols = sorted(c for c in all_cols if c not in key_set)
    ordered_cols = list(spec.key) + non_key_cols

    src_map = {_row_key(r, spec.key): r for r in src_rows}
    trg_map = {_row_key(r, spec.key): r for r in trg_rows}

    to_insert: list[dict] = []  # rows present in src only
    to_update: list[dict] = []  # rows present in both with differing non-key cols
    to_delete: list[dict] = []  # rows present in trg only

    for k, srow in src_map.items():
        if k not in trg_map:
            to_insert.append(srow)
            continue
        trow = trg_map[k]
        if any(srow.get(c) != trow.get(c) for c in non_key_cols):
            to_update.append(srow)

    if spec.delete_extras:
        for k, trow in trg_map.items():
            if k not in src_map:
                to_delete.append(trow)

    inserted = updated = deleted = 0
    if not dry_run:
        inserted = _sync_inserts(trg, table, ordered_cols, to_insert, spec)
        updated = _sync_updates(trg, table, spec.key, non_key_cols, to_update, spec)
        if spec.delete_extras:
            deleted = _sync_deletes(trg, table, spec.key, to_delete, spec)

    unchanged = len(src_map) - len(to_insert) - len(to_update)
    note = "dry-run" if dry_run else ""
    report.results.append(
        SyncResult(
            table=table,
            src_rows=len(src_rows),
            trg_rows=len(trg_rows),
            inserted=inserted,
            updated=updated,
            deleted=deleted,
            unchanged=unchanged,
            duration_ms=(time.monotonic() - started_all) * 1000,
            note=note,
        )
    )
    report.total_ms = (time.monotonic() - started_all) * 1000
    return report


def _sync_inserts(trg: OpenedConn, table: str, cols: list[str], rows: list[dict], spec: SyncSpec) -> int:
    if not rows:
        return 0
    preparer = trg.engine.dialect.identifier_preparer
    cols_q = ", ".join(preparer.quote(c) for c in cols)
    ph = ", ".join(f":{c}" for c in cols)
    sql = f"INSERT INTO {_quote_ident(table, trg.engine)} ({cols_q}) VALUES ({ph})"
    written = 0
    for i in range(0, len(rows), spec.batch_size):
        batch = rows[i : i + spec.batch_size]
        payload = [{c: r.get(c) for c in cols} for r in batch]
        with trg.engine.begin() as c:
            c.execute(text(sql), payload)
        written += len(batch)
    return written


def _sync_updates(
    trg: OpenedConn, table: str, key: list[str], non_key_cols: list[str], rows: list[dict], spec: SyncSpec
) -> int:
    if not rows or not non_key_cols:
        return 0
    preparer = trg.engine.dialect.identifier_preparer
    set_clause = ", ".join(f"{preparer.quote(c)} = :{c}" for c in non_key_cols)
    where = " AND ".join(f"{preparer.quote(k)} = :_k_{k}" for k in key)
    sql = f"UPDATE {_quote_ident(table, trg.engine)} SET {set_clause} WHERE {where}"
    written = 0
    for i in range(0, len(rows), spec.batch_size):
        batch = rows[i : i + spec.batch_size]
        payload = []
        for r in batch:
            params = {c: r.get(c) for c in non_key_cols}
            params.update({f"_k_{k}": r.get(k) for k in key})
            payload.append(params)
        with trg.engine.begin() as c:
            c.execute(text(sql), payload)
        written += len(batch)
    return written


def _sync_deletes(trg: OpenedConn, table: str, key: list[str], rows: list[dict], spec: SyncSpec) -> int:
    if not rows:
        return 0
    preparer = trg.engine.dialect.identifier_preparer
    where = " AND ".join(f"{preparer.quote(k)} = :_k_{k}" for k in key)
    sql = f"DELETE FROM {_quote_ident(table, trg.engine)} WHERE {where}"
    written = 0
    for i in range(0, len(rows), spec.batch_size):
        batch = rows[i : i + spec.batch_size]
        payload = [{f"_k_{k}": r.get(k) for k in key} for r in batch]
        with trg.engine.begin() as c:
            c.execute(text(sql), payload)
        written += len(batch)
    return written


# --------------------------------------------------------------------------- #
# validate — structural schema diff (columns + types) via SQLAlchemy inspect()
# --------------------------------------------------------------------------- #
@dataclass
class ValidateMismatch:
    table: str
    column: str
    kind: str  # "missing_in_trg" | "missing_in_src" | "type_mismatch"
    src_type: str
    trg_type: str


@dataclass
class ValidateReport:
    mismatches: list[ValidateMismatch] = field(default_factory=list)
    tables_compared: int = 0
    duration_ms: float = 0.0


def _column_repr(col) -> str:
    """Best-effort portable type string: ``"VARCHAR(255)"``, ``"INTEGER"``, …"""
    try:
        return str(col["type"])
    except Exception:  # noqa: BLE001 - reflection quirks vary by dialect
        return getattr(col.get("type", None), "__visit_name__", "unknown") or "unknown"


def run_validate(src: OpenedConn, trg: OpenedConn, spec: ValidateSpec) -> ValidateReport:
    """Compare column sets + types for each table present in both schemas.

    Tables are taken from ``spec.tables`` when set; otherwise the
    intersection of both schemas' user tables is used (system schemas
    skipped). ``spec.include`` / ``spec.exclude`` are column-name filters:
    when ``include`` is non-empty only those columns are compared; any name
    in ``exclude`` is dropped from the comparison.
    """
    report = ValidateReport()
    started = time.monotonic()

    src_tables = set(_introspect_tables(src.engine))
    trg_tables = set(_introspect_tables(trg.engine))
    if spec.tables:
        wanted = [t for t in spec.tables if t in src_tables and t in trg_tables]
    else:
        wanted = sorted(src_tables & trg_tables)
    report.tables_compared = len(wanted)

    include = set(spec.include) if spec.include else None
    exclude = set(spec.exclude)

    for tbl in wanted:
        src_cols = {c["name"]: c for c in inspect(src.engine).get_columns(tbl)}
        trg_cols = {c["name"]: c for c in inspect(trg.engine).get_columns(tbl)}

        src_names = {n for n in src_cols if (include is None or n in include) and n not in exclude}
        trg_names = {n for n in trg_cols if (include is None or n in include) and n not in exclude}

        for n in sorted(src_names - trg_names):
            report.mismatches.append(
                ValidateMismatch(tbl, n, "missing_in_trg", _column_repr(src_cols[n]), "")
            )
        for n in sorted(trg_names - src_names):
            report.mismatches.append(
                ValidateMismatch(tbl, n, "missing_in_src", "", _column_repr(trg_cols[n]))
            )
        for n in sorted(src_names & trg_names):
            st = _column_repr(src_cols[n])
            tt = _column_repr(trg_cols[n])
            if st != tt:
                report.mismatches.append(ValidateMismatch(tbl, n, "type_mismatch", st, tt))

    report.duration_ms = (time.monotonic() - started) * 1000
    return report


# --------------------------------------------------------------------------- #
# replay — copy with a per-row Python transform
# --------------------------------------------------------------------------- #
def _resolve_transform(name: str) -> Callable[[dict], dict]:
    """Resolve a transform spec to a ``Callable[[dict], dict]``.

    * ``"identity"`` → no-op (returns the row unchanged).
    * ``"package.module:callable"`` or ``"package.module.callable"`` →
      import the target and getattr the callable. The callable receives the
      row dict and must return a dict.
    """
    if name == "identity":
        return lambda row: row
    # Allow ``module:attr`` (entry-point flavor) or ``module.attr``.
    if ":" in name:
        mod_name, attr = name.split(":", 1)
    elif "." in name:
        mod_name, attr = name.rsplit(".", 1)
    else:
        raise RuntimeError(f"replay transform {name!r} is neither 'identity' nor a 'module:callable' path")
    import importlib

    try:
        mod = importlib.import_module(mod_name)
        fn = getattr(mod, attr)
    except (ImportError, AttributeError) as e:
        raise RuntimeError(f"cannot resolve replay transform {name!r}: {e}") from e
    if not callable(fn):
        raise RuntimeError(f"replay transform {name!r} resolved to non-callable {fn!r}")
    return fn


def run_replay(
    src: OpenedConn,
    trg: OpenedConn,
    spec: ReplaySpec,
    *,
    batch_size: int | None = None,
    dry_run: bool = False,
) -> CopyReport:
    """Bulk-copy with a per-row transform. Reuses ``run_copy``.

    The transform resolves to a ``Callable[[dict], dict]`` and is applied to
    each row before it lands in the insert batch. ``"identity"`` makes the
    replay equivalent to a plain copy.
    """
    from dbctl.config import CopySpec, OnConflict

    # Adapt ReplaySpec → CopySpec so we reuse the copy machinery verbatim.
    copy_spec = CopySpec(
        batch_size=spec.batch_size,
        tables=spec.tables,
        where=spec.where,
        on_conflict=OnConflict.error,  # replay is copy-with-transform; default to no skip
    )
    transform = _resolve_transform(spec.transform)
    return run_copy(
        src,
        trg,
        copy_spec,
        batch_size=batch_size or spec.batch_size,
        dry_run=dry_run,
        row_transform=transform,
    )
