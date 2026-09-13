# AGENTS.md

Guidance for AI coding agents (and humans) working on dbctl.

## What this is

dbctl is a Python CLI that monitors/controls/administers many databases through one
declarative config. Two YAML registries in `~/.dbctl/` (or `--profile` dirs):

- `connections.yaml` — how to reach a DB (tunnel spec + driver + safety), pydantic models in `dbctl/config.py`
- `operations.yaml` — what to run once connected (parameterized SQL, `$name` bind placeholders), same file

The CLI (`dbctl/cli.py`) synthesizes one Click subcommand per connection × single-scope
operation. It is **not** an ORM, migration, or provisioning tool — raw parameterized SQL
via SQLAlchemy `text()`, plus multi-DB diff/copy/sync/validate/replay orchestration
(`dbctl/multi.py`).

## Commands

```
make check         # ruff + pytest — the blocking pre-commit gate; run this before finishing
make check-strict  # adds mypy (strict, advisory — pre-existing annotation debt fails this)
make lint          # ruff check dbctl tests
make test          # pytest (in-memory SQLite, no docker needed)
make format        # ruff check --fix + ruff format
make install       # uv sync --extra dev
```

The project uses `uv` (lockfile `uv.lock` committed). Run anything via `uv run <cmd>`.
If you change `pyproject.toml` dependencies, `uv lock` and commit the updated lockfile.

## Architecture map

| Module | Role |
|---|---|
| `cli.py` | Root Click group; dynamic connection/op command synthesis; `_execute_single` is THE single-op execution path (safety gates, audit, rendering) |
| `config.py` | Pydantic v2 models for both registries; `extra="forbid"` everywhere |
| `connections.py` / `operations.py` | Loaders + name resolution. Loader resilience: invalid entries are skipped with an error, valid ones still load (`*FileError.valid`) |
| `runtime.py` | `registries(ctx)`, `opened_conn()` (tunnel+engine+healthcheck in one context manager) |
| `execute.py` | `$name` → `:name` bind rewrite (never string interpolation), `bind_params` coercion, `render()` mode dispatch |
| `multi.py` | Multi-connection orchestration (diff/compare/copy/sync/validate/replay); `opened()` = ctx-free tunnel+engine |
| `db.py` | URL building, password resolution, healthcheck, `fmt_db_error` |
| `tunnels/` | `Tunnel` Protocol + `build_tunnel()` factory; tunnels shell out to `aws`/`ssh`/`kubectl`/`az`/`gcloud` binaries on purpose (user SSO/MFA flows keep working) |
| `audit.py` | Append-only `~/.dbctl/history.jsonl` |
| `reports.py` | rich/json/csv/yaml renderers |
| `ui/` | Textual TUI sharing the same safety/audit plumbing |
| `catalog.py` | Secret-free registry summaries (shared by context/ask/mcp) |
| `context.py` | `dbctl context` — markdown LLM context pack |
| `ask.py` | `dbctl ask` — natural-language router (offline heuristics + optional LLM); never writes SQL, only picks declared ops |
| `mcp_server.py` | `dbctl mcp serve` — MCP tools over the registries (`mcp` is a core dependency; supports mcp 1.x and 2.x): catalogs, inspector schema with dialect authoring guide, `draft_operation` → `<config>/drafts/` (never active), safety-gated `run_operation` |

## Invariants — do not break these

1. **Safety model**: per-connection `safety.confirm` (DML dry-runs until `--apply`),
   `safety.read_only`, `safety.allowed_operations`. Confirmation happens BEFORE the
   transaction opens. Exit code 6 for any safety gate. Any new execution path
   (CLI, TUI, MCP, ask) must go through equivalent gates and must audit.
2. **Exit codes**: 0 ok, 1 SQL failure, 2 unknown name/bad param, 3 tunnel, 4 driver,
   5 healthcheck, 6 safety gate. Scripts and agents branch on these.
3. **No string-interpolated SQL.** Params go through `execute.bind_params` /
   `to_bindparams`. `$1` and `$$…$$` must stay untouched.
4. **No secrets in output**: passwords/`password_env`/`url` never appear in catalogs,
   context packs, LLM prompts, or non-redacted audit entries (`ParamType.secret`).
5. **Loader resilience**: one bad YAML entry must not take down the whole registry.
6. **Pydantic `extra="forbid"`** on every model — typos in YAML must fail loudly.
7. **The MCP stdio server must keep stdout clean** — nothing may print to stdout
   (use stderr or return structured payloads).

## Conventions

- Python ≥3.12, ruff line length 110, rules `E/F/I/UP/B/SIM` (`B904` ignored).
- Match types/match-case idioms are used throughout; `from __future__ import annotations` everywhere.
- Heavy imports are lazy (inside functions/callbacks) to keep `import dbctl.cli` light.
- Click callbacks pop their flags by name; dynamic commands are built in `_make_*_command` factories.
- Rich `console`/`err_console` from `dbctl.runtime` — errors to stderr, data to stdout.
- New user-facing modules get module docstrings explaining the why (see existing files).

## Testing

- `tests/` runs on in-memory SQLite only — no docker, no network, no live tunnels.
- MCP tests import the real `mcp` package (core dep) and call `build_server().call_tool` directly.
- `dbctl ask` tests monkeypatch `ask._chat` for the LLM path; heuristic path is pure logic.
- To point a test CLI run at a config: `monkeypatch.setenv("HOME", tmp_path)` and write
  `tmp_path/.dbctl/{connections,operations}.yaml` (registry paths resolve via `Path.home()`).
- `make smoke` runs the docker fleet (postgres/mysql/mssql) — only when docker is up.

## Adding things

- **New CLI command**: define it in `cli.py` (lazy imports in the callback), add to
  `_root_list` static list + `_root_get` static dict.
- **New tunnel type**: implement the `Tunnel` protocol in `tunnels/`, add a pydantic
  model in `config.py`, wire the match arm in `Connection._check` + `build_tunnel`.
- **New operation mode**: enum in `OpMode`, dispatch in `execute.render` (single) or
  `multi.py` + a `_do_*` in `cli.py` (multi), validator arm in `Operation._check_multi_mode`.
- **AI surfaces** (context/ask/mcp): share `catalog.py` for registry summaries; route
  execution through the existing safety/audit plumbing; extend `tests/test_ai_*.py`.
