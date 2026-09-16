"""Index database helpers: connect, migrate, upsert.

The index is a CACHE (invariant 1). Everything here is reconstructable from receipts, git,
GitHub, and on-disk Claude Code state; dropping the file costs only rebuild time. The DDL
lives in ``sql/schema.sql`` and is applied idempotently (every table is IF NOT EXISTS).
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "sql" / "schema.sql"


def default_path() -> Path:
    """Index location: FOREMAN_INDEX, else ~/foreman/index.db."""
    return Path(os.path.expanduser(os.environ.get("FOREMAN_INDEX", "~/foreman/index.db")))


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Read paths (brief, web) connect without migrating. Self-heal additive columns here so a
    # query against an index built by an older schema doesn't crash -- the index is a cache,
    # and this ALTER is idempotent and only touches tables that already exist.
    _ensure_columns(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA.read_text())
    _ensure_columns(conn)
    conn.commit()


# Columns added to existing tables after the first release. IF-NOT-EXISTS handles new tables;
# a new column on an existing table needs an idempotent ALTER so an index built by an older
# schema keeps working without a manual drop (the index is a cache, but avoid needless churn).
_ADDED_COLUMNS = {
    "component": [("disagrees_with_probe", "INTEGER")],
}


def _ensure_columns(conn: sqlite3.Connection) -> None:
    for table, cols in _ADDED_COLUMNS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not have:
            continue  # table not created yet (fresh DB); schema.sql will add it with the column
        for name, decl in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.commit()


def open_index(path: str | Path | None = None) -> sqlite3.Connection:
    """Connect and migrate in one step, creating the parent directory if needed."""
    path = Path(path) if path is not None else default_path()
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    migrate(conn)
    return conn


def upsert(conn: sqlite3.Connection, table: str, row: dict, keys: list[str]) -> None:
    """INSERT ... ON CONFLICT(keys) DO UPDATE, so a collector re-run replaces, never dupes."""
    cols = list(row.keys())
    placeholders = ", ".join(f":{c}" for c in cols)
    conflict = ", ".join(keys)
    updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c not in keys)
    sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
    sql += (f" ON CONFLICT({conflict}) DO UPDATE SET {updates}"
            if updates else f" ON CONFLICT({conflict}) DO NOTHING")
    conn.execute(sql, row)
