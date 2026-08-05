# `connections.yaml` reference

<img src="logo_small.png" alt="dbctl" width="120">

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

There are two ways to declare a connection's DB credentials:

1. **Individual fields** (`driver` / `database` / `username` / `password` / …)
   — `dbctl` assembles a SQLAlchemy URL from the pieces, swapping in the
   tunnel's local bind as host:port. Best for most cases.
2. **Full `url:` string** — provide a complete SQLAlchemy URL
   (e.g. `postgresql+psycopg://user:pw@host:5432/db`). The URL is used
   as-is; the tunnel's local bind is **not** injected. Best for exotic
   connection strings (Azure AD, cloud sockets, ODBC-specific kwargs)
   that don't fit the individual-field model.

The two shapes are mutually exclusive — the config validator rejects any
overlap with a clear message.

### Individual-field mode

| field           | type                                      | required | notes |
|-----------------|-------------------------------------------|----------|-------|
| `description`   | string                                    | no       | shown in the dashboard and `dbctl connections list`. |
| `aliases`       | list of strings                           | no       | alternates that resolve back to this connection (e.g. `prod` → `db1`). |
| `type`          | `ssm` \| `ssh` \| `k8s` \| `direct` \| `azure` \| `gcp` | **yes**  | selects the tunnel implementation. |
| `driver`        | string                                    | **yes**  | SQLAlchemy URL scheme. Supported: `postgresql+psycopg`, `mysql+pymysql`, `mariadb+pymysql`, `mssql+pyodbc`, `oracle+oracledb`, `sqlite`, `duckdb`. Any other SQLAlchemy scheme works as long as its driver is importable. |
| `database`      | string                                    | **yes**  | database / catalog name passed to SQLAlchemy. |
| `username`      | string                                    | **yes** (unless `windows_sso`) | DB user. |
| `password`      | string                                    | see rule | plaintext DB password (local dev only — don't commit real secrets to YAML). Mutually exclusive with `password_env`, `prompt`, and `windows_sso`. |
| `password_env`  | string                                    | see rule | name of the environment variable holding the DB password. Mutually exclusive with `password`, `prompt`, and `windows_sso`. |
| `prompt`        | bool                                      | see rule | prompt for the DB password interactively each run. Mutually exclusive with `password`, `password_env`, and `windows_sso`. |
| `windows_sso`   | bool                                      | see rule | mssql+pyodbc only: use Windows Integrated Security (`Trusted_Connection=yes`). Mutually exclusive with all password sources. Omit `username` when this is set. |
| `ssm`           | [`SsmTunnel`](#ssm-block)                 | yes if `type: ssm` | tunnel params. |
| `ssh`           | [`SshTunnel`](#ssh-block)                 | yes if `type: ssh` | tunnel params. |
| `k8s`           | [`K8sTunnel`](#k8s-block)                 | yes if `type: k8s` | tunnel params. |
| `direct`        | [`DirectTunnel`](#direct-block)           | yes if `type: direct` | upstream params (no tunnel). |
| `azure`         | [`AzureBastionTunnel`](#azure-block)      | yes if `type: azure` | tunnel params. |
| `gcp`           | [`GcpIapTunnel`](#gcp-block)              | yes if `type: gcp` | tunnel params. |
| `healthcheck`   | [`Healthcheck`](#healthcheck-block)      | no       | `SELECT 1` by default. |
| `info`          | list of [`InfoQuery`](#info-query)        | no       | named introspection queries that `dbctl <conn> info <name>` can run. |
| `safety`        | [`Safety`](#safety-block)                 | no       | gates DML. |

**Credential rule.** Exactly one of `password`, `password_env`, `prompt`,
and `windows_sso` must be set; they are mutually exclusive. `password`
(plaintext) is fine for local dev fleets like the bundled docker-compose
sample; for anything shared or production-shaped prefer `password_env`
(the value lives in your shell / secret manager, never in YAML) or
`prompt: true` (interactive, leaves no trace). `windows_sso: true` is for
SQL Server on Windows with Integrated Security (the ODBC driver uses the
current Windows user's Kerberos/NTLM credentials).

### Full-URL mode

| field  | type   | required | notes |
|--------|--------|----------|-------|
| `url`  | string | **yes** (instead of the individual fields above) | full SQLAlchemy URL, e.g. `postgresql+psycopg://user:pw@host:5432/db` or `mssql+pyodbc://user:pw@host:1433/db?driver=ODBC+Driver+18+for+SQL+Server`. Used as-is; the tunnel's local bind is NOT injected. |

When `url:` is set, `driver` / `database` / `username` / `password` /
`password_env` / `prompt` / `windows_sso` must all be absent — the config
validator rejects the overlap. The `type` + tunnel block (`direct` / `ssm` /
`ssh` / `k8s`) and `healthcheck` / `info` / `safety` blocks are still
required/optional as above.

Use full-URL mode when:
- you need ODBC-specific query-string params (`?driver=…&Encrypt=yes`);
- you're connecting to Azure SQL with `Authentication=ActiveDirectoryPassword`;
- your SQLAlchemy URL uses a non-standard scheme dbctl's individual fields
  don't model.

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

## `{{ssm:...}}` references

Any string field on a connection — `password`, `username`, `database`,
`driver`, `url`, or a nested tunnel field like `ssm.remote_host` or
`direct.host` — can be a placeholder that resolves against AWS SSM
Parameter Store instead of a literal value:

```
{{ssm:<parameter-name>[;property:<json-key>][;profile:<aws-profile>]}}
```

| part | required | meaning |
|------|----------|---------|
| `<parameter-name>` | yes | full SSM parameter name/path, e.g. `/prod/db/password`. |
| `property` | no | if the parameter value is a JSON object, extract only this key. Omit to use the whole raw value. |
| `profile` | no | AWS CLI profile for the lookup. Omit to use the default credential chain (env vars, default profile, instance role). |

```yaml
connections:
  prod-pg:
    type: direct
    driver: postgresql+psycopg
    database: app
    username: app_admin
    password: "{{ssm:/prod/db/credentials;property:password;profile:prod-admin}}"
    direct:
      host: "{{ssm:/prod/db/credentials;property:host;profile:prod-admin}}"
```

Given an SSM parameter `/prod/db/credentials` holding
`{"username": "app_admin", "password": "s3cret", "host": "prod.rds.internal"}`,
`{{ssm:/prod/db/credentials;property:password}}` resolves to `s3cret`.

**When resolution happens.** References are resolved lazily, right before a
connection is actually used to open a tunnel or build an engine (`dbctl
<conn> health`, `dbctl tunnel open`, an operation, …) — never at load time.
`dbctl connections list` / `dbctl connections show` never touch AWS and
never print a resolved secret; they show the raw `{{ssm:...}}` placeholder
as written in the YAML. Because resolution is per-connection and on-demand,
an unreachable profile on one connection never breaks loading or using any
other connection.

**Resolution details:**
- Parameters are always fetched `--with-decryption`, so `SecureString`
  parameters work transparently; plain `String` parameters are unaffected.
- If `property` is set but the parameter's value isn't valid JSON, or the
  key is missing, resolution fails with an error naming the parameter and
  property — never the (partial) value.
- If the parameter doesn't exist or access is denied, the error names the
  parameter but never attempts to print a value.
- Resolution shells out to the `aws` CLI (same mechanism `ssm` tunnels
  already use for bastion lookups), so no extra AWS SDK dependency is
  required — just a working `aws` CLI on `PATH` and credentials for the
  chosen profile.

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
  disable_automatic_sso_login: false          # true = never auto-run `aws sso login`
```

- `bastion_instance_id` is used directly as the SSM `--target`.
- `bastion_tags` (alternative) is resolved to an instance id at tunnel-open
  time via `aws ec2 describe-instances --filters Name=tag:K,Values=V …`.
- `local_port: 0` makes `dbctl` discover a free port *before* invoking the
  SSM document — there's no API to read back the port SSM auto-picks, so
  pre-discovery avoids the race entirely.
- `disable_automatic_sso_login: true` — by default dbctl checks the SSO
  token cache (`~/.aws/sso/cache/`) before opening the tunnel; if the token
  is missing or expired it automatically runs `aws sso login --profile
  <profile>` (which opens a browser window for SSO authentication). Set
  this to `true` if you prefer to manage your SSO session manually — dbctl
  will instead raise a clean error telling you to run `aws sso login` by
  hand.
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

## `azure` block

Azure Bastion tunnel to a VM via `az network bastion tunnel`. The `az` CLI
on your `PATH` is invoked as a subprocess; your existing `az login` session
is used directly. Requires an Azure Bastion resource on the **Standard**
SKU (the Basic SKU doesn't support the native-client tunnel command) in the
target VM's VNet.

```yaml
azure:
  resource_group: prod-rg
  bastion_name: prod-bastion
  target_resource_id: /subscriptions/xxxx/resourceGroups/prod-rg/providers/Microsoft.Compute/virtualMachines/prod-db-vm
  subscription: prod                          # optional; az CLI --subscription (name or id)
  remote_port: 5432                           # port on the target VM to forward
  local_port: 0                               # 0 = dbctl picks a free local port
```

- `target_resource_id` is the full ARM resource id of the target VM (the
  bastion's target, not the bastion itself).
- `local_port: 0` makes `dbctl` discover a free port itself before invoking
  `az`, the same as the `ssm` / `ssh` / `k8s` tunnels.
- The subprocess is terminated cleanly on exit; an `atexit` fallback covers
  hard crashes.

## `gcp` block

GCP Identity-Aware Proxy (IAP) TCP tunnel to a Compute Engine instance via
`gcloud compute start-iap-tunnel`. The `gcloud` CLI on your `PATH` is
invoked as a subprocess; your existing `gcloud auth login` session is used
directly. Requires IAP TCP forwarding enabled for the target instance's
network and the caller to hold the `roles/iap.tunnelResourceAccessor` IAM
role (directly or via a broader role).

```yaml
gcp:
  project: my-gcp-project                     # optional; blank = gcloud's active project
  zone: europe-west1-b
  instance: prod-db-vm
  remote_port: 5432                           # port on the instance to forward
  local_port: 0                               # 0 = dbctl picks a free local port
```

- `local_port: 0` makes `dbctl` discover a free port itself before invoking
  `gcloud`, the same as the other tunnel types.
- The subprocess is terminated cleanly on exit; an `atexit` fallback covers
  hard crashes.

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

### Oracle Database (thin mode — no native client needed)

```yaml
connections:
  prod-oracle:
    description: "Production Oracle (oracledb thin mode)"
    type: direct
    driver: oracle+oracledb
    database: ORCLPDB1   # service name (or SID)
    username: app_admin
    password_env: DBCTL_ORACLE_PASSWORD
    direct: { host: db.internal, port: 1521 }
    healthcheck: { query: "SELECT 1 FROM DUAL", timeout_seconds: 10 }
    safety:
      confirm: true
      read_only: false
```

> `oracledb` defaults to **thin mode** (pure Python, no Oracle Instant
> Client needed). If you need thick mode (native Oracle client libs), set
> it via the `url:` field with `thick_mode=true` in the query string.

### Local SQLite database (file-based)

```yaml
connections:
  local-sqlite:
    description: "Local SQLite database"
    type: direct
    driver: sqlite
    database: /path/to/mydata.db   # absolute path to the .db file
    username: ""                   # sqlite ignores these but config requires one
    password: ""                   # sqlite ignores
    direct: { host: localhost, port: 0 }  # ignored by sqlite; required by config schema
    healthcheck: { query: "SELECT 1" }
    safety:
      confirm: true
      read_only: false
```

> SQLite and DuckDB are file-based — the `host` / `port` fields are
> ignored by the driver but `type: direct` still requires a `direct:`
> block. Use `url:` mode if you prefer:
> ```yaml
> url: "sqlite:////absolute/path/to/mydata.db"
> ```
> (Note the four slashes for absolute paths in SQLAlchemy's sqlite scheme.)

### Local DuckDB database (file-based)

```yaml
connections:
  local-duckdb:
    description: "Local DuckDB database"
    type: direct
    driver: duckdb
    database: /path/to/mydata.duckdb   # or ":memory:" for in-memory
    username: ""                        # duckdb ignores
    password: ""                        # duckdb ignores
    direct: { host: localhost, port: 0 }
    healthcheck: { query: "SELECT 1" }
    safety:
      confirm: true
      read_only: false
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

### Azure Postgres VM via Azure Bastion

```yaml
connections:
  azure-prod-pg:
    description: "Production Postgres VM reached via Azure Bastion"
    aliases: [prod]
    type: azure
    driver: postgresql+psycopg
    database: app
    username: app_admin
    password_env: DBCTL_AZURE_PROD_PG_PASSWORD
    azure:
      resource_group: prod-rg
      bastion_name: prod-bastion
      target_resource_id: /subscriptions/xxxx/resourceGroups/prod-rg/providers/Microsoft.Compute/virtualMachines/prod-db-vm
      subscription: prod
      remote_port: 5432
      local_port: 0
    healthcheck: { query: "SELECT 1" }
    safety:
      confirm: true
      read_only: false
      allowed_operations: []
```

### GCP Cloud SQL-adjacent VM via IAP tunnel

```yaml
connections:
  gcp-prod-pg:
    description: "Production Postgres VM reached via GCP IAP tunnel"
    aliases: [prod]
    type: gcp
    driver: postgresql+psycopg
    database: app
    username: app_admin
    password_env: DBCTL_GCP_PROD_PG_PASSWORD
    gcp:
      project: my-gcp-project
      zone: europe-west1-b
      instance: prod-db-vm
      remote_port: 5432
      local_port: 0
    healthcheck: { query: "SELECT 1" }
    safety:
      confirm: true
      read_only: false
      allowed_operations: []
```

### SQL Server with Windows SSO (Integrated Security)

```yaml
connections:
  prod-mssql-sso:
    description: "Production SQL Server (Windows Integrated Security)"
    type: direct
    driver: mssql+pyodbc
    database: app
    windows_sso: true              # ODBC: Trusted_Connection=yes
    # no username / password / password_env / prompt needed
    direct: { host: 10.0.0.5, port: 1433 }
    healthcheck: { query: "SELECT 1", timeout_seconds: 5 }
    safety:
      confirm: true
      read_only: false
```

### Full SQLAlchemy URL (Azure SQL with ActiveDirectoryPassword)

```yaml
connections:
  azure-sql:
    description: "Azure SQL Database (Azure AD password auth)"
    type: direct
    url: "mssql+pyodbc://app_admin@myserver.database.windows.net:1433/app?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes&Authentication=ActiveDirectoryPassword"
    direct: { host: myserver.database.windows.net, port: 1433 }
    healthcheck: { query: "SELECT 1", timeout_seconds: 10 }
    safety:
      confirm: true
      read_only: true
```

> When `url:` is set, the tunnel's local bind is **not** injected — the
> URL's own host:port wins. Use this for cloud databases where the
> connection string already encodes everything (Azure SQL, Cloud SQL
> via the Cloud SQL connector, etc.). For tunnelled connections where
> `dbctl` must rewrite host:port to the local bind, use the
> individual-field mode instead.