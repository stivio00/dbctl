# Changelog

All notable changes to this project will be documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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