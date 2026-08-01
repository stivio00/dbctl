# dbctl

`dbctl` is a small Python CLI for **monitoring, controlling, and administering
multiple databases** through a single declarative config. It supports three
ways of reaching a database:

| type     | how                                                            | external deps |
|----------|----------------------------------------------------------------|---------------|
| `ssm`    | AWS SSM port-forward through an EC2 bastion to a private host | `aws` CLI on PATH, active SSO session |
| `ssh`    | Classic `ssh -N -L` port-forward through a bastion             | `ssh` CLI on PATH, an SSH key file |
| `direct` | No tunnel — connect to the upstream host:port directly        | none |

Each connection declares its SQLAlchemy URL scheme (`postgresql+psycopg`,
`mysql+pymysql`, `mariadb+pymysql`, `mssql+pyodbc`, …), a healthcheck query,
optional introspection (`info`) queries, and a `safety` policy.

Operations are declared separately in `operations.yaml`. Each operation is a
parameterised SQL block (using `$name` placeholders) with declared parameters;
`dbctl` builds one Click subcommand per operation, so every connection
automatically exposes a subcommand for every declared single-DB operation.

```text
dbctl pg add-user stephen 12 --apply
dbctl pg list-users 20
dbctl pg info row_counts
dbctl diff user-count pg my
dbctl diff compare-quotas pg my Daily
dbctl doctor
dbctl history list
```

A query-only operation runs immediately. A DML operation (`mode: execute`,
`upsert`, or `script`) on a connection with `safety.confirm: true` runs as a
**dry-run by default** — it shows the resolved SQL + bound values and writes
nothing, until you pass `--apply`. `--yes` skips the final `[y/N]` confirmation.
Every run is appended to `~/.dbctl/history.jsonl` (with secret-typed
parameters redacted), and `dbctl <conn> again` re-runs the last operation with
the same params.

## Why "the operations are declarative"

There is **no ad-hoc query command**. To run SQL through `dbctl` you must
declare an operation in `operations.yaml` first — its name, its parameters
(with types, descriptions, positional vs keyword), its SQL, and its mode
(`execute` / `fetch` / `fetch_one` / `script` / `upsert` for single-DB;
`diff` / `compare` / `sync` for multi-DB). The CLI then synthesises a Click
subcommand per operation, so:

- the `--help` for `<conn> <op>` is generated from the declared parameters;
- parameters are type-checked before the SQL is ever rendered;
- secret-typed parameters are identified for audit redaction;
- the audit log is queryable by operation name (`dbctl history list`).

This keeps "what can be run against this DB" discoverable from a versioned
config file, instead of buried in shell history.

## Install (uv)

`uv` is the recommended workflow:

```bash
# Install dbctl as a tool with one or more dialect drivers
uv tool install ./dbctl --with psycopg --with pymysql
# (add `--with pyodbc` if you need SQL Server)

# Or hack locally:
cd dbctl
uv sync --extra dev --extra postgres --extra mysql
uv pip install -e .
uv run dbctl --help
```

The `aws` and `ssh` binaries are expected on `PATH`. No `boto3`, no
`paramiko` — dbctl always shells out so you keep your existing SSO session,
key agents, and MFA flows.

## Quick start with the bundled docker-compose fleet

> **First time here?** Follow the [step-by-step tutorial](docs/tutorial.md)
> instead — it walks through install, the dashboard, single-DB operations,
> the audit log, multi-DB diff, writing your own operation, and pointing
> `dbctl` at real AWS RDS via SSM.

`docker-compose.yml` brings up three databases (postgres on `:5433`, mysql on
`:3307`, mssql on `:1434`) with the same four-table schema (`users`, `quotas`,
`usage`, `logs`) and slightly different sample data — perfect for trying the
multi-connection `diff` commands.

```bash
docker compose up -d

# passwords for the sample config (set them in your shell):
export DBCTL_PG_PASSWORD=pwd_postgres
export DBCTL_MY_PASSWORD=pwd_mysql
export DBCTL_MS_PASSWORD='PwdSqlServer2026!'

# install the sample config
mkdir -p ~/.dbctl
cp .dbctl/connections.yaml ~/.dbctl/connections.yaml
cp .dbctl/operations.yaml  ~/.dbctl/operations.yaml

dbctl                                  # dashboard
dbctl doctor                           # healthcheck all three

dbctl pg health
dbctl pg info row_counts
dbctl pg list-users 10
dbctl pg add-user stephen 12 --show-sql        # dry-run (prints SQL)
dbctl pg add-user stephen 12 --apply --yes     # commit (no prompt)

dbctl diff user-count pg my                    # multi-DB diff
dbctl diff compare-quotas pg my Daily

dbctl pg history                               # per-connection audit log
dbctl pg again                                 # re-run last op on pg

dbctl tunnel open pg                           # hold tunnel for ad-hoc psql
```

> SQL Server needs an installed ODBC driver on the host (`ODBC Driver 18 for
> SQL Server`). The sample `ms` connection is `read_only: true` and is only
> used for the `diff` operations against the smaller `users` table.

## Config layout

```text
~/.dbctl/
├── connections.yaml     # named connections (ssm / ssh / direct)
├── operations.yaml      # parameterised SQL operations (single + multi)
└── history.jsonl        # audit log (one JSON event per run)
~/.dbctl/profiles/<name>/ # switch config dir with --profile <name>
```

`dbctl init` walks you through adding a new connection interactively and
writes it back to `~/.dbctl/connections.yaml` — it will test the tunnel +
healthcheck before saving (for `direct` connections).

## Adding an operation

Operations are global — every connection sees them. Declare one:

```yaml
# ~/.dbctl/operations.yaml
operations:
  reset-quota:
    description: "Reset a user's daily quota back to their yearly baseline"
    scope: single
    mode: execute
    confirm: true
    parameters:
      - { name: name,   type: string,  required: true, position: 1, description: "User name" }
      - { name: floor,  type: integer, default: 10,   position: 2, description: "Don't reset below this floor" }
    sql: |
      UPDATE users
         SET quota_daily = GREATEST($floor, quota_yearly / 365)
       WHERE name = $name
```

Then:

```bash
dbctl pg reset-quota alice --show-sql        # preview
dbctl pg reset-quota alice --apply --yes     # commit
```

`$name` and `$floor` are rewritten to SQLAlchemy bind-params (`:name`,
`:floor`) — they are always parameterised, never string-interpolated, so SQL
injection through a parameter value is not possible. (For *dynamic
identifiers* such as table names, write one operation per table for now;
`${var}` identifier interpolation is on the v2 roadmap.)

See [`docs/operations.md`](docs/operations.md) for the full parameter and
mode reference.

## Adding a connection

Edit `~/.dbctl/connections.yaml`, or run `dbctl init`:

```yaml
connections:
  prod-pg:
    description: "Production main Postgres (private RDS, SSM tunnel)"
    aliases: [prod]
    type: ssm
    driver: postgresql+psycopg
    database: app
    username: app_admin
    password_env: DBCTL_PROD_PG_PASSWORD
    ssm:
      region: eu-west-1
      profile: prod                              # AWS SSO profile; tokens cached in ~/.aws/cache
      bastion_instance_id: i-0abcd1234            # or use bastion_tags: { Name: bastion-prod }
      remote_host: mydb.xxxx.eu-west-1.rds.amazonaws.com
      remote_port: 5432
      local_port: 0                               # 0 = auto-pick a free port
    healthcheck: { query: "SELECT 1" }
    info:
      - name: row_counts
        query: "SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY n_live_tup DESC LIMIT 10"
    safety:
      confirm: true        # dry-run-by-default for DML
      read_only: false
      allowed_operations: []   # empty = any
```

See [`docs/connections.md`](docs/connections.md) for the full reference.

## Safety model

Each connection has a `safety` block:

| field               | effect                                                                  |
|---------------------|-------------------------------------------------------------------------|
| `confirm: true`     | DML ops print resolved SQL + dry-run **unless `--apply`** is passed. `--yes` skips the final prompt. |
| `read_only: true`   | Blocks every DML op (execute / upsert / script) entirely.               |
| `allowed_operations`| Whitelist of operation names; empty list = all allowed.                 |

`dbctl` **confirms before opening the transaction** — saying `N` at the
prompt leaves the database untouched.

Only `password_env: VARNAME` and `prompt: true` are accepted as DB password
sources — no plaintext in YAML, no Secrets Manager code (you said your SSO
session already wraps the secrets you need). Secret-typed operation parameters
(`type: secret`) are redacted in the audit log.

## Shell completion

```bash
dbctl --install-completion bash         # prints the snippet
echo 'eval "$(_DBCTL_COMPLETE=bash_source dbctl)"' >> ~/.bashrc
# similarly for zsh / fish
```

Completion then covers connection names, operation names, and (for multi-DB
verbs) `dbctl diff <TAB>` lists the available operations.

## Project layout

```text
dbctl/
├── pyproject.toml          # uv / hatchling; ruff + pytest + mypy config
├── docker-compose.yml      # postgres :5433, mysql :3307, mssql :1434
├── seed/                   # four-table schema + sample data for the fleet
├── .dbctl/                 # sample connections.yaml + operations.yaml
├── docs/
│   ├── connections.md      # connections.yaml reference
│   ├── operations.md       # operations.yaml reference
│   └── DESIGN.md           # architecture and design decisions
└── dbctl/
    ├── cli.py              # dynamic groups + per-op Click commands
    ├── config.py           # pydantic v2 models
    ├── connections.py      # loader + alias resolution + "did you mean?"
    ├── operations.py       # loader + scope filter
    ├── tunnels/{ssm,ssh,direct}.py
    ├── db.py               # URL builder + password resolution + healthcheck
    ├── execute.py          # $name → :name, mode routing, bind_params
    ├── multi.py            # per-role engine open + query runner for diffs
    ├── reports.py          # rich tables / json / csv / yaml + diff rendering
    ├── audit.py            # history.jsonl
    ├── runtime.py          # opened_conn() ctx-mgr (tunnel+engine+healthcheck)
    └── init.py             # dbctl init wizard
```

## Development

```bash
uv sync --extra dev --extra postgres --extra mysql
uv run ruff check dbctl tests   # lint
uv run pytest tests/             # 15 unit tests
uv run mypy dbctl                 # type-check
```

The unit tests use an in-memory SQLite database so they run without docker.

## Roadmap

- `mode: upsert` (autoload target table, dialect-aware `ON CONFLICT` / `ON DUPLICATE KEY`)
- `mode: script` (multi-statement support beyond single-statement passthrough)
- Multi-file operations loader (`.dbctl/operations/<name>.yaml`)
- Dynamic identifier interpolation (`${var}`) for table/column names in diff ops
- `dbctl compare` and `dbctl sync` modes (the framework is ready; sample ops not shipped)
- Tag-based operation filtering (`dbctl pg --tag user-mgmt <op>`)
- SSH agent / MFA support surfacing through the existing `ssh` subprocess

## License

MIT