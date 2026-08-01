# dbctl — build summary & audit

This file is the human-readable record of the second-pass review + the
documentation work performed on 2026-08-01. It is *not* read by the tool; it
exists for the operator to skim "what was done, what was checked, what is
broken, what is left".

## What this pass did

1. **Audited every source file** for correctness bugs.
2. **Fixed 13 concrete issues** listed below.
3. **Re-ran lint + tests** after each fix to catch regressions
   (`uv run ruff check dbctl tests` → all clean; `uv run pytest tests/` →
   15 passed).
4. **Re-verified the CLI surface** end-to-end (`dbctl --help`, `dbctl diff
   user-count --help`, `dbctl diff compare-quotas --help`, `dbctl pg
   add-user --help`, `dbctl pg add-user stephen 12 --show-sql` — dry-run path
   all green).
5. **Wrote the documentation set**: `README.md`, `docs/DESIGN.md`,
   `docs/connections.md`, `docs/operations.md`, `CHANGELOG.md`.

## Bugs fixed in this pass

| # | file                | severity   | bug                                                                        | fix |
|---|---------------------|------------|----------------------------------------------------------------------------|-----|
| 1 | `dbctl/cli.py`      | **critical** | `_execute_single` ran `with engine.begin():` (which commits) *before* the `confirm_or_abort` prompt — DML was already persisted by the time the user said N. | Moved the confirm **before** `engine.begin()` so `N` / Ctrl-C leaves the DB untouched. |
| 2 | `dbctl/db.py`       | high       | `healthcheck` used `__import__("time").monotonic()` instead of a normal import. | `import time` at top of file. |
| 3 | `dbctl/db.py`       | high       | `create_engine` doesn't import the dialect driver at build time (lazy import), so the `try/except ModuleNotFoundError` block was dead code — a missing driver surfaced as a wrapped error inside `healthcheck` instead of the helpful hint. | Added `_check_driver_available()` that imports the dialect's package eagerly (psycopg/pymysql/pyodbc) and raises a clean `DBError` with the install command. |
| 4 | `dbctl/db.py`       | medium     | `healthcheck` accessed `e.orig` directly, which is `None` for some `SQLAlchemyError` subclasses (e.g. bind-param `StatementError`). | Use `getattr(e, "orig", None)`, fall back to `str(e)`. |
| 5 | `dbctl/execute.py`  | medium     | Same `e.orig` issue as #4 in `_render`'s `except SQLAlchemyError`. | Same guard. |
| 6 | `dbctl/tunnels/ssm.py` | medium   | `bastion_tags` was accepted by the config validator but never resolved to an instance ID — the SSM command would get an empty `--target`. | Added `_resolve_bastion_id()` that shells out to `aws ec2 describe-instances --filters Name=tag:K,Values=V …` when only tags are provided. |
| 7 | `dbctl/tunnels/ssh.py` | low      | `identity: ~/.ssh/id_rsa` was passed verbatim to `ssh -i`; `~` is not always expanded by the binary depending on the build. | `os.path.expanduser(self.conn.identity)` before building the command. |
| 8 | `dbctl/cli.py`      | medium     | `dbctl tunnel open <name>` used `if name not in conns` and didn't resolve aliases — `dbctl tunnel open prod` (an alias) failed with "unknown connection". | Use `connections.resolve()` and report the canonical name in the success line. |
| 9 | `dbctl/cli.py`      | medium     | The `tunnel open` SIGINT handler used a generator-throw hack (`lambda *_: (_ for _ in ()).throw(SystemExit(0))`) which is fragile. | Replaced with a plain `try/except KeyboardInterrupt` around the sleep loop — Ctrl-C exits cleanly and tears the tunnel down via `finally`. |
| 10 | `dbctl/multi.py`   | low        | `run_role`'s error message referenced `op.scope!r` ("operation OpScope.multi has no query for role 'src'") which is confusing. | Now reports declared roles and the keys actually present in `queries`. |
| 11 | `dbctl/runtime.py`  | high       | `registries()` propagates `ValidationError` / `yaml.YAMLError` straight up, which broke `dbctl --help` and `dbctl` (dashboard) entirely on a malformed YAML. | `registries()` now catches, prints once to stderr, and returns empty registries so `--help` and the dashboard still render and the operator sees the error in context. |
| 12 | `dbctl/cli.py`      | medium     | `operations_validate` was a no-op — it printed "all N operations valid" without re-loading or catching errors (validation happened implicitly at load time, so a *new* error made `--help` crash instead of being reported by `validate`). | Now re-loads `operations.yaml` from disk via `load_operations(path=...)`, catches and reports each error, and additionally surfaces ops whose `sql` / `queries` are inconsistent with their `mode` / `scope`. Added a `--strict` flag for CI. |
| 13 | `dbctl/cli.py`     | **UX**     | Multi-DB used `--src` / `--trg` flags — `dbctl diff user-count --src pg --trg my`. The agreed UX is verb-first positional connections: `dbctl diff user-count pg my`. | Refactored `_make_multi_group` + `_make_multi_op_command` so each role becomes a leading positional Argument (matching `roles` in declared order), followed by the op's own positional/keyword params. `dbctl diff compare-quotas pg my Daily` now works. The audit log records the joined connection names. |

## Bonus cleanups

- `pyproject.toml`: removed `pydantic-settings` (unused), `tabulate` (we use
  `rich.table`), and the empty `ssh` extra. Added `[tool.pytest.ini_options]`
  so `uv run pytest` finds `tests/` and runs quietly by default.
- `.dbctl/connections.yaml`: the mssql `direct: { host: "127.0.0.1,1434" }`
  entry used ADO-style comma syntax which produces a wrong SQLAlchemy URL
  (`mssql+pyodbc://…@127.0.0.1,1434:5432/…`). Split to `host: 127.0.0.1`,
  `port: 1434` with a doc-comment about ODBC driver install.
- `dbctl/db.py`: removed the unused `DRIVER_HINTS` dict (the new
  `_check_driver_available` builds its own message inline).

## Things checked but **not** fixed (intentional)

- **`$1` / `$$ ... $$` dollar-quoting**: `to_bindparams()` only matches
  `$<identifier>`, so `$1` (positional params) and `$$ ... $$` (PG
  dollar-quoting) are untouched. This is documented in
  `docs/operations.md` with the warning to avoid mixing dollar-quoting with
  `$name` placeholders in a single operation. A real fix would need a
  tokenizer; out of scope for v1.
- **`mode: script` single-statement**: v1 routes `script` through the same
  `text()` path as `execute`, so multi-statement scripts only run the first
  statement. Documented in CHANGELOG + roadmap.
- **`mode: upsert` placeholder**: `_render` raises `RuntimeError` for
  `mode: upsert`; the autoload logic is v2.
- **Operations are dialect-specific**: `ON CONFLICT` is PG; `ON DUPLICATE
  KEY` is MySQL. There is no per-connection operations override in v1; use
  `safety.allowed_operations` to scope an op to the dialect it targets.
  Documented in `docs/operations.md`.

## Verification commands

```bash
cd ~/Projects/dbctl
uv sync --extra dev --extra postgres --extra mysql
uv run ruff check dbctl tests        # → "All checks passed!"
uv run pytest tests/                  # → 15 passed in ~0.4s
uv run dbctl --help                   # → root help with dynamic commands
uv run dbctl operations validate      # → "all 6 operations valid"
uv run dbctl diff --help              # → "Multi-connection `diff` operations"
uv run dbctl diff user-count --help   # → "Usage: dbctl diff user-count [OPTIONS] SRC TRG"
uv run dbctl pg add-user stephen 12 --show-sql   # → dry-run, prints resolved SQL
uv run dbctl history list             # → audit row with status: dry-run
```

Docker isn't reachable from the build sandbox without `sudo`, so the live
postgres/mysql/mssql end-to-end run is left to the operator (instructions in
`README.md`).

## What is left for the operator

- Bring up the docker fleet (`docker compose up -d`) and run the README's
  "Quick start" section to verify SSM/SSH/direct against real databases.
- For real AWS SSM: drop a `prod-pg` connection in
  `~/.dbctl/connections.yaml` pointing at a real bastion + RDS endpoint,
  set `DBCTL_PROD_PG_PASSWORD`, and `dbctl prod-pg health` should work.
- For real SSH: same, with an `ssh:` block pointing at a real bastion and a
  key on disk (`identity`).
- File any bugs against this todo's checkboxes; the implementation is
  intended to be a complete v1 starting point, not a finished product.