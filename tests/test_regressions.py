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
    op = Operation.model_validate({
        "description": "test diff",
        "scope": "multi",
        "mode": "diff",
        "roles": ["src", "trg"],
        "queries": {
            "src": "SELECT 'u' AS t, 1 AS n",
            "trg": "SELECT 'u' AS t, 2 AS n",
        },
        "diff": {"key": ["t"], "show": ["n"]},
    })

    conns = {"pg": SimpleNamespace(description="a", aliases=[]),
             "my": SimpleNamespace(description="b", aliases=[])}

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

    append(profile=None, connection="pg", operation="op-a", params={},
           mode="execute", status="ok")
    append(profile=None, connection="pg", operation="op-b", params={},
           mode="execute", status="ok")

    # append a corrupted tail (simulating a crash mid-write)
    (tmp_path / "h.jsonl").open("a").write("{ broken json\n")

    entries = read(None, limit=5)
    assert len(entries) == 2
    assert {e["operation"] for e in entries} == {"op-a", "op-b"}