"""Shared fixtures for the `dbctl.ui` test suite.

These tests use a `direct` sqlite connection - no docker, no real database,
no network I/O in the `DirectTunnel` passthrough - so they run everywhere
`pytest` does. They are separate from any manual smoke test against the
docker-compose fleet.
"""

from __future__ import annotations

import sqlite3

import pytest

pytest.importorskip("textual")

from dbctl.config import Connection  # noqa: E402


@pytest.fixture()
def sqlite_db_path(tmp_path):
    path = tmp_path / "ui_test.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
    con.execute("CREATE INDEX idx_users_name ON users(name)")
    con.execute("CREATE VIEW active_users AS SELECT * FROM users")
    con.executemany("INSERT INTO users (name) VALUES (?)", [("alice",), ("bob",)])
    con.commit()
    con.close()
    return path


@pytest.fixture()
def sqlite_connection(sqlite_db_path) -> Connection:
    return Connection.model_validate(
        {
            "type": "direct",
            "driver": "sqlite",
            "database": str(sqlite_db_path),
            "username": "",
            "password": "",
            "direct": {"host": "localhost", "port": 0},
        }
    )


@pytest.fixture()
def stub_registry(monkeypatch, tmp_path, sqlite_connection):
    """Point `dbctl.ui.registry.load_registries` at an in-memory registry
    (one `sqlite-test` connection, no operations) and redirect the audit
    log to a tmp file - never touches the user's real ~/.dbctl config."""
    connections = {"sqlite-test": sqlite_connection}
    monkeypatch.setattr("dbctl.ui.registry.load_connections", lambda profile=None: dict(connections))
    monkeypatch.setattr("dbctl.ui.registry.load_operations", lambda profile=None: {})
    monkeypatch.setattr("dbctl.audit.history_path", lambda profile=None: tmp_path / "history.jsonl")
    return connections
