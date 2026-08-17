"""Shared per-rep map schema -- codegraph's shape so one engine serves all reps.

Every rep's map (``<repo>/.perrep/<rep>.db``) has these tables. What fills them
differs per rep (see perrep package docstring), but the SHAPE is identical so
perrep.engine's explore/node work unchanged for every representation.

    nodes(id, kind, name, qualname, file, start_line, end_line, signature, doc)
    edges(src, dst, kind, line)
    nodes_fts  -- FTS5 over (name, qualname, signature, doc), bm25-ranked

``kind`` on edges is the rep's edge semantics (calls / contains / imports / ...).
"""

import sqlite3
from pathlib import Path

NODE_COLS = ("id", "kind", "name", "qualname", "file", "start_line",
             "end_line", "signature", "doc")

_DDL = [
    "PRAGMA journal_mode=OFF",
    "PRAGMA synchronous=OFF",
    """CREATE TABLE nodes(
        id        INTEGER PRIMARY KEY,
        kind      TEXT,
        name      TEXT,
        qualname  TEXT,
        file      TEXT,
        start_line INT,
        end_line  INT,
        signature TEXT,
        doc       TEXT
    )""",
    "CREATE TABLE edges(src INT, dst INT, kind TEXT, line INT)",
    "CREATE INDEX idx_edges_src ON edges(src, kind)",
    "CREATE INDEX idx_edges_dst ON edges(dst, kind)",
    "CREATE INDEX idx_nodes_name ON nodes(name)",
    "CREATE INDEX idx_nodes_file ON nodes(file)",
    """CREATE VIRTUAL TABLE nodes_fts USING fts5(
        name, qualname, signature, doc,
        content='nodes', content_rowid='id'
    )""",
]

_INSERT_NODE = (
    "INSERT INTO nodes(id, kind, name, qualname, file, start_line, end_line, "
    "signature, doc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_INSERT_EDGE = "INSERT INTO edges(src, dst, kind, line) VALUES (?, ?, ?, ?)"


def create(db_path: Path) -> sqlite3.Connection:
    """Create a fresh map DB at ``db_path`` and return an open connection."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(str(db_path))
    for stmt in _DDL:
        conn.execute(stmt)
    return conn


def insert_nodes(conn: sqlite3.Connection, rows) -> int:
    """Bulk-insert node rows (each a 9-tuple in NODE_COLS order)."""
    rows = list(rows)
    conn.executemany(_INSERT_NODE, rows)
    return len(rows)


def insert_edges(conn: sqlite3.Connection, rows) -> int:
    """Bulk-insert edge rows (each (src, dst, kind, line))."""
    rows = list(rows)
    conn.executemany(_INSERT_EDGE, rows)
    return len(rows)


def finalize(conn: sqlite3.Connection) -> None:
    """Populate the FTS index from nodes and commit."""
    conn.execute(
        "INSERT INTO nodes_fts(rowid, name, qualname, signature, doc) "
        "SELECT id, name, qualname, signature, doc FROM nodes"
    )
    conn.commit()
