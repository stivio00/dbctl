"""Unit tests for `dbctl.ui.session.SessionManager` - the one new piece of
state management the UI introduces (a tunnel+engine kept open across many
tab-runs, instead of `dbctl.runtime.opened_conn`'s per-CLI-invocation scope).

No Textual app involved here; a `direct` sqlite connection needs no real
network I/O (`DirectTunnel` is a passthrough), so these run everywhere.
"""

from __future__ import annotations

from dbctl.config import Connection
from dbctl.refs import RefResolutionError
from dbctl.ui.session import SessionManager


def test_connect_then_disconnect(sqlite_connection):
    manager = SessionManager({"sqlite-test": sqlite_connection})

    session = manager.connect("sqlite-test")
    assert session.connected
    assert session.error is None

    # reconnecting an already-open session is a no-op, not a new engine.
    engine = session.engine
    again = manager.connect("sqlite-test")
    assert again.engine is engine

    manager.disconnect("sqlite-test")
    assert not manager.get("sqlite-test").connected


def test_connect_reports_healthcheck_failure(sqlite_connection):
    bad = sqlite_connection.model_copy(deep=True)
    bad.healthcheck.query = "SELECT * FROM this_table_does_not_exist"
    manager = SessionManager({"sqlite-test": bad})

    session = manager.connect("sqlite-test")
    assert not session.connected
    assert session.error and "healthcheck failed" in session.error


def test_test_tunnel_does_not_open_a_persistent_session(sqlite_connection):
    manager = SessionManager({"sqlite-test": sqlite_connection})
    ok, msg = manager.test_tunnel("sqlite-test")
    assert ok
    assert "OK" in msg
    assert not manager.get("sqlite-test").connected


def test_reload_disconnects_and_replaces_connections(sqlite_connection):
    manager = SessionManager({"sqlite-test": sqlite_connection})
    manager.connect("sqlite-test")
    assert manager.get("sqlite-test").connected

    other: Connection = sqlite_connection.model_copy(deep=True)
    manager.reload({"other": other})

    assert manager.names() == ["other"]
    assert not manager.get("other").connected


def test_disconnect_all(sqlite_connection):
    manager = SessionManager({"a": sqlite_connection, "b": sqlite_connection.model_copy(deep=True)})
    manager.connect("a")
    manager.connect("b")
    manager.disconnect_all()
    assert not manager.get("a").connected
    assert not manager.get("b").connected


def test_get_or_none_for_a_removed_connection(sqlite_connection):
    manager = SessionManager({"sqlite-test": sqlite_connection})
    manager.reload({})
    assert manager.get_or_none("sqlite-test") is None
    assert manager.get_or_none("sqlite-test") is None  # idempotent, no KeyError


def _raise_ref_error(*_a, **_k):
    raise RefResolutionError("failed to resolve ssm parameter '/missing/credentials': ParameterNotFound")


def test_connect_reports_unresolvable_ssm_ref_as_config_error(sqlite_connection, monkeypatch):
    monkeypatch.setattr("dbctl.ui.session.build_tunnel", _raise_ref_error)
    manager = SessionManager({"sqlite-test": sqlite_connection})

    session = manager.connect("sqlite-test")
    assert not session.connected
    assert session.error and "connections.yaml needs a correction" in session.error
    assert "/missing/credentials" in session.error


def test_test_tunnel_reports_unresolvable_ssm_ref_as_config_error(sqlite_connection, monkeypatch):
    monkeypatch.setattr("dbctl.ui.session.build_tunnel", _raise_ref_error)
    manager = SessionManager({"sqlite-test": sqlite_connection})

    ok, msg = manager.test_tunnel("sqlite-test")
    assert not ok
    assert "connections.yaml needs a correction" in msg
