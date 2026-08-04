"""Tests for the Azure Bastion tunnel.

Mocks ``subprocess.Popen`` and the local-port wait helper so we can verify
the ``az network bastion tunnel`` command is constructed correctly without
needing a real Azure Bastion. Confirms that:

* ``bastion_name`` / ``resource_group`` / ``target_resource_id`` are
  forwarded via their respective flags.
* ``subscription`` is forwarded via ``--subscription`` when set, omitted
  when blank.
* ``local_port: 0`` triggers the free-port discovery (find_free_port).
* Missing ``az`` binary raises a clear, actionable error.
* The config model requires its mandatory fields and rejects extras.
"""

from __future__ import annotations

import subprocess
from unittest import mock

import pytest

from dbctl.config import AzureBastionTunnel as AzureConn


# --------------------------------------------------------------------------- #
# config model
# --------------------------------------------------------------------------- #
def test_azure_config_defaults():
    t = AzureConn(
        resource_group="prod-rg",
        bastion_name="prod-bastion",
        target_resource_id="/subscriptions/x/resourceGroups/prod-rg/providers/Microsoft.Compute/virtualMachines/vm1",
    )
    assert t.subscription is None
    assert t.remote_port == 5432
    assert t.local_port == 0


def test_azure_config_rejects_extra_fields():
    with pytest.raises(ValueError, match="extra"):
        AzureConn(
            resource_group="prod-rg",
            bastion_name="prod-bastion",
            target_resource_id="/subscriptions/x/.../vm1",
            region="eu-west-1",  # type: ignore[call-arg]
        )


def test_azure_config_requires_target_resource_id():
    with pytest.raises(ValueError, match="target_resource_id"):
        AzureConn(resource_group="prod-rg", bastion_name="prod-bastion")  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# command construction
# --------------------------------------------------------------------------- #
def _make_tunnel(conn: AzureConn) -> object:
    from dbctl.tunnels.azure import AzureBastionTunnel

    return AzureBastionTunnel(conn)


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


def test_azure_tunnel_builds_correct_command_with_subscription():
    conn = AzureConn(
        resource_group="prod-rg",
        bastion_name="prod-bastion",
        target_resource_id="/subscriptions/x/resourceGroups/prod-rg/providers/Microsoft.Compute/virtualMachines/vm1",
        subscription="prod",
        remote_port=5432,
        local_port=54321,
    )
    captured: dict = {}

    def _popen(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        return _FakePopen(cmd)

    with (
        mock.patch("dbctl.tunnels.azure.subprocess.Popen", side_effect=_popen),
        mock.patch("dbctl.tunnels.azure.wait_local_open", return_value=True),
        mock.patch("dbctl.tunnels.azure.atexit.register"),
        mock.patch("dbctl.tunnels.azure.shutil.which", return_value=None),
    ):
        tun = _make_tunnel(conn)
        port = tun.__enter__()

    assert port == 54321
    cmd = captured["cmd"]
    assert cmd[0] == "az"
    assert cmd[1:4] == ["network", "bastion", "tunnel"]
    assert "--name" in cmd and "prod-bastion" in cmd
    assert "--resource-group" in cmd and "prod-rg" in cmd
    assert "--target-resource-id" in cmd
    assert "--resource-port" in cmd and "5432" in cmd
    assert "--port" in cmd and "54321" in cmd
    assert "--subscription" in cmd and "prod" in cmd


def test_azure_tunnel_omits_subscription_when_unset():
    conn = AzureConn(
        resource_group="prod-rg",
        bastion_name="prod-bastion",
        target_resource_id="/subscriptions/x/.../vm1",
        local_port=11111,
    )
    captured: dict = {}

    def _popen(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        return _FakePopen(cmd)

    with (
        mock.patch("dbctl.tunnels.azure.subprocess.Popen", side_effect=_popen),
        mock.patch("dbctl.tunnels.azure.wait_local_open", return_value=True),
        mock.patch("dbctl.tunnels.azure.atexit.register"),
        mock.patch("dbctl.tunnels.azure.shutil.which", return_value=None),
    ):
        _make_tunnel(conn).__enter__()

    assert "--subscription" not in captured["cmd"]


def test_azure_tunnel_local_port_0_uses_find_free_port():
    conn = AzureConn(
        resource_group="prod-rg",
        bastion_name="prod-bastion",
        target_resource_id="/subscriptions/x/.../vm1",
        local_port=0,
    )
    captured: dict = {}

    def _popen(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        return _FakePopen(cmd)

    with (
        mock.patch("dbctl.tunnels.azure.subprocess.Popen", side_effect=_popen),
        mock.patch("dbctl.tunnels.azure.wait_local_open", return_value=True),
        mock.patch("dbctl.tunnels.azure.atexit.register"),
        mock.patch("dbctl.tunnels.azure.shutil.which", return_value=None),
        mock.patch("dbctl.tunnels.azure.find_free_port", return_value=33333),
    ):
        port = _make_tunnel(conn).__enter__()

    assert port == 33333
    assert "33333" in captured["cmd"]


def test_azure_tunnel_resolves_az_cmd_launcher_path():
    # On Windows `az` installs as `az.cmd`; subprocess.Popen(["az", ...])
    # without shell=True raises FileNotFoundError even though `az` is
    # genuinely on PATH (CreateProcess won't resolve a bare name to a
    # .cmd/.bat launcher). We resolve the full path via shutil.which first.
    conn = AzureConn(
        resource_group="prod-rg",
        bastion_name="prod-bastion",
        target_resource_id="/subscriptions/x/.../vm1",
        local_port=11111,
    )
    captured: dict = {}

    def _popen(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        return _FakePopen(cmd)

    with (
        mock.patch("dbctl.tunnels.azure.subprocess.Popen", side_effect=_popen),
        mock.patch("dbctl.tunnels.azure.wait_local_open", return_value=True),
        mock.patch("dbctl.tunnels.azure.atexit.register"),
        mock.patch("dbctl.tunnels.azure.shutil.which", return_value=r"C:\Program Files\Azure\az.cmd"),
    ):
        _make_tunnel(conn).__enter__()

    assert captured["cmd"][0] == r"C:\Program Files\Azure\az.cmd"


def test_azure_tunnel_raises_clear_error_when_az_missing():
    conn = AzureConn(
        resource_group="prod-rg",
        bastion_name="prod-bastion",
        target_resource_id="/subscriptions/x/.../vm1",
        local_port=44444,
    )
    with (
        mock.patch("dbctl.tunnels.azure.subprocess.Popen", side_effect=FileNotFoundError),
        pytest.raises(RuntimeError, match="az"),
    ):
        _make_tunnel(conn).__enter__()


def test_azure_tunnel_raises_on_timeout_surfaces_stderr():
    conn = AzureConn(
        resource_group="prod-rg",
        bastion_name="prod-bastion",
        target_resource_id="/subscriptions/x/.../vm1",
        local_port=55555,
    )

    class FakeProc:
        def __init__(self):
            self.stderr = mock.Mock()
            self.stderr.read.return_value = b"bastion prod-bastion not found"
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
        mock.patch("dbctl.tunnels.azure.subprocess.Popen", return_value=FakeProc()),
        mock.patch("dbctl.tunnels.azure.wait_local_open", return_value=False),
        mock.patch("dbctl.tunnels.azure.atexit.register"),
        mock.patch("dbctl.tunnels.azure.shutil.which", return_value=None),
        pytest.raises(RuntimeError, match="bastion prod-bastion not found"),
    ):
        _make_tunnel(conn).__enter__()


# --------------------------------------------------------------------------- #
# integration with build_tunnel
# --------------------------------------------------------------------------- #
def test_build_tunnel_dispatches_azure():
    from dbctl.config import Connection, TunnelType
    from dbctl.tunnels.azure import AzureBastionTunnel
    from dbctl.tunnels.base import build_tunnel

    conn = Connection(
        type=TunnelType.azure,
        driver="postgresql+psycopg",
        database="app",
        username="u",
        password_env="DBCTL_X_PASSWORD",
        azure=AzureConn(
            resource_group="prod-rg",
            bastion_name="prod-bastion",
            target_resource_id="/subscriptions/x/.../vm1",
        ),
    )
    tun = build_tunnel(conn)
    assert isinstance(tun, AzureBastionTunnel)


def test_connection_rejects_azure_type_without_azure_block():
    from dbctl.config import Connection, TunnelType

    with pytest.raises(ValueError, match="'azure' block required"):
        Connection(
            type=TunnelType.azure,
            driver="postgresql+psycopg",
            database="app",
            username="u",
            password_env="DBCTL_X_PASSWORD",
        )


_ = subprocess
