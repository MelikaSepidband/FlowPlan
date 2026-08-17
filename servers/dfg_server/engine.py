"""Beniget def-use engine for the dfg server (study condition C3).

Computes per-module def/use chains on demand with beniget over gast-parsed
source. Results are always fresh: modules are cached keyed (path, mtime) and
reparsed whenever the file changes on disk. The engine only ever sees one
module at a time, so every item it produces carries ``"engine": "beniget"``
and ``"scope": "intra-module"`` (INTERFACES.md sections 12.1 and 12.4).

Graph model (line-granular, built from the beniget chains):

- ``def-use`` edges link a definition anchor ``file.py:LINE:var`` to each
  use anchor of that definition; every pair also yields the mirrored
  ``use-def`` edge.
- For slicing, uses are additionally linked to the definitions made by the
  same (innermost) statement, so ``y = f(x)`` flows ``x -> y``. Backward
  slices walk use->def plus def->same-statement-uses; forward slices walk
  the reverse.
"""

import contextlib
import io
import time
from dataclasses import dataclass
from pathlib import Path

import beniget
import gast

from servers.common import env, staleness
from servers.common.navkit import EXCLUDED_DIRS

__all__ = [
    "ENGINE_NAME",
    "SCOPE",
    "Anchor",
    "ModuleDFG",
    "clear_cache",
    "find_flows",
    "iter_module_paths",
    "module_dfg",
    "refresh_paths",
    "resolve_file",
    "slice_statements",
    "stale_paths",
]

ENGINE_NAME = "beniget"
SCOPE = "intra-module"

# Definition nodes that name something reusable: assigned/param names,
# function and class definitions, and import aliases.
_NAMED_DEF_TYPES = (
    gast.FunctionDef,
    gast.AsyncFunctionDef,
    gast.ClassDef,
    gast.alias,
)
_NAME_DEF_CTX = (gast.Store, gast.Param)


@dataclass(frozen=True, order=True)
class Anchor:
    """One def or use of a variable: (line, var, role)."""

    line: int
    var: str
    is_def: bool


@dataclass(frozen=True)
class FunctionSpan:
    """A function/method definition span used for the def_use filter."""

    name: str
    qualname: str
    line: int
    end_line: int


class _QuietDefUseChains(beniget.DefUseChains):
    """DefUseChains whose warning emission is a NO-OP.

    beniget reports analysis warnings (unbound identifiers etc.) by PRINTING
    to stdout: every such message funnels through ``DefUseChains.warn`` which
    does ``print("W: ...")`` (``unbound_identifier`` delegates to it). The
    dfg server speaks MCP over stdio, so any such print corrupts the protocol
    channel and hangs the client -- a function snippet full of module-level
    names (the ansible ``construct_rule`` shape) triggered exactly that.
    Overriding ``warn`` makes ALL of beniget's stdout writes dead."""

    def warn(self, msg, node):  # noqa: ARG002 - beniget's signature
        pass


class ModuleDFG:
    """Line-granular def/use graph of one Python module."""

    def __init__(self, rel_path: str, source: str) -> None:
        """Parse source with gast and build the beniget def-use graph."""
        self.rel_path = rel_path
        self.lines = source.splitlines()
        # Belt and braces with _QuietDefUseChains: even if a beniget/gast
        # code path prints directly, nothing may reach the real stdout (the
        # MCP stdio channel). Pure I/O hygiene -- that output never
        # legitimately reached any protocol.
        with contextlib.redirect_stdout(io.StringIO()):
            tree = gast.parse(source)
            chains = _QuietDefUseChains(rel_path)
            chains.visit(tree)

        # (var, def_line, use_line) def/use pairs from the beniget chains.
        pairs: set[tuple[str, int, int]] = set()
        defs: set[Anchor] = set()
        uses: set[Anchor] = set()
        for node, definition in chains.chains.items():
            if isinstance(node, gast.Name):
                if not isinstance(node.ctx, _NAME_DEF_CTX):
                    continue
            elif not isinstance(node, _NAMED_DEF_TYPES):
                continue
            line = getattr(node, "lineno", None)
            if line is None:
                continue
            var = definition.name()
            if not var or not var.replace("_", "a").isalnum():
                continue
            defs.add(Anchor(line=line, var=var, is_def=True))
            for user in definition.users():
                user_node = user.node
                if not isinstance(user_node, gast.Name):
                    continue
                use_line = getattr(user_node, "lineno", None)
                if use_line is None:
                    continue
                pairs.add((var, line, use_line))
                uses.add(Anchor(line=use_line, var=var, is_def=False))
        self.pairs = sorted(pairs)
        self.defs = defs
        self.uses = uses

        # Innermost-statement grouping for intra-statement flow edges.
        statements: list[tuple[int, int]] = [
            (node.lineno, node.end_lineno)
            for node in gast.walk(tree)
            if isinstance(node, gast.stmt)
            and getattr(node, "lineno", None) is not None
            and getattr(node, "end_lineno", None) is not None
        ]

        def innermost(line: int) -> tuple[int, int] | None:
            containing = [s for s in statements if s[0] <= line <= s[1]]
            if not containing:
                return None
            return max(containing, key=lambda s: (s[0], -(s[1] - s[0])))

        by_stmt: dict[tuple[int, int], tuple[set[Anchor], set[Anchor]]] = {}
        for anchor in defs | uses:
            stmt = innermost(anchor.line)
            if stmt is None:
                continue
            bucket = by_stmt.setdefault(stmt, (set(), set()))
            bucket[anchor.is_def].add(anchor)

        # Forward influence adjacency: def -> its uses (def-use), and use ->
        # the defs made by the same statement (a def depends on the names
        # its statement reads).
        self.forward: dict[Anchor, set[Anchor]] = {}
        self.backward: dict[Anchor, set[Anchor]] = {}
        for var, def_line, use_line in self.pairs:
            self._add_edge(
                Anchor(line=def_line, var=var, is_def=True),
                Anchor(line=use_line, var=var, is_def=False),
            )
        for _stmt, (stmt_uses, stmt_defs) in by_stmt.items():
            for use in stmt_uses:
                for definition in stmt_defs:
                    self._add_edge(use, definition)

        self.functions = _function_spans(tree)

    def _add_edge(self, src: Anchor, dst: Anchor) -> None:
        """Record a forward influence edge src -> dst (and its reverse)."""
        if src == dst:
            return
        self.forward.setdefault(src, set()).add(dst)
        self.backward.setdefault(dst, set()).add(src)

    def code_line(self, line: int) -> str:
        """The stripped source text of a 1-based line ('' out of range)."""
        if 1 <= line <= len(self.lines):
            return self.lines[line - 1].strip()
        return ""

    def anchors_at(self, line: int, var: str) -> list[Anchor]:
        """All def/use anchors of var at the given line (defs first)."""
        found = [a for a in self.defs | self.uses if a.line == line and a.var == var]
        return sorted(found, key=lambda a: (not a.is_def, a.line, a.var))


def _function_spans(tree: "gast.Module") -> list[FunctionSpan]:
    """Collect function/method spans with dotted in-module qualnames."""
    spans: list[FunctionSpan] = []

    def walk(node, prefix: str) -> None:
        for child in gast.iter_child_nodes(node):
            if isinstance(child, (gast.FunctionDef, gast.AsyncFunctionDef)):
                qualname = f"{prefix}{child.name}"
                spans.append(
                    FunctionSpan(
                        name=child.name,
                        qualname=qualname,
                        line=child.lineno,
                        end_line=child.end_lineno,
                    )
                )
                walk(child, qualname + ".")
            elif isinstance(child, gast.ClassDef):
                walk(child, f"{prefix}{child.name}.")
            else:
                walk(child, prefix)

    walk(tree, "")
    return spans


# -- per-module cache keyed (path, mtime) -------------------------------------

_CACHE: dict[str, tuple[int, ModuleDFG]] = {}


def clear_cache() -> None:
    """Drop every cached module graph (next access reparses)."""
    _CACHE.clear()


def resolve_file(file: str) -> tuple[Path, str]:
    """Resolve a repo-root-relative file argument to (abs_path, rel_posix).

    Raises ValueError for an empty argument and FileNotFoundError when the
    file does not exist under the repository root.
    """
    text = (file or "").strip().replace("\\", "/")
    if not text:
        raise ValueError("file must not be empty")
    root = env.repo_root()
    candidate = Path(text)
    abs_path = candidate if candidate.is_absolute() else root / candidate
    abs_path = abs_path.resolve()
    try:
        rel = abs_path.relative_to(root).as_posix()
    except ValueError:
        raise FileNotFoundError(
            f"file {file!r} is outside the repository root {root}"
        ) from None
    if not abs_path.is_file():
        raise FileNotFoundError(f"file not found: {rel} (repo root {root})")
    return abs_path, rel


def module_dfg(abs_path: Path, rel_path: str) -> ModuleDFG:
    """Build (or return the cached) def-use graph; always fresh by mtime."""
    key = str(abs_path)
    mtime = abs_path.stat().st_mtime_ns
    cached = _CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    source = abs_path.read_text(encoding="utf-8")
    dfg = ModuleDFG(rel_path, source)
    _CACHE[key] = (mtime, dfg)
    return dfg


def iter_module_paths() -> list[tuple[Path, str]]:
    """All Python files under the repo root, honoring EXCLUDED_DIRS, sorted."""
    root = env.repo_root()
    out: list[tuple[Path, str]] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if any(part in EXCLUDED_DIRS for part in rel.parts):
            continue
        if path.is_file():
            out.append((path, rel.as_posix()))
    return out


# -- slicing -------------------------------------------------------------------


def slice_statements(
    dfg: ModuleDFG, line: int, var: str, depth: int, forward: bool
) -> list[dict]:
    """BFS over the def-use graph from var at line; one row per statement.

    Returns ``[{"line": int, "depth": int, "vars": [str, ...]}, ...]`` sorted
    by (depth, line), where depth is the minimum number of def-use-graph hops
    at which the line was reached (the queried line is depth 0). Raises
    ValueError when var has no def or use at the line.
    """
    starts = dfg.anchors_at(line, var)
    if not starts:
        raise ValueError(
            f"no definition or use of {var!r} at line {line} in {dfg.rel_path}"
        )
    adjacency = dfg.forward if forward else dfg.backward
    best: dict[Anchor, int] = {a: 0 for a in starts}
    frontier = list(starts)
    for level in range(1, depth + 1):
        next_frontier: list[Anchor] = []
        for anchor in frontier:
            for neighbor in adjacency.get(anchor, ()):
                if neighbor in best:
                    continue
                best[neighbor] = level
                next_frontier.append(neighbor)
        frontier = next_frontier
        if not frontier:
            break
    by_line: dict[int, dict] = {}
    for anchor, level in best.items():
        row = by_line.setdefault(
            anchor.line, {"line": anchor.line, "depth": level, "vars": set()}
        )
        row["depth"] = min(row["depth"], level)
        row["vars"].add(anchor.var)
    rows = sorted(by_line.values(), key=lambda r: (r["depth"], r["line"]))
    for row in rows:
        row["vars"] = sorted(row["vars"])
    return rows


# -- taint-style flows ----------------------------------------------------------


def find_flows(
    dfg: ModuleDFG, source_pattern: str, sink_pattern: str, max_flows: int
) -> list[list[Anchor]]:
    """Shortest forward chains from source-matching to sink-matching anchors.

    Source/sink matching is a case-sensitive substring match on the variable
    name. One shortest chain (at least one edge long) is found per source
    anchor via BFS; chains are sorted by (length, anchors) and capped at
    max_flows.
    """
    anchors = dfg.defs | dfg.uses
    sources = sorted(a for a in anchors if source_pattern in a.var)
    sinks = {a for a in anchors if sink_pattern in a.var}
    if not sources or not sinks:
        return []
    chains: list[list[Anchor]] = []
    seen: set[tuple[Anchor, ...]] = set()
    for source in sources:
        parents: dict[Anchor, Anchor] = {}
        visited = {source}
        frontier = [source]
        hit: Anchor | None = None
        while frontier and hit is None:
            next_frontier: list[Anchor] = []
            for anchor in frontier:
                for neighbor in sorted(dfg.forward.get(anchor, ())):
                    if neighbor in visited:
                        continue
                    visited.add(neighbor)
                    parents[neighbor] = anchor
                    if neighbor in sinks:
                        hit = neighbor
                        break
                    next_frontier.append(neighbor)
                if hit is not None:
                    break
            frontier = next_frontier
        if hit is None:
            continue
        chain = [hit]
        while chain[-1] != source:
            chain.append(parents[chain[-1]])
        chain.reverse()
        key = tuple(chain)
        if key not in seen:
            seen.add(key)
            chains.append(chain)
    chains.sort(key=lambda c: (len(c), [(a.line, a.var, a.is_def) for a in c]))
    return chains[:max_flows]


# -- reindex support -------------------------------------------------------------


def stale_paths() -> list[str]:
    """Repo-relative paths the index manifest currently reports stale."""
    manifest = staleness.Manifest.load(env.index_dir())
    if manifest is None:
        return []
    root = env.repo_root()
    stale: list[str] = []
    for rel, entry in manifest.files.items():
        path = root / rel
        try:
            st = path.stat()
        except OSError:
            stale.append(rel)
            continue
        if st.st_size != entry.get("size") or st.st_mtime != entry.get("mtime"):
            stale.append(rel)
    return sorted(stale)


def refresh_paths(paths: list[str]) -> tuple[list[str], float]:
    """Drop and rebuild the cached graphs for paths; return (refreshed, ms).

    With an empty list, refreshes everything the manifest reports stale
    (INTERFACES.md section 12.2). Missing or non-Python files are still
    dropped from the cache and reported as refreshed.
    """
    start = time.perf_counter()
    requested = [str(p) for p in (paths or [])] or stale_paths()
    root = env.repo_root()
    refreshed: list[str] = []
    for raw in requested:
        text = raw.strip().replace("\\", "/")
        if not text:
            continue
        candidate = Path(text)
        abs_path = candidate if candidate.is_absolute() else root / candidate
        abs_path = abs_path.resolve()
        try:
            rel = abs_path.relative_to(root).as_posix()
        except ValueError:
            rel = text
        _CACHE.pop(str(abs_path), None)
        if abs_path.is_file() and abs_path.suffix == ".py":
            module_dfg(abs_path, rel)
        refreshed.append(rel)
    wall_ms = round((time.perf_counter() - start) * 1000.0, 3)
    return refreshed, wall_ms
