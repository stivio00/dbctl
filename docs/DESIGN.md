# Design notes for dbctl

This document captures the *why* behind the project's architecture, so future
contributors can extend it without re-litigating settled decisions.

## Goals

1. **One tool, many databases.** A single declarative config describes every
   database the operator cares about. The CLI surface reflects that config.
2. **No ad-hoc SQL.** Everything that touches a database is a *declared
   operation* in `operations.yaml`. This makes the tool auditable, makes
   `--help` self-documenting, and keeps secrets out of shell history.
3. **Tunnels first, by shelling out.** AWS SSM and SSH tunnels are established
   by invoking the user's own `aws` and `ssh` CLI binaries. No `boto3`, no
   `paramiko`, no `sshtunnel` library — the operator's SSO session, SSH
   agent, key rotation, and MFA all keep working unchanged.
4. **SQLAlchemy as the only DB layer.** Every supported dialect goes through
   SQLAlchemy Core / `text()`. There is no ORM, no migrations — just
   parameterised SQL against the driver the user already has installed.
5. **Safety over convenience.** DML against a connection with
   `safety.confirm: true` is a dry-run by default. `--apply` commits, `--yes`
   skips the prompt. The confirm happens **before** the transaction opens.
6. **Audit everything.** Every run is appended to `~/.dbctl/history.jsonl`;
   secret-typed parameters are redacted; `dbctl <conn> again` re-runs the
   last operation with the same params.

## Layering

```text
                    ┌──────────────┐
   Click commands ──│   cli.py     │  dynamic groups (LazyConnGroup)
                    └──────┬───────┘  per-op subcommands
                           │
        ┌──────────────────┼──────────────────┐
        ▼                  ▼                  ▼
  ┌────────────┐    ┌────────────┐    ┌────────────┐
  │ config.py  │    │ runtime.py │    │ execute.py│
  │  pydantic  │    │  opened_   │    │  $ → :    │
  │  models    │    │  conn()    │    │  bind     │
  └────────────┘    └─────┬──────┘    │  render   │
                          │           └────────────┘
              ┌───────────┴────────────┐
              ▼                        ▼
        ┌──────────┐             ┌──────────┐
        │ tunnels/ │             │  db.py   │
        │ ssm ssh  │             │ engine + │
        │ direct   │             │ health-  │
        └──────────┘             │  check   │
                                 └──────────┘
```

- **`config.py`** — pure pydantic v2 models for both registries; no I/O.
  `extra="forbid"` everywhere so a typo in the YAML fails fast.
- **`connections.py` / `operations.py`** — thin loaders that return
  validated dicts and resolve aliases.
- **`tunnels/`** — a `Tunnel` protocol (context manager yielding the local
  bind port) and three implementations. `base.py` owns free-port discovery and
  poll-waiting on the local listener; the implementations just spawn the
  subprocess and tear it down on exit (with an `atexit` fallback).
- **`db.py`** — `_check_driver_available()` imports the dialect's python
  package eagerly so a missing driver becomes a clean message instead of a
  wrapped `connect()` error.
- **`execute.py`** — `to_bindparams()` rewrites `$name` → `:name` via a
  regex that never touches `$1` positional params or `$$ ... $$` dollars.
  `bind_params()` coerces per `ParamType`, fills defaults, and errors on
  missing required values. `render()` routes by `Operation.mode`.
- **`multi.py`** — the multi-DB orchestrator opens one tunnel+engine per role
  (sequentially in v1) and runs the per-role SQL. The CLI's diff rendering
  happens in `reports.py`.
- **`runtime.py`** — `opened_conn()` is the one context manager every command
  callback uses: opens the tunnel, builds the engine, runs the healthcheck,
  and tears the tunnel down in `finally`. Failures during setup exit the
  process with stable codes (2 unknown conn, 3 tunnel, 4 db, 5 health) so
  scripts can branch on them.
- **`audit.py`** — append-only JSONL. `last_for(conn)` powers `dbctl <conn>
  again`.

## Dynamic CLI

The hardest design point is how `dbctl` knows about connection names *and*
operation names at Click-parse time, when both come from YAML that is only
loaded at runtime. The solution is two custom `click.Group` subclasses:

1. **`main`** (the root group) has its `list_commands` / `get_command`
   monkey-patched (`_root_list` / `_root_get`). When Click asks for a name,
   the resolver first checks the static commands, then the loaded connections
   (and their aliases — `prod` → `db1`), finally the multi-DB verbs
   (`diff` / `compare` / `sync`) — one group per distinct `OpMode` seen in
   the registry.
2. **`LazyConnGroup`** is constructed per resolved connection. Its
   `list_commands` merges the static per-connection commands
   (`health` / `info` / `history` / `again`) with every single-scope
   operation. Its `get_command` synthesises, on demand, a `click.Command`
   whose `params` are built from the operation's declared `parameters` —
   one `click.Argument` per positional param, one `click.Option` per
   keyword param, plus the shared `--apply` / `--yes` / `--show-sql` /
   `--output` flags.

This means a brand-new operation added to `operations.yaml` shows up under
`dbctl <conn> <TAB>` and in `dbctl <conn> --help` with no code change.

## Multi-DB operations: verb-first

A multi-scope operation declares `roles` (e.g. `[src, trg]`) and one `queries`
entry per role. The CLI builds **one top-level group per distinct `mode`** —
`diff`, `compare`, `sync` — and one subcommand per operation under it. The
role names are the **leading positional arguments** of each subcommand,
matching `roles` in declared order:

```bash
dbctl diff user-count pg my                     # roles [src, trg]
dbctl diff compare-quotas pg my Daily           # + the op's own positional
```

This is verb-first (matching `git diff A B`) and lets the op's own parameters
be declared per-op (so `--period` for `compare-quotas` is its real flag, not a
shared one). `reports.render_side_by_side` joins the two result sets on
`diff.key` and renders rows of `key | val_a | val_b | Δ`.

## Placeholder semantics

Operations use `$name` placeholders. At runtime `to_bindparams()` rewrites
them to SQLAlchemy `:name` — they are *always parameterised*, never
string-interpolated, so a parameter value can never become SQL. The regex
`\$([a-zA-Z_][a-zA-Z0-9_]*)` deliberately ignores `$1` (positional DB
params) and **cannot** see inside `$$ ... $$` dollar-quoted PostgreSQL
strings — keep dollar-quoting out of any operation that uses `$` parameters,
or write the SQL with single-quoted string literals only.

For **dynamic identifiers** (table names in `compare-row-counts`-style ops)
the v1 workaround is to write the identifiers literally into the operation's
SQL — the sample `user-count` op hard-codes `'users'` rather than templating
it. `${var}` identifier interpolation that quotes via SQLAlchemy's
`quoted_name` is on the roadmap; it is intentionally absent now because
naive `str.replace` would be a SQL-injection vector.

## Confirm-before-commit (the critical correctness invariant)

The naive placement of a confirm prompt is *after* `with engine.begin():` —
which means after the transaction has already committed. `dbctl` places it
**before** `engine.begin()`:

```python
with opened_conn(ctx, canonical) as (name, _conn, stub):
    if needs_confirm and not yes:
        confirm_or_abort(f"Apply {op_name} to {canonical}?", yes=yes)
    with stub.engine.begin() as sa_conn:
        res = render_query(sa_conn, op, bound)
    ...
```

So saying `N` (or Ctrl-C) leaves the database untouched. The dry-run path
(when `needs_confirm and not apply`) runs *no SQL at all* — it just renders
the preview and writes an audit entry with `status: "dry-run"`.

## Config-dir resolution and profiles

`~/.dbctl/` is the default config dir. `--profile <name>` swaps to
`~/.dbctl/profiles/<name>/` so you can keep dev / staging / prod configs
separated without one fat YAML. `registries()` reads from the active profile
on every call. That's intentional — `dbctl --profile prod <conn>` could not
work otherwise — but it means `dbctl --help` does read the YAML. To keep
`--help` resilient, `registries()` swallows malformed-YAML errors and prints
them once on stderr instead of crashing mid-parse.

## Error exit codes

| code | meaning                                  |
|------|------------------------------------------|
| 0    | success (incl. successful dry-run)       |
| 1    | SQL runtime error                        |
| 2    | unknown connection / operation / bad arg |
| 3    | tunnel failed to come up                 |
| 4    | DB driver / engine construction failed   |
| 5    | healthcheck failed                       |
| 6    | safety gate (read-only / not allowed)     |

## Why no boto3 / paramiko

The user explicitly asked for the AWS and SSH CLIs as deps and not their
SDKs. Shelling out:

- keeps the user's AWS SSO session cached in `~/.aws/cache/sso/*.json`
  untouched (boto3 would need a separate `boto3.session.Session(profile=…)`);
- reuses the operator's `~/.ssh/config`, agent, and key rotation without us
  reimplementing any of that;
- keeps the dependency surface to a handful of pure-Python libraries
  (click / pydantic / sqlalchemy / rich / pyyaml).

The cost is that we rely on the `aws` and `ssh` binaries being on `PATH`,
and we cannot unit-test the tunnels without mocking subprocess — which is
fine because the unit tests focus on the SQL/binding/diff/audit layers, and
tunnels are integration-tested against real SSM/SSH endpoints by hand.

## Why pydantic v2 `extra="forbid"`

A typo like `helthcheck:` in `connections.yaml` would silently fall back to
the default `SELECT 1` and mask a real misconfiguration. `extra="forbid"`
turns that typo into a load-time validation error so the operator fixes it
immediately. The same applies to operation parameters — `positon: 1` instead
of `position: 1` would otherwise make the parameter keyword-only silently.

## What this tool is *not*

- It is **not an ORM or migration tool**. No models, no Alembic. Operations
  are raw SQL parameterised through SQLAlchemy `text()`.
- It is **not an ad-hoc REPL**. To run SQL you declare it.
- It is **not a secrets manager**. The only credential sources are
  `password_env: VARNAME` and `prompt: true`.
- It is **not a genericdiff for DDL**. The `diff` mode joins *result sets* on
  a key — it diffs `SELECT COUNT(*) FROM users` between two DBs, not the
  schema of `users` itself.

## Roadmap-aware design decisions

Several decisions were made specifically so the roadmap items land cleanly:

- `Operation.namespace` exists in the model but is unused in v1. When
  `dbctl <conn> <namespace> <op>` lands, the `LazyConnGroup.get_command` will
  add one more level of indirection — no schema change needed.
- `Operation.scope` is an enum (`single` / `multi`) so adding a future
  `cluster` scope (e.g. run-against-all-connections) is a one-line addition.
- `Operation.mode` is an enum, so `sync` (planned) slots in next to
  `diff` / `compare` with no model change — just another group builder in
  `_root_get`.
- The `Operation.queries` map is keyed by role name (not position), so a
  future 3-way diff (`roles: [src, mid, trg]`) is a config change, not a code
  change.
- The `Tunnel` protocol yields `local_host` *and* `local_port`, so a future
  IPv6-aware SSM tunnel that binds to `::1` won't require touching the
  engine builder.