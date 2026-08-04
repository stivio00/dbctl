"""Tests for the `copy` extensions added to make cross-dialect migration
scripts unnecessary: `exclude_columns`, `transforms`, the `--validate-data`
pre-flight pass (`check_copy_constraints`), and batch-failure diagnostics
(`CopyError` / row bisection).

Two real (file-based) SQLite databases stand in for src/trg — separate
files so each gets its own independent schema/data, unlike the shared
in-memory connection used in `test_smoke.py`.
"""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import create_engine

from dbctl.config import ColumnTransform, CopySpec, OnConflict, Operation
from dbctl.multi import CopyError, OpenedConn, check_copy_constraints, run_copy


def _make_db(path, ddl: str, rows: list[tuple]):
    conn = sqlite3.connect(str(path))
    conn.executescript(ddl)
    if rows:
        placeholders = ", ".join("?" * len(rows[0]))
        conn.executemany(f"INSERT INTO t VALUES ({placeholders})", rows)
    conn.commit()
    conn.close()


@pytest.fixture()
def src_trg(tmp_path):
    src_path = tmp_path / "src.db"
    trg_path = tmp_path / "trg.db"
    src_engine = create_engine(f"sqlite:///{src_path}", future=True)
    trg_engine = create_engine(f"sqlite:///{trg_path}", future=True)
    src = OpenedConn(name="src", engine=src_engine, tunnel=None)
    trg = OpenedConn(name="trg", engine=trg_engine, tunnel=None)
    return src_path, trg_path, src, trg


# --------------------------------------------------------------------------- #
# exclude_columns
# --------------------------------------------------------------------------- #
def test_copy_exclude_columns_drops_source_identity_column(src_trg):
    src_path, trg_path, src, trg = src_trg
    # src has an explicit `id` the target must not receive — trg generates
    # its own via AUTOINCREMENT, starting from 1 regardless of src's ids.
    _make_db(
        src_path,
        "CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)",
        [(100, "alice"), (101, "bob")],
    )
    _make_db(trg_path, "CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)", [])

    spec = CopySpec(tables=["t"], exclude_columns=["id"])
    report = run_copy(src, trg, spec)
    assert report.results[0].trg_rows_inserted == 2

    with trg.engine.connect() as c:
        rows = c.exec_driver_sql("SELECT id, name FROM t ORDER BY id").fetchall()
    # trg's own autoincrement ids (1, 2), not src's (100, 101).
    assert [r[1] for r in rows] == ["alice", "bob"]
    assert [r[0] for r in rows] == [1, 2]


# --------------------------------------------------------------------------- #
# transforms
# --------------------------------------------------------------------------- #
def test_copy_transforms_rstrip_matches_source_collation_semantics(src_trg):
    src_path, trg_path, src, trg = src_trg
    # SQL Server-style source data: trailing spaces the source collation
    # ignores in comparisons but the target (byte-exact) would not.
    _make_db(src_path, "CREATE TABLE t (name TEXT)", [("alice   ",), ("bob",)])
    _make_db(trg_path, "CREATE TABLE t (name TEXT)", [])

    spec = CopySpec(tables=["t"], transforms={"name": ColumnTransform.rstrip_})
    run_copy(src, trg, spec)

    with trg.engine.connect() as c:
        names = [r[0] for r in c.exec_driver_sql("SELECT name FROM t ORDER BY name").fetchall()]
    assert names == ["alice", "bob"]


def test_copy_transforms_upper_lower(src_trg):
    src_path, trg_path, src, trg = src_trg
    _make_db(src_path, "CREATE TABLE t (a TEXT, b TEXT)", [("MiXeD", "CaSe")])
    _make_db(trg_path, "CREATE TABLE t (a TEXT, b TEXT)", [])

    spec = CopySpec(tables=["t"], transforms={"a": ColumnTransform.upper_, "b": ColumnTransform.lower_})
    run_copy(src, trg, spec)

    with trg.engine.connect() as c:
        row = c.exec_driver_sql("SELECT a, b FROM t").fetchone()
    assert row == ("MIXED", "case")


def test_copy_transforms_noop_on_non_string_values(src_trg):
    src_path, trg_path, src, trg = src_trg
    _make_db(src_path, "CREATE TABLE t (n INTEGER)", [(5,), (None,)])
    _make_db(trg_path, "CREATE TABLE t (n INTEGER)", [])

    spec = CopySpec(tables=["t"], transforms={"n": ColumnTransform.trim})
    run_copy(src, trg, spec)

    with trg.engine.connect() as c:
        rows = sorted(r[0] for r in c.exec_driver_sql("SELECT n FROM t").fetchall() if r[0] is not None)
    assert rows == [5]


# --------------------------------------------------------------------------- #
# check_copy_constraints (--validate-data pre-flight)
# --------------------------------------------------------------------------- #
def test_check_copy_constraints_reports_not_null_and_too_long(src_trg):
    src_path, trg_path, src, trg = src_trg
    _make_db(
        src_path,
        "CREATE TABLE t (id INTEGER, name TEXT, model TEXT)",
        [
            (1, "ok-row", "short"),
            (2, None, "short"),  # violates NOT NULL on trg.name
            (3, "ok-row-2", "way-too-long-value"),  # exceeds trg.model VARCHAR(10)
        ],
    )
    _make_db(
        trg_path,
        "CREATE TABLE t (id INTEGER, name VARCHAR(50) NOT NULL, model VARCHAR(10))",
        [],
    )

    spec = CopySpec(tables=["t"], exclude_columns=["id"])
    violations = check_copy_constraints(src, trg, spec)

    kinds = {(v.column, v.kind, v.row_index) for v in violations}
    assert ("name", "not_null", 1) in kinds
    assert ("model", "too_long", 2) in kinds
    assert len(violations) == 2


def test_check_copy_constraints_clean_data_reports_nothing(src_trg):
    src_path, trg_path, src, trg = src_trg
    _make_db(src_path, "CREATE TABLE t (name VARCHAR(50))", [("alice",), ("bob",)])
    _make_db(trg_path, "CREATE TABLE t (name VARCHAR(50) NOT NULL)", [])

    spec = CopySpec(tables=["t"])
    assert check_copy_constraints(src, trg, spec) == []


def test_check_copy_constraints_applies_transforms_before_checking(src_trg):
    """A too-long value that a transform shortens (e.g. rstrip removing
    padding) must not be reported — the check simulates the actual insert
    shape, not the raw source value."""
    src_path, trg_path, src, trg = src_trg
    _make_db(src_path, "CREATE TABLE t (name TEXT)", [("short     ",)])  # 10 chars incl. padding
    _make_db(trg_path, "CREATE TABLE t (name VARCHAR(5))", [])

    spec = CopySpec(tables=["t"], transforms={"name": ColumnTransform.rstrip_})
    assert check_copy_constraints(src, trg, spec) == []


# --------------------------------------------------------------------------- #
# failure diagnostics (CopyError + row bisection)
# --------------------------------------------------------------------------- #
def test_copy_failure_diagnosis_identifies_offending_row(src_trg):
    src_path, trg_path, src, trg = src_trg
    _make_db(
        src_path,
        "CREATE TABLE t (name TEXT)",
        [("alice",), ("bob",), (None,), ("dana",)],
    )
    _make_db(trg_path, "CREATE TABLE t (name TEXT NOT NULL)", [])

    spec = CopySpec(tables=["t"], batch_size=10)  # whole table in one batch
    with pytest.raises(CopyError) as exc_info:
        run_copy(src, trg, spec)

    msg = str(exc_info.value)
    assert "row 2" in msg  # 0-based index of the None row
    assert "NOT NULL" in msg
    # No leaked SQLAlchemy wrapper noise (driver class name / doc link).
    assert "Background on this error" not in msg


def test_copy_failure_diagnosis_can_be_disabled(src_trg):
    from sqlalchemy.exc import IntegrityError

    src_path, trg_path, src, trg = src_trg
    _make_db(src_path, "CREATE TABLE t (name TEXT)", [(None,)])
    _make_db(trg_path, "CREATE TABLE t (name TEXT NOT NULL)", [])

    spec = CopySpec(tables=["t"], diagnose_failures=False)
    with pytest.raises(IntegrityError):
        run_copy(src, trg, spec)


def test_copy_on_progress_hook_reports_start_progress_done(src_trg):
    src_path, trg_path, src, trg = src_trg
    _make_db(src_path, "CREATE TABLE t (name TEXT)", [("a",), ("b",), ("c",), ("d",), ("e",)])
    _make_db(trg_path, "CREATE TABLE t (name TEXT)", [])

    events: list[tuple[str, str, int]] = []
    spec = CopySpec(tables=["t"], batch_size=2)  # 3 batches: 2, 2, 1
    run_copy(src, trg, spec, on_progress=lambda event, table, rows: events.append((event, table, rows)))

    assert events[0] == ("start", "t", 0)
    assert events[-1] == ("done", "t", 5)
    # cumulative row count strictly increases across "progress" events
    progress_events = [e for e in events if e[0] == "progress"]
    assert [e[2] for e in progress_events] == [2, 4]


def test_copy_failure_diagnosis_does_not_run_on_dry_run(src_trg):
    """A dry-run never writes, so there is nothing to diagnose — the plain
    exception type from `_insert_batch` (a no-op returning 0) means this
    scenario simply can't fail; this test guards that dry-run short-circuits
    before diagnosis machinery is ever invoked."""
    src_path, trg_path, src, trg = src_trg
    _make_db(src_path, "CREATE TABLE t (name TEXT)", [(None,)])
    _make_db(trg_path, "CREATE TABLE t (name TEXT NOT NULL)", [])

    spec = CopySpec(tables=["t"])
    report = run_copy(src, trg, spec, dry_run=True)
    assert report.results[0].trg_rows_inserted == 0
    assert report.results[0].note == "dry-run"


# --------------------------------------------------------------------------- #
# config validation
# --------------------------------------------------------------------------- #
def test_copy_spec_accepts_exclude_columns_and_transforms():
    op = Operation.model_validate(
        {
            "scope": "multi",
            "mode": "copy",
            "roles": ["src", "trg"],
            "copy_spec": {
                "tables": ["t"],
                "exclude_columns": ["Id"],
                "transforms": {"notes": "rstrip"},
            },
        }
    )
    assert op.copy_spec.exclude_columns == ["Id"]
    assert op.copy_spec.transforms == {"notes": ColumnTransform.rstrip_}
    assert op.copy_spec.diagnose_failures is True  # default on


def test_copy_spec_rejects_unknown_transform_name():
    with pytest.raises(Exception, match="transforms"):
        Operation.model_validate(
            {
                "scope": "multi",
                "mode": "copy",
                "roles": ["src", "trg"],
                "copy_spec": {"tables": ["t"], "transforms": {"col": "not-a-real-transform"}},
            }
        )


def test_copy_spec_on_conflict_still_works_alongside_new_fields():
    op = Operation.model_validate(
        {
            "scope": "multi",
            "mode": "copy",
            "roles": ["src", "trg"],
            "copy_spec": {
                "tables": ["t"],
                "on_conflict": "skip",
                "exclude_columns": ["Id"],
                "diagnose_failures": False,
            },
        }
    )
    assert op.copy_spec.on_conflict == OnConflict.skip
    assert op.copy_spec.diagnose_failures is False
