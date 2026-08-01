"""Tests for the SSM tunnel bastion-tag resolution.

Mocks ``subprocess.run`` so we can verify the ``aws ec2
describe-instances`` command is constructed correctly without needing
real AWS credentials. Confirms that:

* ``bastion_instance_id`` is used directly when set.
* ``bastion_tags`` shells out to ``aws ec2 describe-instances`` with JSON
  filters that always include ``instance-state-name=running``.
* Multiple matching instances produce a warning but pick the first.
* Empty results raise a clear error.
* The config model accepts tags-only, id-only, but not both / neither.
"""

from __future__ import annotations

import json
import subprocess
from unittest import mock

import pytest

from dbctl.config import SsmTunnel
from dbctl.tunnels.ssm import _resolve_bastion_id


# --------------------------------------------------------------------------- #
# config model
# --------------------------------------------------------------------------- #
def test_ssm_config_accepts_tags_only():
    t = SsmTunnel(region="eu-west-1", bastion_tags={"Name": "bastion"}, remote_host="db.internal")
    assert t.bastion_tags == {"Name": "bastion"}
    assert t.bastion_instance_id is None


def test_ssm_config_accepts_id_only():
    t = SsmTunnel(region="eu-west-1", bastion_instance_id="i-0abc", remote_host="db.internal")
    assert t.bastion_instance_id == "i-0abc"
    assert t.bastion_tags is None


def test_ssm_config_rejects_both():
    with pytest.raises(ValueError, match="exclusive"):
        SsmTunnel(
            region="eu-west-1",
            bastion_instance_id="i-0abc",
            bastion_tags={"Name": "x"},
            remote_host="db.internal",
        )


def test_ssm_config_rejects_neither():
    with pytest.raises(ValueError, match="provide"):
        SsmTunnel(region="eu-west-1", remote_host="db.internal")


# --------------------------------------------------------------------------- #
# _resolve_bastion_id — direct id passthrough
# --------------------------------------------------------------------------- #
def test_resolve_with_explicit_id_does_not_shell_out():
    conn = SsmTunnel(region="eu-west-1", bastion_instance_id="i-0deadbeef", remote_host="db.internal")
    with mock.patch("dbctl.tunnels.ssm.subprocess.run") as m:
        out = _resolve_bastion_id(conn)
    assert out == "i-0deadbeef"
    m.assert_not_called()


# --------------------------------------------------------------------------- #
# _resolve_bastion_id — tags resolution command construction
# --------------------------------------------------------------------------- #
def _make_completed(stdout: str, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_resolve_with_tags_builds_correct_aws_command():
    conn = SsmTunnel(
        region="eu-west-1",
        bastion_tags={"Name": "bastion-prod", "Env": "prod"},
        remote_host="db.internal",
        profile="prod",
    )
    with mock.patch("dbctl.tunnels.ssm.subprocess.run", return_value=_make_completed("i-0abcd1234\n")) as m:
        out = _resolve_bastion_id(conn)
    assert out == "i-0abcd1234"
    m.assert_called_once()
    args = m.call_args.args[0]
    kwargs = m.call_args.kwargs

    # Command structure
    assert args[0] == "aws"
    assert "--profile" in args and "prod" in args
    assert "ec2" in args and "describe-instances" in args
    assert "--region" in args and "eu-west-1" in args
    assert "--output" in args and "text" in args
    # The --filters argument must be JSON (not shorthand) so multi-tag
    # filters can't be mis-tokenised.
    filters_idx = args.index("--filters")
    filters_json = args[filters_idx + 1]
    filters = json.loads(filters_json)
    assert {"Name": "tag:Name", "Values": ["bastion-prod"]} in filters
    assert {"Name": "tag:Env", "Values": ["prod"]} in filters
    # Must always filter for running state so a terminated bastion is skipped
    assert {"Name": "instance-state-name", "Values": ["running"]} in filters

    # Must use text output and a query that returns all matching ids (so we
    # can warn on ambiguity), not just the first.
    query_idx = args.index("--query")
    assert "Reservations" in args[query_idx + 1]

    # Subprocess must be called with a timeout (avoid hanging on AWS errors)
    assert "timeout" in kwargs and kwargs["timeout"] > 0


def test_resolve_with_tags_no_profile_omits_profile_flag():
    conn = SsmTunnel(region="eu-west-1", bastion_tags={"Name": "bastion"}, remote_host="db.internal")
    with mock.patch("dbctl.tunnels.ssm.subprocess.run", return_value=_make_completed("i-0abc\n")) as m:
        _resolve_bastion_id(conn)
    args = m.call_args.args[0]
    assert "--profile" not in args


def test_resolve_with_tags_picks_first_on_ambiguous_match(capsys):
    conn = SsmTunnel(region="eu-west-1", bastion_tags={"Env": "prod"}, remote_host="db.internal")
    with mock.patch(
        "dbctl.tunnels.ssm.subprocess.run", return_value=_make_completed("i-0aaa\ni-0bbb\ni-0ccc\n")
    ):
        out = _resolve_bastion_id(conn)
    assert out == "i-0aaa"
    # Warning must be printed to stderr (rich Console(stderr=True))
    captured = capsys.readouterr()
    assert "warning" in captured.err.lower()
    assert "3" in captured.err  # "3 instances match"
    assert "i-0aaa" in captured.err


def test_resolve_with_tags_raises_on_no_match():
    conn = SsmTunnel(region="eu-west-1", bastion_tags={"Name": "nope"}, remote_host="db.internal")
    with (
        mock.patch("dbctl.tunnels.ssm.subprocess.run", return_value=_make_completed("")),
        pytest.raises(RuntimeError, match="no running instance"),
    ):
        _resolve_bastion_id(conn)


def test_resolve_with_tags_raises_on_aws_error():
    conn = SsmTunnel(region="eu-west-1", bastion_tags={"Name": "x"}, remote_host="db.internal")
    err = subprocess.CalledProcessError(
        returncode=255,
        cmd=["aws"],
        stderr="ExpiredTokenException: ...",
    )
    with (
        mock.patch("dbctl.tunnels.ssm.subprocess.run", side_effect=err),
        pytest.raises(RuntimeError, match="ExpiredToken"),
    ):
        _resolve_bastion_id(conn)


def test_resolve_with_tags_raises_on_timeout():
    conn = SsmTunnel(region="eu-west-1", bastion_tags={"Name": "x"}, remote_host="db.internal")
    with (
        mock.patch(
            "dbctl.tunnels.ssm.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd=["aws"], timeout=15)
        ),
        pytest.raises(RuntimeError, match="timed out"),
    ):
        _resolve_bastion_id(conn)
