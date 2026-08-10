"""Dialect-aware default SELECT + identifier quoting - no app, no docker,
no real database: pure functions over `Connection` objects per dialect."""

from __future__ import annotations

import pytest

from dbctl.config import Connection
from dbctl.ui.sql_templates import default_select, qualified_table, quote_identifier


def _conn(driver: str) -> Connection:
    return Connection.model_validate(
        {
            "type": "direct",
            "driver": driver,
            "database": "app",
            "username": "u",
            "password": "p",
            "direct": {"host": "localhost", "port": 0},
        }
    )


@pytest.mark.parametrize(
    ("driver", "expected"),
    [
        ("postgresql+psycopg", "SELECT * FROM users LIMIT 100;"),
        ("mysql+pymysql", "SELECT * FROM users LIMIT 100;"),
        ("sqlite", "SELECT * FROM users LIMIT 100;"),
        ("duckdb", "SELECT * FROM users LIMIT 100;"),
        ("mssql+pyodbc", "SELECT TOP 100 * FROM users;"),
        ("oracle+oracledb", "SELECT * FROM users FETCH FIRST 100 ROWS ONLY;"),
    ],
)
def test_default_select_uses_dialect_row_limit_clause(driver, expected):
    assert default_select(_conn(driver), "users") == expected


def test_default_select_placeholder_is_not_quoted():
    assert default_select(_conn("postgresql+psycopg")) == "SELECT * FROM <table> LIMIT 100;"


def test_default_select_respects_custom_limit():
    assert default_select(_conn("mssql+pyodbc"), "users", limit=10) == "SELECT TOP 10 * FROM users;"


def test_quote_identifier_leaves_plain_lowercase_names_alone():
    for driver in ["postgresql+psycopg", "mysql+pymysql", "mssql+pyodbc", "oracle+oracledb", "sqlite"]:
        assert quote_identifier(_conn(driver), "users") == "users"


@pytest.mark.parametrize(
    ("driver", "expected"),
    [
        ("postgresql+psycopg", '"Users"'),
        ("oracle+oracledb", '"Users"'),
        ("sqlite", '"Users"'),
        ("duckdb", '"Users"'),
        ("mssql+pyodbc", "[Users]"),
        ("mysql+pymysql", "`Users`"),
        ("mariadb+pymysql", "`Users`"),
    ],
)
def test_quote_identifier_quotes_mixed_case_per_dialect(driver, expected):
    assert quote_identifier(_conn(driver), "Users") == expected


def test_qualified_table_quotes_schema_and_table_independently():
    conn = _conn("mssql+pyodbc")
    assert qualified_table(conn, "Users", "Sales") == "[Sales].[Users]"
    assert qualified_table(conn, "users", "dbo") == "dbo.users"


def test_qualified_table_without_schema():
    conn = _conn("postgresql+psycopg")
    assert qualified_table(conn, "Users") == '"Users"'


def test_full_url_connection_still_resolves_dialect():
    conn = Connection.model_validate(
        {
            "type": "direct",
            "url": "mssql+pyodbc://user@host/db?driver=ODBC+Driver+18+for+SQL+Server",
            "direct": {"host": "localhost", "port": 0},
        }
    )
    assert default_select(conn, "Users") == "SELECT TOP 100 * FROM Users;"
    assert quote_identifier(conn, "Users") == "[Users]"
