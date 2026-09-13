"""Shared fixtures for the AI-surface tests (context / ask / mcp).

Everything runs against an isolated ``HOME`` with a sqlite-backed
``.dbctl`` registry — no docker, no network, no tunnels.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

CONNECTIONS_YAML = """
connections:
  pg:
    description: "App database (sqlite stand-in)"
    aliases: [app]
    type: direct
    driver: sqlite
    database: "{db}"
    username: ""
    password: ""
    direct: {{ host: localhost, port: 0 }}
    healthcheck: {{ query: "SELECT 1" }}
    safety: {{ confirm: true, read_only: false }}
  pg-ro:
    description: "Same database, read-only gate"
    type: direct
    driver: sqlite
    database: "{db}"
    username: ""
    password: ""
    direct: {{ host: localhost, port: 0 }}
    healthcheck: {{ query: "SELECT 1" }}
    safety: {{ confirm: true, read_only: true }}
"""

OPERATIONS_YAML = """
operations:
  list-users:
    description: "List users (top N)"
    scope: single
    mode: fetch
    parameters:
      - { name: limit, type: integer, default: 10, position: 1 }
    sql: "SELECT name, credits FROM users ORDER BY name LIMIT $limit"
  find-user:
    description: "Find a user by name prefix"
    scope: single
    mode: fetch
    parameters:
      - { name: prefix, type: string, required: true, position: 1 }
    sql: "SELECT name, credits FROM users WHERE name LIKE $prefix || '%'"
  add-user:
    description: "Create or update an application user"
    scope: single
    mode: execute
    confirm: true
    parameters:
      - { name: name, type: string, required: true, position: 1 }
      - { name: credits, type: integer, required: true, position: 2 }
    sql: "INSERT INTO users (name, credits) VALUES ($name, $credits)"
  user-count:
    description: "Compare user counts between two databases"
    scope: multi
    mode: diff
    roles: [src, trg]
    queries:
      src: "SELECT 'users' AS t, COUNT(*) AS n FROM users"
      trg: "SELECT 'users' AS t, COUNT(*) AS n FROM users"
    diff:
      key: [t]
      show: [n]
"""

_SCHEMA = """
CREATE TABLE tenants (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    credits INT NOT NULL DEFAULT 100,
    tenant_id INT REFERENCES tenants(id)
);
CREATE INDEX idx_users_credits ON users(credits);
CREATE VIEW v_user_credits AS SELECT name, credits FROM users;
INSERT INTO tenants (name) VALUES ('acme'), ('globex');
INSERT INTO users (name, credits, tenant_id) VALUES ('alice', 5, 1), ('bob', 7, 1), ('carol', 9, 2);
"""


@pytest.fixture()
def ai_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HOME with ~/.dbctl/{connections,operations}.yaml pointing
    at a seeded sqlite database; `pg` (writable) + `pg-ro` (read-only)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    db = tmp_path / "app.db"
    con = sqlite3.connect(db)
    con.executescript(_SCHEMA)
    con.commit()
    con.close()
    cfg = tmp_path / ".dbctl"
    cfg.mkdir()
    (cfg / "connections.yaml").write_text(CONNECTIONS_YAML.format(db=db), encoding="utf-8")
    (cfg / "operations.yaml").write_text(OPERATIONS_YAML, encoding="utf-8")
    return tmp_path


@pytest.fixture()
def registries(ai_home: Path):
    """Loaded (conns, ops) for the isolated HOME."""
    from dbctl.connections import load as load_conns
    from dbctl.operations import load as load_ops

    return load_conns(profile=None), load_ops(profile=None)
