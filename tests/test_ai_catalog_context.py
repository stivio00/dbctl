"""Tests for the shared catalog (secret-free summaries) and the markdown
context pack (`dbctl context`)."""

from __future__ import annotations

import sqlite3

import pytest
from click.testing import CliRunner
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from dbctl.catalog import connection_summary, operation_summary, redact_params
from dbctl.config import Operation
from dbctl.context import build_context


@pytest.fixture()
def sqlite_engine(tmp_path) -> Engine:
    con = sqlite3.connect(tmp_path / "ctx.db")
    con.executescript(
        """
        CREATE TABLE tenants (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            tenant_id INT REFERENCES tenants(id)
        );
        CREATE INDEX idx_users_name ON users(name);
        CREATE VIEW v_users AS SELECT name FROM users;
        """
    )
    con.commit()
    con.close()
    return create_engine(f"sqlite:///{tmp_path / 'ctx.db'}", future=True)


# --------------------------------------------------------------------------- #
# catalog
# --------------------------------------------------------------------------- #
def test_connection_summary_has_no_secrets(registries):
    conns, _ = registries
    s = connection_summary("pg", conns["pg"])
    assert s["name"] == "pg"
    assert s["driver"] == "sqlite"
    assert s["database"].endswith("app.db")
    assert s["read_only"] is False
    assert s["aliases"] == ["app"]
    for forbidden in ("password", "password_env", "url", "username"):
        assert forbidden not in s


def test_operation_summary_params_and_sql_flag(registries):
    _, ops = registries
    s = operation_summary("add-user", ops["add-user"])
    assert s["mode"] == "execute"
    assert [p["name"] for p in s["parameters"]] == ["name", "credits"]
    assert "sql" not in s
    s2 = operation_summary("add-user", ops["add-user"], include_sql=True)
    assert "$name" in s2["sql"]


def test_redact_params_masks_secret_typed():
    op = Operation.model_validate(
        {
            "scope": "single",
            "mode": "execute",
            "sql": "SELECT 1",
            "parameters": [
                {"name": "pw", "type": "secret", "required": True},
                {"name": "user", "type": "string", "required": True},
            ],
        }
    )
    out = redact_params(op, {"pw": "hunter2", "user": "alice"})
    assert out == {"pw": "***", "user": "alice"}


# --------------------------------------------------------------------------- #
# context pack
# --------------------------------------------------------------------------- #
def test_global_context_markdown(registries):
    conns, ops = registries
    conns["pg"].password = "super-secret-value"
    md = build_context(conns, ops)
    assert "# dbctl context" in md
    assert "| pg |" in md
    assert "### `add-user`" in md
    assert "$name" in md  # sql included by default
    assert "super-secret-value" not in md  # credentials never rendered


def test_context_markdown_without_sql(registries):
    conns, ops = registries
    md = build_context(conns, ops, include_sql=False)
    assert "### `add-user`" in md
    assert "$credits" not in md


def test_context_schema_section_links_indexes_views(sqlite_engine, registries):
    conns, ops = registries
    md = build_context(
        conns,
        ops,
        conn_name="pg",
        conn=conns["pg"],
        engine=sqlite_engine,
    )
    assert "## Connection: `pg`" in md
    assert "**users**" in md
    assert "| id |" in md
    assert "PK" in md
    assert "link" in md and "tenants(id)" in md  # FK link rendered
    assert "idx_users_name" in md  # index rendered
    assert "v_users" in md  # view rendered
    assert "super-secret" not in md


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def test_cli_context_global(ai_home):
    from dbctl.cli import main

    res = CliRunner().invoke(main, ["context"])
    assert res.exit_code == 0
    assert "# dbctl context" in res.output
    assert "### `list-users`" in res.output


def test_cli_context_connection_with_schema(ai_home):
    from dbctl.cli import main

    res = CliRunner().invoke(main, ["context", "pg"])
    assert res.exit_code == 0
    assert "## Connection: `pg`" in res.output
    assert "**users**" in res.output
    assert "tenants(id)" in res.output


def test_cli_context_unknown_connection_exits_2(ai_home):
    from dbctl.cli import main

    res = CliRunner().invoke(main, ["context", "nope"])
    assert res.exit_code == 2


def test_cli_context_output_file(ai_home):
    from dbctl.cli import main

    out = ai_home / "pack.md"
    res = CliRunner().invoke(main, ["context", "-o", str(out)])
    assert res.exit_code == 0
    assert out.exists()
    assert "# dbctl context" in out.read_text()
