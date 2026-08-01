"""Pydantic models for connections.yaml and operations.yaml.

Two complementary registries are loaded from ~/.dbctl/ (or --profile dir):

* connections.<name>  - how to reach a database (tunnel + driver + safety)
* operations.<name>   - what to do once connected (sql + params + mode)

Both files share a common ``Param`` shape for parameters; connection-level
healthcheck/info queries use only ``name``/``description``/``query``.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


# --------------------------------------------------------------------------- #
# enums
# --------------------------------------------------------------------------- #
class TunnelType(StrEnum):
    ssm = "ssm"
    ssh = "ssh"
    direct = "direct"


class OpScope(StrEnum):
    single = "single"
    multi = "multi"


class OpMode(StrEnum):
    execute = "execute"
    fetch = "fetch"
    fetch_one = "fetch_one"
    script = "script"
    upsert = "upsert"
    compare = "compare"
    diff = "diff"
    sync = "sync"


class ParamType(StrEnum):
    string = "string"
    integer = "integer"
    float = "float"
    bool = "bool"
    date = "date"
    path = "path"
    list = "list"
    secret = "secret"


class OutputFormat(StrEnum):
    table = "table"
    json = "json"
    csv = "csv"
    yaml = "yaml"


# --------------------------------------------------------------------------- #
# parameter
# --------------------------------------------------------------------------- #
class Param(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    type: ParamType = ParamType.string
    required: bool = False
    default: Any = None
    description: str = ""
    position: int | None = None  # 1-based; null = keyword-only
    choices: list[str] | None = None
    completer: str | None = None  # hints shell completion (e.g. "users")

    @model_validator(mode="after")
    def _check(self) -> Param:
        if self.required and self.default is not None:
            raise ValueError(f"param {self.name!r}: 'required' and 'default' are exclusive")
        return self


# --------------------------------------------------------------------------- #
# tunnels
# --------------------------------------------------------------------------- #
class SsmTunnel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    region: str
    profile: str | None = None
    bastion_instance_id: str | None = None  # either this OR bastion_tags
    bastion_tags: dict[str, str] | None = None  # alt: resolved via ec2:DescribeInstances
    remote_host: str
    remote_port: int = 5432
    local_port: int = 0  # 0 = auto-pick free port
    ssm_document: str = "AWS-StartPortForwardingSessionToRemoteHost"

    @model_validator(mode="after")
    def _check(self) -> SsmTunnel:
        if not self.bastion_instance_id and not self.bastion_tags:
            raise ValueError("ssm tunnel: provide 'bastion_instance_id' or 'bastion_tags'")
        if self.bastion_instance_id and self.bastion_tags:
            raise ValueError("ssm tunnel: 'bastion_instance_id' and 'bastion_tags' are exclusive")
        return self


class SshTunnel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str
    user: str
    identity: str  # path to SSH private key
    remote_host: str
    remote_port: int = 5432
    local_port: int = 0
    port: int = 22  # bastion SSH port


class DirectTunnel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str
    port: int = 5432


# --------------------------------------------------------------------------- #
# info / healthcheck
# --------------------------------------------------------------------------- #
class InfoQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    description: str = ""
    query: str


class Healthcheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = "SELECT 1"
    timeout_seconds: float = 5.0


class Safety(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: bool = False
    read_only: bool = False
    allowed_operations: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# connection
# --------------------------------------------------------------------------- #
class Connection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = ""
    aliases: list[str] = Field(default_factory=list)
    type: TunnelType
    driver: str  # SQLAlchemy URL scheme, e.g. postgresql+psycopg
    database: str
    username: str
    password_env: str | None = None
    prompt: bool = False  # prompt for password interactively each run

    ssm: SsmTunnel | None = None
    ssh: SshTunnel | None = None
    direct: DirectTunnel | None = None

    healthcheck: Healthcheck = Field(default_factory=Healthcheck)
    info: list[InfoQuery] = Field(default_factory=list)
    safety: Safety = Field(default_factory=Safety)

    @model_validator(mode="after")
    def _check(self) -> Connection:
        match self.type:
            case TunnelType.ssm:
                if self.ssm is None:
                    raise ValueError("'ssm' block required when type=ssm")
            case TunnelType.ssh:
                if self.ssh is None:
                    raise ValueError("'ssh' block required when type=ssh")
            case TunnelType.direct:
                if self.direct is None:
                    raise ValueError("'direct' block required when type=direct")
        if self.password_env and self.prompt:
            raise ValueError("'password_env' and 'prompt' are exclusive")
        if not (self.password_env or self.prompt):
            raise ValueError("set 'password_env' or 'prompt: true' for each connection")
        return self


class ConnectionsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connections: dict[str, Connection]


# --------------------------------------------------------------------------- #
# operation
# --------------------------------------------------------------------------- #
class DiffSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: list[str]
    show: list[str] | None = None


class Operation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = ""
    scope: OpScope = OpScope.single
    namespace: str | None = None
    mode: OpMode = OpMode.execute
    confirm: bool = True  # prompt before commit for execute/upsert
    output: OutputFormat = OutputFormat.table
    tags: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)  # for multi: ["src","trg"]
    parameters: list[Param] = Field(default_factory=list)
    sql: str | None = None
    queries: dict[str, str] | None = None  # per-role, for multi
    diff: DiffSpec | None = None

    @model_validator(mode="after")
    def _check(self) -> Operation:
        if self.scope is OpScope.multi:
            if not self.roles:
                raise ValueError("multi operations must declare roles")
            if self.queries is None:
                raise ValueError("multi operations must declare queries per role")
            if not set(self.queries).issubset(self.roles):
                raise ValueError(f"queries roles {set(self.queries)} not subset of roles {self.roles}")
        else:
            if self.sql is None and self.mode is not OpMode.upsert:
                raise ValueError("single operations must declare 'sql' (upsert builds SQL from file)")
        return self


class OperationsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operations: dict[str, Operation]


# --------------------------------------------------------------------------- #
# config dir resolution
# --------------------------------------------------------------------------- #
def default_config_dir() -> Path:
    return Path.home() / ".dbctl"


def resolve_config_dir(profile: str | None) -> Path:
    if profile:
        return Path.home() / ".dbctl" / "profiles" / profile
    return default_config_dir()


def connections_path(profile: str | None) -> Path:
    return resolve_config_dir(profile) / "connections.yaml"


def operations_path(profile: str | None) -> Path:
    return resolve_config_dir(profile) / "operations.yaml"


def history_path(profile: str | None) -> Path:
    return resolve_config_dir(profile) / "history.jsonl"
