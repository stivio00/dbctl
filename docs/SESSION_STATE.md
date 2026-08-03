# dbctl — session state

<img src="logo_small.png" alt="dbctl" width="120">

Last active: 2026-08-01
Project: `~/Projects/dbctl/`

## Where we are

`dbctl` is a v1-complete, lint-clean, 26-tests-passing Python CLI for
monitoring + administering multiple databases via SSM / SSH / direct
connections, with declared operations in YAML. Built with `uv`, click,
pydantic v2, sqlalchemy (no boto3 / paramiko).

The last pass added a 689-line step-by-step tutorial (`docs/tutorial.md`)
linked from the README. No code touched in that last step.

## What's done

- `pyproject.toml` (uv + hatchling; ruff + pytest + mypy config)
- `dbctl/` package: cli.py, config.py, connections.py, operations.py,
  tunnels/{ssm,ssh,direct}.py, db.py, execute.py, multi.py, reports.py,
  audit.py, runtime.py, init.py, __main__.py
- `tests/` — 26 tests; `test_smoke.py` (15) + `test_bastion_tags.py` (11)
- `docker-compose.yml` — postgres :5433, mysql :3307, mssql :1434
- `seed/{postgres,mysql,mssql}.sql` — users/credits/usage/logs schema + sample data
- `.dbctl/{connections,operations}.yaml` — sample config with pg/my/ms direct
  + pg-ssm and pg-ssh reference templates
- docs/: README.md (links to tutorial), CHANGELOG.md, DESIGN.md,
  connections.md, operations.md, tutorial.md, ACTION_OUTPUT.md
- Verified: `uv run ruff check dbctl tests` → All checks passed!
  `uv run pytest tests/` → 26 passed

## Known limitations (v1, deliberate)

- `mode: script` runs only the first statement (multi-statement is v2)
- `mode: upsert` reserved — autoload/dialect-aware conflict logic is v2
- Operations are dialect-specific (no per-connection overrides)
- `${var}` identifier interpolation intentionally absent (SQL-injection vector
  with naive str.replace)
- Multi-DB tunnels open sequentially (parallel is v2)
- SSH `connect_args` for mssql ODBC driver not wired into the config schema

## What I would pick up next

- Run the docker compose fleet end-to-end (`docker compose up -d` needs sudo
  in the sandbox; the operator should do this on a real host). README + tutorial
  section 2 have the exact commands.
- Set up CI: GitHub Actions workflow running `uv sync`, `ruff check`,
  `pytest`, `mypy` on push.
- Add `mode: script` multi-statement splitting (respecting quotes / dollar-quoting).
- Add `mode: upsert` autoload + dialect-aware `ON CONFLICT` / `ON DUPLICATE KEY`.
- Add `connect_args` to the Connection config schema so mssql ODBC driver
  selection can be configured without editing code.
- Multi-file operations loader (`.dbctl/operations/<name>.yaml`).
- Tag-based operation filtering.
- Parallel multi-DB tunnel open.

## Common verification one-liners

```bash
cd ~/Projects/dbctl
uv sync --extra dev
uv run ruff check dbctl tests        # → All checks passed!
uv run pytest tests/                  # → 26 passed
uv run dbctl --help
uv run dbctl operations validate     # → all N operations valid
uv run dbctl diff user-count --help
uv run dbctl pg add-user stephen 12 --show-sql   # dry-run preview
uv run dbctl history list
```

## File map

```
~/Projects/dbctl/
├── pyproject.toml
├── README.md                         # quick start + install + safety model
├── CHANGELOG.md                      # 0.1.0 release notes
├── docker-compose.yml
├── seed/{postgres,mysql,mssql}.sql
├── .dbctl/{connections,operations}.yaml
├── docs/
│   ├── tutorial.md                   # 16-section walkthrough ← START HERE
│   ├── connections.md                # connections.yaml reference
│   ├── operations.md                 # operations.yaml reference
│   ├── DESIGN.md                     # architecture + design decisions
│   └── ACTION_OUTPUT.md              # bug-fix audit table from second pass
├── dbctl/
│   ├── cli.py                        # LazyConnGroup + dynamic per-op commands
│   ├── config.py                     # pydantic v2 models (Connection, Operation, ...)
│   ├── connections.py                # loader + alias resolution
│   ├── operations.py                 # loader + scope filter
│   ├── tunnels/{base,ssm,ssh,direct}.py
│   ├── db.py                         # engine + healthcheck
│   ├── execute.py                    # $name → :name, mode routing, bind_params
│   ├── multi.py                      # per-role engine open + query runner
│   ├── reports.py                    # rich tables + side-by-side diff
│   ├── audit.py                      # history.jsonl
│   ├── runtime.py                    # opened_conn() ctx-mgr
│   └── init.py                       # dbctl init wizard
└── tests/
    ├── test_smoke.py                 # 15 tests: placeholders, binding, modes, audit
    └── test_bastion_tags.py          # 11 tests: ssm bastion_tags resolution
```

## Key design decisions to remember

- **Operation-first multi-DB** (preferred since v0.6.0): `dbctl user-count pg my`
  — the deprecated verb-first alias `dbctl diff user-count pg my` still works
- **Confirm before transaction**: `_execute_single` calls `confirm_or_abort`
  *before* `with engine.begin():` so N / Ctrl-C leaves the DB untouched
- **Dry-run by default** for DML on `safety.confirm: true` connections;
  `--apply` commits, `--yes` skips the prompt
- **`$name` placeholders** rewritten to SQLAlchemy `:name` (always parameterised)
- **No boto3 / paramiko**: shell out to `aws` and `ssh` binaries on PATH
- **`extra="forbid"`** on every pydantic model so typos fail fast
- **`ssm.bastion_tags`** and `bastion_instance_id` are mutually exclusive;
  tags are resolved via `aws ec2 describe-instances` with
  `instance-state-name=running` always added, JSON `--filters` form, warning
  on multi-match

## Sample config that's currently installed

`~/.dbctl/connections.yaml` is a copy of the repo's `.dbctl/connections.yaml`
with 5 connections: `pg`, `my`, `ms` (direct), `pg-ssh`, `pg-ssm` (reference
templates, read-only). `~/.dbctl/operations.yaml` is a copy with 6 operations:
`add-user`, `list-users`, `find-user`, `report-logs`, `user-count`,
`compare-credits`.