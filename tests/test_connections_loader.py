"""Tests for ``dbctl.connections.load`` per-connection validation behaviour.

A mis-configured connection (e.g. a reference template with no password
source) must NOT take down the whole registry: the good connections are
returned and the bad ones are reported concisely via
``ConnectionsFileError``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dbctl.connections import ConnectionsFileError, load

VALID = """
connections:
  pg:
    type: direct
    driver: postgresql+psycopg
    database: app
    username: app_admin
    password: pwd
    direct: { host: 127.0.0.1, port: 5432 }
"""


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "connections.yaml"
    p.write_text(body)
    return p


def test_load_returns_empty_when_missing(tmp_path: Path) -> None:
    assert load(tmp_path / "nope.yaml") == {}


def test_load_valid_file(tmp_path: Path) -> None:
    conns = load(_write(tmp_path, VALID))
    assert list(conns) == ["pg"]
    assert conns["pg"].password == "pwd"


def test_bad_connection_does_not_invalidate_good_ones(tmp_path: Path) -> None:
    body = (
        VALID
        + """
  pg-ssm:
    description: "reference template with no password source"
    type: ssm
    driver: postgresql+psycopg
    database: app
    username: app_admin
    # no password / password_env / prompt - intentionally broken
    ssm:
      region: eu-west-1
      bastion_instance_id: i-0abc
      remote_host: db.example.internal
"""
    )
    with pytest.raises(ConnectionsFileError) as exc_info:
        load(_write(tmp_path, body))

    err = exc_info.value
    # Good connections are still recoverable.
    assert list(err.valid) == ["pg"]
    # The bad connection is named in the error report.
    assert "pg-ssm" in err.errors
    # The message is a single concise sentence (no pydantic traceback dump).
    msg = err.errors["pg-ssm"]
    assert "set 'password', 'password_env'" in msg
    assert "\n" not in msg
    # And the whole rendered exception stays short and human-readable.
    rendered = str(err)
    assert "Value error," not in rendered
    assert "input_value" not in rendered
    assert rendered.splitlines()[0].startswith("1 invalid connection skipped:")


def test_missing_top_level_mapping_raises(tmp_path: Path) -> None:
    p = _write(tmp_path, "not_a_mapping: 1\n")
    with pytest.raises(ConnectionsFileError) as exc_info:
        load(p)
    assert "missing top-level 'connections:' mapping" in str(exc_info.value)


def test_connections_not_a_mapping_raises(tmp_path: Path) -> None:
    p = _write(tmp_path, "connections: [1, 2, 3]\n")
    with pytest.raises(ConnectionsFileError) as exc_info:
        load(p)
    assert "must be a mapping" in str(exc_info.value)


def test_singular_plural_wording() -> None:
    err = ConnectionsFileError({"x": "boom"}, {})
    assert str(err).startswith("1 invalid connection skipped:")
    err2 = ConnectionsFileError({"x": "boom", "y": "boom"}, {})
    assert str(err2).startswith("2 invalid connections skipped:")
