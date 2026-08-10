# `dbctl execute` — ad-hoc SQL reference

<img src="logo_small.png" alt="dbctl" width="120">

`dbctl execute` (added in v0.7.6) runs ad-hoc SQL through `dbctl`'s
existing tunnel / safety / audit plumbing without requiring the SQL to be
declared in `operations.yaml`. It's the CLI counterpart of the SQL editor
inside `dbctl ui` — exploratory / break-glass path. For repeatable,
parameterised work, the declarative `operations.yaml` model remains the
recommended path (it keeps "what can be run against this DB" discoverable
from a versioned file).

## Synopsis

```text
dbctl execute -c <conn-or-url> [-o table|json|csv|yaml]
              [--apply] [-y|--yes] [--show-sql] "<sql>"
```

| flag           | effect |
|----------------|--------|
| `-c`, `--connection` | **required**. Either a connection name / alias from `connections.yaml`, or a full SQLAlchemy URL (detected by the presence of `://`). Inline URLs build a transient `direct` Connection so the SSM/SSH password-source plumbing, native-lib install hints, and `connect_args` all apply uniformly. |
| `-o`, `--output`     | `table` (default) / `json` / `csv` / `yaml` — output format for SELECT-shaped results. Ignored for DML. |
| `--apply`            | Commit any DML. Default is a dry-run preview when `safety.confirm` is on. SELECT runs are always read-only; `--apply` has no effect on them. |
| `-y`, `--yes`        | Skip the final `[y/N]` confirmation prompt before DML commit. |
| `--show-sql`         | Print the SQL before executing. |
| `<sql>` (positional) | The SQL to run, as a single shell-quoted argument. |

## SQL classification

The SQL is auto-classified by its leading keyword (case-insensitive,
leading `(` stripped). The following verbs run as a query (rendered via
`--output`):

- `SELECT`, `WITH` (CTE), `VALUES`, `TABLE` — SQL-standard row-set constructors.
- `SHOW`, `EXPLAIN`, `DESC`, `DESCRIBE`, `PRAGMA` — dialect-specific
  introspection verbs that return a result set the user wants to render like
  a SELECT.

Anything else (`INSERT` / `UPDATE` / `DELETE` / `CREATE` / `DROP` /
`ALTER` / `GRANT` / `TRUNCATE` / `MERGE` / `CALL` / ...) runs as DML inside
`engine.begin()` and reports rows-affected with a green `OK` line. DDL
whose `rowcount` the driver reports as `-1` (`CREATE TABLE`, `DROP`,
`ALTER`, ...) is rendered as `OK in <ms>ms` instead of the misleading
`OK -1 row(s) affected`.

If the SQL begins with a dash (e.g. `-- my SQL comment`), separate the
options from the positional with `--` so Click doesn't treat the leading
dash as an unknown option:

```bash
dbctl execute -c pg -- "-- my SQL comment"
dbctl execute -c pg -- "-SELECT ..."
```

## Safety model

DML runs through the same gate as a declared `mode: execute` operation:

- `safety.read_only: true` → DML is rejected with exit code 6.
- `safety.confirm: true` (the default in the sample config) → DML is a
  **dry-run by default**: the SQL is printed (with `--show-sql`) and the
  audit log records a `dry-run` status, **unless** `--apply` is passed.
  `--yes` then skips the final `[y/N]` confirm prompt.
- `safety.allowed_operations` whitelist is *not* enforced on `dbctl
  execute` (it's binding named operations, not ad-hoc SQL); `confirm` /
  `read_only` *are* enforced.

For an **inline SQLAlchemy URL**, the transient Connection defaults to
`safety.confirm: true` so the same dry-run-by-default guard applies
unless the URL is overridden. There is no `--no-confirm` flag — pass
`--apply --yes` for a fully non-interactive DML run.

## Audit log

Every `dbctl execute` run is appended to `~/.dbctl/history.jsonl` as
`operation="execute"` with:

- `connection`: the canonical connection name (e.g. `pg`), or the literal
  `<inline>` token for inline URL runs.
- `params.sql`: the SQL (truncated to 500 chars).
- `mode`: `fetch` for SELECT-shaped SQL, `execute` for DML.
- `status`: `ok` / `dry-run` / `error`, mirroring declared-op audit entries.
- `rows_affected`: the driver-reported row count for DML; `null` for queries.
- `duration_ms`: total wall-time for the SQL execution.
- `actor`: `$USER` (or `$USERNAME` on Windows), same as declared ops.

```bash
dbctl history list                # ad-hoc runs show up alongside declared ops
dbctl pg history                 # per-connection audit log skips <inline> entries
```

## Examples

```bash
# SELECT rendered as JSON
dbctl execute -c pg -o json "SELECT * FROM users LIMIT 10"

# duckdb inline URL — render YAML
dbctl execute -c "duckdb:/:memory:" -o yaml "SELECT 1 AS one"

# CSV to a file, no TTY
dbctl execute -c pg -o csv "SELECT * FROM users" > users.csv

# DML dry-run preview (default for confirm: true connections)
dbctl execute -c pg "DELETE FROM users WHERE name='bob'"
# → "dry-run (use --apply to commit)"

# Commit DML without a prompt (scripted)
dbctl execute -c pg --apply --yes "DELETE FROM users WHERE name='bob'"

# Schema introspection through SELECT-only verbs
dbctl execute -c sqlite "SELECT name FROM sqlite_master WHERE type='table'"
dbctl execute -c pg -o json "SELECT relname, n_live_tup FROM pg_stat_user_tables ORDER BY 2 DESC LIMIT 10"
dbctl execute -c my "SHOW TABLES"
dbctl execute -c ms "EXEC sp_who"   # stored proc: routed as DML, reports OK

# SQL beginning with a dash — uses `--` to separate options from the positional
dbctl execute -c pg -- "-- comment; SELECT 1"
```

## Connection resolution

- **Inline URL:** any string containing `://` is treated as a SQLAlchemy
  URL. It's wrapped in a transient `direct` Connection so `build_engine`
  honours the URL's host:port as-is, and the existing
  `_check_driver_available` / `connect_timeout` / Windows SSO plumbing all
  apply. The placeholder `direct` block `{host: localhost, port: 1}` is
  required by the `Connection` validator even in URL mode, but is never
  *used*.
- **Named connection:** everything else is resolved via the alias-aware
  `connections.resolve()` (so aliases and the canonical name both work,
  with "did you mean?" hints on a typo). The standard `opened_conn()` ctx
  manager opens the tunnel + engine + healthcheck exactly as a declared
  op would.

## Roadmap / limitations

- `$name` placeholder rewriting (the `to_bindparams` rewriter declared
  operations use) is **not** applied to ad-hoc SQL — that mechanism keys
  off the declared parameter list. Use raw SQLAlchemy `:name` bind params
  and pass values via Python scripting if you need parameterised ad-hoc
  SQL today; a `--param key=value` flag is on the roadmap.
- Multi-statement scripts run as the first statement only (matching
  `mode: script` v1 behaviour); v2 multi-statement support will land
  with the declared `mode: script` upgrade.
- The audit `params.sql` truncates to 500 chars; long DML bulk inserts
  may be clipped. The audit entry's `connection`/`status`/`duration_ms`
  are always preserved.