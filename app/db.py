"""SQLite persistence layer.

Each request gets its own connection.  Atomic read-modify-write cycles use
``BEGIN IMMEDIATE`` so concurrent editors are serialised by SQLite itself, and
the ``UNIQUE`` constraints on ``bulletins.quarter`` / ``detail_versions`` are
the final arbiter when two publishes race.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS quarters(
    quarter              TEXT PRIMARY KEY,
    district_labels      TEXT NOT NULL,
    industry_labels      TEXT NOT NULL,
    row_totals           TEXT NOT NULL,
    col_totals           TEXT NOT NULL,
    registration_version INTEGER NOT NULL DEFAULT 1,
    registered_at        TEXT NOT NULL,
    registered_by        INTEGER
);

-- Every successful totals registration is archived here, version 1 being the
-- initial PUT.  Rows are immutable: a revision inserts a new version instead
-- of rewriting an old one, so the full registration history is retained even
-- while ``quarters`` always reflects the current version.
CREATE TABLE IF NOT EXISTS totals_revisions(
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    quarter          TEXT    NOT NULL REFERENCES quarters(quarter),
    version          INTEGER NOT NULL,
    district_labels  TEXT NOT NULL,
    industry_labels  TEXT NOT NULL,
    row_totals       TEXT NOT NULL,
    col_totals       TEXT NOT NULL,
    change_kind      TEXT NOT NULL,
    revised_at       TEXT NOT NULL,
    revised_by       INTEGER,
    UNIQUE(quarter, version)
);

CREATE TABLE IF NOT EXISTS detail_versions(
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    quarter              TEXT    NOT NULL REFERENCES quarters(quarter),
    version              INTEGER NOT NULL,
    registration_version INTEGER NOT NULL DEFAULT 1,
    values_json          TEXT NOT NULL,
    must_public          TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    created_by           INTEGER,
    UNIQUE(quarter, version)
);

CREATE TABLE IF NOT EXISTS mask_plans(
    quarter        TEXT    NOT NULL REFERENCES quarters(quarter),
    detail_version INTEGER NOT NULL,
    mask_version   INTEGER NOT NULL,
    cells_json     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    updated_by     INTEGER,
    PRIMARY KEY(quarter, detail_version)
);

CREATE TABLE IF NOT EXISTS bulletins(
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    quarter              TEXT    NOT NULL UNIQUE REFERENCES quarters(quarter),
    detail_version       INTEGER NOT NULL,
    mask_version         INTEGER,
    registration_version INTEGER NOT NULL DEFAULT 1,
    payload_json         TEXT NOT NULL,
    published_at         TEXT NOT NULL,
    published_by         INTEGER
);

CREATE TABLE IF NOT EXISTS editor_tokens(
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    label       TEXT    NOT NULL,
    token_hash  TEXT    NOT NULL UNIQUE,
    created_at  TEXT NOT NULL,
    revoked_at  TEXT
);
"""


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(
        f"PRAGMA table_info({table})").fetchall()}


def migrate(conn: sqlite3.Connection) -> None:
    """Bring databases created by older releases up to the current schema.

    Additive column changes (SQLite cannot alter columns in place) plus a
    backfill of the archived initial registration for pre-revision databases.
    """
    if "registration_version" not in _columns(conn, "quarters"):
        conn.execute(
            "ALTER TABLE quarters ADD COLUMN registration_version "
            "INTEGER NOT NULL DEFAULT 1")
    if "registration_version" not in _columns(conn, "detail_versions"):
        conn.execute(
            "ALTER TABLE detail_versions ADD COLUMN registration_version "
            "INTEGER NOT NULL DEFAULT 1")
    if "registration_version" not in _columns(conn, "bulletins"):
        conn.execute(
            "ALTER TABLE bulletins ADD COLUMN registration_version "
            "INTEGER NOT NULL DEFAULT 1")

    archived = {
        row["quarter"] for row in conn.execute(
            "SELECT quarter FROM totals_revisions").fetchall()
    }
    for q in conn.execute("SELECT * FROM quarters").fetchall():
        if q["quarter"] in archived:
            continue
        conn.execute(
            "INSERT INTO totals_revisions(quarter, version, district_labels, "
            "industry_labels, row_totals, col_totals, change_kind, revised_at, "
            "revised_by) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                q["quarter"], 1,
                q["district_labels"], q["industry_labels"],
                q["row_totals"], q["col_totals"],
                "initial", q["registered_at"], q["registered_by"],
            ),
        )


def connect(db_path: str) -> sqlite3.Connection:
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def resolve_db_path(path: str) -> str:
    """Anchor relative paths at the project root, not the process cwd."""
    if path == ":memory:" or path.startswith("file:"):
        return path
    p = Path(path)
    if p.is_absolute():
        return str(p)
    return str(Path(__file__).resolve().parent.parent / p)


def db_path_from_env() -> str:
    return resolve_db_path(os.environ.get("BULLETIN_DB", "./data/bulletins.sqlite3"))
