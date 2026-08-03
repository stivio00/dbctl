# Tutorial: from zero to running your first operation

<img src="logo_small.png" alt="dbctl" width="120">

This walkthrough takes you from a fresh checkout to running real operations
against three live databases in about 15 minutes. It uses the bundled
docker-compose fleet (postgres, mysql, sql server) so you don't need AWS or
a real bastion to learn the tool.

Each command explains *what* it does and *why*; copy-paste-friendly blocks
are marked with `▶`.

---

## 0. What you'll learn

After this tutorial you'll know how to:

- Install `dbctl` with `uv`.
- Bring up the test database fleet with `docker compose`.
- Install the sample `connections.yaml` and `operations.yaml`.
- Run the dashboard, `doctor`, and per-connection introspection.
- Run a declared operation (both dry-run and `--apply`).
- Inspect the audit log and re-run the last operation.
- Run a multi-database `diff` between two connections.
- Write your own operation from scratch.
- Point `dbctl` at a real AWS RDS Postgres through an SSM bastion.

---

## 1. Install dbctl

You need [uv](https://docs.astral.sh/uv/) and docker.

```bash
▶ cd ~/Projects/dbctl
▶ uv sync --extra dev
```

`uv sync` creates a `.venv/` with everything pinned in `uv.lock`. The DB
drivers (`psycopg`, `pymysql`, `pyodbc`) ship as regular dependencies. (SQL
Server additionally needs an ODBC driver on the host — we'll come back to that.)

Run `dbctl` once to confirm it's alive:

```bash
▶ uv run dbctl --version
dbctl, version 0.6.0
```

---

## 2. Bring up the test database fleet

```bash
▶ docker compose up -d
```

The `docker-compose.yml` in this repo starts:

| service  | host port | what                       |
|----------|-----------|----------------------------|
| postgres | `5433`    | Postgres 16 with `app` DB  |
| mysql    | `3307`    | MySQL 8 with `app` DB      |
| mssql    | `1434`    | SQL Server 2022 + `app` DB |

Each one runs the matching seed in `seed/` which creates four tables —
`users`, `credits`, `usage`, `logs` — with slightly different sample data so
`diff` operations have something to compare.

Give it ~20 seconds to come up; then verify:

```bash
▶ docker compose ps
```

You should see all three services `healthy`.

---

## 3. Install the sample config

```bash
▶ mkdir -p ~/.dbctl
▶ cp .dbctl/connections.yaml ~/.dbctl/connections.yaml
▶ cp .dbctl/operations.yaml  ~/.dbctl/operations.yaml
```

The bundled config has three "live" connections — `pg`, `my`, `ms` — plus
three reference-only templates — `pg-ssm`, `pg-k8s`, `pg-ssh` — that we'll
use in section 11.

The sample config ships with **plaintext passwords** out of the box so the
docker fleet "just works" after `cp` — see the `password:` field in
`~/.dbctl/connections.yaml`. For any real environment swap to
`password_env: VARNAME` (read from your shell) or `prompt: true` (typed each
run); the three sources are mutually exclusive.

---

## 4. The dashboard

```bash
▶ uv run dbctl
```

You should see a table of five connections — `pg`, `my`, `ms`, `pg-ssh`,
`pg-ssm` — with their type (`direct`/`ssh`/`ssm`), driver, ops count, and
description. The last two are reference templates marked `read_only: true`.

Bare `dbctl` is the same as `dbctl status` — it never opens a tunnel.

---

## 5. Healthcheck everything (`doctor`)

```bash
▶ uv run dbctl doctor
```

This opens each tunnel (or direct connection), runs the connection's
`healthcheck.query` (`SELECT 1` by default), and reports latency:

```text
┏━━━━━━━━┳━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ conn   ┃ status ┃ latency ┃ note                         ┃
┡━━━━━━━━╇━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ pg     │ OK     │ 4.2ms   │                              │
│ my     │ OK     │ 3.1ms   │                              │
│ ms     │ OK     │ 12.7ms  │                              │
│ pg-ssh │ ERR    │ -       │ ssh: Could not resolve host… │
│ pg-ssm │ ERR    │ -       │ ExpiredTokenException…       │
└────────┴────────┴─────────┴──────────────────────────────┘
```

`pg-ssh` and `pg-ssm` will fail here — they're reference templates pointing
at fake bastions. That's expected; we'll get to them in section 11.

You can also healthcheck a single connection:

```bash
▶ uv run dbctl pg health
OK pg (4.0ms)
```

---

## 6. Introspect a connection

Each connection can declare named `info` queries — read-only SQL surfaced
via `dbctl <conn> info [name]`.

```bash
▶ uv run dbctl pg info
```

The sample `pg` connection declares two info queries — `row_counts` and
`top_users`. Running `info` without a name runs all of them:

```text
                            info: row_counts (pg)
┏━━━━━━━━━━┳━━━━━━━┓
┃ table    ┃ rows  ┃
┡━━━━━━━━━━╇━━━━━━━┩
│ usage    │ 9     │
│ logs     │ 3     │
│ users    │ 3     │
│ credits   │ 2     │
└──────────┴───────┘
                            info: top_users (pg)
┏━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━┓
┃ name  ┃ credits_daily ┃ is_active ┃
┡━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━┩
│ carol │ 1000       │ True      │
│ alice │ 500        │ True      │
│ bob   │ 200        │ True      │
└───────┴────────────┴───────────┘
```

Just one:

```bash
▶ uv run dbctl pg info row_counts
```

To see all operations available on a connection (the connection "page"):

```bash
▶ uv run dbctl pg --help
```

You'll see the static commands (`health`, `info`, `history`, `again`) plus
the declared single-scope operations (`add-user`, `list-users`, `find-user`,
`report-logs`).

---

## 7. Run a fetch operation

`list-users` is declared with `mode: fetch` in `.dbctl/operations.yaml`:

```yaml
list-users:
  description: "List users (top N)"
  scope: single
  mode: fetch
  output: table
  parameters:
    - { name: limit, type: integer, default: 10, position: 1 }
  sql: |
    SELECT name, credits_daily, credits_yearly, type, is_active
    FROM users
    ORDER BY credits_daily DESC
    LIMIT $limit
```

Note the `$limit` placeholder — `dbctl` rewrites it to a SQLAlchemy
bind-param (`:limit`) so it is always parameterised, never string-interpolated.

Run it:

```bash
▶ uv run dbctl pg list-users 10
```

Change the output format with `--output` / `-o`:

```bash
▶ uv run dbctl pg list-users 3 -o json
▶ uv run dbctl pg list-users 3 -o csv
▶ uv run dbctl pg list-users 3 -o yaml
```

`mode: fetch` operations always run read-only — they are never dry-runned
or subject to `safety.confirm`.

---

## 8. Run a DML operation (dry-run vs `--apply`)

`add-user` is declared with `mode: execute`:

```yaml
add-user:
  description: "Create or update an application user (Daily credits by default)"
  scope: single
  mode: execute
  confirm: true
  parameters:
    - { name: name,  type: string,  required: true,  position: 1, description: "Unique user name" }
    - { name: credits, type: integer, required: true,  position: 2, description: "Daily credits" }
    - { name: type,  type: string,  default: "Daily", position: 3, description: "Account type" }
  sql: |
    INSERT INTO users (name, credits_daily, credits_yearly, type)
      VALUES ($name, $credits, $credits * 365, $type)
    ON CONFLICT (name) DO UPDATE
      SET credits_daily  = EXCLUDED.credits_daily,
          credits_yearly = EXCLUDED.credits_yearly,
          type         = EXCLUDED.type,
          updated_at   = NOW()
```

Three declared parameters: `name` and `credits` are required positionals,
`type` has a default. Try without `--apply` to see a dry-run:

```bash
▶ uv run dbctl pg add-user stephen 12 --show-sql
```

Output:

```text
resolved SQL:
INSERT INTO users (name, credits_daily, credits_yearly, type)
  VALUES ('stephen', 12, 12 * 365, 'Daily')
ON CONFLICT (name) DO UPDATE
  SET credits_daily  = EXCLUDED.credits_daily,
      credits_yearly = EXCLUDED.credits_yearly,
      type         = EXCLUDED.type,
      updated_at   = NOW()
dry-run (use --apply to commit)
```

**Nothing was written to the database.** The `pg` connection has
`safety.confirm: true` and we didn't pass `--apply`, so `dbctl` rendered the
SQL (with `--show-sql`), wrote an audit entry with `status: "dry-run"`, and
exited. The confirm step happens *before* the transaction opens, so even a
Ctrl-C at the prompt leaves the DB untouched.

Commit for real with `--apply`:

```bash
▶ uv run dbctl pg add-user stephen 12 --apply
```

You'll get a `[y/N]` prompt. Add `--yes` to skip it:

```bash
▶ uv run dbctl pg add-user mary 50 --apply --yes
```

Verify:

```bash
▶ uv run dbctl pg list-users 10
```

### Interactive fallback

If you forget a required parameter and your shell is a TTY, `dbctl` will
prompt for it interactively. Try:

```bash
▶ uv run dbctl pg add-user
name (Unique user name): joseph
credits (Daily credits): 25
```

In CI (no TTY) the missing param is a hard error so scripts fail loudly.

---

## 9. Audit log and `again`

Every run — including dry-runs — is appended to `~/.dbctl/history.jsonl`
with secret-typed parameters redacted. View it:

```bash
▶ uv run dbctl history list
```

Filter to a single connection:

```bash
▶ uv run dbctl pg history
```

`dbctl <conn> again` re-runs the last operation on that connection with the
same parameters — handy when iterating:

```bash
▶ uv run dbctl pg again
re-running: pg add-user {'name': 'mary', 'credits': 50, 'type': 'Daily'}
OK 1 row(s) affected in 18.0ms
```

To see the full JSON for one entry, grab its `run_id` from `history list`
and:

```bash
▶ uv run dbctl history show <run_id>
```

---

## 10. Multi-database diff

The sample `operations.yaml` declares two multi-scope operations:

```yaml
user-count:
  description: "Compare user counts between two databases"
  scope: multi
  mode: diff
  roles: [src, trg]
  queries:
    src: "SELECT 'users' AS t, COUNT(*) AS n FROM users"
    trg: "SELECT 'users' AS t, COUNT(*) AS n FROM users"
  diff:
    key: [t]
    show: [n]
```

Multi-scope operations surface as **operation-first** top-level commands
(preferred since v0.6.0) — `dbctl <op> SRC TRG`. The deprecated verb-first
form `dbctl diff <op> SRC TRG` still works as an alias:

```bash
▶ uv run dbctl diff --help
Multi-connection `diff` operations: user-count, compare-credits

Commands:
  compare-credits  Side-by-side credits summary across two databases
  user-count      Compare user counts between two databases
```

Get help for one operation (note the positional `SRC` and `TRG` taken from
the declared roles):

```bash
▶ uv run dbctl diff user-count --help
Usage: dbctl diff user-count [OPTIONS] SRC TRG

  dbctl diff user-count SRC TRG
```

Run it against `pg` and `my`:

```bash
▶ uv run dbctl diff user-count pg my
```

Output (after the section 8 inserts, `pg` has 5 users and `my` has 3):

```text
            user-count: pg vs my
┏━━━━━━━━┳───────┳───────┳━━━━━┓
┃ t      ┃ pg.n ┃ my.n ┃  Δ  ┃
┡━━━━━━━━╇───────╇───────╇━━━━━┩
│ users  │  5    │  3    │ +2  │
└────────┴───────┴───────┴─────┘
```

The `compare-credits` operation takes its own positional parameter (`period`)
in addition to the role connections:

```bash
▶ uv run dbctl diff compare-credits --help
Usage: dbctl diff compare-credits [OPTIONS] SRC TRG [PERIOD]
```

```bash
▶ uv run dbctl diff compare-credits pg my Daily
```

`--show-sql` prints the per-role SQL before running so you can sanity-check
what's being executed:

```bash
▶ uv run dbctl diff user-count pg my --show-sql
```

---

## 11. Write your own operation

Let's declare a new read-only report that joins `usage` to `users`. Open
`~/.dbctl/operations.yaml` in your editor and append under `operations:`:

```yaml
  top-active-users:
    description: "Top N users by usage-event count in the last 7 days"
    scope: single
    mode: fetch
    output: table
    parameters:
      - { name: limit, type: integer, default: 10, position: 1, description: "Number of rows" }
    sql: |
      SELECT u.name, COUNT(*) AS events
        FROM usage e
        JOIN users u ON u.id = e.user_id
       WHERE e.created_at > NOW() - INTERVAL '7 days'
       GROUP BY u.name
       ORDER BY events DESC
       LIMIT $limit
```

Save and quit. Confirm it loads:

```bash
▶ uv run dbctl operations validate
all 12 operations valid
```

Run it:

```bash
▶ uv run dbctl pg top-active-users 5
```

You just extended the CLI's vocabulary without touching Python. Note that
the same operation would run against `my` too *if* the SQL were
dialect-portable — `INTERVAL '7 days'` is PG, so on `my` you'd write
`DATE_SUB(NOW(), INTERVAL 7 DAY)` instead. For cross-dialect operations,
the v1 workaround is one op per dialect + scope via
`safety.allowed_operations`.

---

## 12. Point dbctl at real AWS RDS via SSM

Now for real infrastructure. The `pg-ssm` entry in the sample config is a
reference template — let's turn it into a working production connection.

Edit `~/.dbctl/connections.yaml`. Update the `pg-ssm` block to point at
your real bastion + RDS endpoint:

```yaml
pg-ssm:
  description: "Production Postgres (private RDS via SSM)"
  aliases: [prod]
  type: ssm
  driver: postgresql+psycopg
  database: app
  username: app_admin
  password_env: DBCTL_PG_SSM_PASSWORD
  ssm:
    region: eu-west-1
    profile: prod                          # AWS SSO profile; tokens in ~/.aws/cache
    # Two equivalent ways to identify the bastion:

    # Option A — tags (recommended): survives bastion replacement.
    # dbctl calls `aws ec2 describe-instances --filters ...` at tunnel-open
    # time and picks the running instance matching these tags.
    bastion_tags: { Name: bastion-prod, Env: prod }

    # Option B — instance id (fast path, no AWS lookup):
    # bastion_instance_id: i-0abcd1234ef

    remote_host: mydb.xxxx.eu-west-1.rds.amazonaws.com
    remote_port: 5432
    local_port: 0                           # 0 = dbctl picks a free local port
  healthcheck: { query: "SELECT 1" }
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

Three things happening here that match what real ops teams want:

1. **Your SSO session is reused.** `dbctl` shells out to `aws ssm
   start-session` with the `prod` profile, so you don't re-authenticate.
   You need an active `aws sso login --profile prod` session; the cached
   token in `~/.aws/cache/sso/*.json` is enough.
2. **Tags beat instance ids.** `bastion_tags: { Name: bastion-prod, Env: prod }`
   is resolved at tunnel-open time via `aws ec2 describe-instances`, so
   if your bastion is in an ASG and gets replaced, the tag set stays stable
   while the instance id rotates. You can switch to `bastion_instance_id`
   any time — they're mutually exclusive in the schema.
3. **`local_port: 0`** lets `dbctl` discover a free port *before* invoking
   the SSM document — there's no API to read back the port SSM auto-picks,
   so pre-discovery avoids the race entirely.

Set the password and test:

```bash
▶ export DBCTL_PG_SSM_PASSWORD='<real-rds-password>'
▶ uv run dbctl pg-ssm health
```

If you see `OK pg-ssm (4.0ms)` you're connected to production.

Run anything that works against `pg`:

```bash
▶ uv run dbctl pg-ssm list-users 10
▶ uv run dbctl pg-ssm info active_conns
```

Try a DML op without `--apply` first to preview:

```bash
▶ uv run dbctl pg-ssm add-user ci-runner 200 --show-sql
```

And if you decide to commit:

```bash
▶ uv run dbctl pg-ssm add-user ci-runner 200 --apply --yes
```

Because `prod` is an alias:

```bash
▶ uv run dbctl prod health
```

---

## 13. Hold a tunnel open for ad-hoc work

Sometimes you want to drop into `psql` or run something `dbctl` doesn't
declare yet. `dbctl tunnel open <conn>` opens the tunnel and holds it until
Ctrl-C, printing the local bind you should connect to:

```bash
▶ uv run dbctl tunnel open pg
tunnel open: 127.0.0.1:5433 -> pg
Ctrl-C to close...
```

In another shell, use that local bind with your favourite client:

```bash
PGPASSWORD=$DBCTL_PG_PASSWORD psql -h 127.0.0.1 -p 5433 -U app_admin -d app
```

The tunnel is torn down cleanly when you Ctrl-C the `dbctl tunnel open`
process — an `atexit` fallback covers hard kills.

---

## 14. Use `dbctl init` to add the next connection

Typing YAML by hand gets old. `dbctl init` walks you through adding a
connection interactively and writes/merges it into `~/.dbctl/connections.yaml`:

```bash
▶ uv run dbctl init
connection name: reports-pg
description (optional): read replica
aliases (comma-separated, optional): reports
tunnel type [ssm/ssh/direct]: ssh
driver (sqlalchemy url scheme) [postgresql+psycopg]:
database name: reports
username: reporter
password source [env/prompt]: env
bastion host: bastion.example.com
user [ec2-user]: ops
identity file path [~/.ssh/id_rsa]: ~/.ssh/reports_rsa
remote host (db endpoint): db.internal
remote port [5432]: 5432
local port (0 = auto) [0]: 0
healthcheck query [SELECT 1]:
prompt for confirmation before DML (recommended for prod)? [Y/n]: y
make this connection read-only? [y/N]: y
wrote /home/you/.dbctl/connections.yaml
  run: dbctl reports-pg health
```

`init` will also try to connect and healthcheck for `direct` connections
before saving, so you can't accidentally save a broken config.

---

## 15. Add a connection alias & use `--profile`

Two quality-of-life features for managing many connections:

### Aliases

Both `dbctl reports-pg health` and `dbctl reports health` work — the
`aliases: [reports]` line in the YAML means `reports` resolves to
`reports-pg`.

### Profiles

If your team keeps separate configs per environment, drop them under
`~/.dbctl/profiles/<name>/`:

```text
~/.dbctl/
├── connections.yaml             ← default profile
├── operations.yaml
└── profiles/
    ├── prod/
    │   ├── connections.yaml    ← `dbctl --profile prod ...`
    │   └── operations.yaml
    └── staging/
        ├── connections.yaml
        └── operations.yaml
```

`dbctl --profile prod pg health` reads from `~/.dbctl/profiles/prod/` —
the active profile propagates to connections, operations, and the audit
log (`--profile prod` writes to
`~/.dbctl/profiles/prod/history.jsonl`).

---

## 16. Cleanup

```bash
▶ docker compose down
```

That's it — you've used the dashboard, doctor, single-DB operations
(dry-run + apply), the audit log, multi-DB diff, written your own
operation, and connected to a real RDS Postgres through an SSM bastion.

## Where to go next

- [`connections.yaml` reference](connections.md) — every field of every
  block, with `ssm` / `ssh` / `direct` examples.
- [`operations.yaml` reference](operations.md) — every mode, every
  parameter type, and the full safety check matrix.
- [`DESIGN.md`](DESIGN.md) — why the CLI is dynamic, why confirms happen
  before transactions, why placeholder rewriting is regex-based.
- [`CHANGELOG.md`](../CHANGELOG.md) — what's in 0.6.0 (multi-DB copy/sync/
  validate/replay + table_counts + operation-first CLI) and what's planned
  for 0.7 (bidirectional sync, identifier interpolation, multi-statement
  scripts).