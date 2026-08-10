"""Dialect-aware SQL for a fresh editor tab: row-limiting clause and
identifier quoting.

Row limit:
  SQL Server has no `LIMIT` clause (`TOP n` instead); Oracle uses the
  SQL:2008 `FETCH FIRST n ROWS ONLY` form (Oracle 12c+, which is what the
  `oracledb` thin driver targets). Everything else dbctl supports
  (postgresql, mysql/mariadb, sqlite, duckdb) accepts `LIMIT`.

Identifier quoting:
  An unquoted identifier is case-folded by the dialect (lowercase in
  Postgres/MySQL, UPPERCASE in Oracle), so a table/schema name with any
  uppercase letter needs quoting to be addressed as declared - `"name"`
  everywhere except SQL Server (`[name]`) and MySQL/MariaDB (`` `name` ``).
"""

from __future__ import annotations

import re

from dbctl.config import Connection
from dbctl.db import driver_name

DEFAULT_ROW_LIMIT = 100

# A plain lowercase identifier never needs quoting in any of dbctl's
# supported dialects.
_PLAIN_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]*$")


def quote_identifier(conn: Connection, identifier: str) -> str:
    if _PLAIN_IDENTIFIER.match(identifier):
        return identifier
    driver = driver_name(conn)
    if driver.startswith("mssql"):
        return f"[{identifier}]"
    if driver.startswith(("mysql", "mariadb")):
        return f"`{identifier}`"
    return f'"{identifier}"'  # postgresql, oracle, sqlite, duckdb (ANSI)


def qualified_table(conn: Connection, table: str, schema: str | None = None) -> str:
    quoted_table = quote_identifier(conn, table)
    if schema:
        return f"{quote_identifier(conn, schema)}.{quoted_table}"
    return quoted_table


def default_select(conn: Connection, target: str = "<table>", *, limit: int = DEFAULT_ROW_LIMIT) -> str:
    driver = driver_name(conn)
    if driver.startswith("mssql"):
        return f"SELECT TOP {limit} * FROM {target};"
    if driver.startswith("oracle"):
        return f"SELECT * FROM {target} FETCH FIRST {limit} ROWS ONLY;"
    return f"SELECT * FROM {target} LIMIT {limit};"
