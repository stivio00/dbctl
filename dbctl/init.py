"""Interactive `dbctl init` wizard.

Walks the user through adding one connection and writes/merges it into
~/.dbctl/connections.yaml. Tries to validate by opening the tunnel +
healthcheck before writing.
"""

from __future__ import annotations

import click
import yaml
from rich.console import Console

from dbctl.config import (
    Connection,
    DirectTunnel,
    Healthcheck,
    K8sTunnel,
    SshTunnel,
    SsmTunnel,
    TunnelType,
    connections_path,
)

console = Console()


def run_wizard(*, profile: str | None) -> None:
    console.print("[bold cyan]dbctl init[/bold cyan]  add a new connection")
    name = click.prompt("connection name", type=str).strip()
    if not name:
        console.print("[red]name required[/red]")
        raise SystemExit(2)

    description = click.prompt("description (optional)", default="", type=str)
    aliases = click.prompt("aliases (comma-separated, optional)", default="", type=str)
    aliases = [a.strip() for a in aliases.split(",") if a.strip()]

    type_ = click.prompt("tunnel type", type=click.Choice([t.value for t in TunnelType]), default="direct")

    driver = click.prompt(
        "driver (sqlalchemy url scheme)",
        type=str,
        default="postgresql+psycopg",
    )
    database = click.prompt("database name", type=str)
    username = click.prompt("username", type=str)

    cred = click.prompt(
        "password source",
        type=click.Choice(["env", "prompt"]),
        default="prompt",
    )
    password_env = f"DBCTL_{name.upper()}_PASSWORD" if cred == "env" else None
    prompt = cred == "prompt"

    ssm = ssh = k8s = direct = None
    if type_ == "ssm":
        ssm = _ask_ssm()
    elif type_ == "ssh":
        ssh = _ask_ssh()
    elif type_ == "k8s":
        k8s = _ask_k8s()
    else:
        host = click.prompt("host", default="localhost")
        port = click.prompt("port", type=int, default=_default_port(driver))
        direct = DirectTunnel(host=host, port=port)

    healthcheck = Healthcheck(query=click.prompt("healthcheck query", default="SELECT 1"))
    confirm = click.confirm("prompt for confirmation before DML (recommended for prod)?", default=True)
    read_only = click.confirm("make this connection read-only?", default=False)

    conn = Connection(
        description=description,
        aliases=aliases,
        type=TunnelType(type_),
        driver=driver,
        database=database,
        username=username,
        password_env=password_env,
        prompt=prompt,
        ssm=ssm,
        ssh=ssh,
        k8s=k8s,
        direct=direct,
        healthcheck=healthcheck,
        safety={"confirm": confirm, "read_only": read_only},
    )

    # try to validate (only for direct, where we can actually reach)
    if type_ == "direct" and click.confirm("test connection now?", default=True):
        try:
            from dbctl.db import build_engine
            from dbctl.db import healthcheck as hc
            from dbctl.tunnels.base import build_tunnel

            tun = build_tunnel(conn)
            tun.__enter__()
            engine = build_engine(conn, tun)
            ok, ms, msg = hc(engine, conn.healthcheck.query, 5.0)
            color = "green" if ok else "red"
            console.print(f"  [{color}]{'OK' if ok else 'FAIL'}[/{color}] {msg} ({ms:.0f}ms)")
            tun.__exit__(None, None, None)
        except Exception as e:  # noqa: BLE001
            console.print(f"[yellow]test failed: {e}[/yellow]")

    _merge_and_save(name, conn, profile)
    console.print(f"[green] wrote {connections_path(profile)}[/green]")
    console.print(f"  run: [bold]dbctl {name} health[/bold]")


def _ask_ssm() -> SsmTunnel:
    region = click.prompt("aws region", default="eu-west-1")
    profile = click.prompt("aws sso profile (optional)", default="")
    bastion = click.prompt("bastion instance id", type=str)
    remote_host = click.prompt("remote host (rds endpoint)", type=str)
    remote_port = click.prompt("remote port", type=int, default=5432)
    local_port = click.prompt("local port (0 = auto)", type=int, default=0)
    return SsmTunnel(
        region=region,
        profile=profile or None,
        bastion_instance_id=bastion,
        remote_host=remote_host,
        remote_port=remote_port,
        local_port=local_port,
    )


def _ask_ssh() -> SshTunnel:
    host = click.prompt("bastion host", type=str)
    user = click.prompt("user", default="ec2-user")
    identity = click.prompt("identity file path", default="~/.ssh/id_rsa")
    remote_host = click.prompt("remote host (db endpoint)", type=str)
    remote_port = click.prompt("remote port", type=int, default=5432)
    local_port = click.prompt("local port (0 = auto)", type=int, default=0)
    return SshTunnel(
        host=host,
        user=user,
        identity=identity,
        remote_host=remote_host,
        remote_port=remote_port,
        local_port=local_port,
    )


def _ask_k8s() -> K8sTunnel:
    context = click.prompt("k8s context (--context)", type=str)
    namespace = click.prompt("namespace (optional, blank=kubeconfig default)", default="")
    # Accept "svc/foo", "pod/bar" or bare "foo" (we accept whatever the user
    # typed; kubectl itself will reject invalid targets at port-forward time).
    target = click.prompt("target (svc/foo or pod/bar)", type=str)
    remote_port = click.prompt("remote port (on the Service/Pod)", type=int, default=5432)
    local_port = click.prompt("local port (0 = auto)", type=int, default=0)
    return K8sTunnel(
        context=context,
        namespace=namespace or None,
        target=target,
        remote_port=remote_port,
        local_port=local_port,
    )


def _default_port(driver: str) -> int:
    if driver.startswith("postgresql"):
        return 5432
    if driver.startswith(("mysql", "mariadb")):
        return 3306
    if driver.startswith("mssql"):
        return 1433
    return 5432


def _merge_and_save(name: str, conn: Connection, profile: str | None) -> None:
    path = connections_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict = {}
    if path.exists():
        existing = yaml.safe_load(path.read_text()) or {}
    existing.setdefault("connections", {})[name] = conn.model_dump(mode="json", exclude_none=True)
    path.write_text(yaml.safe_dump(existing, sort_keys=False, allow_unicode=True))
