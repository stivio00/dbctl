"""Tests for `dbctl.refs` — `{{ssm:...}}` placeholder resolution against AWS
SSM Parameter Store.

`aws` CLI calls are mocked throughout (via `_fetch_parameter` or
`subprocess.run`) — no AWS credentials are needed to run this file. The
`resolve_connection` integration tests confirm the whole pipeline (raw YAML
-> validated `Connection` with placeholders intact -> resolved `Connection`
with real values) end to end, using a fake SSM backend.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from dbctl.refs import (
    RefResolutionError,
    _extract_property,
    _fetch_parameter,
    _parse_ref,
    resolve_connection,
    resolve_ssm_ref,
    resolve_string,
    resolve_value,
)


# --------------------------------------------------------------------------- #
# _parse_ref
# --------------------------------------------------------------------------- #
def test_parse_ref_name_only():
    assert _parse_ref("/prod/db/password") == ("/prod/db/password", None, None)


def test_parse_ref_with_property():
    assert _parse_ref("/prod/db/creds;property:password") == ("/prod/db/creds", "password", None)


def test_parse_ref_with_property_and_profile():
    assert _parse_ref("/prod/db/creds;property:password;profile:prod-admin") == (
        "/prod/db/creds",
        "password",
        "prod-admin",
    )


def test_parse_ref_profile_only():
    assert _parse_ref("/dev/db/password;profile:developer") == ("/dev/db/password", None, "developer")


def test_parse_ref_ignores_blank_segments():
    # Trailing `;` (or accidental double `;;`) shouldn't blow up parsing.
    assert _parse_ref("/dev/db/password;profile:developer;") == ("/dev/db/password", None, "developer")


def test_parse_ref_empty_name_rejected():
    with pytest.raises(RefResolutionError, match="missing a parameter name"):
        _parse_ref(";property:password")


def test_parse_ref_malformed_option_rejected():
    with pytest.raises(RefResolutionError, match="malformed option"):
        _parse_ref("/prod/db/password;profileprod-admin")


def test_parse_ref_unknown_option_rejected():
    with pytest.raises(RefResolutionError, match="unknown option"):
        _parse_ref("/prod/db/password;region:eu-west-1")


# --------------------------------------------------------------------------- #
# _extract_property
# --------------------------------------------------------------------------- #
def test_extract_property_happy_path():
    raw = json.dumps({"username": "app_user", "password": "s3cret"})
    assert _extract_property(raw, "password", "/prod/db/creds") == "s3cret"


def test_extract_property_not_json_raises_without_leaking_value():
    with pytest.raises(RefResolutionError) as exc_info:
        _extract_property("not-json-at-all", "password", "/prod/db/creds")
    msg = str(exc_info.value)
    assert "/prod/db/creds" in msg
    assert "not-json-at-all" not in msg


def test_extract_property_missing_key_raises_without_leaking_value():
    raw = json.dumps({"username": "app_user", "password": "s3cret"})
    with pytest.raises(RefResolutionError) as exc_info:
        _extract_property(raw, "host", "/prod/db/creds")
    msg = str(exc_info.value)
    assert "host" in msg
    assert "s3cret" not in msg


# --------------------------------------------------------------------------- #
# _fetch_parameter (aws CLI mocked)
# --------------------------------------------------------------------------- #
def _fake_run(value: str, *, secure: bool = True):
    def _run(cmd, **kwargs):
        assert cmd[0] == "aws"
        assert "ssm" in cmd and "get-parameter" in cmd
        assert "--with-decryption" in cmd
        stdout = json.dumps({"Parameter": {"Value": value, "Type": "SecureString" if secure else "String"}})
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    return _run


def test_fetch_parameter_happy_path():
    with patch("dbctl.refs.subprocess.run", side_effect=_fake_run("s3cret")):
        assert _fetch_parameter("/prod/db/password", None) == "s3cret"


def test_fetch_parameter_uses_profile_flag():
    captured_cmd = {}

    def _run(cmd, **kwargs):
        captured_cmd["cmd"] = cmd
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"Parameter": {"Value": "s3cret"}}), stderr=""
        )

    with patch("dbctl.refs.subprocess.run", side_effect=_run):
        _fetch_parameter("/prod/db/password", "prod-admin")
    assert "--profile" in captured_cmd["cmd"]
    assert "prod-admin" in captured_cmd["cmd"]


def test_fetch_parameter_aws_cli_missing():
    with (
        patch("dbctl.refs.subprocess.run", side_effect=FileNotFoundError),
        pytest.raises(RefResolutionError, match="not found on PATH"),
    ):
        _fetch_parameter("/prod/db/password", None)


def test_fetch_parameter_denied_reports_param_name_not_value():
    err = subprocess.CalledProcessError(
        254, ["aws"], stderr="An error occurred (AccessDeniedException): secret-looking-stderr-text"
    )

    with patch("dbctl.refs.subprocess.run", side_effect=err), pytest.raises(RefResolutionError) as exc_info:
        _fetch_parameter("/prod/db/password", None)
    msg = str(exc_info.value)
    assert "/prod/db/password" in msg
    assert "AccessDeniedException" in msg


def test_fetch_parameter_timeout():
    with (
        patch(
            "dbctl.refs.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["aws"], timeout=15),
        ),
        pytest.raises(RefResolutionError, match="timed out"),
    ):
        _fetch_parameter("/prod/db/password", None)


def test_fetch_parameter_bad_json_response():
    def _run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, stdout="not json", stderr="")

    with (
        patch("dbctl.refs.subprocess.run", side_effect=_run),
        pytest.raises(RefResolutionError, match="unexpected response"),
    ):
        _fetch_parameter("/prod/db/password", None)


# --------------------------------------------------------------------------- #
# resolve_ssm_ref / resolve_string / resolve_value (caching + composition)
# --------------------------------------------------------------------------- #
def test_resolve_ssm_ref_caches_same_spec():
    calls = []

    def _run(cmd, **kwargs):
        calls.append(cmd)
        stdout = json.dumps({"Parameter": {"Value": "s3cret"}})
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    cache: dict[str, str] = {}
    with patch("dbctl.refs.subprocess.run", side_effect=_run):
        assert resolve_ssm_ref("/prod/db/password", cache=cache) == "s3cret"
        assert resolve_ssm_ref("/prod/db/password", cache=cache) == "s3cret"
    assert len(calls) == 1  # second call served from cache, no second `aws` invocation


def test_resolve_string_no_placeholder_is_noop_and_never_calls_aws():
    with patch("dbctl.refs.subprocess.run") as mock_run:
        assert resolve_string("plain-value", cache={}) == "plain-value"
    mock_run.assert_not_called()


def test_resolve_string_single_placeholder():
    with patch("dbctl.refs.subprocess.run", side_effect=_fake_run("s3cret")):
        assert resolve_string("{{ssm:/prod/db/password}}", cache={}) == "s3cret"


def test_resolve_string_with_property_and_profile():
    raw = json.dumps({"host": "prod.rds.internal", "password": "s3cret"})
    with patch("dbctl.refs.subprocess.run", side_effect=_fake_run(raw)):
        result = resolve_string("{{ssm:/prod/db/creds;property:host;profile:prod-admin}}", cache={})
    assert result == "prod.rds.internal"


def test_resolve_string_embedded_in_larger_string():
    with patch("dbctl.refs.subprocess.run", side_effect=_fake_run("prod-host")):
        result = resolve_string("jdbc://{{ssm:/prod/db/host}}:5432/app", cache={})
    assert result == "jdbc://prod-host:5432/app"


def test_resolve_value_recurses_dict_and_list():
    with patch("dbctl.refs.subprocess.run", side_effect=_fake_run("resolved")):
        result = resolve_value(
            {"a": "{{ssm:/x}}", "b": ["plain", "{{ssm:/x}}"], "c": {"d": "{{ssm:/x}}"}, "e": 5, "f": None},
            cache={},
        )
    assert result == {
        "a": "resolved",
        "b": ["plain", "resolved"],
        "c": {"d": "resolved"},
        "e": 5,
        "f": None,
    }


# --------------------------------------------------------------------------- #
# resolve_connection — full Connection round-trip
# --------------------------------------------------------------------------- #
def _connection(**overrides):
    from dbctl.config import Connection

    base = {
        "type": "direct",
        "driver": "postgresql+psycopg",
        "database": "app",
        "username": "app_admin",
        "password": "plain-password",
        "direct": {"host": "127.0.0.1", "port": 5432},
    }
    base.update(overrides)
    return Connection.model_validate(base)


def test_resolve_connection_resolves_password_placeholder():
    conn = _connection(password="{{ssm:/prod/db/password}}")
    with patch("dbctl.refs.subprocess.run", side_effect=_fake_run("s3cret")):
        resolved = resolve_connection(conn)
    assert resolved.password == "s3cret"
    # Original connection object (and its placeholder) is untouched.
    assert conn.password == "{{ssm:/prod/db/password}}"


def test_resolve_connection_leaves_plain_values_untouched_and_skips_aws():
    conn = _connection()
    with patch("dbctl.refs.subprocess.run") as mock_run:
        resolved = resolve_connection(conn)
    mock_run.assert_not_called()
    assert resolved.password == "plain-password"
    assert resolved.username == "app_admin"


def test_resolve_connection_resolves_nested_tunnel_field():
    conn = _connection(direct={"host": "{{ssm:/prod/db/host}}", "port": 5432})
    with patch("dbctl.refs.subprocess.run", side_effect=_fake_run("prod.rds.internal")):
        resolved = resolve_connection(conn)
    assert resolved.direct.host == "prod.rds.internal"


def test_resolve_connection_property_extraction_from_shared_parameter():
    raw = json.dumps({"username": "app_admin", "password": "s3cret", "host": "prod.rds.internal"})
    conn = _connection(
        password="{{ssm:/prod/db/creds;property:password}}",
        direct={"host": "{{ssm:/prod/db/creds;property:host}}", "port": 5432},
    )
    with patch("dbctl.refs.subprocess.run", side_effect=_fake_run(raw)):
        resolved = resolve_connection(conn)
    assert resolved.password == "s3cret"
    assert resolved.direct.host == "prod.rds.internal"


def test_resolve_connection_missing_parameter_error_names_param_not_value():
    conn = _connection(password="{{ssm:/prod/db/password}}")
    err = subprocess.CalledProcessError(254, ["aws"], stderr="ParameterNotFound: /prod/db/password")
    with patch("dbctl.refs.subprocess.run", side_effect=err), pytest.raises(RefResolutionError) as exc_info:
        resolve_connection(conn)
    assert "/prod/db/password" in str(exc_info.value)
