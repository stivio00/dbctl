"""Tunnel abstraction.

Three concrete implementations:

* ``DirectTunnel``  - no-op, returns the connection host/port as-is.
* ``SsmTunnel``     - shelled-out ``aws ssm start-session`` port forwarding to a
                      remote host (e.g. RDS) through an EC2 bastion.
* ``SshTunnel``     - shelled-out ``ssh -N -L`` for classic bastions.

All three implement a uniform context manager returning the *local* bind port
(or the upstream port for direct). The tunnels are responsible for finding a
free local port when the configured ``local_port`` is 0, and for tearing down
the subprocess cleanly (with an atexit fallback).
"""

from __future__ import annotations

from dbctl.tunnels.base import Tunnel, build_tunnel, find_free_port, wait_local_open

__all__ = ["Tunnel", "build_tunnel", "find_free_port", "wait_local_open"]