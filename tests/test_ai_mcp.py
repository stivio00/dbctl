"""Tests for `dbctl mcp serve` — tool registration, catalogs, schema
introspection, operation drafting, the safety-gated runner, and auditing.

Calls ``build_server().call_tool`` directly (no stdio server, no network).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from dbctl.mcp_server import build_server, draft_operation_yaml, schema_payload


async def _call(srv: Any, name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Invoke a tool and normalize the result to a dict across mcp 1.x/2.x."""
    res = await srv.call_tool(name, args or {})
    sc = getattr(res, "structured_content", None)
    if isinstance(sc, dict):
        return sc
    content = getattr(res, "content", None)
    if isinstance(content, list) and content and getattr(content[0], "text", None):
        return json.loads(content[0].text)
    if isinstance(res, tuple) and isinstance(res[0], list):  # mcp 1.x (content, _) shape
        return json.loads(res[0][0].text)
    raise AssertionError(f"unexpected call_tool result: {res!r}")


# --------------------------------------------------------------------------- #
# tool registration + catalogs
# --------------------------------------------------------------------------- #
async def test_tools_registered():
    srv = build_server()
    tools = await srv.list_tools()
    names = {t.name for t in tools}
    assert names == {
        "list_connections",
        "list_operations",
        "get_schema",
        "draft_operation",
        "run_operation",
        "health",
    }


async def test_list_connections_no_secrets(ai_home):
    srv = build_server()
    out = await _call(srv, "list_connections")
    names = [c["name"] for c in out["connections"]]
    assert names == ["pg", "pg-ro"]
    pg = out["connections"][0]
    assert pg["driver"] == "sqlite" and pg["aliases"] == ["app"]
    for c in out["connections"]:
        assert "password" not in c and "url" not in c


async def test_list_operations_includes_sql(ai_home):
    srv = build_server()
    out = await _call(srv, "list_operations")
    ops = {o["name"]: o for o in out["operations"]}
    assert "add-user" in ops and "$credits" in ops["add-user"]["sql"]
    assert ops["user-count"]["scope"] == "multi"


# --------------------------------------------------------------------------- #
# schema introspection (SQLAlchemy inspector)
# --------------------------------------------------------------------------- #
async def test_get_schema_full(ai_home):
    srv = build_server()
    out = await _call(srv, "get_schema", {"connection": "pg"})
    assert out["status"] == "ok"
    assert out["dialect"]["name"] == "sqlite"
    assert out["dialect"]["database"].endswith("app.db")
    tables = {t["name"]: t for t in out["tables"]}
    assert set(tables) == {"tenants", "users"}
    users = tables["users"]
    cols = {c["name"]: c for c in users["columns"]}
    assert cols["id"]["primary_key"] is True
    assert cols["name"]["nullable"] is False
    fks = users["foreign_keys"]
    assert fks and fks[0]["ref_table"] == "tenants" and "tenant_id" in fks[0]["columns"]
    ixs = {i["name"] for i in users["indexes"]}
    assert "idx_users_credits" in ixs
    views = {v["name"] for v in out["views"]}
    assert "v_user_credits" in views
    # authoring guide: dialect-correct, parameterized-operation hints
    guide = out["authoring"]
    assert "$name" in guide["placeholder_style"]
    assert "parameters:" in guide["example_entry_yaml"] and "$floor" in guide["example_entry_yaml"]
    assert any("sqlite" not in h and "ON CONFLICT" in h for h in guide["dialect_hints"])
    assert any("datetime('now'" in h for h in guide["dialect_hints"])


async def test_get_schema_table_filter(ai_home):
    srv = build_server()
    out = await _call(srv, "get_schema", {"connection": "pg", "table": "users"})
    assert [t["name"] for t in out["tables"]] == ["users"]
    missing = await _call(srv, "get_schema", {"connection": "pg", "table": "nope"})
    assert missing["status"] == "error" and missing["exit_code"] == 2


async def test_get_schema_truncation(ai_home):
    srv = build_server()
    out = await _call(srv, "get_schema", {"connection": "pg", "max_tables": 1})
    assert out["tables_truncated"] is True
    assert out["table_count"] == 2
    assert len(out["tables"]) == 1


def test_schema_payload_unknown_connection(ai_home):
    out = schema_payload(profile=None, connection="nope")
    assert out["status"] == "error" and out["exit_code"] == 2


def test_authoring_guide_per_dialect():
    from dbctl.mcp_server import authoring_guide

    pg = authoring_guide("postgresql")
    assert any("ON CONFLICT" in h for h in pg["dialect_hints"])
    assert any("RETURNING" in h for h in pg["dialect_hints"])
    my = authoring_guide("mysql")
    assert any("ON DUPLICATE KEY" in h for h in my["dialect_hints"])
    ms = authoring_guide("mssql")
    assert any("MERGE" in h or "TOP" in h for h in ms["dialect_hints"])
    # unknown dialects fall back to the dialect-neutral common hints only
    exotic = authoring_guide("exoticdb")
    common = authoring_guide("sqlite")
    assert len(exotic["dialect_hints"]) < len(common["dialect_hints"])
    assert all("ON CONFLICT" not in h for h in exotic["dialect_hints"])
    # every guide teaches the same placeholder discipline
    for g in (pg, my, ms, exotic):
        assert "$name" in g["placeholder_style"]
        assert "scope" in g["example_entry_yaml"]


# --------------------------------------------------------------------------- #
# draft_operation (LLM-authored operations)
# --------------------------------------------------------------------------- #
_DRAFT = """
my-op:
  description: "Find heavy users by credit floor"
  scope: single
  mode: fetch
  parameters:
    - { name: floor, type: integer, required: true, position: 1 }
  sql: "SELECT name FROM users WHERE credits >= $floor"
"""


def test_draft_operation_valid(ai_home):
    out = draft_operation_yaml(profile=None, operation_yaml=_DRAFT)
    assert out["status"] == "ok", out
    assert out["name"] == "my-op"
    path = Path(out["draft_path"])
    assert path.exists() and path.parent.name == "drafts"
    import yaml

    saved = yaml.safe_load(path.read_text())
    assert saved["my-op"]["sql"].startswith("SELECT name FROM users")
    # draft must NOT leak into the live registry
    from dbctl.operations import load

    assert "my-op" not in load(profile=None)


def test_draft_operation_undeclared_placeholder_rejected(ai_home):
    bad = _DRAFT.replace("$floor", "$min_credits")
    out = draft_operation_yaml(profile=None, operation_yaml=bad)
    assert out["status"] == "error" and out["exit_code"] == 2
    assert "min_credits" in out["error"]


def test_draft_operation_unused_param_warns_but_saves(ai_home):
    draft = _DRAFT.replace(
        "- { name: floor, type: integer, required: true, position: 1 }",
        "- { name: floor, type: integer, required: true, position: 1 }\n"
        "    - { name: unused_one, type: string, default: x }",
    )
    out = draft_operation_yaml(profile=None, operation_yaml=draft)
    assert out["status"] == "ok"
    assert any("unused_one" in w for w in out["warnings"])


def test_draft_operation_invalid_field_rejected(ai_home):
    bad = _DRAFT.replace("mode: fetch", "modee: fetch")  # extra=forbid catches the typo
    out = draft_operation_yaml(profile=None, operation_yaml=bad)
    assert out["status"] == "error" and out["exit_code"] == 2


def test_draft_operation_bad_yaml_rejected(ai_home):
    out = draft_operation_yaml(profile=None, operation_yaml=":\n  - broken [")
    assert out["status"] == "error" and out["exit_code"] == 2


def test_draft_operation_two_ops_rejected(ai_home):
    two = _DRAFT + _DRAFT.replace("my-op", "other-op")
    out = draft_operation_yaml(profile=None, operation_yaml=two)
    assert out["status"] == "error"


def test_draft_operation_wrapped_form_accepted(ai_home):
    wrapped = "operations:\n" + _DRAFT
    out = draft_operation_yaml(profile=None, operation_yaml=wrapped)
    assert out["status"] == "ok" and out["name"] == "my-op"


# --------------------------------------------------------------------------- #
# run_operation: safety-gated execution
# --------------------------------------------------------------------------- #
async def test_run_operation_fetch(ai_home):
    srv = build_server()
    out = await _call(
        srv,
        "run_operation",
        {"connection": "pg", "operation": "list-users", "params": {"limit": 2}},
    )
    assert out["status"] == "ok"
    assert out["row_count"] == 2
    assert [r["name"] for r in out["rows"]] == ["alice", "bob"]


async def test_run_operation_fetch_row_cap(ai_home):
    srv = build_server()
    out = await _call(
        srv,
        "run_operation",
        {"connection": "pg", "operation": "list-users", "params": {"limit": 10}, "max_rows": 2},
    )
    assert out["status"] == "ok"
    assert out["row_count"] == 3
    assert out["rows_truncated"] is True
    assert len(out["rows"]) == 2


async def test_run_operation_dml_defaults_to_dry_run(ai_home):
    srv = build_server()
    out = await _call(
        srv,
        "run_operation",
        {"connection": "pg", "operation": "add-user", "params": {"name": "zelda", "credits": 100}},
    )
    assert out["status"] == "dry-run"
    assert "INSERT INTO users" in out["sql"]
    assert out["run_id"]
    # nothing written
    con = sqlite3.connect(ai_home / "app.db")
    try:
        assert con.execute("SELECT COUNT(*) FROM users WHERE name='zelda'").fetchone() == (0,)
    finally:
        con.close()


async def test_run_operation_dml_apply_blocked_without_allow_write(ai_home):
    srv = build_server()
    out = await _call(
        srv,
        "run_operation",
        {
            "connection": "pg",
            "operation": "add-user",
            "params": {"name": "zelda", "credits": 100},
            "apply": True,
        },
    )
    assert out["status"] == "blocked"
    assert out["exit_code"] == 6
    assert "--allow-write" in out["error"]


async def test_run_operation_dml_apply_with_allow_write(ai_home):
    srv = build_server(allow_write=True)
    out = await _call(
        srv,
        "run_operation",
        {
            "connection": "pg",
            "operation": "add-user",
            "params": {"name": "zelda", "credits": 100},
            "apply": True,
        },
    )
    assert out["status"] == "ok", out
    assert out["rows_affected"] == 1
    con = sqlite3.connect(ai_home / "app.db")
    try:
        assert con.execute("SELECT credits FROM users WHERE name='zelda'").fetchone() == (100,)
    finally:
        con.close()


async def test_run_operation_read_only_connection_blocked(ai_home):
    srv = build_server(allow_write=True)
    out = await _call(
        srv,
        "run_operation",
        {
            "connection": "pg-ro",
            "operation": "add-user",
            "params": {"name": "zelda", "credits": 100},
            "apply": True,
        },
    )
    assert out["status"] == "error" and out["exit_code"] == 6


async def test_run_operation_unknown_names_exit_2(ai_home):
    srv = build_server()
    out = await _call(srv, "run_operation", {"connection": "nope", "operation": "list-users"})
    assert out["status"] == "error" and out["exit_code"] == 2
    out = await _call(srv, "run_operation", {"connection": "pg", "operation": "nope"})
    assert out["status"] == "error" and out["exit_code"] == 2


async def test_run_operation_missing_required_param_exit_2(ai_home):
    srv = build_server()
    out = await _call(srv, "run_operation", {"connection": "pg", "operation": "add-user"})
    assert out["status"] == "error" and out["exit_code"] == 2


async def test_run_operation_multi_scope_rejected(ai_home):
    srv = build_server()
    out = await _call(srv, "run_operation", {"connection": "pg", "operation": "user-count"})
    assert out["status"] == "error" and out["exit_code"] == 2
    assert "multi-scope" in out["error"]


async def test_run_operation_bad_sql_exit_1(ai_home):
    import yaml

    from dbctl.config import operations_path

    ops_path: Path = operations_path(None)
    ops = yaml.safe_load(ops_path.read_text())
    ops["operations"]["broken-op"] = {
        "description": "references a missing table",
        "scope": "single",
        "mode": "fetch",
        "sql": "SELECT * FROM missing_table_xyz",
    }
    ops_path.write_text(yaml.safe_dump(ops))
    srv = build_server()
    out = await _call(srv, "run_operation", {"connection": "pg", "operation": "broken-op"})
    assert out["status"] == "error" and out["exit_code"] == 1


# --------------------------------------------------------------------------- #
# health + audit
# --------------------------------------------------------------------------- #
async def test_health_tool(ai_home):
    srv = build_server()
    out = await _call(srv, "health", {"connection": "pg"})
    assert out["status"] == "ok" and out["latency_ms"] >= 0
    out = await _call(srv, "health", {"connection": "nope"})
    assert out["status"] == "error" and out["exit_code"] == 2


async def test_every_run_is_audited(ai_home):
    from dbctl.audit import read

    srv = build_server()
    await _call(srv, "run_operation", {"connection": "pg", "operation": "list-users"})
    await _call(
        srv,
        "run_operation",
        {"connection": "pg", "operation": "add-user", "params": {"name": "x", "credits": 1}, "apply": True},
    )
    entries = read(None, limit=10)
    statuses = [e["status"] for e in entries]
    assert "ok" in statuses and "blocked" in statuses
    assert all(e.get("actor") == "mcp" for e in entries)
