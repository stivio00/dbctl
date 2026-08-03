# Changelog

All notable changes to this project will be documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.2] — 2026-08-03

### Added

- **`url:` full SQLAlchemy connection string** — a second way to declare a
  connection's DB credentials alongside the individual-field mode
  (`driver` / `database` / `username` / `password` / …). When `url:` is
  set, `build_engine` uses the URL as-is and does **not** inject the
  tunnel's local bind — the URL's own host:port wins. The config
  validator rejects any overlap between `url:` and the individual
  credential fields with a clear message. Use this for Azure AD auth,
  ODBC-specific query params, or any non-standard connection string.
- **`windows_sso: true`** credential source for `mssql+pyodbc` — sets
  `Trusted_Connection=yes` in the ODBC connect args and omits
  `username` / `password` from the URL entirely. Mutually exclusive
  with `password` / `password_env` / `prompt` and only valid with
  `mssql+pyodbc`; config validator enforces all three.
- **Init wizard** now offers two paths: full SQLAlchemy URL or
  individual fields. In individual-field mode the driver prompt is a
  `click.Choice` of the four bundled drivers (`postgresql+psycopg`,
  `mysql+pymysql`, `mariadb+pymysql`, `mssql+pyodbc`); for mssql the
  password source prompt includes `windows-sso` as a fourth option.
- **`logo_small.png`** bundled in the wheel + sdist alongside `logo.png`;
  added to the top of every `docs/*.md` page and as a centred footer in
  the README (the big logo stays at the top).

### Changed

- `Connection.driver` and `Connection.database` are now optional
  (`str | None`) — required only when `url:` is not set. The config
  validator enforces this with a clear message.
- `Connection.username` is `str | None` (was `str`) — omit when
  `windows_sso: true`.
- `dbctl.db._driver_name()` extracts the driver scheme from `conn.url`
  when `conn.driver` is `None`, so `_check_driver_available` and
  `_connect_args` work in full-URL mode.
- `dbctl.db.build_engine()` uses `sqlalchemy.make_url()` when
  `conn.url` is set; otherwise assembles the URL from the individual
  fields + tunnel bind (unchanged).

### Fixed

- Loader error message for missing password source now mentions
  `windows_sso: true` as a fourth option.
- Sample `.dbctl/connections.yaml` includes `azure-sql` (full-URL
  reference template) and `mssql-sso` (Windows SSO reference template),
  both `read_only: true` so accidental runs are safe.

## [0.6.1] — 2026-08-03

### Changed

- **Renamed `quota` → `credits`** across all docs, examples, sample
  operations, seed SQL (postgres/mysql/mssql), and test fixtures. The
  table `quotas` is now `credits`, columns `quota_daily` / `quota_yearly`
  are `credits_daily` / `credits_yearly`, operations `increase-quota` /
  `compare-quotas` / `reset-quota` are `increase-credits` /
  `compare-credits` / `reset-credits`. Historical CHANGELOG entries
  retain the old names.
- **Replaced country-based tenant examples** (Germany / USA / India,
  `prod-de` / `prod-us` / `int-in`) with generic `tenant1` / `tenant2` /
  `tenant3` in the README intro.
- **Docs audit**: fixed 14 stale references across README, tutorial,
  operations.md, DESIGN.md, and SESSION_STATE.md — wrong install path
  (`uv tool install ./dbctl` → `.`), stale version (0.5.3 → 0.6.0),
  stale test count (15/26 → ~80), stale operation count (7 → 12),
  stale "verb-first" framing (now operation-first preferred), missing
  `password` credential source in DESIGN.md, stale "v1" / "v2"
  reservation notes, stale SQL Server note.

### Fixed

- **`run_copy` skipped-count math** for `on_conflict: skip` was
  double-counting because `skipped += len(chunk) - inserted` used the
  cumulative `inserted` accumulator instead of the per-batch return.
  Each batch now computes its own delta.
- **`_insert_batch` rowcount** always returned `len(rows)` regardless of
  how many rows the driver actually wrote — so `INSERT IGNORE` /
  `ON CONFLICT DO NOTHING` reported every duplicate as inserted. Now uses
  `cursor.rowcount` when available.
- **MySQL schema-qualified introspection** — `_introspect_tables` treated
  MySQL's current database as a non-default schema, breaking
  `validate-schema` (0 tables compared). Fixed by including the engine's
  own `url.database` in the default-schema set.
- **SQLAlchemy errors escaped as tracebacks** — the multi-op dispatch
  and single-op execute paths now catch `SQLAlchemyError` and render via
  `fmt_db_error`, stripping the driver-class prefix and
  `Background on this error` trailer.
- **YAML parse errors rendered as multi-line dumps** — both loaders now
  collapse `YAMLError` into one friendly line with line:column prefix.
- **`operations validate` dumped raw `ValidationError`** — now catches
  `OperationsFileError` and prints per-op friendly lines.

## [0.6.0] — 2026-08-03

### Added

- **Multi-DB operation-first CLI** — every multi-scope operation now has a
  top-level command `dbctl <op> <src-conn> <trg-conn> ...` (preferred) in
  addition to the deprecated verb-first alias `dbctl <verb> <op> ...`.
  Operation-first commands emit no deprecation notice; the verb-first
  groups (`dbctl diff <op> ...`, `dbctl copy <op> ...`, etc.) keep working
  for existing scripts but print Click's standard deprecation warning
  pointing at the operation-first form. The root `MultiCommand` now
  exports the multi-op names alongside the deprecated verb-first aliases
  so shell completion lists both shapes.
- **`mode: copy`** — bulk-copy rows from `src` to `trg`, table by table,
  in batches. Cross-dialect (rows travel through Python `dict`s via
  SQLAlchemy mappings and are re-inserted via `executemany` on the
  destination engine). Declared via `copy_spec:` (`batch_size`,
  `tables`, `on_conflict`, `where`, `truncate_first`). `--on-conflict
  truncate` is a CLI shortcut for `truncate_first: true` + `on_conflict:
  error`. Conflict handling is dialect-aware: Postgres `ON CONFLICT DO
  NOTHING` / `DO UPDATE`, MySQL `INSERT IGNORE` / `ON DUPLICATE KEY
  UPDATE`, MSSQL per-row `NOT EXISTS` guard (no native `ON CONFLICT`).
  `--dry-run` reads src + simulates inserts and writes nothing to trg.
- **`mode: sync`** — converge `trg` to match `src` for one `target_table`:
  insert missing rows, update differing non-key columns, optionally
  delete trg-only rows (with `--delete-extras`). Both `queries.src` and
  `queries.trg` must SELECT the same column shape; `sync_spec.key`
  identifies rows. Direction is src → trg only (make-trg-match-src);
  bidirectional merge with conflict arbitration is out of scope.
  `--dry-run` reports insert/update/delete counts without writing.
- **`mode: validate`** — structural schema diff via SQLAlchemy
  `inspect()`: compares columns (name + type) for each table present in
  both schemas. `validate_spec.tables` lists tables explicitly, or null
  to introspect the intersection. `include` / `exclude` are column-name
  filters. A clean schema prints a green `✓`; mismatches render as a
  per-column table tagged `missing_in_trg` / `missing_in_src` /
  `type_mismatch`. The audit `status` is `ok` (no drift) or `drift`.
- **`mode: replay`** — bulk-copy with a per-row Python transform applied
  before writing to trg. Reuses the `copy` machinery; the transform
  resolves to a `Callable[[dict], dict]` via `"identity"` (no-op) or
  `"package.module:callable"` import path. Useful for redacting PII,
  normalising enums, or computing derived columns before the insert.
- **`diff.strategy: table_counts`** — auto-generate
  `SELECT '<t>' AS t, COUNT(*) AS n FROM <t>` per declared table per
  role, so the common "row counts of every table" diff no longer needs
  per-table SQL boilerplate. No `queries:` block required. (NOTE: the
  `["*"]` introspection shorthand is only supported on `copy` / `replay`
  — list tables explicitly for `table_counts`.)
- **`SyncSpec` / `CompareSpec` / `ValidateSpec` / `ReplaySpec`** config
  schemas in `dbctl.config`, so `operations.yaml` validates the new
  modes at load time. `OpMode` extended with `copy` / `validate` /
  `replay`. `DiffSpec` gained a `strategy` field (`custom` /
  `table_counts`).
- **Operations loader resilience** — `operations.load()` now validates
  each operation independently (mirroring the connections loader). A
  single mis-declared op no longer rejects the whole registry; the bad
  ones are reported via a new `OperationsFileError` with a one-line
  per-op message (stripped of pydantic's `ValidationError` boilerplate
  and `errors.pydantic.dev` trailer), and the good ones keep loading so
  `--help` / `dbctl operations list` / dashboard still render.
- **`fmt_db_error`** helper in `dbctl.db` that renders a clean
  one-line message from a SQLAlchemy / DBAPI error: strips the
  `(<driver>.<ExceptionName>)` prefix and the
  `(Background on this error at: https://sqlalche.me/...)` trailer.
  The multi-op dispatch + single-op execute paths now route SQLAlchemy
  errors through this so a dropped connection mid-copy or a wrong
  password surfaces as plain English (e.g. `password authentication
  failed for user "app_admin"`) instead of a multi-line traceback.
  `healthcheck` likewise peels the
  `connection to server at ..., port N failed: FATAL: ...` preamble to
  keep only the meaningful reason.
- **`Makefile`** with standard dev/release targets: `make help`, `lint`,
  `format`, `typecheck`, `check`, `check-strict`, `test`, `test-cov`,
  `smoke`, `chaos`, `docker-up` / `docker-down` / `docker-logs` /
  `docker-reset`, `clean`, `build`, `check-uv-lock`, `publish-test`,
  `publish`, `install`, `venv`, `install-config`.
- **Logo** (`docs/logo.png`) bundled into the wheel and sdist as
  `dbctl/logo.png` (hatchling `force-include`); the README renders it as
  a centred banner above the title.
- **Bundled sample operations** — `.dbctl/operations.yaml` now ships
  showcase examples for every v0.6.0 mode (`copy-users`, `sync-users`,
  `validate-schema`, `replay-users`, `table-counts`).
- **Regression tests** for the new modes (operation-first dispatch,
  copy / sync / validate / replay runtimes, dry-run paths, transform
  resolution) and for the loader-resilience + error-formatting fixes.

### Changed

- **Minimum Python is 3.12.** `pyproject.toml`'s `requires-python` and
  ruff/mypy `target-version` bumped from `py311` to `py312`; the 3.11
  classifier is dropped.
- **`Operation` model validator** now branches on mode for multi-scope:
  `diff` / `compare` need `queries` (per role) unless
  `diff.strategy: table_counts`; `copy` needs `copy_spec` and roles
  `[src, trg]` when introspecting; `sync` needs `sync_spec` +
  `queries.src` + `queries.trg`; `validate` / `replay` need their
  respective specs (queries not required).
- `dbctl operations validate` now catches `OperationsFileError`
  specifically, prints the per-op friendly lines, and bumps the exit
  code to 1 — instead of dumping a raw `ValidationError`. The
  `_check_multi_mode` block's outdated v2-stub reservation note ("the
  CLI rejects attempts to invoke them with a clear message") is
  fulfilled: the runtime now implements all three modes rather than
  rejecting them.
- README + `docs/operations.md` rewritten to document every v0.6.0
  mode, the operation-first CLI form, the dry-run flag matrix for the
  write modes, and the `make`-based dev workflow. The roadmap no longer
  lists `compare` / `sync` as reserved.

### Fixed

- **Schema-qualified table introspection on MySQL/MariaDB** —
  `_introspect_tables` treated MySQL's current database (e.g. `app`) as
  a non-default schema, so every MySQL table came back as `app.users`
  while Postgres stayed bare. This broke `validate-schema` (0 tables
  compared) and the introspected variants of `copy` / `replay` because
  the cross-DB set operations mismatched. The default-schema set now
  also includes the engine's own `url.database`, so MySQL yields bare
  `users` just like Postgres.
- **`run_copy` skipped-count math** for `on_conflict: skip` was
  double-counting because `skipped += len(chunk) - inserted` used the
  cumulative `inserted` accumulator instead of the per-batch return
  value. Each batch now computes its own delta against
  `_insert_batch`'s return.
- **`_insert_batch` rowcount** always returned `len(rows)` (the batch
  size) regardless of how many rows the driver actually wrote — so
  `INSERT IGNORE` / `ON CONFLICT DO NOTHING` reported every duplicate
  as inserted and skipped=0. Now uses `cursor.rowcount` when the
  driver reports it (and falls back to `len(rows)` if the driver
  returns -1 / unknown), so `--on-conflict skip` reports the real
  inserted-vs-skipped split.
- **Schema-qualified table introspection on MySQL/MariaDB** —
  `_introspect_tables` treated MySQL's current database (e.g. `app`) as
  a non-default schema, so every MySQL table came back as `app.users`
  while Postgres stayed bare. This broke `validate-schema` (0 tables
  compared) and the introspected variants of `copy` / `replay` because
  the cross-DB set operations mismatched. The default-schema set now
  also includes the engine's own `url.database`, so MySQL yields bare
  `users` just like Postgres.
- **`run_replay` dropped `--batch-size`.** `run_replay` ignored the
  caller's `batch_size` kwarg; the `_do_replay` dispatcher now forwards
  it so `dbctl replay-users pg my --batch-size 200` actually overrides
  the spec default.
- **`dbctl` SQLAlchemy / DBAPI errors escaped as raw tracebacks** in
  the multi-op dispatch and the single-op execute path when a
  connection went down mid-run (or with a wrong password). The
  dispatchers now catch `SQLAlchemyError` alongside `RuntimeError` and
  render the message via `fmt_db_error`, which strips the
  `(<driver>.<ExceptionName>)` prefix and the
  `(Background on this error at: https://sqlalche.me/...)` trailer.
  `healthcheck` likewise peels the
  `connection to server at ..., port N failed: FATAL: ...` preamble to
  keep only the meaningful reason (e.g. `password authentication failed
  for user "app_admin"`).
- **`operations validate`** dumped a raw pydantic `ValidationError` for
  a broken YAML op. It now catches `OperationsFileError` specifically,
  prints one clean per-op line and exits 1 — no `errors.pydantic.dev`
  trailer, no multi-screen stack trace.
- **YAML parse errors escaped as multi-line `yaml.scanner.ScannerError`
  dumps** with line/column carets and quote markers. Both loaders
  (`connections.load`, `operations.load`) now collapse a `YAMLError`
  into one friendly line via `_friendly_yaml_error`, prefixing the file
  location, e.g. `<file>: YAML parse error:3:1: found character '\t'
  that cannot start any token`. The CLI surfaces this through the same
  `ConnectionsFileError` / `OperationsFileError` path used for
  pydantic-validation failures, so a malformed `connections.yaml` /
  `operations.yaml` no longer reaches the user as a four-line
  traceback.
- **Plaintext `password:` source** round-trips end-to-end against the
  live docker fleet — a connection declared with `password: pwd_postgres`
  (no `password_env`, no `prompt: true`) loads cleanly, resolves to the
  inline value at load time, and `dbctl doctor` / `<op>` invocations
  work without an env var being set. (Already documented since v0.4.0;
  this release adds regression coverage so a future refactor that
  accidentally required an env var would fail the suite.)

### Known limitations

- `validate` compares column types as SQLAlchemy-flavoured strings
  (e.g. `VARCHAR(120)` vs `TEXT`); it does not normalise type aliases.
  Use `include` / `exclude` to silence noise from intentionally
  divergent storage types.
- `replay` doesn't yet ship a `--transform` CLI flag override — edit
  the operation YAML for now. A `--transform` flag is on the v0.7 list.
- `table_counts` diff strategy does not support `tables: ["*"]`
  introspection like `copy` / `replay` do — list the tables explicitly.
- The bundled `ms` (SQL Server) connection is `read_only: true` and
  requires `ODBC Driver 18 for SQL Server` on the host; the docker
  smoke tests in this release ran against `pg` + `my`.

## [0.5.3] — 2026-08-01

### Fixed

- **`$name::type` casts broke psycopg** (the `::type` suffix collides with
  the bind-param parser). `to_bindparams` now rewrites the Postgres cast
  idiom to the SQL-standard `CAST(:name AS type)` form, so the same
  operation YAML works cross-dialect. Parenthesised precision
  (`$x::numeric(10, 2)`) and schema-qualified types (`$x::pg.text`) are
  preserved. The bundled `report-logs` operation (which uses
  `$since::timestamp`) now runs instead of failing with
  `syntax error at or near ":"`.
- **`dbctl connections show <alias>`** reported "unknown connection" for a
  registered alias (e.g. `postgres`) because `connections_show` did a direct
  dict lookup instead of going through the alias-aware `resolve()`. Now
  resolves aliases to the canonical connection and dumps it under the
  canonical name.
- **`dbctl diff <op> <conn> <unknown>`** crashed with a raw
  `UnknownConnectionError` traceback. The multi-op callback now catches the
  `KeyError` and emits a one-line message on stderr with exit code 2.
- **Negative positional arguments** (e.g.
  `dbctl pg increase-quota alice -10`) failed with the opaque Click error
  `No such option '-1'`. Dynamic commands now append a hint pointing at the
  `--` separator and showing the user's actual token (`-10`).

### Added

- **`increase-quota`** bundled operation — bumps a user's daily and yearly
  quota by a percentage (positive or negative via `--`), with postgres
  `::integer` rounding.
- **README + docs** rewritten to reflect the tool's actual thesis — a
  one-command replacement for the DBeaver multi-click flow when
  administering a database explosion (multiple environments × tenants)
  through SSO + SSH tunnels. Adds a "Why not DBeaver?" comparison, documents
  the `password` / `password_env` / `prompt` credential sources (the docs
  previously claimed there was no plaintext-password field, which was
  wrong), the loader-resilience guarantee (one bad connection no longer
  nukes the registry), the `$name::type` CAST syntax, and the
  negative-positional-arg `--` hint.

## [0.5.2] — 2026-08-01

### Fixed

- **connections.yaml loader** validated the whole file with a single
  pydantic `ConnectionsFile.model_validate` call, so one mis-configured
  connection (e.g. a reference template with all password sources
  commented out) rejected the entire registry and surfaced a multi-screen
  `ValidationError` dump. The loader now validates each connection
  individually: good connections load and serve the CLI as before, bad
  ones are reported concisely via the new `ConnectionsFileError` (one
  short sentence per offending connection, stripped of pydantic's
  `Value error, ` wrapper). `runtime.registries` catches it and still
  returns the valid subset so the dashboard and `--help` keep working.
- **`dbctl --version`** reported a hardcoded `0.1.0` instead of the
  installed package version (PyPI was at `0.5.1`). `__version__` now
  resolves dynamically via `importlib.metadata.version("dbctl")`,
  falling back to `0.0.0+unknown` for a source checkout without install.
- **Driver load errors** for native shared-library failures (e.g.
  `pyodbc` installed but `libodbc.so.2` missing at the OS level) escaped
  as raw Python tracebacks. `_check_driver_available` now catches
  `ImportError` too and raises a concise `DBError` with an actionable
  install hint (apt/dnf/brew commands) for the common `libodbc` and
  `libpq` cases.

### Changed

- `.dbctl/connections.yaml` reference templates (`pg-ssm`, `pg-k8s`,
  `pg-ssh`) now ship with `password: "<set-me>"` placeholders uncommented,
  so the sample registry validates cleanly out of the box and the loader
  no longer warns about them on every CLI invocation.

## [0.5.1] — 2026-08-01

### Fixed

- **CI workflow** referenced the removed `postgres` / `mysql` extras in
  the `test` job's `uv sync` line, causing every matrix run to fail with
  `error: Extra 'postgres' is not defined`. Synced to `--extra dev`
  only (DB drivers are now core dependencies).
- **CI workflow** created `reports/` only implicitly via pytest output;
  on a hard test failure before the first XML write the
  `upload-artifact` step errored with "No files were found with the
  provided path: reports/". The directory is now created up-front, so
  the always-on artifact upload never 404s.
- Bumped `actions/setup-uv` from `v3` (Node 20, deprecated on GH runners)
  to `v4`, and standardised the lint / build jobs' setup Python on 3.13
  to match the matrix's newest supported version.

## [0.5.0] — 2026-08-01

### Changed

- **DB drivers are now regular dependencies.** `psycopg[binary]`,
  `pymysql`, and `pyodbc` moved from optional `postgres` / `mysql` /
  `mssql` extras into the core `dependencies` list, so a plain
  `pip install dbctl` (or `uv tool install dbctl`) installs every
  dialect out of the box. The three extras are removed. The `dev`
  extra (pytest / ruff / mypy) is unchanged. Docs, tutorial, and the
  `_check_driver_available` install hint were updated to drop the
  `dbctl[<pkg]` extra syntax.

## [0.4.0] — 2026-08-01

### Added

- **Plaintext `password` field on connections** as a third credential
  source, alongside `password_env` and `prompt`. The three are mutually
  exclusive. Intended for local-dev convenience against the docker-compose
  fleet; the `password_env` / `prompt` paths remain the recommended
  options for any non-trivial environment. The sample `.dbctl/connections.yaml`
  now ships with `password:` set for the three dev connections and the
  `password_env:` line kept as a commented reference. Reference tunnel
  templates (ssm / k8s / ssh) carry commented `password:` / `password_env:`
  / `prompt:` placeholders instead. `dbctl init` now offers a `plain`
  password source option.

## [0.3.0] — 2026-08-01

### Added

- **`k8s` tunnel type** via `kubectl port-forward`. Reaches databases
  exposed as Services or Pods inside a Kubernetes cluster — useful for
  StatefulSets / operators (CloudNativePG, Postgres Operator, etc.).
  Requires the `kubectl` CLI on PATH; kubeconfig resolution is left
  untouched so existing EKS / GKE / k3s auth flows work. Config fields:
  `context` (required), `namespace` (optional), `target`
  (`svc/<name>` or `pod/<name>`), `remote_port`, `local_port`.
- **`dbctl doctor` dependency check** — beyond the per-connection
  healthchecks, doctor now prints an "optional dependencies" table for
  `kubectl`, `aws`, and `ssh`. A tool is shown as `required by config:
  yes` only when at least one configured connection uses the
  corresponding tunnel type, so a direct-only repo won't surface noise.

## [0.2.1] — 2026-08-01

Pipeline smoke release — verifies the trusted-publishing flow end-to-end
on a fresh tag. No code changes.

## [0.2.0] — 2026-08-01

First trusted-published release. Project created on PyPI by the manual
v0.1.1 upload; from this release on, tag pushes publish via OIDC without
any stored token.

## [0.1.1] — 2026-08-01

Retag of the initial public release (v0.1.0 publish failed; the trusted
publisher had not yet been registered on PyPI). No code changes — only
the version bump.

## [0.1.0] — 2026-08-01

Initial public release.

### Added

- **Connection registry** (`~/.dbctl/connections.yaml`) validated with pydantic
  v2, supporting three tunnel types:
  - `ssm` — AWS SSM port-forward through an EC2 bastion, shelled out to the
    `aws` CLI. Supports `bastion_instance_id` or `bastion_tags` (resolved via
    `aws ec2 describe-instances`).
  - `ssh` — classic `ssh -N -L` port-forward, shelled out to the `ssh` CLI
    with `ExitOnForwardFailure=yes` and `StrictHostKeyChecking=accept-new`.
  - `direct` — no tunnel, connect to upstream host:port.
- **Operations registry** (`~/.dbctl/operations.yaml`) with two scopes:
  - `single` — `dbctl <conn> <op> ...` with `execute` / `fetch` / `fetch_one` /
    `script` / `upsert` modes.
  - `multi` — `dbctl diff <op> <src> <trg> ...` with `diff` mode (side-by-side
    join on a `key`); `compare` / `sync` modes reserved for v2.
- **Dynamic CLI** built from the registries: one Click subcommand per
  connection, one per declared operation. Positional params via `position:`,
  keyword params via `--flag`, all generated from the YAML declaration.
- **`$name` placeholders** rewritten to SQLAlchemy bind-params (`:name`) —
  values are always parameterised, never string-interpolated.
- **Safety model**:
  - `safety.confirm: true` makes DML **dry-run by default**; `--apply`
    commits, `--yes` skips the prompt. Confirm happens *before* the
    transaction opens so `N` leaves the DB untouched.
  - `safety.read_only: true` blocks every DML op.
  - `safety.allowed_operations: [...]` whitelists op names.
- **Audit log** at `~/.dbctl/history.jsonl` — one JSON event per run; secret
  parameters redacted. `dbctl history list` / `dbctl <conn> history` /
  `dbctl <conn> again` (re-run last).
- **Dashboard** — `dbctl` bare shows connections table; `dbctl <conn>` shows
  a connection page with health, info queries, and available ops.
- **`dbctl doctor`** — healthcheck every connection.
- **`dbctl init`** — interactive wizard that writes/merges a new connection
  and tests the tunnel + healthcheck before saving.
- **Shell completion** via `dbctl --install-completion bash|zsh|fish`.
- **`--profile <name>`** — swap config dir to `~/.dbctl/profiles/<name>/`.
- **Connection aliases** — `prod` resolves to `db1`, etc.
- **Bundled test fleet** — `docker-compose.yml` brings up postgres on
  `:5433`, mysql on `:3307`, mssql on `:1434` with the same four-table
  schema (`users`, `quotas`, `usage`, `logs`) and slightly different sample
  data for diff testing.
- **15 unit tests** against in-memory SQLite covering placeholder rewriting,
  parameter binding/coercion, mode routing, side-by-side diff, and audit
  redaction.

### Documentation

- `README.md` — usage, install, quick start, config layout, safety model.
- `docs/connections.md` — full `connections.yaml` reference with examples.
- `docs/operations.md` — full `operations.yaml` reference with the safety
  check matrix.
- `docs/DESIGN.md` — architecture, layering, dynamic CLI, placeholder
  semantics, confirm-before-commit invariant, exit codes.

### Known limitations (v1)

- `mode: script` runs only the first statement in v1; multi-statement
  scripts are v2.
- `mode: upsert` is reserved — the autoload/dialect-aware conflict logic is
  v2.
- Operations are dialect-specific (no per-connection overrides).
- `${var}` identifier interpolation is intentionally absent (would be a SQL
  injection vector with naive `str.replace`); use one op per table for now.
- Multi-DB tunnels open sequentially (parallel is v2).