"""SQLAlchemy engine construction + password resolution.

A tunnel yields (local_host, local_port); credentials yield the password.
The URL builder combines them into a SQLAlchemy URL for the configured driver.

Driver -> pip package map (so errors can guide users):

    postgresql+psycopg -> psycopg (v3)
    mysql+pymysql      -> PyMySQL
    mariadb+pymysql    -> PyMySQL
    mssql+pyodbc       -> pyodbc (+ ODBC driver)
"""

from __future__ import annotations

import getpass
import os
import re as _re
import time
from typing import TYPE_CHECKING

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError, SQLAlchemyError

if TYPE_CHECKING:
    from dbctl.config import Connection
    from dbctl.tunnels.base import Tunnel


class DBError(RuntimeError):
    pass


def resolve_password(conn: Connection) -> str:
    if conn.password is not None:
        return conn.password
    if conn.password_env:
        val = os.environ.get(conn.password_env)
        if not val:
            raise DBError(f"environment variable {conn.password_env!r} is not set")
        return val
    if conn.prompt:
        return getpass.getpass(f"Password for {conn.username}@{conn.database}: ")
    # Connection validator already prevents this branch, but be defensive.
    raise DBError("no password source configured")


def _connect_args(conn: Connection, timeout: float) -> dict:
    """Driver-specific connect-time knobs (mainly connect_timeout)."""
    args: dict = {}
    if conn.driver.startswith(("postgresql", "mysql", "mariadb")):
        args["connect_timeout"] = int(max(1, timeout))
    return args


def build_engine(conn: Connection, tunnel: Tunnel, *, echo: bool = False) -> Engine:
    """Engine pointing at the tunnel's local bind (or direct host:port)."""
    _check_driver_available(conn.driver)
    password = resolve_password(conn)

    from sqlalchemy import URL

    url = URL.create(
        conn.driver,
        username=conn.username,
        password=password,
        host=tunnel.local_host,
        port=tunnel.local_port,
        database=conn.database,
    )
    return create_engine(
        url,
        pool_pre_ping=True,
        future=True,
        echo=echo,
        connect_args=_connect_args(conn, conn.healthcheck.timeout_seconds),
    )


def _check_driver_available(driver: str) -> None:
    """Fail fast with a helpful hint when the dialect's python package is
    missing or its native shared-library dependency is absent. SQLAlchemy
    imports the driver lazily at connect time; we trigger it eagerly so
    the user sees a clean message instead of a wrapped traceback."""
    import importlib

    module_map = {
        "postgresql+psycopg": "psycopg",
        "mysql+pymysql": "pymysql",
        "mariadb+pymysql": "pymysql",
        "mssql+pyodbc": "pyodbc",
    }
    pkg = module_map.get(driver)
    if pkg is None:
        return  # unknown driver: let SQLAlchemy raise its own error
    try:
        importlib.import_module(pkg)
    except ModuleNotFoundError as e:
        raise DBError(
            f"driver {driver!r} requires the missing python package {pkg!r}. "
            f"install it with: pip install {pkg}  (or: uv pip install {pkg})"
        ) from e
    except ImportError as e:
        # Python package is installed but it can't load a native shared
        # library — typical for pyodbc without unixODBC installed at the
        # OS level, or psycopg without libpq. The missing .so filename is
        # in the error message text (e.g. "libodbc.so.2: cannot open..."),
        # not in e.name (which holds the python module being imported).
        msg = str(e)
        lib = msg.split(":", 1)[0].strip()
        if not _looks_like_library(lib):
            lib = ""
        hint = _native_lib_hint(driver, lib) or msg
        raise DBError(
            f"driver {driver!r} (python package {pkg!r}) failed to load a native dependency: {hint}"
        ) from e


def _looks_like_library(name: str) -> bool:
    return bool(name) and ("lib" in name or ".so" in name)


def _native_lib_hint(driver: str, library: str) -> str:
    """Translate the most common native-library failures into install hints."""
    if "libodbc" in (library or ""):
        return (
            "missing native ODBC library libodbc.so; install unixODBC at the "
            "OS level (Debian/Ubuntu: `sudo apt install unixodbc`, "
            "RHEL/Fedora: `sudo dnf install unixODBC`, macOS: `brew install unixodbc`)."
        )
    if "libpq" in (library or "") or "psycopg" in (library or ""):
        return (
            "missing native libpq; install PostgreSQL client libs "
            "(Debian/Ubuntu: `sudo apt install libpq5`, "
            "RHEL/Fedora: `sudo dnf install libpq`, macOS: `brew install libpq`)."
        )
    return ""


def healthcheck(engine: Engine, query: str, timeout: float) -> tuple[bool, float, str]:
    """Returns (ok, latency_ms, message)."""
    started = time.monotonic()
    try:
        with engine.connect() as c:
            c.exec_driver_sql(query)
        elapsed_ms = (time.monotonic() - started) * 1000.0
        return True, elapsed_ms, "ok"
    except (OperationalError, SQLAlchemyError) as e:
        # `e.orig` is the underlying DBAPI exception (psycopg / pymysql /
        # pyodbc) — its str() is the clearest message for users. Clean up
        # the SQLAlchemy + driver wrap so the stderr line reads as plain
        # English (e.g. "password authentication failed for user 'app_admin'").
        raw = e.orig if getattr(e, "orig", None) is not None else e
        msg = fmt_db_error(raw)
        # Most DBAPIs wrap the real reason in a connection-failure preamble
        # ("connection to server at ..., port N failed: <real reason>"). The
        # real reason itself is often prefixed with "FATAL:" / "ERROR:" —
        # extract that. Falls back to the full cleaned message.
        m = _re.search(r"\b(FATAL|ERROR):\s*(.+)$", msg, flags=_re.DOTALL)
        if m:
            msg = m.group(2).strip()
        else:
            # Otherwise peel the "connection ... failed:" preamble once.
            preamble = _re.search(r"failed:\s*(.+)$", msg, flags=_re.DOTALL)
            if preamble and len(preamble.group(1)) < len(msg):
                msg = preamble.group(1).strip()
        return False, 0.0, msg[:200]
    except Exception as e:  # noqa: BLE001
        return False, 0.0, f"{type(e).__name__}: {e}"


def fmt_db_error(e: BaseException) -> str:
    """Render a clean one-line message from a DB/SQLAlchemy error.

    SQLAlchemyError ``str()`` includes a multi-line dump with the driver
    class prefix (``"(psycopg.OperationalError)"``) and a
    ``(Background on this error at: https://sqlalche.me/...)"`` trailer —
    fine for a stack trace, noisy for a CLI user. We strip both so the
    stderr line reads as plain English.

    Pass through a plain ``RuntimeError`` (e.g. ``DBError``) unchanged.
    """
    msg = str(e)
    # Drop the leading "(<driver>.<ExceptionName>)" wrap, if present.
    msg = _re.sub(r"^\([A-Za-z_][A-Za-z0-9_.]*\)\s*", "", msg)
    # Drop the "(Background on this error at: ...)" trailer.
    msg = _re.sub(r"\s*\(Background on this error at:.*\)\s*$", "", msg, flags=_re.DOTALL)
    # Collapse internal newlines/tabs to single spaces.
    msg = _re.sub(r"\s+", " ", msg).strip()
    return msg


__all__ = ["build_engine", "resolve_password", "healthcheck", "DBError", "fmt_db_error", "text"]
