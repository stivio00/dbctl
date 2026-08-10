"""Introspection tests against a real (file-based) sqlite engine - no
Textual app involved, no docker, no real database."""

from __future__ import annotations

import sqlite3

from sqlalchemy import create_engine

from dbctl.ui import schema


def _engine(tmp_path):
    path = tmp_path / "schema_test.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT)")
    con.execute("CREATE INDEX idx_users_email ON users(email)")
    con.execute("CREATE VIEW active_users AS SELECT * FROM users")
    con.commit()
    con.close()
    return create_engine(f"sqlite:///{path}")


def test_list_tables_and_views(tmp_path):
    engine = _engine(tmp_path)
    assert schema.list_tables(engine) == ["users"]
    assert schema.list_views(engine) == ["active_users"]


def test_list_columns_marks_primary_key(tmp_path):
    engine = _engine(tmp_path)
    columns = schema.list_columns(engine, "users")
    by_name = {c.name: c for c in columns}
    assert by_name["id"].primary_key
    assert not by_name["name"].primary_key
    assert not by_name["name"].nullable
    assert by_name["email"].nullable


def test_list_indexes(tmp_path):
    engine = _engine(tmp_path)
    indexes = schema.list_indexes(engine, "users")
    assert len(indexes) == 1
    assert indexes[0].name == "idx_users_email"
    assert indexes[0].columns == ["email"]


def test_list_schemas_returns_at_least_one(tmp_path):
    engine = _engine(tmp_path)
    assert schema.list_schemas(engine)
