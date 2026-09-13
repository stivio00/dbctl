"""Read-only schema/table/column introspection for the connection tree's
schema browser. Thin wrapper over ``sqlalchemy.inspect()`` - never executes
a query against the connection's own tables, only dialect catalog lookups.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

# Catalog/system schemas hidden from the tree by default so the "normal"
# view stays legible; only applied when at least one non-system schema
# exists (a DB with nothing but these - unusual - still shows them).
_HIDDEN_SCHEMAS = {"information_schema", "pg_catalog", "sys", "guest"}


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    type: str
    nullable: bool
    primary_key: bool = False


@dataclass(frozen=True)
class IndexInfo:
    name: str
    columns: list[str] = field(default_factory=list)
    unique: bool = False


@dataclass(frozen=True)
class ForeignKeyInfo:
    """One declared FK link: ``columns`` on this table → ``ref_table(ref_columns)``."""

    columns: list[str] = field(default_factory=list)
    ref_table: str = ""
    ref_columns: list[str] = field(default_factory=list)

    def render(self) -> str:
        return f"({', '.join(self.columns)}) -> {self.ref_table}({', '.join(self.ref_columns)})"


def list_foreign_keys(engine: Engine, table: str, schema: str | None = None) -> list[ForeignKeyInfo]:
    """Declared foreign keys via ``inspect()`` — read-only catalog lookup."""
    insp = inspect(engine)
    try:
        fks = insp.get_foreign_keys(table, schema=schema)
    except NotImplementedError:
        return []
    out: list[ForeignKeyInfo] = []
    for fk in fks:
        cols = [c for c in (fk.get("constrained_columns") or []) if c is not None]
        ref_cols = [c for c in (fk.get("referred_columns") or []) if c is not None]
        out.append(
            ForeignKeyInfo(
                columns=cols,
                ref_table=str(fk.get("referred_table") or ""),
                ref_columns=ref_cols,
            )
        )
    return out


def list_schemas(engine: Engine) -> list[str]:
    insp = inspect(engine)
    try:
        schemas = sorted(insp.get_schema_names())
    except NotImplementedError:
        return []
    visible = [s for s in schemas if s not in _HIDDEN_SCHEMAS]
    return visible or schemas


def list_tables(engine: Engine, schema: str | None = None) -> list[str]:
    insp = inspect(engine)
    return sorted(insp.get_table_names(schema=schema))


def list_views(engine: Engine, schema: str | None = None) -> list[str]:
    insp = inspect(engine)
    try:
        return sorted(insp.get_view_names(schema=schema))
    except NotImplementedError:
        return []


def list_columns(engine: Engine, table: str, schema: str | None = None) -> list[ColumnInfo]:
    insp = inspect(engine)
    try:
        pk = set(insp.get_pk_constraint(table, schema=schema).get("constrained_columns") or [])
    except NotImplementedError:
        pk = set()
    out = []
    for col in insp.get_columns(table, schema=schema):
        out.append(
            ColumnInfo(
                name=col["name"],
                type=str(col.get("type", "")),
                nullable=bool(col.get("nullable", True)),
                primary_key=col["name"] in pk,
            )
        )
    return out


def list_indexes(engine: Engine, table: str, schema: str | None = None) -> list[IndexInfo]:
    insp = inspect(engine)
    try:
        indexes = insp.get_indexes(table, schema=schema)
    except NotImplementedError:
        return []
    return [
        IndexInfo(
            name=ix.get("name") or "(unnamed)",
            columns=[c for c in (ix.get("column_names") or []) if c is not None],
            unique=bool(ix.get("unique")),
        )
        for ix in indexes
    ]
