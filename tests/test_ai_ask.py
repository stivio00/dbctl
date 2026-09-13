"""Tests for `dbctl ask` — heuristic routing, parameter extraction, the
optional LLM path (``_chat`` monkeypatched), and the CLI wiring."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from dbctl.ask import (
    AskError,
    Plan,
    ensure_allowed,
    llm_config,
    plan_from_question,
)
from dbctl.config import Connection, Operation


# --------------------------------------------------------------------------- #
# fixtures: registries built directly (no HOME needed)
# --------------------------------------------------------------------------- #
def _conn(read_only: bool = False, allowed: list[str] | None = None) -> Connection:
    return Connection.model_validate(
        {
            "description": "test db",
            "type": "direct",
            "driver": "sqlite",
            "database": ":memory:",
            "username": "",
            "password": "",
            "direct": {"host": "localhost", "port": 0},
            "safety": {"confirm": True, "read_only": read_only, "allowed_operations": allowed or []},
        }
    )


def _ops() -> dict[str, Operation]:
    return {
        "list-users": Operation.model_validate(
            {
                "description": "List users (top N)",
                "scope": "single",
                "mode": "fetch",
                "parameters": [{"name": "limit", "type": "integer", "default": 10, "position": 1}],
                "sql": "SELECT name FROM users LIMIT $limit",
            }
        ),
        "find-user": Operation.model_validate(
            {
                "description": "Find a user by name prefix",
                "scope": "single",
                "mode": "fetch",
                "parameters": [{"name": "prefix", "type": "string", "required": True, "position": 1}],
                "sql": "SELECT name FROM users WHERE name LIKE $prefix",
            }
        ),
        "add-user": Operation.model_validate(
            {
                "description": "Create or update an application user",
                "scope": "single",
                "mode": "execute",
                "confirm": True,
                "parameters": [
                    {"name": "name", "type": "string", "required": True, "position": 1},
                    {"name": "credits", "type": "integer", "required": True, "position": 2},
                ],
                "sql": "INSERT INTO users (name, credits) VALUES ($name, $credits)",
            }
        ),
        "user-count": Operation.model_validate(
            {
                "description": "Compare user counts between two databases",
                "scope": "multi",
                "mode": "diff",
                "roles": ["src", "trg"],
                "queries": {"src": "SELECT 1", "trg": "SELECT 1"},
                "diff": {"key": ["t"]},
            }
        ),
    }


def _one_conn() -> dict[str, Connection]:
    return {"pg": _conn()}


@pytest.fixture()
def one_conn() -> dict[str, Connection]:
    return _one_conn()


# --------------------------------------------------------------------------- #
# heuristic routing
# --------------------------------------------------------------------------- #
def test_routes_by_name_and_extract_positional(one_conn):
    plan = plan_from_question("top 5 users on pg", one_conn, _ops())
    assert plan.operation == "list-users"
    assert plan.connection == "pg"
    assert plan.params == {"limit": "5"}


def test_routes_quoted_string_value(one_conn):
    plan = plan_from_question("find user 'alice' on pg", one_conn, _ops())
    assert plan.operation == "find-user"
    assert plan.params == {"prefix": "alice"}


def test_key_value_params(one_conn):
    plan = plan_from_question("find user on pg prefix=al", one_conn, _ops())
    assert plan.operation == "find-user"
    assert plan.params == {"prefix": "al"}


def test_dml_positional_pair(one_conn):
    plan = plan_from_question("add user zelda with 100 credits on pg", one_conn, _ops())
    assert plan.operation == "add-user"
    assert plan.params == {"name": "zelda", "credits": "100"}


def test_single_conn_default_without_mention():
    plan = plan_from_question("list users", _one_conn(), _ops())
    assert plan.connection == "pg"


def test_ambiguous_connection_returns_none_plan():
    conns = {"pg": _conn(), "my": _conn()}
    plan = plan_from_question("list users", conns, _ops())
    assert plan.operation == "list-users"
    assert plan.connection is None  # CLI prompts / errors


def test_multi_conn_mention_picks_it():
    conns = {"pg": _conn(), "my": _conn()}
    plan = plan_from_question("list users on my", conns, _ops())
    assert plan.connection == "my"


def test_no_match_raises():
    with pytest.raises(AskError, match="no operation matches"):
        plan_from_question("rotate the ssl certificates", _one_conn(), _ops())


def test_empty_question_raises():
    with pytest.raises(AskError, match="empty"):
        plan_from_question("   ", _one_conn(), _ops())


def test_prefer_op_forces():
    plan = plan_from_question("nothing relevant here", _one_conn(), _ops(), prefer_op="find-user")
    assert plan.operation == "find-user"


def test_prefer_op_multi_scope_rejected():
    with pytest.raises(AskError, match="multi-scope"):
        plan_from_question("count users", _one_conn(), _ops(), prefer_op="user-count")


def test_allowed_operations_whitelist_blocks():
    conns = {"pg": _conn(allowed=["find-user"])}
    with pytest.raises(AskError, match="allowed_operations"):
        plan_from_question("add user zelda with 5 credits on pg", conns, _ops())


def test_ensure_allowed():
    conn = _conn(allowed=["list-users"])
    ensure_allowed(conn, "list-users")  # passes
    with pytest.raises(AskError):
        ensure_allowed(conn, "add-user")


def test_plan_preview_masks_secrets(one_conn):
    from dbctl.config import ParamType

    ops = _ops()
    ops["find-user"].parameters[0].type = ParamType.secret
    plan = Plan(connection="pg", operation="find-user", params={"prefix": "hunter2"})
    assert "hunter2" not in plan.preview(ops)
    assert "***" in plan.preview(ops)


# --------------------------------------------------------------------------- #
# LLM path (_chat monkeypatched — no network)
# --------------------------------------------------------------------------- #
_LLM_JSON = json.dumps(
    {
        "connection": "pg",
        "operation": "find-user",
        "params": {"prefix": "al"},
        "rationale": "the user wants to find a user by prefix",
    }
)


def test_llm_config_detected(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k-test")
    monkeypatch.delenv("DBCTL_LLM_PROVIDER", raising=False)
    cfg = llm_config()
    assert cfg is not None and cfg.provider == "anthropic"


def test_llm_config_absent(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DBCTL_LLM_PROVIDER", raising=False)
    assert llm_config() is None


def test_llm_plan_from_question(monkeypatch, one_conn):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k-test")
    monkeypatch.setattr("dbctl.ask._chat", lambda cfg, system, user: _LLM_JSON)
    plan = plan_from_question("find a user called al", one_conn, _ops(), use_llm=True)
    assert plan.source == "llm"
    assert plan.operation == "find-user"
    assert plan.params == {"prefix": "al"}


def test_llm_failure_falls_back_to_heuristic(monkeypatch, one_conn):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k-test")

    def boom(cfg, system, user):
        raise AskError("LLM API error 500 from anthropic")

    monkeypatch.setattr("dbctl.ask._chat", boom)
    plan = plan_from_question("top 5 users on pg", one_conn, _ops())  # auto mode
    assert plan.source == "heuristic"
    assert plan.operation == "list-users"


def test_llm_failure_explicit_raises(monkeypatch, one_conn):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k-test")

    def boom(cfg, system, user):
        raise AskError("LLM API error 500 from anthropic")

    monkeypatch.setattr("dbctl.ask._chat", boom)
    with pytest.raises(AskError, match="500"):
        plan_from_question("top 5 users on pg", one_conn, _ops(), use_llm=True)


def test_llm_unconfigured_explicit_raises(monkeypatch, one_conn):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DBCTL_LLM_PROVIDER", raising=False)
    with pytest.raises(AskError, match="no LLM configured"):
        plan_from_question("top 5 users", one_conn, _ops(), use_llm=True)


def test_llm_unknown_operation_rejected(monkeypatch, one_conn):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k-test")
    bad = json.dumps({"connection": "pg", "operation": "nope", "params": {}, "rationale": ""})
    monkeypatch.setattr("dbctl.ask._chat", lambda cfg, system, user: bad)
    with pytest.raises(AskError, match="unknown operation"):
        plan_from_question("anything", one_conn, _ops(), use_llm=True)


def test_parse_llm_json_strips_fences(monkeypatch, one_conn):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k-test")
    fenced = f"```json\n{_LLM_JSON}\n```"
    monkeypatch.setattr("dbctl.ask._chat", lambda cfg, system, user: fenced)
    plan = plan_from_question("find a user called al", one_conn, _ops(), use_llm=True)
    assert plan.operation == "find-user"


def test_llm_prompt_contains_catalogs_not_secrets(monkeypatch, one_conn):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k-test")
    one_conn["pg"].password = "super-secret-pw"
    captured = {}

    def fake_chat(cfg, system, user):
        captured["user"] = user
        return _LLM_JSON

    monkeypatch.setattr("dbctl.ask._chat", fake_chat)
    plan_from_question("find a user called al", one_conn, _ops(), use_llm=True)
    assert "find-user" in captured["user"]
    assert "super-secret-pw" not in captured["user"]


# --------------------------------------------------------------------------- #
# CLI end-to-end against the isolated sqlite HOME
# --------------------------------------------------------------------------- #
def test_cli_ask_fetch(ai_home):
    from dbctl.cli import main

    res = CliRunner().invoke(main, ["ask", "top", "2", "users", "on", "pg"])
    assert res.exit_code == 0, res.output
    assert "list-users" in res.output
    assert "alice" in res.output


def test_cli_ask_dml_defaults_to_dry_run(ai_home):
    from dbctl.cli import main

    res = CliRunner().invoke(main, ["ask", "add", "user", "zelda", "with", "100", "credits", "on", "pg"])
    assert res.exit_code == 0, res.output
    assert "dry-run" in res.output
    assert "INSERT INTO users" in res.output


def test_cli_ask_dml_apply_yes_commits(ai_home):
    import sqlite3

    from dbctl.cli import main

    res = CliRunner().invoke(
        main, ["ask", "add", "user", "zelda", "with", "100", "credits", "on", "pg", "--apply", "--yes"]
    )
    assert res.exit_code == 0, res.output
    db = ai_home / "app.db"
    con = sqlite3.connect(db)
    try:
        assert con.execute("SELECT credits FROM users WHERE name='zelda'").fetchone() == (100,)
    finally:
        con.close()


def test_cli_ask_missing_required_param_exits_2(ai_home):
    from dbctl.cli import main

    # "add user zelda on pg" — credits is required and not inferable
    res = CliRunner().invoke(main, ["ask", "add", "user", "zelda", "on", "pg"])
    assert res.exit_code == 2, res.output


def test_cli_ask_unknown_connection_exits_2(ai_home):
    from dbctl.cli import main

    res = CliRunner().invoke(main, ["ask", "list", "users", "on", "nope"])
    assert res.exit_code == 2, res.output
