# dbctl for AI agents (context / ask / MCP)

`dbctl` has three surfaces designed for LLM-driven operation, all built on
the same foundation as the CLI: the declared registries, the
`tunnel → engine → healthcheck` plumbing, the safety gates, and the audit
log. No surface can write SQL that isn't either a declared operation or a
human-reviewed draft, and no surface ever sees a credential.

| surface | what it does | who drives it |
|---|---|---|
| `dbctl context` | emits a markdown context pack (catalogs + optional live schema) | you paste it into any chat |
| `dbctl ask` | routes a natural-language request to a declared operation | you, in the terminal |
| `dbctl mcp serve` | exposes the registries as MCP tools | an MCP client (Claude, opencode, Cursor, …) |

## `dbctl context` — the context pack

```bash
dbctl context                    # connections + operations catalogs (markdown)
dbctl context pg                 # + live schema of `pg`: tables, columns, keys,
                                 #   foreign-key links, indexes, views, dialect
dbctl context pg --no-sql        # omit each operation's SQL body
dbctl context -o pack.md         # write to a file instead of stdout
```

The pack contains everything an assistant needs to *drive* dbctl: the
connection catalog (type, driver, database, read-only flag — never
passwords), every operation with its parameter schema and SQL, and — for a
named connection — the SQLAlchemy-inspector view of that database. It is
the offline equivalent of the MCP tools below.

## `dbctl ask` — the natural-language router

```bash
dbctl ask "top 5 users on pg"
dbctl ask "find user 'alice' on pg"
dbctl ask "add user zelda with 100 credits on pg" --apply
dbctl ask "list users" --conn pg --op list-users   # force the target
```

`ask` picks **one declared operation** and fills its declared parameters —
it never writes SQL itself. Routing is offline by default (token/fuzzy
scoring against operation names, descriptions and parameters; connection
inference from mentions). Optional LLM routing:

```bash
export DBCTL_LLM_PROVIDER=anthropic        # or: openai
export ANTHROPIC_API_KEY=...               # or: OPENAI_API_KEY
dbctl ask "who has the most credits on pg" --llm     # route via the API
dbctl ask "top 5 users on pg" --no-llm               # force offline
```

`DBCTL_LLM_MODEL`, `DBCTL_LLM_BASE_URL` (any OpenAI-compatible endpoint)
and `DBCTL_LLM_TIMEOUT` refine it; auto mode uses the LLM only when
configuration is present and falls back to heuristics on failure. The
prompt carries only the secret-free catalogs.

Execution goes through the exact CLI safety path: the plan prints first
(connection, operation, masked params, resolved SQL), missing required
parameters are prompted for, DML dry-runs until `--apply`, and every run
is audited.

## `dbctl mcp serve` — the MCP server

```bash
how dbctl mcp serve               # stdio server; register it with a client
dbctl mcp serve --allow-write # opt in to commits (still gated, see below)
dbctl mcp serve --actor claude
```

Client config (Claude Desktop / opencode / Cursor):

```json
{"mcpServers": {"dbctl": {"command": "dbctl", "args": ["mcp", "serve"]}}}
```

### Tools

| tool | purpose |
|---|---|
| `list_connections` | connection catalog (metadata only, no credentials) |
| `list_operations` | operation catalog with parameter schemas + SQL |
| `get_schema` | SQLAlchemy-inspector introspection + authoring guide |
| `draft_operation` | validate an LLM-authored operation, save as a draft |
| `run_operation` | run one declared single-scope operation |
| `health` | open a connection and run its healthcheck |

Error payloads carry the CLI's stable `exit_code` semantics (1 SQL, 2
unknown name/param, 3 tunnel, 5 health, 6 safety gate) so agents can
branch on failure reason.

### Authoring new operations from the schema

This is the workflow the inspector enrichment serves: an agent can look at
a database it has never seen and propose a *correct* operation for it,
while a human stays in the loop.

1. **`get_schema(connection, table?)`** returns the dialect (db type,
   driver, server version), tables with columns (name / type / nullable /
   primary key), foreign-key links (`columns → ref_table(ref_columns)`),
   indexes (with unique flag), and views — plus an **`authoring`** guide:
   - the `$name` placeholder rule (dbctl rewrites `$name` to a bound
     parameter; values are never interpolated),
   - the single/multi mode vocabulary,
   - an example `operations.yaml` entry shape,
   - **dialect-specific SQL hints** — pagination, upsert syntax,
     RETURNING-equivalents and date arithmetic for `postgresql`, `mysql`,
     `sqlite` and `mssql`, so the drafted SQL is correct for that engine.
2. The agent writes an operations.yaml entry following the guide.
3. **`draft_operation(operation_yaml)`** validates it against the pydantic
   `Operation` schema (typos fail loudly, `extra="forbid"`), cross-checks
   `$placeholders` against declared parameters (undeclared → rejected,
   unused → warning), and persists it under
   `~/.dbctl/drafts/<name>.yaml`.
4. **Drafts are never active.** A human reviews the draft and moves it
   into `operations.yaml` — only then does it become a CLI subcommand and
   an MCP tool.

### Write policy over MCP

Agent-facing policy is deliberately stricter than the CLI: **any** DML
(`execute` / `upsert` / `script`) is a dry-run preview unless the caller
passes `apply=true` **and** the server was started with `--allow-write`.
Read-only connections and `allowed_operations` whitelists apply in
addition. Every run — including dry-runs and blocked attempts — is
appended to `~/.dbctl/history.jsonl` with secret-typed parameters
redacted.

Full tool reference, client setup (opencode / Claude / Cursor), and the
operation-authoring loop: [`docs/mcp.md`](mcp.md).
