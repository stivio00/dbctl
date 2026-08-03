# `connections.yaml` reference

`~/.dbctl/connections.yaml` (or `~/.dbctl/profiles/<name>/connections.yaml`
when using `--profile <name>`) describes every database `dbctl` can reach.
The whole file is one map:

```yaml
connections:
  <name>:
    ...
```

Each connection is validated with `extra="forbid"` in pydantic, so a typo
like `helthcheck:` fails the load with a clear error instead of silently
falling back to defaults.

## Top-level fields

| field           | type                                      | required | notes |
|-----------------|-------------------------------------------|----------|-------|
| `description`   | string                                    | no       | shown in the dashboard and `dbctl connections list`. |
| `aliases`       | list of strings                           | no       | alternates that resolve back to this connection (e.g. `prod` → `db1`). |
| `type`          | `ssm` \| `ssh` \| `k8s` \| `direct`       | **yes**  | selects the tunnel implementation. |
| `driver`        | string                                    | **yes**  | SQLAlchemy URL scheme. Supported conventions: `postgresql+psycopg`, `mysql+pymysql`, `mariadb+pymysql`, `mssql+pyodbc`. Any other SQLAlchemy scheme works as long as its driver is importable. |
| `database`      | string                                    | **yes**  | database / catalog name passed to SQLAlchemy. |
| `username`      | string                                    | **yes**  | DB user. |
| `password`      | string                                    | see rule | plaintext DB password (local dev only — don't commit real secrets to YAML). Mutually exclusive with `password_env` and `prompt`. |
| `password_env`  | string                                    | see rule | name of the environment variable holding the DB password. Mutually exclusive with `password` and `prompt`. |
| `prompt`        | bool                                      | see rule | prompt for the DB password interactively each run. Mutually exclusive with `password` and `password_env`. |
| `ssm`           | [`SsmTunnel`](#ssm-block)                 | yes if `type: ssm` | tunnel params. |
| `ssh`           | [`SshTunnel`](#ssh-block)                 | yes if `type: ssh` | tunnel params. |
| `k8s`           | [`K8sTunnel`](#k8s-block)                 | yes if `type: k8s` | tunnel params. |
| `direct`        | [`DirectTunnel`](#direct-block)           | yes if `type: direct` | upstream params (no tunnel). |
| `healthcheck`   | [`Healthcheck`](#healthcheck-block)      | no       | `SELECT 1` by default. |
| `info`          | list of [`InfoQuery`](#info-query)        | no       | named introspection queries that `dbctl <conn> info <name>` can run. |
| `safety`        | [`Safety`](#safety-block)                 | no       | gates DML. |

**Credential rule.** Exactly one of `password`, `password_env`, and `prompt`
must be set; they are mutually exclusive. `password` (plaintext) is fine for
local dev fleets like the bundled docker-compose sample; for anything shared
or production-shaped prefer `password_env` (the value lives in your shell /
secret manager, never in YAML) or `prompt: true` (interactive, leaves no
trace). There is no Secrets Manager / Vault integration — the assumption is
your existing SSO session and shell environment already wrap the secrets you
need.

### Loader resilience

Each connection is validated **individually**, not as one all-or-nothing
`ConnectionsFile`. A mis-configured connection (a reference template with no
password source, a typo'd driver name, an unknown tunnel block, …) is skipped
with a one-line warning on stderr, and the good connections still load so the
dashboard and `--help` keep working:

```
connections.yaml: 1 invalid connection skipped:
  pg-ssm: set 'password', 'password_env' or 'prompt: true' for each connection
```

The offending connection is simply absent from `dbctl connections list` until
you fix it — the CLI never refuses to start because of one bad entry.

## `ssm` block

AWS SSM port-forwarding to a remote host (typically RDS) through an EC2
bastion. The `aws` CLI on your `PATH` is invoked as a subprocess; your
existing SSO session cached in `~/.aws/cache/sso/*.json` is used directly.

```yaml
ssm:
  region: eu-west-1
  profile: prod                              # optional; AWS SSO profile name
  bastion_instance_id: i-0abcd1234            # mutually exclusive with bastion_tags
  # bastion_tags: { Name: bastion-prod }     # resolved via aws ec2 describe-instances
  remote_host: mydb.xxxx.eu-west-1.rds.amazonaws.com
  remote_port: 5432
  local_port: 0                               # 0 = dbctl picks a free local port
  ssm_document: AWS-StartPortForwardingSessionToRemoteHost
```

- `bastion_instance_id` is used directly as the SSM `--target`.
- `bastion_tags` (alternative) is resolved to an instance id at tunnel-open
  time via `aws ec2 describe-instances --filters Name=tag:K,Values=V …`.
- `local_port: 0` makes `dbctl` discover a free port *before* invoking the
  SSM document — there's no API to read back the port SSM auto-picks, so
  pre-discovery avoids the race entirely.
- The subprocess is terminated cleanly on exit; an `atexit` fallback covers
  hard crashes.

## `ssh` block

Classic `ssh -N -L <local>:<remote_host>:<remote_port> <user>@<host> -i
<identity> -p <port>` port-forward. The `ssh` binary on your `PATH` is
invoked as a subprocess; your `~/.ssh/config`, agent, and key rotation all
keep working.

```yaml
ssh:
  host: bastion.example.com
  user: ec2-user
  identity: ~/.ssh/id_rsa                      # ~ is expanded by dbctl
  remote_host: db.internal                     # the database's own hostname
  remote_port: 5432
  local_port: 0                                # 0 = auto
  port: 22                                     # bastion's SSH port
```

- `identity` is run through `os.path.expanduser`, so `~/.ssh/...` works.
- The tunnel uses `ExitOnForwardFailure=yes` so a port conflict fails fast
  (rather than hanging) and `StrictHostKeyChecking=accept-new` so a
  first-time bastion is accepted automatically.

## `k8s` block

Kubernetes port-forward via `kubectl port-forward`. Useful for databases
exposed as Services or Pods inside a cluster — notably StatefulSets /
operators such as [CloudNativePG](https://cloudnative-pg.io/) and
[Postgres Operator](https://github.com/zalando/postgres-operator) where
there is no long-lived external endpoint.

```yaml
k8s:
  context: prod-cluster                  # --context; required
  namespace: data                        # optional; blank = kubeconfig default
  target: svc/postgres-primary           # also accepts pod/<name>
  remote_port: 5432                      # port on the Service/Pod to forward
  local_port: 0                          # 0 = auto (dbctl picks a free port)
```

- `target` is forwarded verbatim to `kubectl port-forward`, so anything
  kubectl accepts as the resource identifier works: `svc/<name>`,
  `pod/<name>`, or bare `<name>` (resolved as a Service).
- `namespace` is optional; when omitted the kubeconfig default is used.
- kubectl's own kubeconfig resolution (`~/.kube/config`, `KUBECONFIG`,
  …) is left untouched — dbctl only adds `--context` (and `--namespace`
  when set). Your existing EKS / GKE / k3s auth flows work as-is.
- `dbctl doctor` reports whether `kubectl` is on `PATH`; it is listed as
  `required by config` only when at least one connection uses `type: k8s`.

## `direct` block

No tunnel. `dbctl` connects to the upstream host:port directly.

```yaml
direct:
  host: 127.0.0.1
  port: 5432
```

Use this for local dev DBs, public test DBs, or any case where you can reach
the database without a bastion.

> For SQL Server (`mssql+pyodbc`) you typically need an installed ODBC
> driver on the host. SQLAlchemy's URL will be
> `mssql+pyodbc://sa:pwd@127.0.0.1:1434/app`; if pyodbc can't resolve the
> driver, install `ODBC Driver 18 for SQL Server` (or similar) on your
> system first.

## `healthcheck` block

```yaml
healthcheck:
  query: SELECT 1
  timeout_seconds: 5.0
```

Run before every command unless `--skip-healthcheck` is passed. Failure
stops the command with exit code 5 and prints the (first line of the)
DB error. The query is executed via `engine.connect().exec_driver_sql()`,
so any server-side "SELECT N" works without `FROM`.

## `info` query

Named read-only queries surfaced by `dbctl <conn> info [name]`:

```yaml
info:
  - name: row_counts
    description: "Top tables by row count"
    query: |
      SELECT relname, n_live_tup
      FROM pg_stat_user_tables
      ORDER BY n_live_tup DESC LIMIT 10
  - name: active_conns
    query: "SELECT count(*) FROM pg_stat_activity"
```

`dbctl <conn> info` runs every declared query; `dbctl <conn> info row_counts`
runs just one. The `name` is the only required field; `description` is used
in the connection page (`dbctl <conn>`).

## `safety` block

```yaml
safety:
  confirm: true            # default false
  read_only: false         # default false
  allowed_operations: []   # default [] = any
```

- `confirm: true` makes DML ops **dry-run by default**. The op prints the
  resolved SQL + bound values, writes an audit entry with
  `status: "dry-run"`, and exits without committing. Add `--apply` to commit
  (still with a `[y/N]` prompt unless `--yes`). The confirm happens *before*
  the transaction opens, so answering `N` (or Ctrl-C) leaves the DB
  untouched.
- `read_only: true` blocks every DML op (`mode: execute` / `upsert` /
  `script`) entirely — the connection can only run `fetch` / `fetch_one`
  operations, `info` queries, and `health`. Use it for reporting replicas.
- `allowed_operations: [add-user, reset-credits]` restricts the connection to
  those operation names; an empty list means any operation is allowed. This
  is the last line of defense for production.

## Examples

### Production RDS Postgres via SSO

```yaml
connections:
  prod-pg:
    description: "Production main Postgres"
    aliases: [prod]
    type: ssm
    driver: postgresql+psycopg
    database: app
    username: app_admin
    password_env: DBCTL_PROD_PG_PASSWORD
    ssm:
      region: eu-west-1
      profile: prod
      bastion_tags: { Name: bastion-prod, Env: prod }
      remote_host: mydb.xxxx.eu-west-1.rds.amazonaws.com
      remote_port: 5432
      local_port: 0
    healthcheck: { query: "SELECT 1", timeout_seconds: 5 }
    info:
      - name: row_counts
        query: "SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY n_live_tup DESC LIMIT 10"
      - name: active_conns
        query: "SELECT count(*) FROM pg_stat_activity"
    safety:
      confirm: true
      read_only: false
      allowed_operations: []   # any
```

### Reporting MySQL replica via SSH (read-only)

```yaml
connections:
  reports-mysql:
    aliases: [reports]
    type: ssh
    driver: mysql+pymysql
    database: reports
    username: reporter
    password_env: DBCTL_REPORTS_PASSWORD
    ssh:
      host: bastion.example.com
      user: ec2-user
      identity: ~/.ssh/prod_rsa
      remote_host: db.internal
      remote_port: 3306
      local_port: 0
    healthcheck: { query: "SELECT 1" }
    safety:
      confirm: false   # read-only below already blocks DML
      read_only: true
```

### Local dev SQL Server

```yaml
connections:
  dev-mssql:
    type: direct
    driver: mssql+pyodbc
    database: dev
    username: sa
    prompt: true
    direct: { host: localhost, port: 1433 }
    healthcheck: { query: "SELECT 1" }
    safety: { confirm: false, read_only: false }
```

### CloudNativePG cluster via kubectl port-forward

```yaml
connections:
  cnpg-prod:
    description: "Production CloudNativePG cluster"
    aliases: [prod]
    type: k8s
    driver: postgresql+psycopg
    database: app
    username: app_admin
    password_env: DBCTL_CNPG_PROD_PASSWORD
    k8s:
      context: prod-euks
      namespace: data
      target: svc/cnpg-primary
      remote_port: 5432
      local_port: 0
    healthcheck: { query: "SELECT 1", timeout_seconds: 5 }
    info:
      - name: row_counts
        query: "SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY n_live_tup DESC LIMIT 10"
      - name: active_conns
        query: "SELECT count(*) FROM pg_stat_activity"
    safety:
      confirm: true
      read_only: false
      allowed_operations: []
```