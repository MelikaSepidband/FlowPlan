"""Symbols backend: build symbols.db from tree-sitter parsing (python + c).

Extracts, per language:

- Python: modules, classes, functions, and methods with dotted qualnames
  (e.g. ``sampleproj.models.User.save``) and signatures taken from the def or
  class header line(s).
- C (``.c``/``.h``): function definitions, named struct/union/enum specifiers
  (with bodies), and typedefs. For C the qualname IS the name (no module
  dotting) and the signature is the collapsed declarator line.

Writes the FIXED schema from INTERFACES.md section 6.1 into
``<index_dir>/symbols.db``. This backend feeds the navigation kit and is
built for ALL study conditions.

For kernel-scale trees (tens of thousands of files) the build fans out over a
ProcessPoolExecutor: when the file count exceeds ``_PARALLEL_FILE_THRESHOLD``
the default worker count is ``min(cpu_count, 20)``; smaller trees build
sequentially. Each worker chunk yields one batch INSERT, and chunks are
consumed in submission order so parallel output is row-for-row identical to
the sequential build.
"""

import math
import os
import sqlite3
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

from tree_sitter_language_pack import get_parser

from indexer.manifest import iter_source_files

__all__ = ["build", "module_qualname", "EXT_LANGUAGES"]

# Extension -> tree-sitter grammar name. Drives which files are indexed.
# C++ sources use the 'cpp' grammar, which is a superset of C: it emits the same
# ``function_definition`` nodes (so _walk_c handles them) plus C++-only
# declarators (qualified_identifier for out-of-class ``Class::method`` defs,
# handled in _c_declarator_name). .h is treated as C by default; C++ headers
# more often use .hpp/.hh/.hxx which map to cpp here.
EXT_LANGUAGES: dict[str, str] = {
    ".py": "python",
    ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".c++": "cpp",
    ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp", ".h++": "cpp",
}

# Multiprocessing knobs: parallelize only above the threshold; cap workers.
_PARALLEL_FILE_THRESHOLD = 500
_MAX_JOBS = 20
_CHUNKS_PER_JOB = 4

_SYMBOLS_DDL = (
    "CREATE TABLE symbols("
    "id INTEGER PRIMARY KEY, name TEXT, qualname TEXT, kind TEXT, "
    "file TEXT, line INT, end_line INT, signature TEXT)",
    "CREATE INDEX idx_symbols_name ON symbols(name)",
)

_INSERT = (
    "INSERT INTO symbols(name, qualname, kind, file, line, end_line, signature) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)

# C named-type specifiers (a row is emitted only when name AND body exist).
_C_TYPE_KINDS: dict[str, str] = {
    "struct_specifier": "struct",
    "union_specifier": "union",
    "enum_specifier": "enum",
}

_C_NAME_KINDS = ("identifier", "type_identifier", "field_identifier")

# THREAD-LOCAL parser cache. tree-sitter's Parser (pyo3/Rust) is "unsendable":
# pyo3 asserts the object is only ever touched by the thread that created it, and
# panics ("_native::Parser is unsendable, but sent to another thread") otherwise.
# A module-level shared dict let thread B reuse a parser thread A created -> panic
# when the swebench orchestrator builds indexes from multiple worker threads. A
# threading.local() store gives each thread its OWN parser, so none is ever shared
# across threads. (Process-pool workers still each get a fresh store.)
import threading as _threading
_PARSER_TLS = _threading.local()


def module_qualname(rel_posix: str) -> str:
    """Dotted module path for a repo-relative posix path to a .py file."""
    stem = rel_posix[:-3] if rel_posix.endswith(".py") else rel_posix
    parts = [p for p in stem.split("/") if p]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        parts = ["__init__"]
    return ".".join(parts)


def _parser_for(language: str) -> Any:
    """Return a THREAD-LOCAL tree-sitter parser for language (never shared across
    threads -- tree-sitter Parser is unsendable, see _PARSER_TLS above)."""
    cache = getattr(_PARSER_TLS, "cache", None)
    if cache is None:
        cache = _PARSER_TLS.cache = {}
    parser = cache.get(language)
    if parser is None:
        parser = get_parser(language)
        cache[language] = parser
    return parser


def _clear_parser_cache() -> None:
    """ProcessPoolExecutor initializer: never reuse a fork-inherited parser."""
    cache = getattr(_PARSER_TLS, "cache", None)
    if cache is not None:
        cache.clear()


def _node_text(source_bytes: bytes, node: Any) -> str:
    """Decode the UTF-8 source slice covered by a node."""
    return source_bytes[node.start_byte():node.end_byte()].decode(
        "utf-8", errors="replace"
    )


def _collapse(source_bytes: bytes, start: int, end: int) -> str:
    """Whitespace-collapsed source text between two byte offsets."""
    text = source_bytes[start:end].decode("utf-8", errors="replace")
    return " ".join(text.split())


def _signature(source_bytes: bytes, node: Any) -> str:
    """Header text of a def/class node: start through body, colon stripped."""
    body = node.child_by_field_name("body")
    end = body.start_byte() if body is not None else node.end_byte()
    text = source_bytes[node.start_byte():end].decode("utf-8", errors="replace")
    text = " ".join(text.split())
    return text.rstrip(":").rstrip()


def _walk(
    node: Any,
    scope: list[str],
    in_class: bool,
    rows: list[tuple],
    rel: str,
    source_bytes: bytes,
) -> None:
    """Recursively collect def/class symbols, tracking the qualname scope."""
    for i in range(node.child_count()):
        child = node.child(i)
        kind = child.kind()
        if kind in ("function_definition", "class_definition"):
            name_node = child.child_by_field_name("name")
            name = _node_text(source_bytes, name_node) if name_node else "<anonymous>"
            qualname = ".".join(scope + [name])
            if kind == "class_definition":
                sym_kind = "class"
            else:
                sym_kind = "method" if in_class else "function"
            rows.append(
                (
                    name,
                    qualname,
                    sym_kind,
                    rel,
                    child.start_position().row + 1,
                    child.end_position().row + 1,
                    _signature(source_bytes, child),
                )
            )
            body = child.child_by_field_name("body")
            if body is not None:
                _walk(
                    body,
                    scope + [name],
                    kind == "class_definition",
                    rows,
                    rel,
                    source_bytes,
                )
        else:
            _walk(child, scope, in_class, rows, rel, source_bytes)


def _c_declarator_name(node: Any, source_bytes: bytes) -> str | None:
    """Descend a C declarator chain to the declared identifier text.

    Handles pointer, array, function, and parenthesized declarators (e.g.
    ``static char *trim(char *s)`` or ``typedef int (*cb)(int, int)``).
    """
    current = node
    while current is not None:
        # C++ out-of-class definitions declare a qualified_identifier
        # (``Class::method`` / ``ns::Class::method``); keep the full qualified
        # text so the symbol is resolvable by the name the agent will use.
        if current.kind() == "qualified_identifier":
            return _node_text(source_bytes, current)
        if current.kind() in _C_NAME_KINDS:
            return _node_text(source_bytes, current)
        nxt = current.child_by_field_name("declarator")
        if nxt is None:
            nxt = None
            for i in range(current.child_count()):
                child = current.child(i)
                if child.is_named():
                    nxt = child
                    break
        current = nxt
    return None


def _c_row(name: str, kind: str, rel: str, node: Any, signature: str) -> tuple:
    """One symbols row for a C definition: qualname == name (no dotting)."""
    return (
        name,
        name,
        kind,
        rel,
        node.start_position().row + 1,
        node.end_position().row + 1,
        signature,
    )


def _walk_c(node: Any, rows: list[tuple], rel: str, source_bytes: bytes) -> None:
    """Collect C functions, named struct/union/enum, typedefs (pre-order).

    Iterative with an explicit stack: real-world C files (e.g. Linux kernel
    sources with long if/else or expression chains) produce tree-sitter trees
    deeper than Python's recursion limit, so a recursive walk RecursionErrors.
    The stack-based pre-order visits nodes in exactly the same order as the
    recursive formulation, keeping rows byte-identical.
    """
    stack = [node.child(i) for i in range(node.child_count() - 1, -1, -1)]
    while stack:
        child = stack.pop()
        kind = child.kind()
        if kind == "function_definition":
            declarator = child.child_by_field_name("declarator")
            if declarator is not None:
                name = _c_declarator_name(declarator, source_bytes)
                if name:
                    signature = _collapse(
                        source_bytes, child.start_byte(), declarator.end_byte()
                    )
                    rows.append(_c_row(name, "function", rel, child, signature))
        elif kind in _C_TYPE_KINDS:
            name_node = child.child_by_field_name("name")
            body = child.child_by_field_name("body")
            if name_node is not None and body is not None:
                name = _node_text(source_bytes, name_node)
                signature = _collapse(
                    source_bytes, child.start_byte(), name_node.end_byte()
                )
                rows.append(_c_row(name, _C_TYPE_KINDS[kind], rel, child, signature))
        elif kind == "type_definition":
            declarator = child.child_by_field_name("declarator")
            if declarator is not None:
                name = _c_declarator_name(declarator, source_bytes)
                if name:
                    signature = _collapse(
                        source_bytes, child.start_byte(), child.end_byte()
                    ).rstrip(";").rstrip()
                    rows.append(_c_row(name, "typedef", rel, child, signature))
        for i in range(child.child_count() - 1, -1, -1):
            stack.append(child.child(i))


def _extract_file(rel: str, text: str, language: str) -> list[tuple] | None:
    """Symbol rows for one file, or None when the parse failed."""
    parser = _parser_for(language)
    try:
        tree = parser.parse(text)
        root_node = tree.root_node()
    except Exception:
        return None
    source_bytes = text.encode("utf-8")
    rows: list[tuple] = []
    if language == "python":
        modname = module_qualname(rel)
        rows.append(
            (
                modname.split(".")[-1],
                modname,
                "module",
                rel,
                1,
                max(1, len(text.splitlines())),
                f"module {modname}",
            )
        )
        _walk(root_node, [modname], False, rows, rel, source_bytes)
    else:
        _walk_c(root_node, rows, rel, source_bytes)
    return rows


def _extract_chunk(args: tuple[str, list[str]]) -> tuple[list[tuple], int, int]:
    """Worker: extract rows for a chunk of repo-relative paths.

    Returns (rows, files_parsed, parse_errors). Module-level and
    tuple-argumented so it pickles cleanly into ProcessPoolExecutor workers.
    """
    root_str, rels = args
    root = Path(root_str)
    rows: list[tuple] = []
    n_files = 0
    parse_errors = 0
    for rel in rels:
        language = EXT_LANGUAGES[Path(rel).suffix]
        text = (root / rel).read_text(encoding="utf-8", errors="replace")
        file_rows = _extract_file(rel, text, language)
        if file_rows is None:
            parse_errors += 1
            continue
        rows.extend(file_rows)
        n_files += 1
    return rows, n_files, parse_errors


def build(repo_root: Path, index_dir: Path, jobs: int | None = None) -> dict:
    """Build symbols.db for every .py/.c/.h file under repo_root; return stats.

    ``jobs=None`` picks automatically: sequential at or below
    ``_PARALLEL_FILE_THRESHOLD`` files, else ``min(cpu_count, 20)`` worker
    processes. ``jobs=1`` forces the sequential path; ``jobs>=2`` forces the
    multiprocessing path. Both paths produce identical rows in identical
    order (one batch INSERT per worker chunk, consumed in submission order).
    """
    root = Path(repo_root).resolve()
    out_dir = Path(index_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    rels = [
        path.relative_to(root).as_posix()
        for path in iter_source_files(root, sorted(EXT_LANGUAGES))
    ]
    if jobs is None:
        if len(rels) > _PARALLEL_FILE_THRESHOLD:
            jobs = min(os.cpu_count() or 1, _MAX_JOBS)
        else:
            jobs = 1
    jobs = max(1, int(jobs))
    db_path = out_dir / "symbols.db"
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(str(db_path))
    n_files = 0
    parse_errors = 0
    n_symbols = 0
    try:
        for ddl in _SYMBOLS_DDL:
            conn.execute(ddl)
        if jobs == 1 or len(rels) < 2:
            rows, n_files, parse_errors = _extract_chunk((str(root), rels))
            conn.executemany(_INSERT, rows)
            n_symbols = len(rows)
        else:
            chunk_size = max(1, math.ceil(len(rels) / (jobs * _CHUNKS_PER_JOB)))
            chunks = [
                (str(root), rels[start : start + chunk_size])
                for start in range(0, len(rels), chunk_size)
            ]
            with ProcessPoolExecutor(
                max_workers=jobs, initializer=_clear_parser_cache
            ) as pool:
                for rows, files_done, errors in pool.map(_extract_chunk, chunks):
                    conn.executemany(_INSERT, rows)
                    n_files += files_done
                    parse_errors += errors
                    n_symbols += len(rows)
        conn.commit()
    finally:
        conn.close()
    return {
        "backend": "symbols",
        "files": n_files,
        "parse_errors": parse_errors,
        "symbols": n_symbols,
        "jobs": jobs,
        "db": str(db_path),
    }
