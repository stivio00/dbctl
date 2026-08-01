"""Tests for the Kubernetes port-forward tunnel.

Mocks ``subprocess.Popen`` and the local-port wait helper so we can verify
the ``kubectl port-forward`` command is constructed correctly without
needing a real cluster. Confirms that:

* ``context`` is forwarded via ``--context``.
* ``namespace`` is forwarded via ``--namespace`` when set, omitted when blank.
* ``target`` is passed verbatim (svc/..., pod/...).
* ``local_port:0`` triggers the free-port discovery (find_free_port).
* Missing ``kubectl`` binary raises a clear, actionable error.
* The config model accepts svc/ and pod/ targets and rejects extras.
"""

from __future__ import annotations

import subprocess
from unittest import mock

import pytest

from dbctl.config import K8sTunnel as K8sConn


# --------------------------------------------------------------------------- #
# config model
# --------------------------------------------------------------------------- #
def test_k8s_config_accepts_svc_target():
    t = K8sConn(context="prod", target="svc/postgres-primary")
    assert t.context == "prod"
    assert t.target == "svc/postgres-primary"
    assert t.namespace is None
    assert t.remote_port == 5432
    assert t.local_port == 0


def test_k8s_config_accepts_pod_target_with_namespace():
    t = K8sConn(context="prod", namespace="data", target="pod/postgres-0", remote_port=5433)
    assert t.namespace == "data"
    assert t.remote_port == 5433


def test_k8s_config_rejects_extra_fields():
    with pytest.raises(ValueError, match="extra"):
        K8sConn(context="prod", target="svc/x", region="eu-west-1")  # type: ignore[call-arg]


def test_k8s_config_requires_context():
    with pytest.raises(ValueError, match="context"):
        K8sConn(target="svc/x")  # type: ignore[call-arg]


def test_k8s_config_requires_target():
    with pytest.raises(ValueError, match="target"):
        K8sConn(context="prod")  # type: ignore[call-arg]


# --------------------------------------------------------------------------- #
# command construction
# --------------------------------------------------------------------------- #
def _make_tunnel(conn: K8sConn) -> object:
    from dbctl.tunnels.k8s import K8sTunnel

    return K8sTunnel(conn)


def test_k8s_tunnel_builds_correct_command_with_namespace():
    conn = K8sConn(
        context="prod-euks",
        namespace="data",
        target="svc/postgres-primary",
        remote_port=5432,
        local_port=54321,  # fixed so the assertion is deterministic
    )
    captured: dict = {}

    class FakePopen:
        def __init__(self, cmd, *args, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
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

    with (
        mock.patch("dbctl.tunnels.k8s.subprocess.Popen", FakePopen),
        mock.patch("dbctl.tunnels.k8s.wait_local_open", return_value=True),
        mock.patch("dbctl.tunnels.k8s.atexit.register"),
    ):
        tun = _make_tunnel(conn)
        port = tun.__enter__()

    assert port == 54321
    cmd = captured["cmd"]
    assert cmd[0] == "kubectl"
    assert "port-forward" in cmd
    assert "--context" in cmd and "prod-euks" in cmd
    assert "--namespace" in cmd and "data" in cmd
    assert cmd[-2] == "svc/postgres-primary"
    assert cmd[-1] == "54321:5432"


def test_k8s_tunnel_omits_namespace_when_blank():
    conn = K8sConn(context="prod", target="svc/x", local_port=11111)
    captured: dict = {}

    class FakePopen:
        def __init__(self, cmd, *args, **kwargs):
            captured["cmd"] = cmd
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

    with (
        mock.patch("dbctl.tunnels.k8s.subprocess.Popen", FakePopen),
        mock.patch("dbctl.tunnels.k8s.wait_local_open", return_value=True),
        mock.patch("dbctl.tunnels.k8s.atexit.register"),
    ):
        _make_tunnel(conn).__enter__()

    cmd = captured["cmd"]
    assert "--namespace" not in cmd
    assert "--context" in cmd and "prod" in cmd


def test_k8s_tunnel_accepts_pod_target_verbatim():
    conn = K8sConn(context="prod", target="pod/postgres-0", local_port=22222)
    captured: dict = {}

    class FakePopen:
        def __init__(self, cmd, *args, **kwargs):
            captured["cmd"] = cmd
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

    with (
        mock.patch("dbctl.tunnels.k8s.subprocess.Popen", FakePopen),
        mock.patch("dbctl.tunnels.k8s.wait_local_open", return_value=True),
        mock.patch("dbctl.tunnels.k8s.atexit.register"),
    ):
        _make_tunnel(conn).__enter__()

    assert captured["cmd"][-2] == "pod/postgres-0"
    assert captured["cmd"][-1] == "22222:5432"


def test_k8s_tunnel_local_port_0_uses_find_free_port():
    # When local_port=0 the tunnel must discover a free port before invoking
    # kubectl — kubectl port-forward doesn't auto-pick (it errors on :0).
    conn = K8sConn(context="prod", target="svc/x", local_port=0)
    captured: dict = {}

    class FakePopen:
        def __init__(self, cmd, *args, **kwargs):
            captured["cmd"] = cmd
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

    with (
        mock.patch("dbctl.tunnels.k8s.subprocess.Popen", FakePopen),
        mock.patch("dbctl.tunnels.k8s.wait_local_open", return_value=True),
        mock.patch("dbctl.tunnels.k8s.atexit.register"),
        mock.patch("dbctl.tunnels.k8s.find_free_port", return_value=33333),
    ):
        port = _make_tunnel(conn).__enter__()

    assert port == 33333
    assert captured["cmd"][-1] == "33333:5432"


def test_k8s_tunnel_raises_clear_error_when_kubectl_missing():
    conn = K8sConn(context="prod", target="svc/x", local_port=44444)
    with (
        mock.patch("dbctl.tunnels.k8s.subprocess.Popen", side_effect=FileNotFoundError),
        pytest.raises(RuntimeError, match="kubectl"),
    ):
        _make_tunnel(conn).__enter__()


def test_k8s_tunnel_raises_on_time_out_surfaces_stderr():
    conn = K8sConn(context="prod", target="svc/missing", local_port=55555)

    class FakeProc:
        def __init__(self):
            self.stderr = mock.Mock()
            self.stderr.read.return_value = b"service svc/missing not found"
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
        mock.patch("dbctl.tunnels.k8s.subprocess.Popen", return_value=FakeProc()),
        mock.patch("dbctl.tunnels.k8s.wait_local_open", return_value=False),
        mock.patch("dbctl.tunnels.k8s.atexit.register"),
        pytest.raises(RuntimeError, match="svc/missing not found"),
    ):
        _make_tunnel(conn).__enter__()


# --------------------------------------------------------------------------- #
# integration with build_tunnel
# --------------------------------------------------------------------------- #
def test_build_tunnel_dispatches_k8s():
    from dbctl.config import Connection, TunnelType
    from dbctl.tunnels.base import build_tunnel
    from dbctl.tunnels.k8s import K8sTunnel

    conn = Connection(
        type=TunnelType.k8s,
        driver="postgresql+psycopg",
        database="app",
        username="u",
        password_env="DBCTL_X_PASSWORD",
        k8s=K8sConn(context="prod", target="svc/x"),
    )
    tun = build_tunnel(conn)
    assert isinstance(tun, K8sTunnel)


def test_connection_rejects_k8s_type_without_k8s_block():
    from dbctl.config import Connection, TunnelType

    with pytest.raises(ValueError, match="'k8s' block required"):
        Connection(
            type=TunnelType.k8s,
            driver="postgresql+psycopg",
            database="app",
            username="u",
            password_env="DBCTL_X_PASSWORD",
        )


# Silence the "unused import" linter paranoia when subprocess module is only
# used through mock — the symbol is needed in scope for type annotation above.
_ = subprocess
