"""The uniform 2-tool navigation kit shared by every repr-study server.

``find_symbol`` searches the prebuilt symbols.db (built by the symbols
indexer backend for ALL conditions); ``list_files`` globs the repository.
Both return plain item lists; the ReprServer wrapper supplies the envelope.

NOTE: do not add ``from __future__ import annotations`` here -- tool
implementation annotations must be real objects for FastMCP schema
generation (INTERFACES.md section 8).
"""

import sqlite3
from typing import Literal

from servers.common import env

__all__ = ["EXCLUDED_DIRS", "SYMBOLS_DB_NAME", "find_symbol", "list_files"]

EXCLUDED_DIRS: frozenset[str] = frozenset(
    {".git", ".venv", ".repr_index", "__pycache__", "node_modules"}
)

SYMBOLS_DB_NAME = "symbols.db"

_VALID_KINDS = ("function", "class", "module", "any")


def _escape_like(text: str) -> str:
    """Escape SQL LIKE wildcards so the pattern is a literal substring."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def find_symbol(
    pattern: str,
    kind: Literal["function", "class", "module", "any"] = "any",
    max_results: int = 50,
) -> list[dict]:
    """Find symbols whose name or qualname contains pattern (case-insensitive).

    kind="function" also matches methods; "class" and "module" match exactly;
    "any" matches every kind. Items: {name, kind, file, line, signature}.
    """
    if kind not in _VALID_KINDS:
        raise ValueError(
            f"invalid kind {kind!r}; expected one of {', '.join(_VALID_KINDS)}"
        )
    if max_results <= 0:
        return []
    db_path = env.index_dir() / SYMBOLS_DB_NAME
    if not db_path.is_file():
        raise FileNotFoundError(
            f"symbols.db not found at {db_path}; "
            "run 'python -m indexer.build_index' first"
        )
    lowered = pattern.lower()
    like = f"%{_escape_like(lowered)}%"
    where = ["(LOWER(name) LIKE ? ESCAPE '\\' OR LOWER(qualname) LIKE ? ESCAPE '\\')"]
    params: list[object] = [like, like]
    if kind == "function":
        where.append("kind IN ('function', 'method')")
    elif kind != "any":
        where.append("kind = ?")
        params.append(kind)
    # Relevance ranking so the intended symbol is first even when a fuzzy
    # pattern also substring-matches dozens of unrelated names across the
    # tree (the "find_symbol('mcast') -> 20 cross-tree junk hits" problem):
    # exact name (0) < prefix (1) < exact qualname suffix (2) < other (3),
    # then shorter names, then name/file/line for stability.
    rank = (
        "CASE "
        "WHEN LOWER(name) = ? THEN 0 "
        "WHEN LOWER(name) LIKE ? ESCAPE '\\' THEN 1 "
        "WHEN LOWER(qualname) LIKE ? ESCAPE '\\' THEN 2 "
        "ELSE 3 END"
    )
    params.extend([lowered, f"{_escape_like(lowered)}%", f"%.{_escape_like(lowered)}"])
    sql = (
        "SELECT name, kind, file, line, signature FROM symbols "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY {rank}, LENGTH(name), name, file, line LIMIT ?"
    )
    params.append(max_results)
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [
        {"name": r[0], "kind": r[1], "file": r[2], "line": r[3], "signature": r[4]}
        for r in rows
    ]


def list_files(glob: str, max_results: int = 100) -> list[dict]:
    """List repository files matching a glob pattern, paths only.

    Walks env.repo_root() with rglob, skipping any file whose relative path
    contains an excluded directory component. Items: {path} with repo-root-
    relative POSIX paths, sorted, at most max_results.
    """
    if max_results <= 0:
        return []
    root = env.repo_root()
    items: list[dict] = []
    for path in sorted(root.rglob(glob)):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in EXCLUDED_DIRS for part in rel.parts[:-1]):
            continue
        items.append({"path": rel.as_posix()})
        if len(items) >= max_results:
            break
    return items
