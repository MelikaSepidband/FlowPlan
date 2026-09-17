"""CFG MCP server (study condition C2).

Four representation tools per STUDY_PLAN.md sections 2 (C2) and 4:
``get_cfg``, ``cfg_paths``, ``dominators``, and ``reindex``. Function
arguments accept bare names, dotted qualnames (or dotted suffixes such as
``User.save``), or ``file.py:line`` anchors, resolved against the prebuilt
``symbols.db``; ambiguous names resolve to ALL matches (one CFG per match).

Engine selection (INTERFACES.md section 12.1): when ``JOERN_SERVER_URL`` is
set, ``get_cfg``, ``cfg_paths``, and ``dominators`` answer from a running
``joern --server`` via ``servers.common.joern.JoernClient``; when Joern is
unset, unreachable (``JoernUnavailable``), or has no CPG coverage for the
function, the pure-Python stdlib-``ast`` engine
(``servers.cfg_server.engine``) answers instead. Every item carries an
``"engine"`` key. ``cfg_paths`` and ``dominators`` fetch the CFG node/edge
lists from Joern and run the path/dominator computation locally (paths via
``networkx.shortest_simple_paths``; dominators and post-dominators via
``networkx.immediate_dominators``, post-dominators on the reversed graph
rooted at the exit). On the python-ast engine the same computations run over
the stdlib-``ast`` basic-block CFG. A non-Python source with no reachable
Joern yields an explicit note item instead of a parse error.

NOTE: do not add ``from __future__ import annotations`` here -- tool
implementation annotations must be real objects for FastMCP schema
generation (INTERFACES.md section 8).
"""

import ast as _pyast
import itertools
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Optional

import networkx as nx

from servers.cfg_server import engine
from servers.common import budget, env, navkit, staleness
from servers.common.base import ReprServer
from servers.common.joern import JoernClient, JoernUnavailable
from servers.common.tracefile import (
    NO_TRACE_NOTE as _NO_TRACE_NOTE,
    lines_for as _trace_lines,
    load_trace as _load_trace,
)

__all__ = [
    "get_cfg",
    "cfg_paths",
    "dominators",
    "reindex",
    "executed_path",
    "covered_blocks",
    "crash_path",
    "create_server",
]

# Frozen tool descriptions -- verb-first, one example each.
GET_CFG_DESCRIPTION = (
    "Get the control-flow graph of a function as basic blocks and edges: "
    "what executes in what order, and where the branches are. 'function' "
    "is the exact function name (a bare name, dotted qualname/suffix, or "
    "file:line anchor); 'file' is OPTIONAL and, when given, must be a "
    "repo-relative path (or basename) used only to narrow an ambiguous name -- "
    "do NOT pass an absolute path, which can cause a miss. Blocks carry id, "
    "line_range, and first_line_code; edges carry src, dst, and kind "
    "(true/false = branch arms, loop = back edge); entry and exit are "
    "synthetic block ids. An ambiguous name returns one CFG per match, each "
    "capped at max_nodes blocks; every item names the engine that produced "
    "it. An empty blocks/edges list with a 'note' means the engine has no "
    "coverage for that function's language or scope -- not an error; do not "
    "retry the same call. Example: get_cfg(function='run_pipeline')."
)
CFG_PATHS_DESCRIPTION = (
    "Enumerate control-flow paths between two source lines (from_line, "
    "to_line) inside one function, shortest first, up to max_paths -- use "
    "this to see how execution can reach a faulty line, or which branch "
    "decisions separate two statements. 'function' is the exact function "
    "name (bare name, dotted qualname/suffix, or file:line anchor). Each "
    "item is one path: the block id sequence plus the branch conditions "
    "taken along it (condition line, condition code, true/false arm) or, "
    "when conditions are unavailable, the line-level steps. An empty result "
    "means no control-flow path connects the two lines or the engine has no "
    "coverage for the function -- not an error; do not retry the same call. "
    "Example: cfg_paths(function='validate_input', from_line=9, to_line=12)."
)
DOMINATORS_DESCRIPTION = (
    "Find the statements that dominate and post-dominate a source line in "
    "its function's control-flow graph -- i.e. which guards MUST execute "
    "before the line is reached, and what always runs after it. 'function' "
    "is the exact function name (bare name, dotted qualname/suffix, or "
    "file:line anchor); 'line' is the target source line. Dominating "
    "statements (e.g. the guarding branch heads) execute on every path from "
    "function entry to the line; post-dominating statements execute on "
    "every path from the line to function exit. Each is reported as line "
    "plus first_line_code, nearest first. An empty result is an engine "
    "coverage gap, not an error; do not retry the same call. Example: "
    "dominators(function='validate_input', line=11)."
)
REINDEX_DESCRIPTION = (
    "Refresh the CFG engine's view of edited source files by dropping and "
    "rebuilding their cached per-file CFGs. Pass repo-relative paths to "
    "refresh (NOT absolute paths), or no paths to refresh every file "
    "currently reported stale by index_state. Returns exactly one item with "
    "the list of paths actually refreshed and the wall-clock cost in "
    "milliseconds. Example: reindex(paths=['src/core/session.py'])."
)
EXECUTED_PATH_DESCRIPTION = (
    "Show which of a function's control-flow blocks the FAILING TEST "
    "actually executed -- the recorded execution path of the bug's "
    "reproducing test run (collected before your session; the code is "
    "unmodified). Each block carries executed=true/false, so you can see "
    "exactly which branches the failing run took and which it skipped; the "
    "fix usually belongs ON the executed path (or on a guard the run "
    "wrongly took/skipped). 'function' is the exact function name (bare "
    "name, dotted qualname/suffix, or file:line anchor). Items summarize "
    "executed_blocks/total_blocks and name the test. A note item means no "
    "execution trace is available or the function was never executed by the "
    "failing test -- also a useful signal (the bug is upstream of it); not "
    "an error, do not retry. Example: executed_path(function='serialize')."
)
COVERED_BLOCKS_DESCRIPTION = (
    "Summarize, for every function in one file, how much of it the FAILING "
    "TEST's recorded run executed (executed lines and blocks per function). "
    "Use this to rank which functions in a suspect file are actually on the "
    "failing execution path before reading or editing them. Pass file as a "
    "repo-relative path. Items: {function, line, executed_lines, "
    "blocks_executed, blocks_total}, most-executed first. A note item means "
    "no execution trace is available -- not an error, do not retry. "
    "Example: covered_blocks(file='src/core/session.py')."
)
CRASH_PATH_DESCRIPTION = (
    "Show the sanitizer CRASH STACK as an ordered, source-annotated call "
    "chain -- the exact path the crashing input actually executed, recorded "
    "when the vulnerability fired. Frames run innermost first (frame #0 = the "
    "crashing statement) to outermost (the entry point); each carries its "
    "repo-relative file:line, function, and source line. When the report "
    "includes an allocation/free site (heap overflow, use-after-free), those "
    "provenance frames are returned too -- the allocation site is often the "
    "true repair locus, not the crashing read. Frames outside the repo (libc, "
    "sanitizer runtime) are marked in_repo=false. Use this FIRST on a "
    "security patch: the fix almost always lies on this path or at the "
    "allocation site. A note item means no sanitizer trace is available. "
    "Example: crash_path()."
)


# -- dynamic (execution-trace) tools ------------------------------------------


def _blocks_with_lines(row: dict, max_nodes: int):
    """(engine, [(block_id, {lines}, first_code)]) for a function's CFG.

    Uses Joern when a server is reachable (so C/C++ works), else the
    python-ast engine. Returns (engine_name, None) when neither can produce a
    CFG (non-Python source, no Joern) so callers emit a coverage note.
    """
    client = JoernClient()
    if client.available():
        try:
            recs = _joern_method_records(client, [row], row.get("file"))
        except JoernUnavailable:
            recs = []
        for _name, method in recs:
            graph, _e, _x = _joern_graph(method)
            if graph.number_of_nodes() == 0:
                continue
            out = []
            for nid, data in graph.nodes(data=True):
                ln = data.get("line")
                if not ln:
                    continue
                out.append((nid, {ln}, data.get("code", "")))
            if out:
                return "joern", out[: max(0, max_nodes)]
        # Joern reachable but no CFG for this function: coverage gap.
        return "joern", None
    # python-ast fallback
    try:
        cfg = _cfg_for_row(row)
    except (SyntaxError, UnicodeDecodeError):
        return engine.ENGINE_NAME, None
    out = []
    for block in cfg.blocks[: max(0, max_nodes)]:
        lines: set[int] = set()
        for (a, b) in block.get("spans") or []:
            lines.update(range(a, b + 1))
        first = cfg.first_stmt_line(block["id"])
        out.append((block["id"], lines,
                    cfg.code_at(first) if first else ""))
    return engine.ENGINE_NAME, out


def executed_path(function: str, max_nodes: int = 100) -> list[dict]:
    """The failing test's executed path through a function's CFG."""
    matches = _resolve_functions(function)
    trace = _load_trace()
    if trace is None or not trace.get("ok"):
        return [
            {
                "function": row["name"],
                "file": row["file"],
                "line": row["line"],
                "blocks": [],
                "executed_blocks": 0,
                "total_blocks": 0,
                "test": (trace or {}).get("test_id"),
                "reason": (trace or {}).get("reason"),
                "engine": engine.ENGINE_NAME,
                "note": _NO_TRACE_NOTE,
            }
            for row in matches
        ]
    items: list[dict] = []
    for row in matches:
        executed = _trace_lines(trace, row["file"])
        if executed is None:
            items.append(
                {
                    "function": row["name"],
                    "file": row["file"],
                    "line": row["line"],
                    "blocks": [],
                    "executed_blocks": 0,
                    "total_blocks": 0,
                    "test": trace.get("test_id"),
                    "engine": engine.ENGINE_NAME,
                    "note": (
                        "this file was never executed by the failing test -- "
                        "the bug's execution path does not reach it"
                    ),
                }
            )
            continue
        eng, block_data = _blocks_with_lines(row, max_nodes)
        if block_data is None:
            items.append(
                {
                    "function": row["name"],
                    "file": row["file"],
                    "line": row["line"],
                    "blocks": [],
                    "executed_blocks": 0,
                    "total_blocks": 0,
                    "test": trace.get("test_id"),
                    "engine": eng,
                    "note": (
                        "no CFG available for this function "
                        "(non-Python source and no Joern CPG coverage)"
                    ),
                }
            )
            continue
        blocks: list[dict] = []
        n_exec = 0
        for bid, lines, code in block_data:
            is_exec = bool(lines & executed)
            n_exec += is_exec
            blocks.append(
                {
                    "id": bid,
                    "line_range": [min(lines), max(lines)] if lines else None,
                    "first_line_code": code,
                    "executed": is_exec,
                }
            )
        items.append(
            {
                "function": row["name"],
                "file": row["file"],
                "line": row["line"],
                "blocks": blocks,
                "executed_blocks": n_exec,
                "total_blocks": len(blocks),
                "test": trace.get("test_id"),
                "engine": eng,
            }
        )
    return items


def covered_blocks(file: str) -> list[dict]:
    """Per-function failing-test coverage summary for one file."""
    rel = _normalize_rel(file)
    trace = _load_trace()
    if trace is None or not trace.get("ok"):
        return [
            {
                "file": rel,
                "test": (trace or {}).get("test_id"),
                "reason": (trace or {}).get("reason"),
                "engine": engine.ENGINE_NAME,
                "note": _NO_TRACE_NOTE,
            }
        ]
    executed = _trace_lines(trace, rel)
    if executed is None:
        return [
            {
                "file": rel,
                "test": trace.get("test_id"),
                "engine": engine.ENGINE_NAME,
                "note": (
                    "this file was never executed by the failing test -- the "
                    "bug's execution path does not reach it"
                ),
            }
        ]
    rows = [r for r in _load_function_rows() if r["file"] == rel]
    if not rows:
        raise ValueError(
            f"no functions found in {rel!r}; pass a repo-relative source path"
        )
    items: list[dict] = []
    for row in rows:
        span = set(range(row["line"], (row["end_line"] or row["line"]) + 1))
        fn_exec = executed & span
        blocks_total, blocks_exec = 0, 0
        eng, block_data = _blocks_with_lines(row, max_nodes=10_000)
        for _bid, lines, _code in (block_data or []):
            if not lines:
                continue
            blocks_total += 1
            blocks_exec += bool(lines & executed)
        items.append(
            {
                "function": row["qualname"] or row["name"],
                "file": rel,
                "line": row["line"],
                "executed_lines": len(fn_exec),
                "blocks_executed": blocks_exec,
                "blocks_total": blocks_total,
                "test": trace.get("test_id"),
                "engine": eng,
            }
        )
    items.sort(key=lambda it: (-it["executed_lines"], it["line"]))
    return items


#: Cap on how many source lines of context crash_path shows per frame.
_CRASH_SNIPPET_CHARS = 200


def _snippet_for(rel_file: str, line: int) -> str:
    """The single source line at rel_file:line from the repo, trimmed."""
    try:
        path = env.repo_root() / rel_file
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for i, text in enumerate(fh, start=1):
                if i == line:
                    return text.strip()[:_CRASH_SNIPPET_CHARS]
    except OSError:
        pass
    return ""


def crash_path(max_frames: int = 30) -> list[dict]:
    """The sanitizer crash stack as an ordered, source-annotated call chain.

    SecBench-native dynamic tool: the recorded sanitizer run's stack is the
    exact path the crashing input executed. Returns the crash frames innermost
    (#0, the crashing statement) to outermost (the entry point), each with its
    repo-relative file:line and source line, and -- when present -- the
    allocation/free provenance frames (the site that produced the bad memory,
    the usual root-cause locus for overflow/use-after-free). Frames outside the
    repo (libc, sanitizer runtime) are marked in_repo=false. A note item means
    no sanitizer trace is available for this run.
    """
    trace = _load_trace()
    if trace is None or not trace.get("ok"):
        return [
            {
                "frames": [],
                "sanitizer": (trace or {}).get("sanitizer"),
                "reason": (trace or {}).get("reason"),
                "engine": "sanitizer",
                "note": _NO_TRACE_NOTE,
            }
        ]

    def _shape(frames: list) -> list[dict]:
        out = []
        for fr in frames[: max(0, max_frames)]:
            in_repo = bool(fr.get("in_repo"))
            out.append(
                {
                    "frame": fr.get("frame_no"),
                    "function": fr.get("func"),
                    "file": fr.get("file"),
                    "line": fr.get("line"),
                    "in_repo": in_repo,
                    "code": _snippet_for(fr["file"], fr["line"])
                    if in_repo and fr.get("line") else "",
                }
            )
        return out

    return [
        {
            "sanitizer": trace.get("sanitizer"),
            "crash_frames": _shape(trace.get("crash_frames") or []),
            "alloc_frames": _shape(trace.get("alloc_frames") or []),
            "engine": "sanitizer",
        }
    ]


# -- function resolution (symbols.db conventions, as in callgraph_server) -----


def _load_function_rows() -> list[dict]:
    """All function/method rows from symbols.db in the index dir."""
    db_path = env.index_dir() / navkit.SYMBOLS_DB_NAME
    if not db_path.is_file():
        raise FileNotFoundError(
            f"symbols.db not found at {db_path}; "
            "run 'python -m indexer.build_index' first"
        )
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT name, qualname, file, line, end_line FROM symbols "
            "WHERE kind IN ('function', 'method') ORDER BY qualname, file, line"
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "name": name,
            "qualname": qualname,
            "file": file,
            "line": line,
            "end_line": end_line,
        }
        for name, qualname, file, line, end_line in rows
    ]


def _parse_anchor(spec: str) -> Optional[tuple[str, int]]:
    """Parse a ``file.py:line`` anchor; None when spec is not anchor-shaped."""
    if ":" not in spec:
        return None
    path_part, _, line_part = spec.rpartition(":")
    if not path_part or not line_part.isdigit():
        return None
    return path_part.replace("\\", "/"), int(line_part)


# -- live-file fallback: agent-written code ----------------------------------
#
# symbols.db is a build-time snapshot, so functions the AGENT writes during a
# run -- a new def Edited into an indexed file, or a brand-new file Written
# whole -- do not exist in it, and resolution returned null on code that
# existed in the worktree at call time (2026-08-21 calls10 audit, GRAPH_WRONG
# cells 2dd8966f/7e1a3476). The staleness disclosure cannot even see the
# second case: Manifest.check only stats manifest entries, so a brand-new
# file reports stale_files=0. The fallback runs ONLY on a resolution miss:
# anchored specs re-parse the named file; bare names re-parse the (bounded)
# set of files changed or created since index build. Python-only -- C keeps
# the index/Joern path.

# path -> (mtime_ns, size, rows); reparse only when the file changes
_LIVE_ROW_CACHE: dict[str, tuple[int, int, list[dict]]] = {}
# throttle for the new-file walk: (monotonic_stamp, rel_paths)
_LIVE_SCAN_CACHE: dict = {"stamp": 0.0, "rels": []}
_LIVE_SCAN_TTL = 5.0
_LIVE_PARSE_CAP = 50
_LIVE_SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules",
                   ".tox", ".mypy_cache", ".pytest_cache", ".repr_index"}


def _live_module_qualname(rel: str) -> str:
    """Dotted module path for a repo-relative posix path (symbols.db
    convention -- mirrors indexer.backends.symbols.module_qualname)."""
    stem = rel[:-3] if rel.endswith(".py") else rel
    parts = [p for p in stem.split("/") if p]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        parts = ["__init__"]
    return ".".join(parts)


def _live_rows_for_file(rel: str) -> list[dict]:
    """Symbol rows parsed from ONE live .py file (indexed or not).

    Row shape and qualname convention match _load_function_rows so callers
    can mix db and live rows freely. [] on any failure -- the caller falls
    through to its normal miss handling."""
    rel = rel.replace("\\", "/").lstrip("/")
    if not rel.endswith(".py"):
        return []
    path = env.repo_root() / rel
    try:
        st = path.stat()
    except OSError:
        # the spec may name the file by a path SUFFIX (db filtering allows
        # that); look for a changed/new file ending in it
        tail = "/" + rel
        for cand in _changed_or_new_py_rels():
            if cand == rel or cand.endswith(tail):
                rel, path = cand, env.repo_root() / cand
                break
        else:
            return []
        try:
            st = path.stat()
        except OSError:
            return []
    key = str(path)
    cached = _LIVE_ROW_CACHE.get(key)
    if cached is not None and cached[0] == st.st_mtime_ns \
            and cached[1] == st.st_size:
        return cached[2]
    try:
        tree = _pyast.parse(path.read_text(encoding="utf-8",
                                           errors="replace"))
    except (SyntaxError, ValueError, OSError):
        _LIVE_ROW_CACHE[key] = (st.st_mtime_ns, st.st_size, [])
        return []
    modq = _live_module_qualname(rel)
    rows: list[dict] = []

    def walk(node, scope: str) -> None:
        for child in _pyast.iter_child_nodes(node):
            if isinstance(child, (_pyast.FunctionDef,
                                  _pyast.AsyncFunctionDef)):
                qual = scope + "." + child.name
                rows.append({
                    "name": child.name,
                    "qualname": qual,
                    "file": rel,
                    "line": child.lineno,
                    "end_line": getattr(child, "end_lineno", None)
                    or child.lineno,
                })
                walk(child, qual)
            elif isinstance(child, _pyast.ClassDef):
                walk(child, scope + "." + child.name)
            else:
                walk(child, scope)

    walk(tree, modq)
    _LIVE_ROW_CACHE[key] = (st.st_mtime_ns, st.st_size, rows)
    return rows


def _changed_or_new_py_rels() -> list[str]:
    """Repo-relative .py files changed or created since index build.

    Manifest drift (cheap stats) finds edited files; a pruned walk finds
    files the manifest has never heard of. Result cached briefly -- this
    only runs on a resolution miss, but misses can cluster."""
    now = time.monotonic()
    if now - _LIVE_SCAN_CACHE["stamp"] < _LIVE_SCAN_TTL:
        return _LIVE_SCAN_CACHE["rels"]
    root = env.repo_root()
    manifest = None
    try:
        manifest = staleness.Manifest.load(env.index_dir())
    except Exception:
        manifest = None
    known = dict(manifest.files) if manifest is not None else {}
    changed: list[tuple[float, str]] = []
    for rel, entry in known.items():
        if not rel.endswith(".py"):
            continue
        try:
            st = (root / rel).stat()
        except OSError:
            continue
        if st.st_size != entry.get("size") \
                or st.st_mtime != entry.get("mtime"):
            changed.append((st.st_mtime, rel))
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _LIVE_SKIP_DIRS]
        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            p = Path(dirpath) / fname
            try:
                rel = p.relative_to(root).as_posix()
            except ValueError:
                continue
            if rel in known:
                continue
            try:
                changed.append((p.stat().st_mtime, rel))
            except OSError:
                continue
    # newest first; cap so a pathological worktree cannot trigger a
    # repo-wide parse storm (agents touch a handful of files per run)
    changed.sort(reverse=True)
    rels = [rel for _mt, rel in changed[:_LIVE_PARSE_CAP]]
    _LIVE_SCAN_CACHE["stamp"] = now
    _LIVE_SCAN_CACHE["rels"] = rels
    return rels


def _live_changed_rows() -> list[dict]:
    """Rows from every file changed/created since index build (bounded)."""
    rows: list[dict] = []
    for rel in _changed_or_new_py_rels():
        rows.extend(_live_rows_for_file(rel))
    return rows


def _resolve_functions(spec: str, file: Optional[str] = None) -> list[dict]:
    """Resolve a bare name, qualname (or suffix), or anchor to symbol rows.

    Ambiguous names return ALL matching rows; anchors return the innermost
    function whose line range contains the anchor line; ``file`` narrows the
    candidates first. Raises ValueError when nothing matches.
    """
    text = spec.strip()
    if not text:
        raise ValueError("function name must not be empty")
    rows = _load_function_rows()
    if file:
        rel = file.replace("\\", "/")
        rows = [
            row
            for row in rows
            if row["file"] == rel or row["file"].endswith("/" + rel)
        ]
        if not rows:
            # a file the index has never seen (agent-Written mid-run)
            rows = _live_rows_for_file(rel)
    anchor = _parse_anchor(text)
    if anchor is not None:
        path, line = anchor

        def _containing(cand_rows):
            return [
                row
                for row in cand_rows
                if (row["file"] == path or row["file"].endswith("/" + path))
                and row["line"] <= line <= row["end_line"]
            ]

        containing = _containing(rows)
        if not containing:
            # live fallback: the anchored line may sit in a function the
            # agent just wrote (new def in an indexed file, or a new file)
            containing = _containing(_live_rows_for_file(path))
        if containing:
            return [max(containing, key=lambda row: row["line"])]
        raise ValueError(f"no function found at anchor {text!r}")
    # file-scoped NAME form ('pkg/mod.py:func_name'): a natural agent request
    # (the anchor form with a name instead of a line) that used to fall
    # through to GLOBAL name matching and miss (2026-08-14 audit: the target
    # existed in the named file). Resolve the name restricted to that file.
    if ":" in text and "::" not in text:
        path_part, _, name_part = text.rpartition(":")
        src_exts = (".py", ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp")
        if (path_part and name_part and not name_part.isdigit()
                and path_part.replace("\\", "/").lower().endswith(src_exts)
                and re.fullmatch(r"[A-Za-z_][\w.]*", name_part)):
            return _resolve_functions(name_part, file=path_part)
    matches: list[dict] = []
    seen: set[tuple[str, int]] = set()

    def _add(row: dict) -> None:
        key = (row["file"], row["line"])
        if key not in seen:
            seen.add(key)
            matches.append(row)

    def _match_names(cand_rows) -> None:
        for row in cand_rows:
            if row["qualname"] == text:
                _add(row)
        for row in cand_rows:
            if row["name"] == text:
                _add(row)
        if "." in text:
            suffix = "." + text
            for row in cand_rows:
                if row["qualname"].endswith(suffix):
                    _add(row)
        # C++ ``Class::method`` handling: rows may be indexed either
        # qualified (out-of-class defs -> "Class::method") or bare (in-class
        # defs -> "method"). Match in both directions on the last ``::``
        # component so the agent's chosen form resolves regardless of how
        # the def was written.
        if "::" in text:
            last = text.rsplit("::", 1)[-1]
            for row in cand_rows:
                rn, rq = row["name"], row["qualname"]
                if (rn == last or rq == last
                        or rq.endswith("::" + text)
                        or text.endswith("::" + rn)):
                    _add(row)
        elif text:
            # bare name: also match a qualified row whose final component
            # equals it
            for row in cand_rows:
                if row["qualname"].endswith("::" + text):
                    _add(row)

    _match_names(rows)
    if not matches:
        # live fallback: the name may belong to code the agent just wrote
        # (not in the build-time snapshot). File-scoped queries re-parse
        # that file (covers a new def Edited into an indexed file); bare
        # names scan only files changed/created since index build.
        if file:
            _match_names(_live_rows_for_file(file.replace("\\", "/")))
        else:
            _match_names(_live_changed_rows())
    if not matches:
        raise ValueError(
            f"no function matching {spec!r} in the symbol index; "
            "use find_symbol to locate the right name"
        )
    return matches


def _cfg_for_row(row: dict) -> "engine.FunctionCFG":
    """Build (or fetch from cache) the python-ast CFG for one symbol row."""
    abs_path = env.repo_root() / row["file"]
    return engine.cfg_at_line(abs_path, row["file"], row["line"])


# -- Joern engine path (get_cfg only; never integration-tested here) ----------


def _cpgql_escape(value: str) -> str:
    """Escape a string for embedding in a double-quoted CPGQL literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _file_name_regex(file: str) -> str:
    """A CPGQL file-name regex that matches by basename.

    The scoped kernel CPGs are built with a subsystem directory (or single
    file) as the c2cpg root, so ``location.filename`` is stored relative to
    that root -- typically just the basename (e.g. ``vga_switcheroo.c``), NOT
    the repo-relative path the tool caller supplies. Matching on the basename
    (anchored at the end with ``$``) is robust to either build root. The dot
    is left as a regex wildcard on purpose: a literal ``\\.`` is an illegal
    escape inside a Scala string literal, and matching ``c`` after any char is
    harmless for C source names.
    """
    basename = file.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return ".*" + _cpgql_escape(basename) + "$"


#: Cap on a block's ``first_line_code`` so the whole-method-body code of
#: Joern's entry/block node cannot blow the 4k-token envelope budget (which
#: would silently drop every block from the first page). One line, bounded.
_FIRST_LINE_MAX = 200


def _first_line(code: object) -> str:
    """The first source line of a node's code, whitespace-trimmed and capped.

    Joern's method/block node carries the entire function body as its
    ``code``; the ``first_line_code`` contract is one representative line, so
    only the first non-empty line is kept (bounded to keep the result
    envelope within its token budget).
    """
    text = str(code or "")
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if stripped:
            return stripped[:_FIRST_LINE_MAX]
    return text.strip()[:_FIRST_LINE_MAX]


def _joern_cfg_query(name: str, file: Optional[str] = None) -> str:
    """The CPGQL query returning CFG nodes and edges of methods named name."""
    base = f'cpg.method.name("{_cpgql_escape(name)}")'
    if file:
        base += f'.where(_.file.name("{_file_name_regex(file)}"))'
    return (
        base
        + '.map(m => Map('
        '"name" -> m.name, '
        '"fullName" -> m.fullName, '
        '"file" -> m.location.filename, '
        '"line" -> m.lineNumber.getOrElse(-1), '
        '"entry" -> m.id, '
        '"exit" -> m.methodReturn.id, '
        '"nodes" -> m.cfgNode.map(n => Map('
        '"id" -> n.id, '
        '"code" -> n.code, '
        '"line" -> n.lineNumber.getOrElse(-1))).l, '
        '"edges" -> m.cfgNode.flatMap(n => n.cfgNext.map(t => Map('
        '"src" -> n.id, "dst" -> t.id))).l'
        ")).toJson"
    )


def _joern_method_records(
    client: JoernClient, matches: list[dict], file: Optional[str]
) -> list[tuple[str, dict]]:
    """Raw (queried-name, method-record) pairs from the Joern server.

    Raises JoernUnavailable when the server cannot answer; returns [] when
    the CPG has no coverage for the requested names.
    """
    records: list[tuple[str, dict]] = []
    for name in sorted({row["name"] for row in matches}):
        payload = client.query_json(_joern_cfg_query(name, file))
        methods = payload if isinstance(payload, list) else [payload]
        for method in methods:
            if isinstance(method, dict):
                records.append((name, method))
    return records


def _joern_get_cfg(
    client: JoernClient, matches: list[dict], file: Optional[str], max_nodes: int
) -> list[dict]:
    """Answer get_cfg from the Joern server; one item per returned method.

    Raises JoernUnavailable when the server cannot answer; the caller falls
    back to the python-ast engine. Returns [] when the CPG has no coverage
    for the requested names (the caller also falls back then).
    """
    return [
        _joern_cfg_item(method, name, max_nodes)
        for name, method in _joern_method_records(client, matches, file)
    ]


def _joern_graph(method: dict) -> tuple["nx.DiGraph", object, object]:
    """A full (un-truncated) DiGraph over a Joern method's raw CFG node ids.

    Nodes carry ``line`` (int or None for synthetic nodes) and ``code``
    attributes. The METHOD entry and METHOD_RETURN exit nodes are not
    cfgNodes, so they arrive without edges of their own; they are wired to
    the in-degree-0 (resp. out-degree-0) real nodes so dominator and path
    computations rooted at entry/exit see the whole graph.
    """
    graph: "nx.DiGraph" = nx.DiGraph()
    raw_entry = method.get("entry")
    raw_exit = method.get("exit")
    for rid in (raw_entry, raw_exit):
        if rid is not None:
            graph.add_node(rid, line=None, code="")
    for node in method.get("nodes", []):
        if not isinstance(node, dict) or node.get("id") is None:
            continue
        line = node.get("line")
        graph.add_node(
            node["id"],
            line=line if isinstance(line, int) and line > 0 else None,
            code=_first_line(node.get("code", "")),
        )
    for edge in method.get("edges", []):
        if (
            isinstance(edge, dict)
            and edge.get("src") in graph
            and edge.get("dst") in graph
        ):
            graph.add_edge(edge["src"], edge["dst"])
    if raw_entry is not None:
        for node in list(graph.nodes):
            if node not in (raw_entry, raw_exit) and graph.in_degree(node) == 0:
                graph.add_edge(raw_entry, node)
        if graph.out_degree(raw_entry) == 0:
            # Every real node sits on a cycle (e.g. a loop heads the body):
            # fall back to wiring entry to the earliest-line node.
            real = [n for n, d in graph.nodes(data=True) if d.get("line")]
            if real:
                graph.add_edge(
                    raw_entry, min(real, key=lambda n: graph.nodes[n]["line"])
                )
    if raw_exit is not None:
        for node in list(graph.nodes):
            if node not in (raw_entry, raw_exit) and graph.out_degree(node) == 0:
                graph.add_edge(node, raw_exit)
    return graph, raw_entry, raw_exit


def _joern_id_map(graph: "nx.DiGraph") -> dict:
    """Sequential small-int ids for a method graph's raw 64-bit Joern ids."""
    return {nid: idx for idx, nid in enumerate(graph.nodes)}


def _nodes_at_line(graph: "nx.DiGraph", line: int) -> list:
    """Raw node ids whose source line equals ``line``, insertion-ordered."""
    return [n for n, d in graph.nodes(data=True) if d.get("line") == line]


def _paths_between(
    graph: "nx.DiGraph", sources: list, sinks: list, max_paths: int
) -> list[list]:
    """Up to max_paths shortest simple paths from any source to any sink.

    Uses temporary synthetic super-source/sink nodes so one enumeration
    covers the whole source x sink product, shortest first.
    """
    if not sources or not sinks:
        return []
    super_src, super_dst = "__cfg_paths_src__", "__cfg_paths_dst__"
    graph.add_node(super_src)
    graph.add_node(super_dst)
    for src in sources:
        graph.add_edge(super_src, src)
    for dst in sinks:
        graph.add_edge(dst, super_dst)
    try:
        raw = list(
            itertools.islice(
                nx.shortest_simple_paths(graph, super_src, super_dst), max_paths
            )
        )
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        raw = []
    finally:
        graph.remove_node(super_src)
        graph.remove_node(super_dst)
    return [path[1:-1] for path in raw]


#: Cap on the per-step code lines included with each Joern path item.
_PATH_STEP_MAX = 40


def _joern_cfg_paths(
    client: JoernClient,
    matches: list[dict],
    from_line: int,
    to_line: int,
    max_paths: int,
) -> tuple[list[dict], bool]:
    """(items, covered) for cfg_paths answered from the Joern CFG.

    ``covered`` is False when the CPG has no method record for any match, so
    the caller can distinguish "no coverage" from "no connecting path".
    Joern CFG edges carry no true/false labels, so ``conditions`` is empty
    and each path instead reports its line-level steps.
    """
    items: list[dict] = []
    covered = False
    for name, method in _joern_method_records(client, matches, None):
        graph, _entry, _exit = _joern_graph(method)
        if graph.number_of_nodes() == 0:
            continue
        covered = True
        id_map = _joern_id_map(graph)
        sources = _nodes_at_line(graph, from_line)
        sinks = _nodes_at_line(graph, to_line)
        for path in _paths_between(graph, sources, sinks, max_paths):
            steps = [
                {
                    "line": graph.nodes[node]["line"],
                    "code": graph.nodes[node]["code"],
                }
                for node in path[:_PATH_STEP_MAX]
                if graph.nodes[node].get("line")
            ]
            items.append(
                {
                    "function": str(method.get("name", name)),
                    "file": str(method.get("file", "")),
                    "from_line": from_line,
                    "to_line": to_line,
                    "blocks": [id_map[node] for node in path],
                    "length": len(path) - 1,
                    "conditions": [],
                    "steps": steps,
                    "engine": "joern",
                }
            )
    items.sort(key=lambda item: item["length"])
    return items[:max_paths], covered


def _joern_dominators(
    client: JoernClient, matches: list[dict], line: int
) -> tuple[list[dict], bool]:
    """(items, covered) for dominators answered from the Joern CFG."""
    items: list[dict] = []
    covered = False
    for name, method in _joern_method_records(client, matches, None):
        graph, raw_entry, raw_exit = _joern_graph(method)
        if graph.number_of_nodes() == 0:
            continue
        covered = True
        targets = _nodes_at_line(graph, line)
        if not targets:
            continue
        node = targets[0]
        id_map = _joern_id_map(graph)

        def _chain_statements(chain: list) -> list[dict]:
            out: list[dict] = []
            seen: set = set()
            for block in chain:
                data = graph.nodes[block]
                block_line = data.get("line")
                if block_line is None or block_line == line:
                    continue
                if block_line in seen:
                    continue
                seen.add(block_line)
                out.append(
                    {"line": block_line, "first_line_code": data.get("code", "")}
                )
            return out

        dom_chain = (
            _dominator_chain(graph, raw_entry, node)
            if raw_entry is not None
            else []
        )
        post_chain = (
            _dominator_chain(graph.reverse(copy=True), raw_exit, node)
            if raw_exit is not None
            else []
        )
        items.append(
            {
                "function": str(method.get("name", name)),
                "file": str(method.get("file", "")),
                "line": line,
                "block": id_map.get(node),
                "dominating": _chain_statements(dom_chain),
                "post_dominating": _chain_statements(post_chain),
                "engine": "joern",
            }
        )
    return items, covered


def _joern_cfg_item(method: dict, name: str, max_nodes: int) -> dict:
    """Build one cfg result item from a Joern method record (id-remapped).

    Joern's opaque 64-bit node ids (~11 digits, each appearing twice per
    edge) are remapped to small sequential ints; the ids are only meaningful
    within this one CFG, and the originals blow the 4k-token envelope budget
    on a large method (which would drop the whole item from the first page).
    The block count is capped both by ``max_nodes`` and -- defensively -- so
    the finished item stays within the envelope's token budget.
    """
    all_nodes = [n for n in method.get("nodes", []) if isinstance(n, dict)]
    raw_entry = method.get("entry")
    raw_exit = method.get("exit")
    cap = max(0, max_nodes)

    def _build(node_limit: int) -> dict:
        nodes = all_nodes[:node_limit]
        id_map: dict = {}
        # Register entry/exit first so they always get an id even if they are
        # not themselves cfgNodes (METHOD / METHOD_RETURN nodes are not).
        for rid in (raw_entry, raw_exit):
            if rid is not None and rid not in id_map:
                id_map[rid] = len(id_map)
        for node in nodes:
            nid = node.get("id")
            if nid is not None and nid not in id_map:
                id_map[nid] = len(id_map)
        blocks = []
        for node in nodes:
            line = node.get("line")
            in_file = isinstance(line, int) and line > 0
            blocks.append(
                {
                    "id": id_map.get(node.get("id")),
                    "line_range": [line, line] if in_file else None,
                    "first_line_code": _first_line(node.get("code", "")),
                }
            )
        edges = [
            {
                "src": id_map[edge.get("src")],
                "dst": id_map[edge.get("dst")],
                "kind": "next",
            }
            for edge in method.get("edges", [])
            if isinstance(edge, dict)
            and edge.get("src") in id_map
            and edge.get("dst") in id_map
        ]
        return {
            "function": str(method.get("name", name)),
            "file": str(method.get("file", "")),
            "line": method.get("line"),
            "blocks": blocks,
            "edges": edges,
            "entry": id_map.get(raw_entry),
            "exit": id_map.get(raw_exit),
            "engine": "joern",
        }

    item = _build(cap)
    # Keep a single item within the per-call token budget: a CFG that fills
    # the whole budget on its own would otherwise be dropped entirely by the
    # envelope. Shrink the node cap until it fits (or only one block remains).
    limit = cap
    while limit > 1 and budget.estimate_tokens([item]) > budget.TOKEN_BUDGET:
        limit = max(1, int(limit * 0.8))
        item = _build(limit)
    if limit < len(all_nodes):
        item["truncated_blocks"] = True
    return item


# -- dominator helpers ---------------------------------------------------------


def _dominator_chain(graph: "nx.DiGraph", root: int, node: int) -> list[int]:
    """Strict dominators of node w.r.t. root, nearest first, ending at root."""
    if root not in graph or node not in graph:
        return []
    # NOTE: networkx may or may not include the root in the idom mapping.
    idom = nx.immediate_dominators(graph, root)
    chain: list[int] = []
    current = node
    while current != root:
        parent = idom.get(current)
        if parent is None or parent == current:
            break
        chain.append(parent)
        current = parent
    return chain


def _statements(
    cfg: "engine.FunctionCFG", block_ids: list[int], use_last: bool
) -> list[dict]:
    """(line, first_line_code) anchors for blocks; synthetic blocks skipped.

    ``use_last=True`` reports each block's last statement (the branch
    decision); ``use_last=False`` reports its first statement (the rejoin
    point).
    """
    out: list[dict] = []
    for block_id in block_ids:
        line = (
            cfg.last_stmt_line(block_id)
            if use_last
            else cfg.first_stmt_line(block_id)
        )
        if line is None:
            continue
        out.append({"line": line, "first_line_code": cfg.code_at(line)})
    return out


def _stale_paths() -> list[str]:
    """Repo-relative paths currently reported stale by the index manifest."""
    manifest = staleness.Manifest.load(env.index_dir())
    if manifest is None:
        return []
    root = env.repo_root()
    stale: list[str] = []
    for rel, entry in manifest.files.items():
        path = root / rel
        try:
            stat = path.stat()
        except OSError:
            stale.append(rel)
            continue
        if stat.st_size != entry.get("size") or stat.st_mtime != entry.get("mtime"):
            stale.append(rel)
    return stale


def _normalize_rel(path: str) -> str:
    """Normalize a user-supplied path to repo-root-relative POSIX form."""
    text = path.replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


# -- tools -----------------------------------------------------------------------


def get_cfg(
    function: str, file: Optional[str] = None, max_nodes: int = 100
) -> list[dict]:
    """Control-flow graph (blocks, edges, entry, exit) per matching function."""
    if max_nodes <= 0:
        return []
    matches = _resolve_functions(function, file)
    client = JoernClient()
    if client.available():
        try:
            items = _joern_get_cfg(client, matches, file, max_nodes)
        except JoernUnavailable:
            # Server configured but UNREACHABLE -> behave as if Joern were
            # absent: snippet CPG for C/C++, python-ast engine otherwise.
            return _cfg_fallback_items(matches, max_nodes)
        if items:
            return items
        # Server REACHABLE but no coverage for this function. Try a snippet
        # CPG of just this function's source (works for any C function the
        # symbol index resolves); only when that too fails emit a note.
        return _cfg_fallback_items(matches, max_nodes, joern_reachable=True)
    return _cfg_fallback_items(matches, max_nodes)


def _snippet_cfg_item(row: dict, max_nodes: int) -> Optional[dict]:
    """get_cfg item for a C/C++ function via a PER-FUNCTION snippet CPG.

    Needs NO whole-repo CPG and no running ``joern --server``: the function
    is resolved in the (unscoped, whole-tree) symbol index, its source is
    line-extracted, and c2cpg builds a CPG of just that source
    (``joern_cfg.c_cfg_method_records`` -- the same mechanism flow_plan/
    flow_check already use to fingerprint C). Coverage is therefore uniform
    across the repo instead of following a prebuilt CPG's scope (the
    LinuxFLBench leak fix: the per-version kernel CPGs were scoped to the
    ground-truth subsystem). Returns None when the row is not C/C++, the
    Joern CLIs are absent, or the build fails -- callers fall back.
    """
    from servers.common import flowedit, joern_cfg
    path = str(row.get("file", ""))
    if not joern_cfg.is_c_file(path) or not joern_cfg.available():
        return None
    src = flowedit._func_source_ranged(
        env.repo_root() / path, row["line"], row.get("end_line")
    )
    if not src:
        return None
    suffix = Path(path).suffix.lower() or ".c"
    records = joern_cfg.c_cfg_method_records(src, suffix=suffix)
    if not records:
        return None
    # the snippet can parse more than one method (helper decls); prefer the
    # record whose name matches the resolved row.
    named = [r for r in records
             if isinstance(r, dict) and r.get("name") == row["name"]]
    candidates = named or [r for r in records if isinstance(r, dict)]
    if not candidates:
        return None
    method = dict(candidates[0])
    offset = int(row["line"]) - 1
    method["file"] = path
    method["line"] = row["line"]
    method["nodes"] = [
        {
            **node,
            "line": (
                node["line"] + offset
                if isinstance(node.get("line"), int) and node["line"] > 0
                else None
            ),
        }
        for node in method.get("nodes", [])
        if isinstance(node, dict)
    ]
    item = _joern_cfg_item(method, row["name"], max_nodes)
    item["engine"] = "joern_snippet"
    return item


def _cfg_fallback_items(
    matches: list[dict], max_nodes: int, joern_reachable: bool = False
) -> list[dict]:
    """Best-effort CFG per row when no (reachable, covering) Joern server.

    C/C++ rows get a snippet-CPG item; Python rows get the python-ast
    engine; a C row that cannot even snippet-build yields the explicit miss
    note when a Joern server was reachable (its CPG lacked the function),
    else the python-ast parse note.
    """
    from servers.common import joern_cfg
    items: list[dict] = []
    for row in matches:
        snippet = _snippet_cfg_item(row, max_nodes)
        if snippet is not None:
            items.append(snippet)
            continue
        if joern_reachable and joern_cfg.is_c_file(str(row.get("file", ""))):
            items.append(_joern_miss_item(row))
            continue
        items.extend(_python_ast_cfg([row], max_nodes))
    return items


def _joern_miss_item(row: dict) -> dict:
    """An empty cfg item: no CFG could be produced for this function.

    The note deliberately says nothing about WHAT the loaded CPG covers
    (the old wording named the benchmark-subsystem scoping -- an oracle
    tell); it only asks for a better anchor.
    """
    return {
        "function": row["name"],
        "file": row["file"],
        "line": row["line"],
        "blocks": [],
        "edges": [],
        "entry": None,
        "exit": None,
        "engine": "joern",
        "note": (
            "no control-flow graph could be built for this function; pass "
            "the exact function name (or a file:line anchor inside it)"
        ),
    }


def _python_ast_cfg(matches: list[dict], max_nodes: int) -> list[dict]:
    """python-ast CFG per match; non-Python sources yield a clear note item.

    The pure-Python engine parses with stdlib ``ast`` and therefore cannot
    handle C/other languages; a parse failure becomes an empty note item
    instead of an exception so the tool never crashes on a non-Python repo
    with no Joern server configured.
    """
    items: list[dict] = []
    for row in matches:
        try:
            items.append(_cfg_for_row(row).to_item(max_nodes))
        except (SyntaxError, UnicodeDecodeError) as exc:
            items.append(
                {
                    "function": row["name"],
                    "file": row["file"],
                    "line": row["line"],
                    "blocks": [],
                    "edges": [],
                    "entry": None,
                    "exit": None,
                    "engine": engine.ENGINE_NAME,
                    "note": (
                        "python-ast engine could not parse this source "
                        f"({type(exc).__name__}); a Joern CPG is required for "
                        "non-Python languages"
                    ),
                }
            )
    return items


def _paths_note_item(
    row: dict, from_line: int, to_line: int, note: str, engine_name: str
) -> dict:
    """An empty cfg_paths item carrying an explanatory note."""
    return {
        "function": row["name"],
        "file": row["file"],
        "from_line": from_line,
        "to_line": to_line,
        "blocks": [],
        "length": 0,
        "conditions": [],
        "engine": engine_name,
        "note": note,
    }


_CPG_SCOPE_NOTE = (
    "function not found in the loaded Joern CPG; pass the exact function "
    "name (or a file:line anchor inside it)"
)


def cfg_paths(
    function: str, from_line: int, to_line: int, max_paths: int = 5
) -> list[dict]:
    """Control-flow paths between two lines, with branch conditions taken."""
    if max_paths <= 0:
        return []
    matches = _resolve_functions(function)
    client = JoernClient()
    if client.available():
        try:
            items, covered = _joern_cfg_paths(
                client, matches, from_line, to_line, max_paths
            )
        except JoernUnavailable:
            # Server configured but UNREACHABLE -> python-ast engine.
            return _python_cfg_paths(matches, from_line, to_line, max_paths)
        if covered:
            return items
        # Server REACHABLE but no CPG coverage: try the python-ast engine
        # (answers Python repos); on non-Python sources report a note item.
        return _python_cfg_paths(
            matches, from_line, to_line, max_paths, miss_note=_CPG_SCOPE_NOTE
        )
    return _python_cfg_paths(matches, from_line, to_line, max_paths)


def _python_cfg_paths(
    matches: list[dict],
    from_line: int,
    to_line: int,
    max_paths: int,
    miss_note: Optional[str] = None,
) -> list[dict]:
    """cfg_paths on the python-ast engine; parse failures become note items."""
    items: list[dict] = []
    note_items: list[dict] = []
    lines_found = False
    for row in matches:
        try:
            cfg = _cfg_for_row(row)
        except (SyntaxError, UnicodeDecodeError) as exc:
            note = miss_note or (
                "python-ast engine could not parse this source "
                f"({type(exc).__name__}); a Joern CPG is required for "
                "non-Python languages"
            )
            note_items.append(
                _paths_note_item(
                    row,
                    from_line,
                    to_line,
                    note,
                    "joern" if miss_note else engine.ENGINE_NAME,
                )
            )
            continue
        src = cfg.block_for_line(from_line)
        dst = cfg.block_for_line(to_line)
        if src is None or dst is None:
            continue
        lines_found = True
        graph = cfg.graph()
        if src == dst:
            paths = [[src]]
        else:
            try:
                paths = list(
                    itertools.islice(
                        nx.shortest_simple_paths(graph, src, dst), max_paths
                    )
                )
            except (nx.NetworkXNoPath, nx.NodeNotFound):
                paths = []
        for path in paths:
            conditions: list[dict] = []
            for src_block, dst_block in zip(path, path[1:]):
                kind = graph.edges[src_block, dst_block]["kind"]
                if kind not in ("true", "false"):
                    continue
                line = cfg.last_stmt_line(src_block)
                conditions.append(
                    {
                        "line": line,
                        "condition": cfg.code_at(line) if line else "",
                        "taken": kind,
                    }
                )
            items.append(
                {
                    "function": cfg.name,
                    "file": cfg.file,
                    "from_line": from_line,
                    "to_line": to_line,
                    "blocks": list(path),
                    "length": len(path) - 1,
                    "conditions": conditions,
                    "engine": engine.ENGINE_NAME,
                }
            )
    if not lines_found:
        if note_items:
            return note_items
        raise ValueError(
            f"lines {from_line} and {to_line} do not both fall inside any "
            "definition matching the requested function"
        )
    return items[:max_paths]


def _dominators_note_item(
    row: dict, line: int, note: str, engine_name: str
) -> dict:
    """An empty dominators item carrying an explanatory note."""
    return {
        "function": row["name"],
        "file": row["file"],
        "line": line,
        "block": None,
        "dominating": [],
        "post_dominating": [],
        "engine": engine_name,
        "note": note,
    }


def dominators(function: str, line: int) -> list[dict]:
    """Dominating and post-dominating statements of a line, nearest first."""
    matches = _resolve_functions(function)
    client = JoernClient()
    if client.available():
        try:
            items, covered = _joern_dominators(client, matches, line)
        except JoernUnavailable:
            return _python_dominators(matches, line)
        if covered:
            if items:
                return items
            raise ValueError(
                f"line {line} is not inside any CPG-covered definition "
                "matching the requested function"
            )
        return _python_dominators(matches, line, miss_note=_CPG_SCOPE_NOTE)
    return _python_dominators(matches, line)


def _python_dominators(
    matches: list[dict], line: int, miss_note: Optional[str] = None
) -> list[dict]:
    """dominators on the python-ast engine; parse failures become note items."""
    items: list[dict] = []
    note_items: list[dict] = []
    for row in matches:
        try:
            cfg = _cfg_for_row(row)
        except (SyntaxError, UnicodeDecodeError) as exc:
            note = miss_note or (
                "python-ast engine could not parse this source "
                f"({type(exc).__name__}); a Joern CPG is required for "
                "non-Python languages"
            )
            note_items.append(
                _dominators_note_item(
                    row, line, note, "joern" if miss_note else engine.ENGINE_NAME
                )
            )
            continue
        block = cfg.block_for_line(line)
        if block is None:
            continue
        graph = cfg.graph()
        dom_blocks = _dominator_chain(graph, cfg.entry, block)
        post_blocks = _dominator_chain(
            graph.reverse(copy=True), cfg.exit, block
        )
        items.append(
            {
                "function": cfg.name,
                "file": cfg.file,
                "line": line,
                "block": block,
                "dominating": _statements(cfg, dom_blocks, use_last=True),
                "post_dominating": _statements(cfg, post_blocks, use_last=False),
                "engine": engine.ENGINE_NAME,
            }
        )
    if not items:
        if note_items:
            return note_items
        raise ValueError(
            f"line {line} is not inside any definition matching the "
            "requested function"
        )
    return items


def reindex(paths: list[str] = []) -> list[dict]:
    """Refresh the engine's per-file CFG cache; one {reindexed, wall_ms} item."""
    start = time.perf_counter()
    root = env.repo_root()
    requested = (
        [_normalize_rel(path) for path in paths] if paths else _stale_paths()
    )
    refreshed: list[str] = []
    for rel in requested:
        abs_path = root / rel
        dropped = engine.clear_cache([abs_path]) > 0
        rebuilt = False
        if Path(abs_path).is_file():
            try:
                engine.file_cfgs(abs_path, rel)
            except Exception:
                pass  # stale cache is gone; the fresh file does not parse
            rebuilt = True
        if dropped or rebuilt:
            refreshed.append(rel)
    wall_ms = round((time.perf_counter() - start) * 1000.0, 3)
    return [
        {"reindexed": refreshed, "wall_ms": wall_ms, "engine": engine.ENGINE_NAME}
    ]


def create_server() -> ReprServer:
    """Create the cfg server with its four representation tools."""
    server = ReprServer("cfg")
    server.tool(description=GET_CFG_DESCRIPTION)(get_cfg)
    server.tool(description=CFG_PATHS_DESCRIPTION)(cfg_paths)
    server.tool(description=DOMINATORS_DESCRIPTION)(dominators)
    server.tool(description=REINDEX_DESCRIPTION)(reindex)
    return server
