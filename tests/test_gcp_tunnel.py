"""Tests for the GCP IAP TCP tunnel.

Mocks ``subprocess.Popen`` and the local-port wait helper so we can verify
the ``gcloud compute start-iap-tunnel`` command is constructed correctly
without needing a real GCP project. Confirms that:

* ``instance`` / ``zone`` / ``remote_port`` are forwarded positionally /
  via their respective flags.
* ``project`` is forwarded via ``--project=`` when set, omitted when blank.
* ``local_port: 0`` triggers the free-port discovery (find_free_port).
* Missing ``gcloud`` binary raises a clear, actionable error.
* The config model requires its mandatory fields and rejects extras.
"""

from __future__ import annotations

import subprocess
from unittest import mock

import pytest

from dbctl.config import GcpIapTunnel as GcpConn


# --------------------------------------------------------------------------- #
# config model
# --------------------------------------------------------------------------- #
def test_gcp_config_defaults():
    t = GcpConn(zone="europe-west1-b", instance="prod-db-vm")
    assert t.project is None
    assert t.remote_port == 5432
    assert t.local_port == 0


def test_gcp_config_rejects_extra_fields():
    with pytest.raises(ValueError, match="extra"):
        GcpConn(zone="europe-west1-b", instance="prod-db-vm", region="eu-west-1")  # type: ignore[call-arg]


def test_gcp_config_requires_zone():
    with pytest.raises(ValueError, match="zone"):
        GcpConn(instance="prod-db-vm")  # type: ignore[call-arg]


def test_gcp_config_requires_instance():
    with pytest.raises(ValueError, match="instance"):
        GcpConn(zone="europe-west1-b")  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# command construction
# --------------------------------------------------------------------------- #
def _make_tunnel(conn: GcpConn) -> object:
    from dbctl.tunnels.gcp import GcpIapTunnel

    return GcpIapTunnel(conn)


class _FakePopen:
    def __init__(self, cmd, *args, **kwargs):
        self.cmd = cmd
        self.stderr = None
        self.returncode = 0

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


def test_gcp_tunnel_builds_correct_command_with_project():
    conn = GcpConn(
        project="my-project",
        zone="europe-west1-b",
        instance="prod-db-vm",
        remote_port=5432,
        local_port=54321,
    )
    captured: dict = {}

    def _popen(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        return _FakePopen(cmd)

    with (
        mock.patch("dbctl.tunnels.gcp.subprocess.Popen", side_effect=_popen),
        mock.patch("dbctl.tunnels.gcp.wait_local_open", return_value=True),
        mock.patch("dbctl.tunnels.gcp.atexit.register"),
        mock.patch("dbctl.tunnels.gcp.shutil.which", return_value=None),
    ):
        tun = _make_tunnel(conn)
        port = tun.__enter__()

    assert port == 54321
    cmd = captured["cmd"]
    assert cmd[0] == "gcloud"
    assert cmd[1:3] == ["compute", "start-iap-tunnel"]
    assert "prod-db-vm" in cmd
    assert "5432" in cmd
    assert "--local-host-port=localhost:54321" in cmd
    assert "--zone" in cmd and "europe-west1-b" in cmd
    assert "--project=my-project" in cmd


def test_gcp_tunnel_omits_project_when_unset():
    conn = GcpConn(zone="europe-west1-b", instance="prod-db-vm", local_port=11111)
    captured: dict = {}

    def _popen(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        return _FakePopen(cmd)

    with (
        mock.patch("dbctl.tunnels.gcp.subprocess.Popen", side_effect=_popen),
        mock.patch("dbctl.tunnels.gcp.wait_local_open", return_value=True),
        mock.patch("dbctl.tunnels.gcp.atexit.register"),
        mock.patch("dbctl.tunnels.gcp.shutil.which", return_value=None),
    ):
        _make_tunnel(conn).__enter__()

    assert not any(part.startswith("--project=") for part in captured["cmd"])


def test_gcp_tunnel_local_port_0_uses_find_free_port():
    conn = GcpConn(zone="europe-west1-b", instance="prod-db-vm", local_port=0)
    captured: dict = {}

    def _popen(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        return _FakePopen(cmd)

    with (
        mock.patch("dbctl.tunnels.gcp.subprocess.Popen", side_effect=_popen),
        mock.patch("dbctl.tunnels.gcp.wait_local_open", return_value=True),
        mock.patch("dbctl.tunnels.gcp.atexit.register"),
        mock.patch("dbctl.tunnels.gcp.shutil.which", return_value=None),
        mock.patch("dbctl.tunnels.gcp.find_free_port", return_value=33333),
    ):
        port = _make_tunnel(conn).__enter__()

    assert port == 33333
    assert "--local-host-port=localhost:33333" in captured["cmd"]


def test_gcp_tunnel_resolves_gcloud_cmd_launcher_path():
    # On Windows `gcloud` installs as `gcloud.cmd`; subprocess.Popen(["gcloud", ...])
    # without shell=True raises FileNotFoundError even though `gcloud` is
    # genuinely on PATH (CreateProcess won't resolve a bare name to a
    # .cmd/.bat launcher). We resolve the full path via shutil.which first.
    conn = GcpConn(zone="europe-west1-b", instance="prod-db-vm", local_port=11111)
    captured: dict = {}

    def _popen(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        return _FakePopen(cmd)

    with (
        mock.patch("dbctl.tunnels.gcp.subprocess.Popen", side_effect=_popen),
        mock.patch("dbctl.tunnels.gcp.wait_local_open", return_value=True),
        mock.patch("dbctl.tunnels.gcp.atexit.register"),
        mock.patch("dbctl.tunnels.gcp.shutil.which", return_value=r"C:\Google\gcloud.cmd"),
    ):
        _make_tunnel(conn).__enter__()

    assert captured["cmd"][0] == r"C:\Google\gcloud.cmd"


def test_gcp_tunnel_raises_clear_error_when_gcloud_missing():
    conn = GcpConn(zone="europe-west1-b", instance="prod-db-vm", local_port=44444)
    with (
        mock.patch("dbctl.tunnels.gcp.subprocess.Popen", side_effect=FileNotFoundError),
        pytest.raises(RuntimeError, match="gcloud"),
    ):
        _make_tunnel(conn).__enter__()


def test_gcp_tunnel_raises_on_timeout_surfaces_stderr():
    conn = GcpConn(zone="europe-west1-b", instance="missing-vm", local_port=55555)

    class FakeProc:
        def __init__(self):
            self.stderr = mock.Mock()
            self.stderr.read.return_value = b"instance missing-vm not found"
            self.returncode = 1

        def poll(self):
            return 1

        def wait(self, timeout=None):
            return 1

        def terminate(self):
            pass

        def kill(self):
            pass

    with (
        mock.patch("dbctl.tunnels.gcp.subprocess.Popen", return_value=FakeProc()),
        mock.patch("dbctl.tunnels.gcp.wait_local_open", return_value=False),
        mock.patch("dbctl.tunnels.gcp.atexit.register"),
        mock.patch("dbctl.tunnels.gcp.shutil.which", return_value=None),
        pytest.raises(RuntimeError, match="instance missing-vm not found"),
    ):
        _make_tunnel(conn).__enter__()


# --------------------------------------------------------------------------- #
# integration with build_tunnel
# --------------------------------------------------------------------------- #
def test_build_tunnel_dispatches_gcp():
    from dbctl.config import Connection, TunnelType
    from dbctl.tunnels.base import build_tunnel
    from dbctl.tunnels.gcp import GcpIapTunnel

    conn = Connection(
        type=TunnelType.gcp,
        driver="postgresql+psycopg",
        database="app",
        username="u",
        password_env="DBCTL_X_PASSWORD",
        gcp=GcpConn(zone="europe-west1-b", instance="prod-db-vm"),
    )
    tun = build_tunnel(conn)
    assert isinstance(tun, GcpIapTunnel)


def test_connection_rejects_gcp_type_without_gcp_block():
    from dbctl.config import Connection, TunnelType

    with pytest.raises(ValueError, match="'gcp' block required"):
        Connection(
            type=TunnelType.gcp,
            driver="postgresql+psycopg",
            database="app",
            username="u",
            password_env="DBCTL_X_PASSWORD",
        )


_ = subprocess
