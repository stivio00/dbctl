# `dbctl mcp serve` — the MCP server

`dbctl mcp serve` is a **stdio MCP server** that exposes your two
registries (`connections.yaml` + `operations.yaml`) as tools, so an MCP
client — Claude Desktop, opencode, Cursor, or any MCP SDK — can inspect
your databases and run your **declared** operations through the exact same
safety gates and audit trail as the CLI. The `mcp` package is a core
dependency (since 0.8.2): nothing extra to install.

In one sentence: **the LLM can understand a connection and author new
operations in natural language, but it can only *execute* what a human has
declared — and writes stay dry-run until a human opts in.**

```bash
dbctl mcp serve                  # stdio server, read + dry-run DML
dbctl mcp serve --allow-write    # also allow DML commits (apply=true still required)
dbctl mcp serve --actor claude   # tag audit entries with who is driving
dbctl --profile <dir> mcp serve  # serve a non-default profile dir
```

## Client setup

### opencode

Project-local (`.opencode/opencode.json`) or global
(`~/.config/opencode/opencode.json`):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "dbctl": {
      "type": "local",
      "command": ["dbctl", "mcp", "serve"],
      "enabled": true
    }
  }
}
```

Notes:

- `command` is an array of strings — never a single string.
- To serve a profile instead of the default `~/.dbctl`, insert the flag
  before the subcommand: `["dbctl", "--profile", "work", "mcp", "serve"]`
  (or pass an absolute config dir path).
- Add `"--allow-write"` to the args to opt in to DML commits.
- Config is loaded at startup — restart opencode after editing.

### Claude Desktop

`~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "dbctl": {
      "command": "dbctl",
      "args": ["mcp", "serve", "--allow-write"]
    }
  }
}
```

### Cursor

`.cursor/mcp.json` in the project:

```json
{
  "mcpServers": {
    "dbctl": {
      "command": "dbctl",
      "args": ["mcp", "serve"],
      "env": {}
    }
  }
}
```

### Generic (any MCP SDK)

The server speaks MCP over stdio; launch it and speak the protocol:

```
command: dbctl
args:    [mcp, serve]
```

## The tools

| tool | purpose |
|---|---|
| `list_connections` | registry catalog — metadata only, never credentials |
| `list_operations` | every declared operation with its parameters + SQL |
| `get_schema` | live schema of one connection + an authoring guide |
| `draft_operation` | validate an LLM-authored operation, save as **draft** |
| `run_operation` | execute a declared operation through the safety gates |
| `health` | run a connection's healthcheck |

### `list_connections`

No arguments. Returns `{connections: [{name, description, type, driver,
database, read_only, tunnel_target, allowed_operations, …}]}` — the
secret-free catalog shared with `dbctl context` and `dbctl ask`.
Passwords, `password_env` values, and full URLs never appear.

### `list_operations`

No arguments. Returns `{operations: [{name, description, scope, mode,
confirm, parameters: [{name, type, required, position, …}], sql}]}`.

### `get_schema`

```json
{"connection": "pg"}
```

Optional: `"table": "users"` (one table, full column detail),
`"max_tables": 100`.

Runs the SQLAlchemy inspector over a **live** connection and returns:

- `tables` — name, columns (name, type, nullable, default), primary keys,
  foreign keys (as `links`), indexes
- `views`
- `dialect` — engine name, driver, database, server version
- `authoring` — the authoring guide (below)

### `draft_operation`

```json
{"operation_yaml": "deactivate-user:\n  description: …\n  sql: |\n    UPDATE …"}
```

The **operation-authoring** tool. Takes a single YAML mapping of one
operation name to its fields (a bare `{name: {…}}` or the full
`operations:` envelope — both accepted). It validates against the
Operation schema (typos fail loudly, `extra="forbid"`), cross-checks
`$placeholders` against the declared parameters, and writes
`<config dir>/drafts/<name>.yaml`.

**Drafts are never active.** The human reviews the file and moves it into
`operations.yaml` (or deletes it). A draft does not appear in
`list_operations` and cannot be run until activated.

### `run_operation`

```json
{"connection": "pg", "operation": "increase-credits",
 "params": {"name": "zelda", "pct": 5}}
```

Optional: `"apply": true`.

Runs a declared operation through the same path as the CLI: connection
resolve → tunnel → engine → healthcheck → safety gates → execute → audit.

- `fetch` / `fetch_one` operations return rows immediately.
- DML (`execute` / `upsert` / `script` with `confirm: true`) runs as a
  **dry-run** by default: the response carries the fully resolved SQL and
  commits nothing.
- `"apply": true` commits — but only if the server was started with
  `--allow-write`. Otherwise the response is `{"status": "blocked", …}`.
- Connection-level gates still apply: `read_only` connections refuse
  writes, `allowed_operations` whitelists what may run at all.

### `health`

```json
{"connection": "pg"}
```

Runs the configured healthcheck query. `exit_code` follows the CLI
convention: 0 ok, 5 healthcheck failure, and error payloads elsewhere
carry 2 (unknown name), 3 (tunnel), 4 (driver), 6 (safety gate).

## The authoring loop (how an LLM adds operations)

This is the workflow the server is built around — an assistant can *write*
new operations, but a human *activates* them:

1. **Understand the database** — `list_connections`, then `get_schema`
   for the target connection. The `authoring` block carries:
   - `placeholder_style` — SQL uses `$name` placeholders bound via
     SQLAlchemy `text()` binds; never string interpolation
   - `modes` — `fetch | fetch_one | execute | script | upsert` (single
     scope) and the multi-scope modes
   - `example_entry_yaml` — a complete, valid entry to copy
   - `dialect_hints` — SQL quirks for the connection's engine (upsert
     syntax, parameter casting, quoting, …)
2. **Author** the YAML for a new operation following that guide.
3. **`draft_operation`** it — validation errors come back as
   `{"status": "error", "exit_code": 2, …}`; success writes the draft.
4. **Human review** — read `<config dir>/drafts/<name>.yaml`, then move it
   into `operations.yaml`. It is now a CLI command (`dbctl pg <op>`) and
   runnable via `run_operation`.

## Safety model

| gate | behavior |
|---|---|
| DML dry-run | `execute`/`upsert`/`script` with `confirm: true` return resolved SQL, commit nothing, unless `apply=true` |
| write switch | `apply=true` is honored only when the server was started `--allow-write`; otherwise `blocked` |
| connection gates | `safety.read_only` refuses writes; `safety.allowed_operations` whitelists runnable ops |
| drafts | never active; activation is a human file edit |
| audit | every run appends to `~/.dbctl/history.jsonl` with secret-typed params redacted; `--actor` tags the driver |
| secrets | never in any tool response |

Error responses carry the CLI's exit-code semantics in `exit_code`:
0 ok · 1 SQL failure · 2 unknown name/bad param · 3 tunnel · 4 driver ·
5 healthcheck · 6 safety gate.

## Protocol notes

- **stdio only** — the server keeps stdout clean for the protocol stream;
  diagnostics go to stderr or structured payloads.
- Works with both the `mcp` 1.x and 2.x SDK lines (the server class is
  selected at import time).
- Tools are session-scoped to the profile the server was started with;
  changing registries means restarting the server (or activating drafts
  and restarting).
