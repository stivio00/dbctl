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
    k8s = "k8s"
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
    # multi-scope modes:
    compare = "compare"
    diff = "diff"
    copy = "copy"
    sync = "sync"
    validate = "validate"
    replay = "replay"


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


class K8sTunnel(BaseModel):
    """Kubernetes port-forward via ``kubectl port-forward`` subprocess.

    Reaches databases exposed as Services or Pods inside a cluster - useful
    for StatefulSets / operators (CloudNativePG, Postgres Operator, etc.).
    The ``kubectl`` CLI on PATH is invoked; kubeconfig / context resolution
    is left to kubectl itself, so existing ``~/.kube/config`` works.
    """

    model_config = ConfigDict(extra="forbid")
    context: str  # k8s context to use (--context)
    namespace: str | None = None  # optional; defaults to kubeconfig default
    target: str  # e.g. "svc/postgres-primary" or "pod/postgres-0"
    remote_port: int = 5432  # port on the Service/Pod to forward
    local_port: int = 0  # 0 = auto-pick free port


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
    password: str | None = None  # plaintext password (for local dev only)
    password_env: str | None = None  # name of env var holding the DB password
    prompt: bool = False  # prompt for password interactively each run

    ssm: SsmTunnel | None = None
    ssh: SshTunnel | None = None
    k8s: K8sTunnel | None = None
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
            case TunnelType.k8s:
                if self.k8s is None:
                    raise ValueError("'k8s' block required when type=k8s")
            case TunnelType.direct:
                if self.direct is None:
                    raise ValueError("'direct' block required when type=direct")
        sources = [bool(self.password), bool(self.password_env), self.prompt]
        if sum(sources) > 1:
            raise ValueError("'password', 'password_env' and 'prompt' are mutually exclusive")
        if not any(sources):
            raise ValueError("set 'password', 'password_env' or 'prompt: true' for each connection")
        return self


class ConnectionsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connections: dict[str, Connection]


# --------------------------------------------------------------------------- #
# operation
# --------------------------------------------------------------------------- #
class DiffStrategy(StrEnum):
    custom = "custom"  # user-supplied SQL per role (the v1 behaviour)
    table_counts = "table_counts"  # auto-gen `SELECT COUNT(*) FROM <t>` per table


class OnConflict(StrEnum):
    error = "error"  # fail the copy on the first PK conflict
    skip = "skip"  # INSERT … ON CONFLICT DO NOTHING (or IGNORE)
    update = "update"  # upsert (INSERT … ON CONFLICT DO UPDATE)
    truncate = "truncate"  # TRUNCATE target table first, then bulk INSERT


class DiffSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: list[str] = Field(default_factory=list)
    show: list[str] | None = None
    strategy: DiffStrategy = DiffStrategy.custom
    tables: list[str] | None = None  # only used by `table_counts` strategy


class CopySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    batch_size: int = 10000  # rows per executemany() batch
    tables: list[str] | None = None  # None = introspect src; .* alias not supported
    on_conflict: OnConflict = OnConflict.error
    where: dict[str, str] = Field(default_factory=dict)  # per-table WHERE pushed into src SELECT
    truncate_first: bool = False  # TRUNCATE target table before inserting (manual shortcut)


class SyncSpec(BaseModel):
    """Converge trg to match src for one table (insert missing, update
    differing, optionally delete extras), keyed by `key`.

    Direction is src → trg only (make-trg-match-src). True bidirectional
    merge would need conflict arbitration, which is out of scope.
    """

    model_config = ConfigDict(extra="forbid")
    key: list[str]
    target_table: str  # trg table written to (INSERT/UPDATE/DELETE)
    delete_extras: bool = False  # delete trg rows whose key is absent in src
    batch_size: int = 10000  # rows per executemany() batch


class CompareSpec(BaseModel):
    """Row-level checksum/hash diff with sample mismatched rows."""

    model_config = ConfigDict(extra="forbid")
    key: list[str]
    sample_mismatches: int = 10


class ValidateSpec(BaseModel):
    """Structural schema diff: compare columns (name + type) across src and
    trg for each table in `tables` (or the introspected intersection when
    `tables` is null). `include`/`exclude` are column-name filters."""

    model_config = ConfigDict(extra="forbid")
    tables: list[str] | None = None  # None = introspect intersection of both schemas
    include: list[str] = Field(default_factory=list)  # only these column names
    exclude: list[str] = Field(default_factory=list)  # drop these column names


class ReplaySpec(BaseModel):
    """Copy with a per-row Python transform applied before writing to trg.

    `transform` is either ``"identity"`` (no-op) or a dotted import path
    ``package.module:callable`` / ``package.module.callable`` resolving to a
    ``Callable[[dict], dict]``. The callable runs in-process; it must not
    reach across connections.
    """

    model_config = ConfigDict(extra="forbid")
    batch_size: int = 10000
    tables: list[str] | None = None  # None = introspect src
    where: dict[str, str] = Field(default_factory=dict)
    transform: str = "identity"


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
    queries: dict[str, str] | None = (
        None  # per-role SQL for diff/compare; copy builds its own from `copy_spec`
    )
    diff: DiffSpec | None = None
    # mode-specific specs (suffixed `_spec` to avoid shadowing BaseModel.copy/.validate):
    copy_spec: CopySpec | None = None
    sync_spec: SyncSpec | None = None
    compare_spec: CompareSpec | None = None
    validate_spec: ValidateSpec | None = None
    replay_spec: ReplaySpec | None = None

    @model_validator(mode="after")
    def _check(self) -> Operation:
        if self.scope is OpScope.multi:
            if not self.roles:
                raise ValueError("multi operations must declare roles")
            self._check_multi_mode()
        else:
            if self.sql is None and self.mode is not OpMode.upsert:
                raise ValueError("single operations must declare 'sql' (upsert builds SQL from file)")
        return self

    def _check_multi_mode(self) -> None:
        m = self.mode
        if m in {OpMode.diff, OpMode.compare}:
            # query-based, needs queries for each role unless table_counts strategy
            if self.queries is None and self.diff and self.diff.strategy is DiffStrategy.table_counts:
                return  # introspected; queries built at runtime
            if self.queries is None:
                raise ValueError(f"{m.value} operations must declare `queries` per role")
            if not set(self.queries).issubset(self.roles):
                raise ValueError(f"queries roles {set(self.queries)} not subset of roles {self.roles}")
        elif m is OpMode.copy:
            if self.copy_spec is None:
                raise ValueError("copy operations must declare a `copy_spec:` spec")
            # introspect mode: ensure roles are exactly [src, trg]
            if self.copy_spec.tables is None and self.queries is None and self.roles != ["src", "trg"]:
                raise ValueError(
                    "copy with table introspection (no `tables:` list) requires roles [src, trg]"
                )
        elif m is OpMode.sync:
            if self.sync_spec is None:
                raise ValueError("sync operations must declare a `sync_spec:` spec")
            if not self.queries or not {"src", "trg"}.issubset(self.queries):
                raise ValueError(
                    "sync operations require `queries.src` (SELECT) + `queries.trg` (SELECT, same shape)"
                )
            if not set(self.queries).issubset(self.roles):
                raise ValueError(f"queries roles {set(self.queries)} not subset of roles {self.roles}")
        elif m in {OpMode.validate, OpMode.replay}:
            if m is OpMode.validate and self.validate_spec is None:
                raise ValueError("validate operations must declare a `validate_spec:` spec")
            if m is OpMode.replay and self.replay_spec is None:
                raise ValueError("replay operations must declare a `replay_spec:` spec")


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
