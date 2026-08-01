"""End-to-end logic tests using an in-memory SQLite database.

These tests bypass the subprocess tunnels entirely (direct connection to an
in-memory DB), focusing on the parts of dbctl that actually run SQL:

* placeholder rewriting ($name → :name)
* parameter binding + type coercion
* operation mode routing (execute, fetch, fetch_one)
* multi-connection diff join + side-by-side rendering
* audit log append/read
"""

from __future__ import annotations

import contextlib
import sqlite3

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from dbctl.audit import append, read
from dbctl.config import (
    Operation,
)
from dbctl.execute import bind_params, format_sql, render, to_bindparams


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture()
def sqlite_conn() -> sqlite3.Connection:
    """The DBAPI connection that SQLAlchemy and the code path both bind to."""
    cwd = sqlite3.connect(":memory:")
    cur = cwd.cursor()
    cur.executescript("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            quota_daily INT NOT NULL DEFAULT 100,
            quota_yearly INT NOT NULL DEFAULT 36500,
            type TEXT NOT NULL DEFAULT 'Daily',
            is_active INT NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    cwd.commit()
    yield cwd
    cwd.close()


@pytest.fixture()
def engine(sqlite_conn) -> Engine:
    """SQLAlchemy engine that reuses the same in-memory DBAPI connection."""
    e = create_engine("sqlite://", creator=lambda: sqlite_conn, future=True)
    yield e


@pytest.fixture()
def add_user_op() -> Operation:
    return Operation.model_validate(
        {
            "description": "add or update a user (sqlite dialect)",
            "scope": "single",
            "mode": "execute",
            "confirm": True,
            "parameters": [
                {"name": "name", "type": "string", "required": True, "position": 1, "description": "name"},
                {"name": "quota", "type": "integer", "required": True, "position": 2, "description": "daily"},
                {"name": "type", "type": "string", "default": "Daily", "position": 3, "description": "type"},
            ],
            "sql": (
                "INSERT INTO users (name, quota_daily, quota_yearly, type) "
                "VALUES ($name, $quota, $quota * 365, $type) "
                "ON CONFLICT (name) DO UPDATE SET quota_daily = EXCLUDED.quota_daily"
            ),
        }
    )


# --------------------------------------------------------------------------- #
# placeholder rewriting
# --------------------------------------------------------------------------- #
def test_to_bindparams_simple():
    assert to_bindparams("SELECT $name") == "SELECT :name"


def test_to_bindparams_multiple():
    assert to_bindparams("VALUES ($a, $b, $c)") == "VALUES (:a, :b, :c)"


def test_to_bindparams_leaves_dollar_digit_alone():
    # $1 is a positional param in some DBs - not a valid identifier after $
    assert to_bindparams("SELECT $1") == "SELECT $1"


# --------------------------------------------------------------------------- #
# parameter binding
# --------------------------------------------------------------------------- #
def test_bind_params_coerces_int():
    op = Operation.model_validate(
        {
            "scope": "single",
            "mode": "execute",
            "sql": "SELECT 1",
            "parameters": [{"name": "n", "type": "integer", "required": True}],
        }
    )
    out = bind_params(op, {"n": "42"})
    assert out == {"n": 42} and isinstance(out["n"], int)


def test_bind_params_uses_default():
    op = Operation.model_validate(
        {
            "scope": "single",
            "mode": "execute",
            "sql": "SELECT 1",
            "parameters": [
                {"name": "n", "type": "integer", "default": 99},
            ],
        }
    )
    out = bind_params(op, {})
    assert out == {"n": 99}


def test_bind_params_list_splits_commas():
    op = Operation.model_validate(
        {
            "scope": "single",
            "mode": "execute",
            "sql": "SELECT 1",
            "parameters": [{"name": "ts", "type": "list", "required": True}],
        }
    )
    out = bind_params(op, {"ts": "a, b ,c"})
    assert out == {"ts": ["a", "b", "c"]}


def test_bind_params_missing_required_raises():
    op = Operation.model_validate(
        {
            "scope": "single",
            "mode": "execute",
            "sql": "SELECT 1",
            "parameters": [{"name": "n", "type": "string", "required": True}],
        }
    )
    with pytest.raises(ValueError, match="missing required parameter"):
        bind_params(op, {})


def test_format_sql_interpolates():
    add = Operation.model_validate(
        {
            "scope": "single",
            "mode": "execute",
            "sql": "INSERT INTO t VALUES ($name, $n)",
            "parameters": [
                {"name": "name", "type": "string", "required": True},
                {"name": "n", "type": "integer", "required": True},
            ],
        }
    )
    rendered = format_sql(add, {"name": "alice", "n": 12})
    assert ":name" not in rendered and "'alice'" in rendered and "12" in rendered


# --------------------------------------------------------------------------- #
# execution modes against sqlite
# --------------------------------------------------------------------------- #
def test_execute_modewrites_row(engine, add_user_op):
    bound = bind_params(add_user_op, {"name": "alice", "quota": 100})
    with engine.begin() as c:
        res = render(c, add_user_op, bound)
    assert res.rows_affected == 1
    assert res.rows is None  # execute mode doesn't return rows


def test_fetch_mode_returns_dict_rows(engine):
    fetch_op = Operation.model_validate(
        {
            "scope": "single",
            "mode": "fetch",
            "sql": "SELECT name, quota_daily FROM users",
            "parameters": [],
        }
    )
    with engine.begin() as c:
        c.execute(text("INSERT INTO users (name, quota_daily) VALUES ('a', 1), ('b', 2)"))
    with engine.connect() as c:
        res = render(c, fetch_op, {})
    assert res.rows == [{"name": "a", "quota_daily": 1}, {"name": "b", "quota_daily": 2}]


def test_fetch_one_mode_returns_one(engine):
    fetch_one = Operation.model_validate(
        {
            "scope": "single",
            "mode": "fetch_one",
            "sql": "SELECT quota_daily FROM users WHERE name = $name",
            "parameters": [{"name": "name", "type": "string", "required": True}],
        }
    )
    with engine.begin() as c:
        c.execute(text("INSERT INTO users (name, quota_daily) VALUES ('a', 1)"))
    with engine.connect() as c:
        res = render(c, fetch_one, {"name": "a"})
    assert res.rows == [{"quota_daily": 1}]


def test_upsert_mode_dispatches_error(engine):
    up = Operation.model_validate(
        {
            "scope": "single",
            "mode": "upsert",
            "sql": None,
            "parameters": [],
        }
    )
    with engine.connect() as c, pytest.raises(RuntimeError, match="dispatched in execute.upsert"):
        render(c, up, {})


# --------------------------------------------------------------------------- #
# multi-connection diff
# --------------------------------------------------------------------------- #
def test_diff_side_by_side_renders(engine):
    from dbctl.reports import render_side_by_side

    with engine.begin() as c:
        c.exec_driver_sql("CREATE TABLE t (k TEXT, n INT)")
        c.exec_driver_sql("INSERT INTO t VALUES ('users', 10), ('events', 5)")
    rows_a = [{"k": "users", "n": 10}, {"k": "events", "n": 5}]
    rows_b = [{"k": "users", "n": 12}, {"k": "events", "n": 5}]
    # should not raise, just render.
    with contextlib.suppress(SystemExit):
        render_side_by_side(rows_a, rows_b, key=["k"], show=["n"], label_a="src", label_b="trg")


# --------------------------------------------------------------------------- #
# audit log
# --------------------------------------------------------------------------- #
def test_audit_append_read(tmp_path, monkeypatch):
    from dbctl import audit

    monkeypatch.setattr(audit, "history_path", lambda profile=None: tmp_path / "h.jsonl")
    rid = append(
        profile=None,
        connection="pg",
        operation="add-user",
        params={"name": "alice"},
        mode="execute",
        status="ok",
        rows_affected=1,
        duration_ms=12.3,
    )
    assert isinstance(rid, str) and len(rid) == 12
    entries = read(None, limit=5)
    assert len(entries) == 1
    assert entries[0]["connection"] == "pg" and entries[0]["status"] == "ok"


def test_audit_redacts_secret_params(tmp_path, monkeypatch):
    from dbctl import audit

    monkeypatch.setattr(audit, "history_path", lambda profile=None: tmp_path / "h.jsonl")
    append(
        profile=None,
        connection="pg",
        operation="change-pwd",
        params={"username": "alice", "password": "hunter2"},
        mode="execute",
        status="ok",
        redact={"password"},
    )
    entries = read(None, limit=5)
    assert entries[0]["params"]["password"] == "***"
    assert entries[0]["params"]["username"] == "alice"
