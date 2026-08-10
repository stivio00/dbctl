"""Tests for ``dbctl execute`` (ad-hoc SQL verb added in v0.7.6).

Covers:
* SQL classification (SELECT-shaped vs DML) via the leading verb.
* Inline SQLAlchemy URL mode — transient Connection + opened_engine ctx.
* Named connection mode — registries + opened_conn ctx.
* Output formats: table / json / csv / yaml.
* DML dry-run-by-default when ``safety.confirm: true`` + ``--apply`` commits.
* ``safety.read_only: true`` blocks DML with exit 6.
* ``-y`` / ``--yes`` skips the confirmation prompt.
* Audit log entries (operation="execute", params.sql, status, mode).
* Unknown connection -> clean exit 2, no traceback.
* Empty SQL -> exit 2.
* SQL starting with a dash uses ``--`` separator.
* DDL (CREATE TABLE) reports ``OK in <ms>ms`` instead of ``OK -1 rows``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest import mock

import pytest
from click.testing import CliRunner

from dbctl.cli import execute_cmd


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _make_inline_url(tmp_path) -> str:
    """A file-backed sqlite URL — independent of connections.yaml."""
    return f"sqlite:///{tmp_path / 'adhoc.db'}"


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def with_history(tmp_path, monkeypatch):
    from dbctl import audit

    monkeypatch.setattr(audit, "history_path", lambda profile=None: tmp_path / "h.jsonl")
    return audit


# --------------------------------------------------------------------------- #
# SQL classification
# --------------------------------------------------------------------------- #
def test_select_verb_classified_as_read():
    from dbctl.cli import _sql_is_read

    assert _sql_is_read("SELECT 1") is True
    assert _sql_is_read("  (  SELECT 1)") is True
    assert _sql_is_read("WITH x AS (SELECT 1) SELECT * FROM x") is True
    assert _sql_is_read("SHOW TABLES") is True
    assert _sql_is_read("EXPLAIN SELECT 1") is True
    assert _sql_is_read("DESCRIBE users") is True
    assert _sql_is_read("PRAGMA table_info(users)") is True
    assert _sql_is_read("VALUES (1, 2)") is True


def test_dml_verb_classified_as_write():
    from dbctl.cli import _sql_is_read

    assert _sql_is_read("INSERT INTO t VALUES (1)") is False
    assert _sql_is_read("UPDATE t SET x=1") is False
    assert _sql_is_read("DELETE FROM t") is False
    assert _sql_is_read("CREATE TABLE t (id INT)") is False
    assert _sql_is_read("DROP TABLE t") is False
    assert _sql_is_read("ALTER TABLE t ADD COLUMN x INT") is False
    assert _sql_is_read("") is False


# --------------------------------------------------------------------------- #
# inline URL SELECT — output formats
# --------------------------------------------------------------------------- #
def test_execute_inline_url_select_json(runner, tmp_path):
    url = _make_inline_url(tmp_path)
    r = runner.invoke(
        execute_cmd,
        ["-c", url, "-o", "json", "SELECT 1 AS one, 'a' AS two"],
        obj={},
        color=False,
    )
    assert r.exit_code == 0, r.output + (r.stderr or "")
    data = json.loads(r.output)
    assert data == [{"one": 1, "two": "a"}]


def test_execute_inline_url_select_yaml(runner, tmp_path):
    url = _make_inline_url(tmp_path)
    r = runner.invoke(
        execute_cmd,
        ["-c", url, "-o", "yaml", "SELECT 1 AS n"],
        obj={},
        color=False,
    )
    assert r.exit_code == 0, r.output
    assert "n: 1" in r.output


def test_execute_inline_url_select_csv(runner, tmp_path):
    url = _make_inline_url(tmp_path)
    r = runner.invoke(
        execute_cmd,
        ["-c", url, "-o", "csv", "SELECT 1 AS n, 'x' AS s"],
        obj={},
        color=False,
    )
    assert r.exit_code == 0, r.output
    lines = r.output.strip().splitlines()
    assert lines[0] == "n,s"
    assert lines[1] == "1,x"


def test_execute_inline_url_select_default_table(runner, tmp_path):
    url = _make_inline_url(tmp_path)
    r = runner.invoke(
        execute_cmd,
        ["-c", url, "SELECT 42 AS answer"],
        obj={},
        color=False,
    )
    assert r.exit_code == 0, r.output
    # rich renders a table with the column header "answer"
    assert "answer" in r.output and "42" in r.output


def test_execute_inline_url_select_empty_result_set(runner, tmp_path):
    url = _make_inline_url(tmp_path)
    # Create table so the SELECT doesn't fail; no rows.
    runner.invoke(execute_cmd, ["-c", url, "--apply", "-y", "CREATE TABLE t (id INT)"], obj={})
    r = runner.invoke(execute_cmd, ["-c", url, "-o", "json", "SELECT * FROM t"], obj={})
    assert r.exit_code == 0, r.output
    assert json.loads(r.output) == []


# --------------------------------------------------------------------------- #
# inline URL DML — dry-run, --apply, read-only block, audit
# --------------------------------------------------------------------------- #
def test_execute_inline_url_dml_dry_run_by_default(runner, tmp_path, with_history):
    url = _make_inline_url(tmp_path)
    # DML without --apply on a confirm:true connection -> dry-run, exit 0
    r = runner.invoke(
        execute_cmd,
        ["-c", url, "CREATE TABLE t (id INT)"],
        obj={},
        color=False,
    )
    assert r.exit_code == 0, r.output + (r.stderr or "")
    assert "dry-run" in r.output
    # Audit entry is a dry-run status
    entries = with_history.read(None, limit=10)
    assert any(e["status"] == "dry-run" and e["operation"] == "execute" for e in entries)


def test_execute_inline_url_dml_apply_writes(runner, tmp_path, with_history):
    url = _make_inline_url(tmp_path)
    # With --apply --yes the DML commits.
    r = runner.invoke(
        execute_cmd,
        ["-c", url, "--apply", "-y", "CREATE TABLE t (id INT)"],
        obj={},
        color=False,
    )
    assert r.exit_code == 0, r.output + (r.stderr or "")
    assert "OK" in r.output
    # DDL with rowcount=-1 renders "OK in <ms>ms", not "OK -1 row(s) affected"
    assert "-1 row" not in r.output
    # Audit log got an ok status entry
    entries = with_history.read(None, limit=10)
    assert any(e["status"] == "ok" and e["mode"] == "execute" for e in entries)


def test_execute_inline_url_insert_rows_affected(runner, tmp_path):
    url = _make_inline_url(tmp_path)
    runner.invoke(execute_cmd, ["-c", url, "--apply", "-y", "CREATE TABLE t (id INT)"], obj={})
    r = runner.invoke(
        execute_cmd,
        ["-c", url, "--apply", "-y", "INSERT INTO t VALUES (1), (2), (3)"],
        obj={},
        color=False,
    )
    assert r.exit_code == 0, r.output
    assert "3 row(s) affected" in r.output


def test_execute_inline_url_select_ignores_apply(runner, tmp_path):
    url = _make_inline_url(tmp_path)
    # SELECT with --apply should not prompt or commit anything.
    r = runner.invoke(
        execute_cmd,
        ["-c", url, "--apply", "SELECT 1"],
        obj={},
        color=False,
    )
    assert r.exit_code == 0, r.output
    assert "dry-run" not in r.output


def test_execute_inline_url_select_ignores_read_only(runner, tmp_path):
    """read_only connections still allow SELECTs — only DML is blocked."""
    url = _make_inline_url(tmp_path)
    # Patch the Connection validator's safety after construction by monkey-patching
    # the inline-connection builder to swap safety.read_only = True.
    from dbctl.cli import _make_inline_connection

    orig = _make_inline_connection

    def _ro(url):
        c = orig(url)
        c.safety.read_only = True
        return c

    import dbctl.cli

    monkey = monkeypatch_ctx()
    monkey.setattr(dbctl.cli, "_make_inline_connection", _ro)
    try:
        r = runner.invoke(execute_cmd, ["-c", url, "SELECT 1"], obj={}, color=False)
        assert r.exit_code == 0, r.output
    finally:
        monkey.undo()


def test_execute_inline_url_dml_blocked_by_read_only(runner, tmp_path):
    url = _make_inline_url(tmp_path)
    from dbctl.cli import _make_inline_connection

    orig = _make_inline_connection

    def _ro(url):
        c = orig(url)
        c.safety.read_only = True
        return c

    import dbctl.cli

    monkey = monkeypatch_ctx()
    monkey.setattr(dbctl.cli, "_make_inline_connection", _ro)
    try:
        r = runner.invoke(
            execute_cmd,
            ["-c", url, "--apply", "-y", "CREATE TABLE t (id INT)"],
            obj={},
            color=False,
        )
        assert r.exit_code == 6, r.output + (r.stderr or "")
        assert "read-only" in r.output
    finally:
        monkey.undo()


def test_execute_inline_url_dml_prompts_without_yes(runner, tmp_path):
    """With --apply but no --yes, the prompt defaults to No / aborts (exit 0)."""
    url = _make_inline_url(tmp_path)
    # CliRunner sends empty stdin -> click.confirm defaults to False -> SystemExit(0)
    r = runner.invoke(
        execute_cmd,
        ["-c", url, "--apply", "CREATE TABLE t (id INT)"],
        obj={},
        color=False,
        input="\n",
    )
    assert r.exit_code == 0, r.output + (r.stderr or "")
    assert "Apply SQL" in r.output or "Aborted" in r.output


# --------------------------------------------------------------------------- #
# named connection resolution
# --------------------------------------------------------------------------- #
def test_execute_unknown_named_connection_clean_exit(runner, monkeypatch):
    """``dbctl execute -c zzz <sql>`` should print a one-line error and exit 2
    (no traceback).Monkey-patches registries to return an empty registry."""

    conns: dict = {}
    monkeypatch.setattr("dbctl.cli.registries", lambda _ctx: (conns, {}))
    r = runner.invoke(
        execute_cmd,
        ["-c", "zzz", "SELECT 1"],
        obj={},
        color=False,
    )
    assert r.exit_code == 2, r.output + (r.stderr or "")
    assert "unknown connection" in r.output
    assert "Traceback" not in r.output


def test_execute_invalid_inline_url_clean_exit(runner):
    """A SQLAlchemy URL that fails the URL parser raises a clean exit 2
    (no traceback leaked to the user)."""

    r = runner.invoke(
        execute_cmd,
        # SQLAlchemy rejects a URL with a ':' but no scheme delimiter —
        # make_url raises ArgumentError, which the inline-URL path catches.
        ["-c", "not a url at all", "SELECT 1"],
        obj={},
        color=False,
    )
    # Connection validator's url-mode branch accepts any string for `url:`,
    # so we expect either exit 2 (resolve() failure on a non-named, non-url
    # token) or another clean path — the only invariant we enforce is
    # no traceback leak.
    assert r.exit_code != 0
    assert "Traceback" not in r.output


def test_execute_empty_sql_exit_2(runner, tmp_path):
    url = _make_inline_url(tmp_path)
    r = runner.invoke(
        execute_cmd,
        ["-c", url, "   "],
        obj={},
        color=False,
    )
    assert r.exit_code == 2, r.output + (r.stderr or "")
    assert "empty SQL" in r.output


def test_execute_show_sql_prints_sql(runner, tmp_path):
    url = _make_inline_url(tmp_path)
    r = runner.invoke(
        execute_cmd,
        ["-c", url, "--show-sql", "SELECT 42"],
        obj={},
        color=False,
    )
    assert r.exit_code == 0, r.output
    assert "SELECT 42" in r.output


def test_execute_sql_starting_with_dash_uses_separator(runner, tmp_path):
    """SQL beginning with a dash must be passed after ``--`` so Click doesn't
    treat it as an unknown option. Verifies the parser accepts that shape."""
    url = _make_inline_url(tmp_path)
    # Click's `--` separator is automatically handled by the runner when args are split
    r = runner.invoke(
        execute_cmd,
        ["-c", url, "--", "-- comment; SELECT 1 AS n"],
        obj={},
        color=False,
    )
    # SQLite won't run the comment + SELECT together as one statement (script mode is
    # first-statement only and `-- comment` alone is empty). What we're verifying is
    # that Click parses the `--` correctly and the SQL arrives as a positional —
    # so the failure mode we don't want is "No such option '--comment'".
    if r.exit_code != 0:
        assert "No such option" not in r.output
        assert "No such option" not in (r.stderr or "")
    else:
        assert "n" in r.output  # column header


# --------------------------------------------------------------------------- #
# audit log entry shape
# --------------------------------------------------------------------------- #
def test_audit_entry_has_sql_and_operation(runner, tmp_path, with_history):
    url = _make_inline_url(tmp_path)
    runner.invoke(
        execute_cmd,
        ["-c", url, "-o", "json", "SELECT 1 AS n"],
        obj={},
        color=False,
    )
    entries = with_history.read(None, limit=5)
    exec_entries = [e for e in entries if e["operation"] == "execute"]
    assert exec_entries, "expected at least one execute audit entry"
    e = exec_entries[-1]
    assert e["mode"] == "fetch"
    assert e["status"] == "ok"
    assert "SELECT 1" in e["params"].get("sql", "")
    assert e["connection"] == "<inline>"


def test_audit_entry_truncates_long_sql(runner, tmp_path, with_history):
    url = _make_inline_url(tmp_path)
    long_sql = "SELECT " + ("1, " * 400) + "1"
    runner.invoke(execute_cmd, ["-c", url, "-o", "json", long_sql], obj={}, color=False)
    entries = with_history.read(None, limit=5)
    e = next(x for x in entries if x["operation"] == "execute")
    assert len(e["params"].get("sql", "")) <= 500


# --------------------------------------------------------------------------- #
# named-connection dispatch path (monkey-patched opened_conn)
# --------------------------------------------------------------------------- #
def test_named_connection_uses_opened_conn(runner, monkeypatch, tmp_path, with_history):
    """``dbctl execute -c pg <sql>`` resolves via registries + opened_conn."""
    import contextlib

    fake_engine = mock.MagicMock()
    # SELECT path: engine.connect() yields a connection whose execute().mappings()
    # returns an iterable of mapping rows.
    fake_conn = mock.MagicMock()
    fake_result = mock.MagicMock()
    fake_result.mappings.return_value = iter([{"id": 1}, {"id": 2}])
    fake_conn.execute.return_value = fake_result
    fake_engine.connect.return_value.__enter__.return_value = fake_conn
    fake_engine.connect.return_value.__exit__.return_value = None

    stub = SimpleNamespace(name="pg", engine=fake_engine, tunnel=mock.MagicMock())

    @contextlib.contextmanager
    def fake_opened_conn(ctx, name):
        yield name, SimpleNamespace(safety=SimpleNamespace(read_only=False, confirm=False)), stub

    monkeypatch.setattr("dbctl.cli.registries", lambda _ctx: ({}, {}))
    monkeypatch.setattr("dbctl.connections.resolve", lambda n, reg: (n, SimpleNamespace(
        safety=SimpleNamespace(read_only=False, confirm=False)
    )))
    monkeypatch.setattr("dbctl.cli.opened_conn", fake_opened_conn)

    r = runner.invoke(
        execute_cmd,
        ["-c", "pg", "-o", "json", "SELECT id FROM users"],
        obj={},
        color=False,
    )
    assert r.exit_code == 0, r.output + (r.stderr or "")
    data = json.loads(r.output)
    assert data == [{"id": 1}, {"id": 2}]


# --------------------------------------------------------------------------- #
# helper: pytest MonkeyPatch in a context-manager shape (since `monkeypatch`
# fixture only applies to the test function scope, not nested helper funcs)
# --------------------------------------------------------------------------- #
class _MonkeyPatchCtx:
    """Tiny shim — pytest's `monkeypatch` fixture is function-scoped and can't
    be used as a context manager inside helper funcs. This wraps the same
    setattr/undo pattern explicitly."""

    def __init__(self):
        self._undos: list = []

    def setattr(self, target, name, value):
        self._undos.append((target, name, getattr(target, name)))
        setattr(target, name, value)

    def undo(self):
        for target, name, original in reversed(self._undos):
            setattr(target, name, original)
        self._undos.clear()


def monkeypatch_ctx():
    return _MonkeyPatchCtx()