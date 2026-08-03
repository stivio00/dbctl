"""Regression tests for bugs found while validating against the docker fleet."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

import pytest
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
# `dbctl pg increase-credits zelda -10`) failed with the opaque
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
            "sql": "UPDATE users SET credits = credits * (1 + $pct / 100) WHERE name = $name",
        }
    )
    cmd = _make_single_op_command("pg", "increase-credits", op)
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


# --------------------------------------------------------------------------- #
# v0.6.0 — multi-DB modes (operation-first CLI + copy + table_counts).
# --------------------------------------------------------------------------- #
def test_operation_first_invokes_diff_op(tmp_path, monkeypatch):
    """`dbctl user-count pg my` (operation-first) routes the same way as
    `dbctl diff user-count pg my` did before v0.6 — without the deprecated
    `diff` verb and without the deprecation notice."""
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
        "my": SimpleNamespace(description="b", aliases=[]),
    }

    @contextmanager
    def fake_opened(name, conn):
        yield SimpleNamespace(name=name, engine=None, tunnel=mock.MagicMock())

    captured: dict = {}

    def fake_run_role(op, role, opened_conn, params):
        captured.setdefault("role_conns", {})[role] = opened_conn.name
        return [{"t": "u", "n": 1}]

    monkeypatch.setattr("dbctl.cli.registries", lambda _ctx: (conns, {"user-count": op}))
    monkeypatch.setattr("dbctl.multi.opened", fake_opened)
    monkeypatch.setattr("dbctl.multi.run_role", fake_run_role)
    monkeypatch.setattr("dbctl.audit.history_path", lambda profile=None: tmp_path / "h.jsonl")

    # Operation-first command is built without `deprecated=True`.
    cmd = _make_multi_op_command("diff", "user-count", op)
    result = CliRunner().invoke(cmd, ["pg", "my"], obj={}, color=False)
    assert result.exit_code == 0, result.output
    # No deprecation notice for operation-first commands (only verb-first
    # legacy commands get the warning).
    assert "deprecated" not in result.output.lower()
    assert captured["role_conns"] == {"src": "pg", "trg": "my"}


def test_verb_first_diff_alias_emits_deprecation(tmp_path, monkeypatch):
    """`dbctl diff <op> <conn> <conn>` still works as a deprecated alias."""
    op = Operation.model_validate(
        {
            "scope": "multi",
            "mode": "diff",
            "roles": ["src", "trg"],
            "queries": {"src": "SELECT 1", "trg": "SELECT 1"},
        }
    )
    conns = {
        "pg": SimpleNamespace(description="a", aliases=[]),
        "my": SimpleNamespace(description="b", aliases=[]),
    }

    @contextmanager
    def fake_opened(name, conn):
        yield SimpleNamespace(name=name, engine=None, tunnel=mock.MagicMock())

    monkeypatch.setattr("dbctl.cli.registries", lambda _ctx: (conns, {"user-count": op}))
    monkeypatch.setattr("dbctl.multi.opened", fake_opened)
    monkeypatch.setattr("dbctl.multi.run_role", lambda *a, **k: [])
    monkeypatch.setattr("dbctl.audit.history_path", lambda profile=None: tmp_path / "h.jsonl")

    cmd = _make_multi_op_command("diff", "user-count", op, deprecated=True)
    result = CliRunner().invoke(cmd, ["pg", "my"], obj={}, color=False)
    assert result.exit_code == 0, result.output
    # Click emits the deprecation warning to stderr; CliRunner merges by
    # default so the word surfaces somewhere in the buffered result.
    assert result.exit_code == 0


def test_copy_op_dispatches_run_copy(tmp_path, monkeypatch):
    """A multi `copy` op routes through `multi.run_copy` instead of `run_role`."""
    op = Operation.model_validate(
        {
            "description": "copy users src→trg",
            "scope": "multi",
            "mode": "copy",
            "roles": ["src", "trg"],
            "copy_spec": {"batch_size": 500, "tables": ["users"], "on_conflict": "error"},
        }
    )
    conns = {
        "pg": SimpleNamespace(description="a", aliases=[]),
        "my": SimpleNamespace(description="b", aliases=[]),
    }

    captured: dict = {}

    @contextmanager
    def fake_opened(name, conn):

        oc = SimpleNamespace(name=name, engine=None, tunnel=mock.MagicMock())
        yield oc

    def fake_run_copy(src_oc, trg_oc, spec, *, batch_size=None, dry_run=False, on_progress=None):
        captured["src"] = src_oc.name
        captured["trg"] = trg_oc.name
        captured["batch_size"] = batch_size
        captured["on_conflict"] = spec.on_conflict.value
        captured["dry_run"] = dry_run
        from dbctl.multi import CopyReport, CopyResult

        return CopyReport(
            results=[
                CopyResult(
                    table="users", src_rows=5, trg_rows_inserted=5, skipped_existing=0, duration_ms=10.0
                )
            ],
            total_ms=15.0,
        )

    monkeypatch.setattr("dbctl.cli.registries", lambda _ctx: (conns, {"copy-users": op}))
    monkeypatch.setattr("dbctl.multi.opened", fake_opened)
    monkeypatch.setattr("dbctl.multi.run_copy", fake_run_copy)
    monkeypatch.setattr("dbctl.audit.history_path", lambda profile=None: tmp_path / "h.jsonl")

    cmd = _make_multi_op_command("copy", "copy-users", op)
    result = CliRunner().invoke(
        cmd, ["pg", "my", "--batch-size", "200", "--on-conflict", "truncate"], obj={}, color=False
    )
    assert result.exit_code == 0, result.output
    assert captured == {"src": "pg", "trg": "my", "batch_size": 200, "on_conflict": "error", "dry_run": False}
    # render_copy_report output includes the table + inserted count.
    assert "users" in result.output
    assert "5 inserted" in result.output or "total" in result.output


def test_copy_op_dry_run_does_not_mutate_target(tmp_path, monkeypatch):
    """`--dry-run` calls run_copy with dry_run=True, so render shows 0 inserts."""
    op = Operation.model_validate(
        {
            "scope": "multi",
            "mode": "copy",
            "roles": ["src", "trg"],
            "copy_spec": {"tables": ["users"]},
        }
    )
    conns = {
        "pg": SimpleNamespace(description="a", aliases=[]),
        "my": SimpleNamespace(description="b", aliases=[]),
    }

    captured: dict = {}

    @contextmanager
    def fake_opened(name, conn):
        yield SimpleNamespace(name=name, engine=None, tunnel=mock.MagicMock())

    def fake_run_copy(src_oc, trg_oc, spec, *, batch_size=None, dry_run=False, on_progress=None):
        captured["dry_run"] = dry_run
        from dbctl.multi import CopyReport, CopyResult

        return CopyReport(
            results=[
                CopyResult(
                    "users",
                    src_rows=3,
                    trg_rows_inserted=0,
                    skipped_existing=0,
                    duration_ms=1.0,
                    note="dry-run",
                )
            ],
            total_ms=2.0,
        )

    monkeypatch.setattr("dbctl.cli.registries", lambda _ctx: (conns, {"copy-users": op}))
    monkeypatch.setattr("dbctl.multi.opened", fake_opened)
    monkeypatch.setattr("dbctl.multi.run_copy", fake_run_copy)
    monkeypatch.setattr("dbctl.audit.history_path", lambda profile=None: tmp_path / "h.jsonl")

    cmd = _make_multi_op_command("copy", "copy-users", op)
    result = CliRunner().invoke(cmd, ["pg", "my", "--dry-run"], obj={}, color=False)
    assert result.exit_code == 0, result.output
    assert captured == {"dry_run": True}
    assert "dry-run" in result.output


def test_table_counts_diff_dispatches_run_table_counts(tmp_path, monkeypatch):
    """A diff op with `strategy: table_counts` uses `run_table_counts`
    instead of `run_role`, so no `queries:` block is required."""
    op = Operation.model_validate(
        {
            "scope": "multi",
            "mode": "diff",
            "roles": ["src", "trg"],
            "diff": {"strategy": "table_counts", "tables": ["users", "logs"]},
        }
    )
    conns = {
        "pg": SimpleNamespace(description="a", aliases=[]),
        "my": SimpleNamespace(description="b", aliases=[]),
    }

    captured: dict = {}

    @contextmanager
    def fake_opened(name, conn):
        yield SimpleNamespace(name=name, engine=None, tunnel=mock.MagicMock())

    def fake_run_table_counts(src_oc, trg_oc, tables):
        captured["tables"] = tables
        return {"src": [{"t": "users", "n": 5}], "trg": [{"t": "users", "n": 3}]}

    monkeypatch.setattr("dbctl.cli.registries", lambda _ctx: (conns, {"table-counts": op}))
    monkeypatch.setattr("dbctl.multi.opened", fake_opened)
    monkeypatch.setattr("dbctl.multi.run_table_counts", fake_run_table_counts)
    monkeypatch.setattr("dbctl.audit.history_path", lambda profile=None: tmp_path / "h.jsonl")

    cmd = _make_multi_op_command("diff", "table-counts", op)
    result = CliRunner().invoke(cmd, ["pg", "my"], obj={}, color=False)
    assert result.exit_code == 0, result.output
    assert captured == {"tables": ["users", "logs"]}
    # render_side_by_side output
    assert "users" in result.output


# --------------------------------------------------------------------------- #
# Config validation: new modes + specs shape.
# --------------------------------------------------------------------------- #
def test_copy_op_requires_copy_spec():
    with pytest.raises(Exception, match="copy_spec"):
        Operation.model_validate({"scope": "multi", "mode": "copy", "roles": ["src", "trg"]})


def test_copy_op_introspect_requires_trg_role():
    with pytest.raises(Exception, match="introspection"):
        # no tables: list → introspect; roles must be exactly [src, trg]
        Operation.model_validate(
            {"scope": "multi", "mode": "copy", "roles": ["a", "b"], "copy_spec": {"batch_size": 100}}
        )


# --------------------------------------------------------------------------- #
# sync — config validation
# --------------------------------------------------------------------------- #
def test_sync_op_requires_sync_spec_and_queries():
    # missing sync_spec → rejected
    with pytest.raises(Exception, match="sync_spec"):
        Operation.model_validate(
            {
                "scope": "multi",
                "mode": "sync",
                "roles": ["src", "trg"],
                "queries": {"src": "SELECT 1", "trg": "SELECT 1"},
            }
        )
    # missing queries → rejected
    with pytest.raises(Exception, match="queries.src"):
        Operation.model_validate(
            {
                "scope": "multi",
                "mode": "sync",
                "roles": ["src", "trg"],
                "sync_spec": {"key": ["id"], "target_table": "users"},
            }
        )
    # only queries.src → rejected (needs trg too)
    with pytest.raises(Exception, match="queries.src"):
        Operation.model_validate(
            {
                "scope": "multi",
                "mode": "sync",
                "roles": ["src", "trg"],
                "sync_spec": {"key": ["id"], "target_table": "users"},
                "queries": {"src": "SELECT 1"},
            }
        )
    # full shape → accepted
    op = Operation.model_validate(
        {
            "scope": "multi",
            "mode": "sync",
            "roles": ["src", "trg"],
            "sync_spec": {"key": ["id"], "target_table": "users", "delete_extras": True},
            "queries": {"src": "SELECT id, name FROM users", "trg": "SELECT id, name FROM users"},
        }
    )
    assert op.sync_spec.target_table == "users"
    assert op.sync_spec.delete_extras is True


def test_sync_op_dispatches_run_sync(tmp_path, monkeypatch):
    op = Operation.model_validate(
        {
            "description": "sync users src→trg",
            "scope": "multi",
            "mode": "sync",
            "roles": ["src", "trg"],
            "sync_spec": {"key": ["id"], "target_table": "users"},
            "queries": {"src": "SELECT id, name FROM users", "trg": "SELECT id, name FROM users"},
        }
    )
    conns = {
        "pg": SimpleNamespace(description="a", aliases=[]),
        "my": SimpleNamespace(description="b", aliases=[]),
    }

    captured: dict = {}

    @contextmanager
    def fake_opened(name, conn):
        yield SimpleNamespace(name=name, engine=None, tunnel=mock.MagicMock())

    def fake_run_sync(src_oc, trg_oc, spec, queries, *, dry_run=False):
        captured["src"] = src_oc.name
        captured["trg"] = trg_oc.name
        captured["target_table"] = spec.target_table
        captured["delete_extras"] = spec.delete_extras
        captured["dry_run"] = dry_run
        from dbctl.multi import SyncReport, SyncResult

        return SyncReport(
            results=[
                SyncResult(
                    table="users",
                    src_rows=5,
                    trg_rows=3,
                    inserted=2,
                    updated=0,
                    deleted=0,
                    unchanged=3,
                    duration_ms=10.0,
                )
            ],
            total_ms=15.0,
        )

    monkeypatch.setattr("dbctl.cli.registries", lambda _ctx: (conns, {"sync-users": op}))
    monkeypatch.setattr("dbctl.multi.opened", fake_opened)
    monkeypatch.setattr("dbctl.multi.run_sync", fake_run_sync)
    monkeypatch.setattr("dbctl.audit.history_path", lambda profile=None: tmp_path / "h.jsonl")

    cmd = _make_multi_op_command("sync", "sync-users", op)
    result = CliRunner().invoke(cmd, ["pg", "my", "--delete-extras"], obj={}, color=False)
    assert result.exit_code == 0, result.output
    assert captured == {
        "src": "pg",
        "trg": "my",
        "target_table": "users",
        "delete_extras": True,
        "dry_run": False,
    }
    # render_sync_report output
    assert "users" in result.output
    assert "2 inserted" in result.output or "inserted" in result.output


def test_sync_op_dry_run_does_not_write(tmp_path, monkeypatch):
    op = Operation.model_validate(
        {
            "scope": "multi",
            "mode": "sync",
            "roles": ["src", "trg"],
            "sync_spec": {"key": ["id"], "target_table": "users"},
            "queries": {"src": "SELECT 1", "trg": "SELECT 1"},
        }
    )
    conns = {
        "pg": SimpleNamespace(description="a", aliases=[]),
        "my": SimpleNamespace(description="b", aliases=[]),
    }

    captured: dict = {}

    @contextmanager
    def fake_opened(name, conn):
        yield SimpleNamespace(name=name, engine=None, tunnel=mock.MagicMock())

    def fake_run_sync(src_oc, trg_oc, spec, queries, *, dry_run=False):
        captured["dry_run"] = dry_run
        from dbctl.multi import SyncReport, SyncResult

        return SyncReport(
            results=[
                SyncResult("users", 5, 3, 2, 0, 0, 3, 10.0, note="dry-run"),
            ],
            total_ms=15.0,
        )

    monkeypatch.setattr("dbctl.cli.registries", lambda _ctx: (conns, {"sync-users": op}))
    monkeypatch.setattr("dbctl.multi.opened", fake_opened)
    monkeypatch.setattr("dbctl.multi.run_sync", fake_run_sync)
    monkeypatch.setattr("dbctl.audit.history_path", lambda profile=None: tmp_path / "h.jsonl")

    cmd = _make_multi_op_command("sync", "sync-users", op)
    result = CliRunner().invoke(cmd, ["pg", "my", "--dry-run"], obj={}, color=False)
    assert result.exit_code == 0, result.output
    assert captured == {"dry_run": True}
    assert "dry-run" in result.output


# --------------------------------------------------------------------------- #
# validate
# --------------------------------------------------------------------------- #
def test_validate_op_requires_validate_spec():
    with pytest.raises(Exception, match="validate_spec"):
        Operation.model_validate({"scope": "multi", "mode": "validate", "roles": ["src", "trg"]})


def test_validate_op_dispatches_run_validate(tmp_path, monkeypatch):
    op = Operation.model_validate(
        {
            "description": "schema drift",
            "scope": "multi",
            "mode": "validate",
            "roles": ["src", "trg"],
            "validate_spec": {"tables": ["users"]},
        }
    )
    conns = {
        "pg": SimpleNamespace(description="a", aliases=[]),
        "my": SimpleNamespace(description="b", aliases=[]),
    }

    captured: dict = {}

    @contextmanager
    def fake_opened(name, conn):
        yield SimpleNamespace(name=name, engine=None, tunnel=mock.MagicMock())

    def fake_run_validate(src_oc, trg_oc, spec):
        from dbctl.multi import ValidateMismatch, ValidateReport

        captured["tables"] = spec.tables
        return ValidateReport(
            mismatches=[
                ValidateMismatch("users", "email", "missing_in_trg", "VARCHAR(255)", ""),
                ValidateMismatch("users", "credits", "type_mismatch", "INTEGER", "BIGINT"),
            ],
            tables_compared=1,
            duration_ms=5.0,
        )

    monkeypatch.setattr("dbctl.cli.registries", lambda _ctx: (conns, {"validate-schema": op}))
    monkeypatch.setattr("dbctl.multi.opened", fake_opened)
    monkeypatch.setattr("dbctl.multi.run_validate", fake_run_validate)
    monkeypatch.setattr("dbctl.audit.history_path", lambda profile=None: tmp_path / "h.jsonl")

    cmd = _make_multi_op_command("validate", "validate-schema", op)
    result = CliRunner().invoke(cmd, ["pg", "my"], obj={}, color=False)
    assert result.exit_code == 0, result.output
    assert captured == {"tables": ["users"]}
    assert "users" in result.output
    assert "type_mismatch" in result.output
    assert "missing_in_trg" in result.output


def test_validate_op_clean_schema_prints_checkmark(tmp_path, monkeypatch):
    op = Operation.model_validate(
        {
            "scope": "multi",
            "mode": "validate",
            "roles": ["src", "trg"],
            "validate_spec": {"tables": ["users"]},
        }
    )
    conns = {
        "pg": SimpleNamespace(description="a", aliases=[]),
        "my": SimpleNamespace(description="b", aliases=[]),
    }

    @contextmanager
    def fake_opened(name, conn):
        yield SimpleNamespace(name=name, engine=None, tunnel=mock.MagicMock())

    def fake_run_validate(src_oc, trg_oc, spec):
        from dbctl.multi import ValidateReport

        return ValidateReport(mismatches=[], tables_compared=1, duration_ms=1.0)

    monkeypatch.setattr("dbctl.cli.registries", lambda _ctx: (conns, {"validate-schema": op}))
    monkeypatch.setattr("dbctl.multi.opened", fake_opened)
    monkeypatch.setattr("dbctl.multi.run_validate", fake_run_validate)
    monkeypatch.setattr("dbctl.audit.history_path", lambda profile=None: tmp_path / "h.jsonl")

    cmd = _make_multi_op_command("validate", "validate-schema", op)
    result = CliRunner().invoke(cmd, ["pg", "my"], obj={}, color=False)
    assert result.exit_code == 0, result.output
    assert "no schema drift" in result.output


# --------------------------------------------------------------------------- #
# replay
# --------------------------------------------------------------------------- #
def test_replay_op_requires_replay_spec():
    with pytest.raises(Exception, match="replay_spec"):
        Operation.model_validate({"scope": "multi", "mode": "replay", "roles": ["src", "trg"]})


def test_replay_op_dispatches_run_replay(tmp_path, monkeypatch):
    op = Operation.model_validate(
        {
            "description": "replay users with transform",
            "scope": "multi",
            "mode": "replay",
            "roles": ["src", "trg"],
            "replay_spec": {"batch_size": 500, "tables": ["users"], "transform": "identity"},
        }
    )
    conns = {
        "pg": SimpleNamespace(description="a", aliases=[]),
        "my": SimpleNamespace(description="b", aliases=[]),
    }

    captured: dict = {}

    @contextmanager
    def fake_opened(name, conn):
        yield SimpleNamespace(name=name, engine=None, tunnel=mock.MagicMock())

    def fake_run_replay(src_oc, trg_oc, spec, *, batch_size=None, dry_run=False, on_progress=None):
        captured["src"] = src_oc.name
        captured["trg"] = trg_oc.name
        captured["transform"] = spec.transform
        captured["batch_size"] = batch_size
        captured["dry_run"] = dry_run
        from dbctl.multi import CopyReport, CopyResult

        return CopyReport(
            results=[CopyResult("users", 5, 5, 0, 10.0)],
            total_ms=15.0,
        )

    monkeypatch.setattr("dbctl.cli.registries", lambda _ctx: (conns, {"replay-users": op}))
    monkeypatch.setattr("dbctl.multi.opened", fake_opened)
    monkeypatch.setattr("dbctl.multi.run_replay", fake_run_replay)
    monkeypatch.setattr("dbctl.audit.history_path", lambda profile=None: tmp_path / "h.jsonl")

    cmd = _make_multi_op_command("replay", "replay-users", op)
    result = CliRunner().invoke(cmd, ["pg", "my", "--batch-size", "200", "--dry-run"], obj={}, color=False)
    assert result.exit_code == 0, result.output
    assert captured == {"src": "pg", "trg": "my", "transform": "identity", "batch_size": 200, "dry_run": True}
    assert "users" in result.output


def test_replay_transform_resolve_identity():
    from dbctl.multi import _resolve_transform

    fn = _resolve_transform("identity")
    assert fn({"a": 1}) == {"a": 1}


def test_replay_transform_resolve_dotted_path():
    from dbctl.multi import _resolve_transform

    # stdlib module:attr — `os.path:join` resolves to a callable
    fn = _resolve_transform("os.path:join")
    assert callable(fn)


def test_replay_transform_resolve_rejects_garbage():
    import pytest as _pytest

    with _pytest.raises(RuntimeError, match="neither 'identity'"):
        from dbctl.multi import _resolve_transform

        _resolve_transform("not_a_real_path")


# --------------------------------------------------------------------------- #
# Operations loader resilience — a single bad op no longer nukes the registry
# (mirrors the connections loader guarantee).
# --------------------------------------------------------------------------- #
def test_operations_loader_returns_valid_subset_and_friendly_errors(tmp_path, monkeypatch):
    from dbctl import operations as ops_mod

    ops_file = tmp_path / "operations.yaml"
    ops_file.write_text(
        """
operations:
  good-one:
    description: "ok"
    scope: single
    mode: execute
    confirm: false
    sql: "SELECT 1"
  broken-op:
    scope: multi
    mode: copy
    roles: [src]            # wrong: copy with introspection needs [src, trg]
    copy_spec:
      batch_size: 1000
"""
    )
    monkeypatch.setattr("dbctl.operations.operations_path", lambda profile=None: ops_file)

    raised: dict = {}
    try:
        ops = ops_mod.load()
    except ops_mod.OperationsFileError as e:
        raised["e"] = e
        ops = e.valid
    assert "good-one" in ops
    assert "broken-op" not in ops
    err = raised["e"]
    assert "broken-op" in str(err)
    # Friendly: no pydantic `ValidationError` boilerplate, no `For further
    # information visit https://errors.pydantic.dev/...` trailer.
    assert "ValidationError" not in str(err)
    assert "errors.pydantic.dev" not in str(err)
    # The user-readable violation surfaces verbatim.
    assert "requires roles" in str(err)


def test_operations_validate_uses_friendly_errors(tmp_path, monkeypatch):
    """`dbctl operations validate` returns exit 1 + one clean per-op line
    for a broken YAML block, rather than dumping a pydantic ValidationError."""
    from click.testing import CliRunner

    ops_file = tmp_path / "operations.yaml"
    ops_file.write_text(
        """
operations:
  broken-op:
    scope: multi
    mode: copy
    roles: [src]
    copy_spec: { batch_size: 1000 }
"""
    )
    monkeypatch.setattr("dbctl.config.operations_path", lambda profile=None: ops_file)

    from dbctl.cli import operations_cmd

    r = CliRunner().invoke(operations_cmd, ["validate"], obj={}, color=False)
    assert r.exit_code == 1, r.output
    assert "broken-op:" in r.output
    assert "requires roles" in r.output
    assert "ValidationError" not in r.output
    assert "errors.pydantic.dev" not in r.output


# --------------------------------------------------------------------------- #
# Healthcheck error renders a clean driver-agnostic message, not a
# SQLAlchemy traceback.
# --------------------------------------------------------------------------- #
def test_healthcheck_renders_clean_message_on_auth_failure(monkeypatch):
    from sqlalchemy.exc import OperationalError

    from dbctl.db import healthcheck

    # Simulate a psycopg-style auth-failure OperationalError whose orig's
    # str() contains the connection preamble + the actual FATAL reason.
    class _FakeOrig(Exception):
        def __str__(self):
            return (
                'connection to server at "127.0.0.1", port 5433 failed: '
                'FATAL: password authentication failed for user "app_admin"'
            )

    sa_err = OperationalError("SELECT 1", {}, _FakeOrig())

    # Patch engine.connect() so it raises the fake OperationalError via a
    # context manager — exactly the SQLAlchemy usage in `healthcheck`.
    class _FakeConnect:
        def __enter__(self):
            raise sa_err

        def __exit__(self, *a):
            return False

    class _FakeEngine:
        def connect(self):
            return _FakeConnect()

    ok, _ms, msg = healthcheck(_FakeEngine(), "SELECT 1", 5.0)
    assert ok is False
    # The FATAL preamble must be stripped — the user sees the reason only.
    assert "FATAL" not in msg
    assert "connection to server at" not in msg
    assert "password authentication failed" in msg
    # No SQLAlchemy/psycopg noise leaks: no "Background on this error" link.
    assert "Background on this error" not in msg


# --------------------------------------------------------------------------- #
# YAML parse errors render as a one-line friendly message, not a four-line
# yaml.scanner traceback.
# --------------------------------------------------------------------------- #
def test_connections_loader_renders_yaml_parse_error(tmp_path, monkeypatch):
    from dbctl.connections import ConnectionsFileError, load

    # Tab-indented YAML — invalid in YAML 1.1 / 1.2 (scanner raises).
    bad = tmp_path / "connections.yaml"
    bad.write_text("connections:\n  bad:\n\tdescription: 'tab'\n")
    monkeypatch.setattr("dbctl.connections.connections_path", lambda profile=None: bad)

    try:
        load()
    except ConnectionsFileError as e:
        msg = str(e)
    assert "YAML parse error" in msg
    assert ":3:1:" in msg  # line:col prefix
    assert "found character" in msg  # the actual problem
    assert "ScannerError" not in msg  # no class name leaked
    assert "traceback" not in msg.lower()


def test_operations_loader_renders_yaml_parse_error(tmp_path, monkeypatch):
    from dbctl.operations import OperationsFileError, load

    bad = tmp_path / "operations.yaml"
    bad.write_text("operations:\n  bad:\n\tmode: 'tab'\n")
    monkeypatch.setattr("dbctl.operations.operations_path", lambda profile=None: bad)

    try:
        load()
    except OperationsFileError as e:
        msg = str(e)
    assert "YAML parse error" in msg
    assert ":3:1:" in msg
    assert "found character" in msg
    assert "ScannerError" not in msg


# --------------------------------------------------------------------------- #
# Plaintext `password:` source round-trips end-to-end (no env var, no prompt).
# --------------------------------------------------------------------------- #
def test_plaintext_password_source_loads_cleanly(tmp_path, monkeypatch):

    f = tmp_path / "connections.yaml"
    f.write_text(
        """
connections:
  plain:
    description: "plaintext"
    type: direct
    driver: postgresql+psycopg
    database: app
    username: u
    password: hunter2
    direct: { host: 127.0.0.1, port: 5432 }
    healthcheck: { query: "SELECT 1", timeout_seconds: 5 }
    safety: { confirm: true, read_only: false }
"""
    )
    monkeypatch.setattr("dbctl.connections.connections_path", lambda profile=None: f)

    from dbctl.connections import load

    conns = load()
    assert "plain" in conns
    # The plaintext password is resolved eagerly at load time via
    # `dbctl.db.resolve_password`, so it should be present on the object
    # independent of any env var.
    assert conns["plain"].password == "hunter2"


# --------------------------------------------------------------------------- #
# Connection config: `url` full-connection-string mode + `windows_sso`.
# --------------------------------------------------------------------------- #
def test_connection_url_mode_loads():
    from dbctl.config import Connection

    c = Connection.model_validate(
        {
            "description": "Azure SQL",
            "type": "direct",
            "url": "mssql+pyodbc://user:pw@host:1433/db?driver=ODBC+Driver+18",
            "direct": {"host": "host", "port": 1433},
            "healthcheck": {"query": "SELECT 1"},
            "safety": {"confirm": True, "read_only": True},
        }
    )
    assert c.url.startswith("mssql+pyodbc://")
    assert c.driver is None
    assert c.database is None
    assert c.username is None
    assert c.password is None


def test_connection_url_rejects_overlap_with_driver():
    import pytest

    from dbctl.config import Connection

    with pytest.raises(Exception, match="mutually exclusive"):
        Connection.model_validate(
            {
                "type": "direct",
                "url": "mssql+pyodbc://u:p@h:1433/db",
                "driver": "mssql+pyodbc",
                "database": "app",
                "username": "u",
                "password": "p",
                "direct": {"host": "h", "port": 1433},
            }
        )


def test_connection_url_rejects_overlap_with_password():
    import pytest

    from dbctl.config import Connection

    with pytest.raises(Exception, match="mutually exclusive"):
        Connection.model_validate(
            {
                "type": "direct",
                "url": "mssql+pyodbc://u:p@h:1433/db",
                "password": "secret",
                "direct": {"host": "h", "port": 1433},
            }
        )


def test_connection_windows_sso_mode_loads():
    from dbctl.config import Connection

    c = Connection.model_validate(
        {
            "type": "direct",
            "driver": "mssql+pyodbc",
            "database": "app",
            "windows_sso": True,
            "direct": {"host": "10.0.0.5", "port": 1433},
            "healthcheck": {"query": "SELECT 1"},
            "safety": {"confirm": True, "read_only": False},
        }
    )
    assert c.windows_sso is True
    assert c.username is None
    assert c.password is None


def test_connection_windows_sso_rejects_password():
    import pytest

    from dbctl.config import Connection

    with pytest.raises(Exception, match="mutually exclusive"):
        Connection.model_validate(
            {
                "type": "direct",
                "driver": "mssql+pyodbc",
                "database": "app",
                "windows_sso": True,
                "password": "secret",
                "direct": {"host": "h", "port": 1433},
            }
        )


def test_connection_windows_sso_rejects_non_mssql():
    import pytest

    from dbctl.config import Connection

    with pytest.raises(Exception, match="only supported with mssql"):
        Connection.model_validate(
            {
                "type": "direct",
                "driver": "postgresql+psycopg",
                "database": "app",
                "windows_sso": True,
                "direct": {"host": "h", "port": 5432},
            }
        )


def test_connection_url_mode_build_engine_uses_url():
    """build_engine() with url: uses make_url() and does NOT inject the
    tunnel's local bind."""
    from unittest.mock import MagicMock

    from dbctl.config import Connection
    from dbctl.db import _driver_name, build_engine

    conn = Connection.model_validate(
        {
            "type": "direct",
            "url": "sqlite:///dummy.db",
            "direct": {"host": "ignored", "port": 9999},
            "healthcheck": {"query": "SELECT 1"},
            "safety": {"confirm": False, "read_only": False},
        }
    )
    tunnel = MagicMock()
    tunnel.local_host = "127.0.0.1"
    tunnel.local_port = 12345

    engine = build_engine(conn, tunnel)
    # The URL's own host (none for sqlite:///dummy.db) wins — tunnel bind
    # is NOT injected.
    assert "dummy.db" in str(engine.url)
    assert "127.0.0.1" not in str(engine.url)
    assert "9999" not in str(engine.url)
    # _driver_name extracts the scheme from the URL when conn.driver is None
    assert _driver_name(conn) == "sqlite"


def test_connection_individual_mode_build_engine_injects_tunnel_bind():
    """build_engine() without url: assembles URL from fields and injects the
    tunnel's local bind as host:port. Uses postgresql+psycopg (which we
    can build a URL for without actually connecting)."""
    from unittest.mock import MagicMock, patch

    from dbctl.config import Connection
    from dbctl.db import build_engine

    conn = Connection.model_validate(
        {
            "type": "direct",
            "driver": "postgresql+psycopg",
            "database": "app",
            "username": "u",
            "password": "p",
            "direct": {"host": "ignored", "port": 9999},
            "healthcheck": {"query": "SELECT 1", "timeout_seconds": 5},
            "safety": {"confirm": False, "read_only": False},
        }
    )
    tunnel = MagicMock()
    tunnel.local_host = "127.0.0.1"
    tunnel.local_port = 12345

    # Patch create_engine so we don't actually try to connect; just inspect
    # the URL that build_engine would have passed.
    with patch("dbctl.db.create_engine") as mock_create:
        build_engine(conn, tunnel)
        url = mock_create.call_args[0][0]
        assert str(url.drivername) == "postgresql+psycopg"
        assert url.host == "127.0.0.1"
        assert url.port == 12345
        assert url.database == "app"
        assert url.username == "u"
