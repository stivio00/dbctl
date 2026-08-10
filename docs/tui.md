# `dbctl ui` reference

<img src="logo_small.png" alt="dbctl" width="120">

`dbctl ui` launches a [Textual](https://textual.textualize.io/) TUI on top of
the same `connections.yaml` / `operations.yaml` / tunnels / audit-log stack
as the CLI - it's an additional consumer of that stack, not a separate code
path with its own rules. Nothing here changes how `connections.yaml` /
`operations.yaml` are declared or validated; see
[`connections.md`](connections.md) and [`operations.md`](operations.md) for
those.

```bash
dbctl ui                    # default profile: ~/.dbctl/
dbctl --profile foo ui      # ~/.dbctl/profiles/foo/
```

## Layout

```
+- connections -------+- [ pg: sql x] [ pg: add-user ] [ + ] ---------------+
| > pg        (conn)  |  SELECT * FROM users LIMIT 100;                    |
|   v users            |                                                   |
|     id INTEGER (PK)  |  [Run] [x]                                        |
|     name TEXT        |---------------------------------------------------|
| > mssql-qa  (disc)   |  id | name  | created_at                          |
| > my-sqlite (disc)   |  1  | alice | 2026-01-02                          |
|                       |  2  | bob   | 2026-01-03                         |
+----------------------+---------------------------------------------------+
```

- **Left pane**: a connection tree that doubles as a lazily-loaded schema
  browser - nothing is queried until you expand a node.
- **Right pane**: a tabbed workspace of SQL editor / operation-launcher
  tabs, each with a results table below it.

## Connection tree

Expanding a connection node connects it first if it isn't already
(auto-connect on expand, like clicking a datasource in a DB GUI); `c` still
connects explicitly without drilling in. Two browsing depths, toggled with
`m`:

| mode     | hierarchy                                                          |
|----------|---------------------------------------------------------------------|
| simple   | `connection → table → column`                                       |
| normal   | `connection → schema → Tables/Views → table/view → Columns/Indexes` |

Activating (Enter) a table or view opens a SQL tab pre-filled with a
dialect-correct preview query using the real table name (see
[Dialect handling](#dialect-handling) below).

| key | action |
|-----|--------|
| `c` | connect the highlighted connection |
| `d` | disconnect the highlighted connection |
| `t` | test the tunnel (open + healthcheck + close, mirrors `dbctl tunnel test`) |
| `a` / `e` | add / edit a connection - opens `connections.yaml` in `$EDITOR` |
| `m` | toggle simple / normal view |
| `g` | toggle flat / grouped connection list |
| `Enter` | activate: open a SQL tab (connection) or a prefilled SQL tab (table/view) |

`a` and `e` both shell out to `$EDITOR` on the whole `connections.yaml` file
(a full in-app add/edit form is out of scope for now); on save, the registry
reloads and every open connection is disconnected (existing tabs show "not
connected" until reconnected - press `c` or re-expand the tree node).

Connect, disconnect, and test-tunnel all run in the background (tunnel
setup can spawn `aws`/`kubectl`/`ssh`/`az`/`gcloud` and take several
seconds) - the node's own label animates as a spinner while its request is
in flight, so the tree stays interactive rather than freezing.

### Grouping (`g`)

Real fleets often name connections hierarchically
(`in-gateway-ifp-dev`, `imageextractor-prod`, `lookup-test`). `g` toggles
between the flat list (default) and a grouping that treats `-` as a path
separator, so `in-gateway-ifp-dev` nests under `in-gateway` → `ifp`. A
folder that would only ever hold one child collapses into it - `azure` +
`sql` becomes a single `azure-sql` node, not an `azure` folder wrapping a
lone `sql` leaf.

A name can be both a group *and* a real connection at once - `ifp` and
`ifp-gateway` might both exist, or `lookup`/`lookup-dev`/`lookup-test`.
Such a node still shows its own connect-state icon and responds to
`c`/`d`/`t`/Enter directly; expanding it reveals a synthetic
`(this connection)` child (drill into that to browse its own schema)
alongside its sub-connections. Disconnecting a dual-purpose node only
resets its own schema - its sub-connections are independent and are left
alone.

Pure organizational folders (no real connection at that exact name) show
as `name/` and aren't connectable themselves.

## Workspace tabs

| key | action |
|-----|--------|
| `Ctrl+N` | new tab - pick a connection, and either a blank SQL editor or a declared operation |
| `Ctrl+O` | searchable operation launcher (type to filter, Enter to launch) - see below |
| `Ctrl+W` | close the active tab |
| `Ctrl+R` | run the active tab (SQL or operation) - same as clicking the ▶ toolbar icon |
| `Escape` | cancel any open modal (new-tab picker, operation launcher, confirmation) |

Each tab has a small icon toolbar (▶ run, ✕ close) instead of a big button,
and a `#editor-area` / results-table split that's independently resizable
per tab (`Ctrl+Up` / `Ctrl+Down`, or drag the bar between them). The
connection-tree / workspace split resizes the same way with
`Ctrl+Left` / `Ctrl+Right`, or by dragging the vertical bar between the two
panes.

A run replaces the results table with an animated loading indicator while
the query/operation is in flight (the DB round trip happens in a background
thread, so the rest of the UI - other tabs, the tree, resizing - stays
responsive) and shows a small status line below the results once it's
done: row count or rows-affected plus duration (`3 row(s) · 4.2 ms`,
`1 row(s) affected · 1.8 ms`, or `error · 0.6 ms`).

**Ctrl+O** picks a target connection automatically - the active tab's
connection, else whatever's highlighted in the tree, else the only
connection if there's just one - and notifies you to highlight one in the
tree if that's ambiguous. The chosen operation opens the same
operation-launcher tab as `Ctrl+N`'s "Operation" kind: a form built from the
operation's declared parameters, `Ctrl+R` to run.

**Safety and audit** behave exactly like the CLI path (`_execute_single` in
`cli.py`): `safety.read_only` blocks every DML run, `safety.confirm` (or an
operation's own `confirm: true`) shows a confirmation modal before applying,
`allowed_operations` whitelists operations by name, and every run - SQL or
operation - is appended to `~/.dbctl/history.jsonl` (`dbctl history list`
shows it), with `type: secret` parameters redacted the same way.

## Dialect handling

A SQL tab opened from the tree (or the blank default when the connection is
already known) is generated for the connection's actual dialect - resolved
from `driver:` or the scheme of a full `url:`, the same way
[`connections.md`](connections.md) documents `driver:` values.

**Row limit clause:**

| dialect                              | template |
|---------------------------------------|----------|
| postgresql, mysql/mariadb, sqlite, duckdb | `SELECT * FROM <table> LIMIT 100;` |
| mssql (`mssql+pyodbc`)                | `SELECT TOP 100 * FROM <table>;` |
| oracle (`oracle+oracledb`)            | `SELECT * FROM <table> FETCH FIRST 100 ROWS ONLY;` (12c+) |

**Identifier quoting:** a table or schema name is quoted only when it isn't
a plain lowercase identifier (`^[a-z_][a-z0-9_]*$`) - e.g. it has an
uppercase letter, since Postgres/Oracle fold unquoted identifiers to
lower/UPPER case and would otherwise miss a mixed-case table:

| dialect                              | quoting |
|---------------------------------------|---------|
| postgresql, oracle, sqlite, duckdb    | `"Name"` (ANSI) |
| mssql                                 | `[Name]` |
| mysql/mariadb                         | `` `Name` `` |

A schema-qualified table quotes each part independently, e.g. SQL Server's
`[Sales].[Users]` rather than `[Sales.Users]`.

## Known limitations

`:memory:` sqlite/duckdb connections aren't recommended through the UI:
connecting and running queries each happen on a background thread, and an
in-memory database is tied to whichever single thread touched it first
(`sqlite3`'s `check_same_thread` behavior) - a later operation on a
different thread can silently see an empty database, and disposing the
connection can log (harmless, but noisy) a driver-level warning on
shutdown. File-based sqlite/duckdb connections aren't affected. Use a real
file path for anything beyond a quick one-shot check.

## Installing

`textual` is a base dependency of `dbctl` (not an optional extra) - `dbctl
ui` is available right after a normal install, no separate step.

## Project layout

See the `dbctl/ui/` tree in the main [`README.md`](../README.md#project-layout)
for where each piece of the TUI lives (`app.py`, `connection_tree.py`,
`grouping.py`, `schema.py`, `editor_tab.py`, `operation_tab.py`,
`session.py`, `sql_templates.py`, `screens.py`, `splitter.py`, `tabs.py`,
`results.py`, `registry.py`).
