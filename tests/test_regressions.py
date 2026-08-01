"""Regression tests for bugs found while validating against the docker fleet."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

from click.testing import CliRunner

from dbctl.audit import append, read
from dbctl.cli import _make_multi_op_command
from dbctl.config import Operation


# --------------------------------------------------------------------------- #
# Multi-op CLI used to crash on every invocation: click lowercases argument
# names when exposing them as kwargs, but the callback popped `r.upper()`.
# Verifying the fix + guarding against a reintroduction.
# --------------------------------------------------------------------------- #
def test_multi_op_command_uses_lowercase_role_kwargs(tmp_path, monkeypatch):
    op = Operation.model_validate(
        {
            "description": "test diff",
            "scope": "multi",
            "mode": "diff",
            "roles": ["src", "trg"],
            "queries": {
                "src": "SELECT 'u' AS t, 1 AS n",
                "trg": "SELECT 'u' AS t, 2 AS n",
            },
            "diff": {"key": ["t"], "show": ["n"]},
        }
    )

    conns = {
        "pg": SimpleNamespace(description="a", aliases=[]),
        "my": SimpleNamespace(description="b", aliases=[]),
    }

    @contextmanager
    def fake_opened(name, conn):
        yield SimpleNamespace(name=name, engine=None, tunnel=mock.MagicMock())

    captured: dict = {}

    def fake_run_role(_op, role, opened_conn, _params):
        captured.setdefault("role_conns", {})[role] = opened_conn.name
        return [{"t": "u", "n": 1 if role == "src" else 2}]

    monkeypatch.setattr("dbctl.cli.registries", lambda _ctx: (conns, {"test-diff": op}))
    monkeypatch.setattr("dbctl.multi.opened", fake_opened)
    monkeypatch.setattr("dbctl.multi.run_role", fake_run_role)
    monkeypatch.setattr("dbctl.audit.history_path", lambda profile=None: tmp_path / "h.jsonl")

    cmd = _make_multi_op_command("diff", "test-diff", op)
    result = CliRunner().invoke(cmd, ["pg", "my"], obj={})
    assert result.exit_code == 0, result.output
    assert captured["role_conns"] == {"src": "pg", "trg": "my"}


# --------------------------------------------------------------------------- #
# audit.read() must skip corrupt lines *before* truncating to `limit`, not
# after — otherwise a half-written trailing line evicts a valid older entry
# from the window.
# --------------------------------------------------------------------------- #
def test_audit_read_skips_corrupt_lines_without_losing_history(tmp_path, monkeypatch):
    from dbctl import audit

    monkeypatch.setattr(audit, "history_path", lambda profile=None: tmp_path / "h.jsonl")

    append(profile=None, connection="pg", operation="op-a", params={}, mode="execute", status="ok")
    append(profile=None, connection="pg", operation="op-b", params={}, mode="execute", status="ok")

    # append a corrupted tail (simulating a crash mid-write)
    (tmp_path / "h.jsonl").open("a").write("{ broken json\n")

    entries = read(None, limit=5)
    assert len(entries) == 2
    assert {e["operation"] for e in entries} == {"op-a", "op-b"}


# --------------------------------------------------------------------------- #
# Bug: `$name::type` cast broke psycopg (syntax error at ":").
# `to_bindparams` now rewrites to `CAST(:name AS type)`.
# --------------------------------------------------------------------------- #
def test_to_bindparams_handles_cast_idiom():
    from dbctl.execute import to_bindparams

    assert to_bindparams("SELECT $x::timestamp") == "SELECT CAST(:x AS timestamp)"
    assert (
        to_bindparams("WHERE created_at >= $since::timestamp")
        == "WHERE created_at >= CAST(:since AS timestamp)"
    )


# --------------------------------------------------------------------------- #
# Bug: a negative number for a positional Argument (e.g.
# `dbctl pg increase-quota zelda -10`) failed with the opaque
# `No such option '-1'`. The CLI now sub-classes `click.Command` to
# append a "-- separator" hint showing the user's actual token.
# --------------------------------------------------------------------------- #
def test_negative_positional_arg_emits_separator_hint():
    from dbctl.cli import _make_single_op_command
    from dbctl.config import Operation

    op = Operation.model_validate(
        {
            "description": "bump pct",
            "scope": "single",
            "mode": "execute",
            "confirm": False,  # don't prompt; we never reach execution
            "parameters": [
                {"name": "name", "type": "string", "required": True, "position": 1},
                {"name": "pct", "type": "float", "required": True, "position": 2},
            ],
            "sql": "UPDATE users SET quota = quota * (1 + $pct / 100) WHERE name = $name",
        }
    )
    cmd = _make_single_op_command("pg", "increase-quota", op)
    result = CliRunner().invoke(cmd, ["zelda", "-10"], obj={}, color=False)
    # Click returns exit code 2 on UsageError; we just want the hint.
    assert "No such option '-1'" in result.output
    assert "hint:" in result.output
    # The user's actual token `-10` must appear (Click truncates to `-1`).
    assert "-10" in result.output
    assert "--" in result.output


# --------------------------------------------------------------------------- #
# Bug: `dbctl connections show <alias>` reported "unknown connection"
# because `connections_show` did a direct dict lookup instead of going
# through the alias-aware `resolve()`.
# --------------------------------------------------------------------------- #
def test_connections_show_resolves_alias(tmp_path, monkeypatch):
    from dbctl.config import Connection, TunnelType

    conns = {
        "pg": Connection(
            description="Postgres dev",
            aliases=["postgres"],
            type=TunnelType.direct,
            driver="postgresql+psycopg",
            database="app",
            username="u",
            password="p",
            direct={"host": "127.0.0.1", "port": 5432},
        )
    }
    monkeypatch.setattr("dbctl.cli.registries", lambda _ctx: (conns, {}))

    from dbctl.cli import connections_cmd

    # Canonical name resolves.
    r = CliRunner().invoke(connections_cmd, ["show", "pg"], obj={}, color=False)
    assert r.exit_code == 0, r.output
    assert "pg:" in r.output
    assert "Postgres dev" in r.output

    # Alias resolves to the canonical connection (top key is "pg", not "postgres").
    r = CliRunner().invoke(connections_cmd, ["show", "postgres"], obj={}, color=False)
    assert r.exit_code == 0, r.output
    assert "pg:" in r.output
    assert "postgres:" not in r.output.split("pg:")[0]  # not the alias as the key

    # Unknown connection still produces a clean error + exit code 2.
    r = CliRunner().invoke(connections_cmd, ["show", "zzz"], obj={}, color=False)
    assert r.exit_code == 2
    assert "unknown connection 'zzz'" in r.output


# --------------------------------------------------------------------------- #
# Bug: `dbctl diff user-count pg zzz` crashed with a raw
# `UnknownConnectionError` traceback. The multi-op callback now catches
# `KeyError` and emits a one-line message on stderr instead.
# --------------------------------------------------------------------------- #
def test_multi_op_unknown_connection_emits_clean_error(tmp_path, monkeypatch):
    op = Operation.model_validate(
        {
            "description": "test diff",
            "scope": "multi",
            "mode": "diff",
            "roles": ["src", "trg"],
            "queries": {"src": "SELECT 1", "trg": "SELECT 1"},
        }
    )
    conns = {
        "pg": SimpleNamespace(description="a", aliases=[]),
        # note: no "zzz" connection
    }

    @contextmanager
    def fake_opened(name, conn):
        yield SimpleNamespace(name=name, engine=None, tunnel=mock.MagicMock())

    def fake_run_role(_op, role, opened_conn, _params):
        return [{"t": "u", "n": 1}]

    monkeypatch.setattr("dbctl.cli.registries", lambda _ctx: (conns, {"test-diff": op}))
    monkeypatch.setattr("dbctl.multi.opened", fake_opened)
    monkeypatch.setattr("dbctl.multi.run_role", fake_run_role)
    monkeypatch.setattr("dbctl.audit.history_path", lambda profile=None: tmp_path / "h.jsonl")

    cmd = _make_multi_op_command("diff", "test-diff", op)
    result = CliRunner().invoke(cmd, ["pg", "zzz"], obj={}, color=False)
    assert result.exit_code == 2, result.output
    assert "unknown connection" in result.output
    # No traceback leaked to the user.
    assert "Traceback" not in result.output
    assert "UnknownConnectionError" not in result.output
