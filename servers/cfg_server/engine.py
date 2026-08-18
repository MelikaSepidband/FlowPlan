"""Pure-Python control-flow-graph engine (the "python-ast" engine).

Builds per-function CFGs from stdlib ``ast``: basic blocks of consecutive
simple statements, edges labeled ``"true"``/``"false"`` for branch arms,
``"loop"`` for back edges, and ``"next"`` for fallthrough, plus synthetic
entry/exit blocks (always block ids 0 and 1). ``if``/``elif``/``else``,
``while``, ``for``, ``return``, ``raise``, ``break``/``continue``, ``with``,
``match``, and ``try``/``except`` are handled; exception edges are
over-approximated (every block inside a ``try`` body may jump to every
handler). Per STUDY_PLAN.md section 3 this engine is the per-file fallback
when Joern is unavailable and the default engine when ``JOERN_SERVER_URL``
is unset.

File CFGs are cached keyed (path, mtime); the cfg server's ``reindex`` tool
drops cache entries via :func:`clear_cache`.
"""

import ast
from pathlib import Path
from typing import Optional

import networkx as nx

__all__ = [
    "ENGINE_NAME",
    "ENTRY_CODE",
    "EXIT_CODE",
    "FunctionCFG",
    "cfg_at_line",
    "clear_cache",
    "file_cfgs",
]

ENGINE_NAME = "python-ast"

ENTRY_CODE = "<entry>"
EXIT_CODE = "<exit>"

_LOOP_TYPES = (ast.While, ast.For, ast.AsyncFor)
_TRY_TYPES = tuple(
    node_type
    for node_type in (getattr(ast, "Try", None), getattr(ast, "TryStar", None))
    if node_type is not None
)
_DEF_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


class FunctionCFG:
    """The control-flow graph of one function definition in one file."""

    def __init__(
        self,
        name: str,
        file: str,
        line: int,
        end_line: int,
        blocks: list[dict],
        edges: list[dict],
        entry: int,
        exit_id: int,
        src_lines: list[str],
    ) -> None:
        """Index blocks by line for lookups; the graph is built lazily."""
        self.name = name
        self.file = file
        self.line = line
        self.end_line = end_line
        self.blocks = blocks
        self.edges = edges
        self.entry = entry
        self.exit = exit_id
        self._src_lines = src_lines
        self._line_to_block: dict[int, int] = {}
        for block in blocks:
            for start, end in block["spans"]:
                for lineno in range(start, end + 1):
                    self._line_to_block.setdefault(lineno, block["id"])
        self._graph: Optional[nx.DiGraph] = None

    def graph(self) -> "nx.DiGraph":
        """The block graph; parallel edges keep the first-inserted kind."""
        if self._graph is None:
            graph: nx.DiGraph = nx.DiGraph()
            graph.add_nodes_from(block["id"] for block in self.blocks)
            for edge in self.edges:
                if not graph.has_edge(edge["src"], edge["dst"]):
                    graph.add_edge(edge["src"], edge["dst"], kind=edge["kind"])
            self._graph = graph
        return self._graph

    def block_for_line(self, line: int) -> Optional[int]:
        """The block id containing a 1-based source line, or None."""
        return self._line_to_block.get(line)

    def code_at(self, line: int) -> str:
        """The stripped source text of a 1-based line ('' out of range)."""
        if 1 <= line <= len(self._src_lines):
            return self._src_lines[line - 1].strip()
        return ""

    def first_stmt_line(self, block_id: int) -> Optional[int]:
        """Line of the first statement in a block (None for synthetic)."""
        spans = self.blocks[block_id]["spans"]
        return spans[0][0] if spans else None

    def last_stmt_line(self, block_id: int) -> Optional[int]:
        """Line of the last statement in a block -- the branch decision."""
        spans = self.blocks[block_id]["spans"]
        return spans[-1][0] if spans else None

    def to_item(self, max_nodes: int) -> dict:
        """The get_cfg item: blocks, edges, entry, exit, engine (+ anchors)."""
        kept = self.blocks[: max(0, max_nodes)]
        kept_ids = {block["id"] for block in kept}
        return {
            "function": self.name,
            "file": self.file,
            "line": self.line,
            "blocks": [
                {
                    "id": block["id"],
                    "line_range": block["line_range"],
                    "first_line_code": block["first_line_code"],
                }
                for block in kept
            ],
            "edges": [
                edge
                for edge in self.edges
                if edge["src"] in kept_ids and edge["dst"] in kept_ids
            ],
            "entry": self.entry,
            "exit": self.exit,
            "engine": ENGINE_NAME,
        }


class _Builder:
    """One-pass basic-block builder over a function body."""

    def __init__(self, src_lines: list[str]) -> None:
        """Create the synthetic entry (id 0) and exit (id 1) blocks."""
        self.src_lines = src_lines
        self.blocks: list[dict] = []
        self.edges: list[dict] = []
        self._edge_keys: set[tuple[int, int, str]] = set()
        self._loops: list[dict] = []
        # enclosing try/finally finalbodies (innermost last): a Return/Raise
        # must route THROUGH them before reaching exit (2026-08-14 audit fix:
        # return edges used to bypass `finally`, and an all-abrupt try left
        # its shared finalbody block disconnected)
        self._finals: list[list] = []
        # blocks created by inlining a finalbody copy on a Return/Raise path:
        # excluded from a Try's "guarded may-raise" set (an exception in a
        # finally propagates OUT of the try, not into its own handlers)
        self._final_copy_blocks: set[int] = set()
        self.entry = self._new_block()
        self.exit = self._new_block()

    # -- low-level helpers ---------------------------------------------------

    def _new_block(self) -> int:
        """Append an empty block; return its id (== list index)."""
        block_id = len(self.blocks)
        self.blocks.append({"id": block_id, "spans": []})
        return block_id

    def _edge(self, src: int, dst: int, kind: str) -> None:
        """Record one edge, deduplicated on (src, dst, kind)."""
        key = (src, dst, kind)
        if key in self._edge_keys:
            return
        self._edge_keys.add(key)
        self.edges.append({"src": src, "dst": dst, "kind": kind})

    def _connect(self, preds: list[tuple[int, str]], dst: int) -> None:
        """Connect every dangling (block, kind) edge to dst."""
        for src, kind in preds:
            self._edge(src, dst, kind)

    def _span(self, block_id: int, start: int, end: Optional[int]) -> None:
        """Record one statement's (start, end) line span in a block."""
        self.blocks[block_id]["spans"].append((start, max(start, end or start)))

    def _ensure(self, current: Optional[int], preds: list[tuple[int, str]]) -> int:
        """Reuse the open block or open a new one fed by the dangling edges."""
        if current is None:
            current = self._new_block()
            self._connect(preds, current)
        return current

    @staticmethod
    def _head_end(stmt: ast.stmt) -> int:
        """End line of a compound statement's header (test/iter), not body."""
        if isinstance(stmt, (ast.If, ast.While)):
            node = stmt.test
        elif isinstance(stmt, (ast.For, ast.AsyncFor)):
            node = stmt.iter
        elif isinstance(stmt, (ast.With, ast.AsyncWith)) and stmt.items:
            node = stmt.items[-1].context_expr
        elif isinstance(stmt, ast.Match):
            node = stmt.subject
        else:
            return stmt.lineno
        return getattr(node, "end_lineno", None) or stmt.lineno

    # -- statement processing --------------------------------------------------

    def build(self, node: ast.AST) -> None:
        """Process the function body; dangle remaining exits into exit."""
        exits = self.process(node.body, [(self.entry, "next")])
        self._connect(exits, self.exit)

    def process(
        self, stmts: list[ast.stmt], preds: list[tuple[int, str]]
    ) -> list[tuple[int, str]]:
        """Build blocks for a statement sequence; return its dangling exits.

        ``preds`` are (block, kind) edges awaiting the sequence's first block.
        Invariant: while ``current`` is open, ``preds`` is empty.
        """
        current: Optional[int] = None
        for stmt in stmts:
            if isinstance(stmt, ast.If):
                current = self._ensure(current, preds)
                preds = []
                self._span(current, stmt.lineno, self._head_end(stmt))
                head = current
                true_exits = self.process(stmt.body, [(head, "true")])
                if stmt.orelse:
                    false_exits = self.process(stmt.orelse, [(head, "false")])
                else:
                    false_exits = [(head, "false")]
                preds = true_exits + false_exits
                current = None
            elif isinstance(stmt, _LOOP_TYPES):
                header = self._new_block()
                if current is not None:
                    self._edge(current, header, "next")
                else:
                    self._connect(preds, header)
                self._span(header, stmt.lineno, self._head_end(stmt))
                self._loops.append({"header": header, "breaks": []})
                body_exits = self.process(stmt.body, [(header, "true")])
                context = self._loops.pop()
                for src, _kind in body_exits:
                    self._edge(src, header, "loop")
                normal_exits: list[tuple[int, str]] = [(header, "false")]
                if stmt.orelse:
                    normal_exits = self.process(stmt.orelse, normal_exits)
                preds = normal_exits + context["breaks"]
                current = None
            elif _TRY_TYPES and isinstance(stmt, _TRY_TYPES):
                current = self._ensure(current, preds)
                preds = []
                self._span(current, stmt.lineno, stmt.lineno)
                head = current
                first_new = len(self.blocks)
                if stmt.finalbody:
                    self._finals.append(stmt.finalbody)
                body_exits = self.process(stmt.body, [(head, "next")])
                guarded = [head] + [
                    block["id"] for block in self.blocks[first_new:]
                    if block["id"] not in self._final_copy_blocks
                ]
                handler_exits: list[tuple[int, str]] = []
                for handler in stmt.handlers:
                    handler_block = self._new_block()
                    self._span(handler_block, handler.lineno, handler.lineno)
                    # Over-approximation: any guarded block may raise.
                    for block_id in guarded:
                        self._edge(block_id, handler_block, "next")
                    handler_exits.extend(
                        self.process(handler.body, [(handler_block, "next")])
                    )
                if stmt.orelse:
                    body_exits = self.process(stmt.orelse, body_exits)
                if stmt.finalbody:
                    self._finals.pop()
                preds = body_exits + handler_exits
                if stmt.finalbody:
                    if preds:
                        preds = self.process(stmt.finalbody, preds)
                    else:
                        # every path through body/handlers exited abruptly:
                        # each Return/Raise already routed through its own
                        # inlined copy of the finalbody -- processing the
                        # shared finalbody here would create a DISCONNECTED
                        # block (the audited `start()` artifact)
                        preds = []
                current = None
            elif isinstance(stmt, (ast.With, ast.AsyncWith)):
                current = self._ensure(current, preds)
                preds = []
                self._span(current, stmt.lineno, self._head_end(stmt))
                preds = self.process(stmt.body, [(current, "next")])
                current = None
            elif isinstance(stmt, ast.Match):
                current = self._ensure(current, preds)
                preds = []
                self._span(current, stmt.lineno, self._head_end(stmt))
                head = current
                exits: list[tuple[int, str]] = [(head, "false")]
                for case in stmt.cases:
                    case_block = self._new_block()
                    self._span(case_block, case.pattern.lineno, case.pattern.lineno)
                    self._edge(head, case_block, "true")
                    exits.extend(self.process(case.body, [(case_block, "next")]))
                preds = exits
                current = None
            elif isinstance(stmt, (ast.Return, ast.Raise)):
                current = self._ensure(current, preds)
                self._span(
                    current, stmt.lineno, getattr(stmt, "end_lineno", stmt.lineno)
                )
                # route through every enclosing finalbody (innermost first),
                # INLINING a copy per abrupt exit -- Python semantics: a
                # return/raise inside try/finally executes the finally suite
                # before leaving. A return inside a finalbody copy threads
                # only the finals OUTSIDE it (hence the truncated stack).
                outs: list[tuple[int, str]] = [(current, "next")]
                finals = list(self._finals)
                copy_start = len(self.blocks)
                for i in range(len(finals) - 1, -1, -1):
                    saved = self._finals
                    self._finals = finals[:i]
                    try:
                        outs = self.process(finals[i], outs)
                    finally:
                        self._finals = saved
                self._final_copy_blocks.update(
                    b["id"] for b in self.blocks[copy_start:])
                self._connect(outs, self.exit)
                current = None
                preds = []
            elif isinstance(stmt, ast.Break):
                current = self._ensure(current, preds)
                self._span(current, stmt.lineno, stmt.lineno)
                if self._loops:
                    self._loops[-1]["breaks"].append((current, "next"))
                else:
                    self._edge(current, self.exit, "next")
                current = None
                preds = []
            elif isinstance(stmt, ast.Continue):
                current = self._ensure(current, preds)
                self._span(current, stmt.lineno, stmt.lineno)
                if self._loops:
                    self._edge(current, self._loops[-1]["header"], "loop")
                else:
                    self._edge(current, self.exit, "next")
                current = None
                preds = []
            elif isinstance(stmt, _DEF_TYPES):
                # Nested defs/classes: one header line, body not descended.
                current = self._ensure(current, preds)
                self._span(current, stmt.lineno, stmt.lineno)
                preds = []
            else:
                current = self._ensure(current, preds)
                self._span(
                    current, stmt.lineno, getattr(stmt, "end_lineno", stmt.lineno)
                )
                preds = []
        if current is not None:
            return [(current, "next")]
        return preds

    # -- output ----------------------------------------------------------------

    def finalized_blocks(self) -> list[dict]:
        """Blocks with line_range and first_line_code computed."""
        out: list[dict] = []
        for block in self.blocks:
            spans = block["spans"]
            if spans:
                start = min(span[0] for span in spans)
                end = max(span[1] for span in spans)
                line_range: Optional[list[int]] = [start, end]
                first = spans[0][0]
                if 1 <= first <= len(self.src_lines):
                    code = self.src_lines[first - 1].strip()
                else:
                    code = ""
            else:
                line_range = None
                if block["id"] == self.entry:
                    code = ENTRY_CODE
                elif block["id"] == self.exit:
                    code = EXIT_CODE
                else:
                    code = ""
            out.append(
                {
                    "id": block["id"],
                    "spans": spans,
                    "line_range": line_range,
                    "first_line_code": code,
                }
            )
        return out


def _build_file(abs_path: Path, rel_path: str) -> list["FunctionCFG"]:
    """Parse a source file and build the CFG of every function in it."""
    source = abs_path.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(source, filename=str(abs_path))
    src_lines = source.splitlines()
    cfgs: list[FunctionCFG] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        builder = _Builder(src_lines)
        builder.build(node)
        cfgs.append(
            FunctionCFG(
                name=node.name,
                file=rel_path,
                line=node.lineno,
                end_line=getattr(node, "end_lineno", None) or node.lineno,
                blocks=builder.finalized_blocks(),
                edges=builder.edges,
                entry=builder.entry,
                exit_id=builder.exit,
                src_lines=src_lines,
            )
        )
    return cfgs


# Per-file CFG cache keyed by absolute path -> (mtime_ns, cfgs). The mtime
# in the value implements the (path, mtime) cache key: a changed mtime is a
# miss, and reindex/clear_cache drops entries explicitly.
_cache: dict[str, tuple[int, list["FunctionCFG"]]] = {}


def file_cfgs(abs_path: Path, rel_path: str) -> list["FunctionCFG"]:
    """All function CFGs of one file, cached keyed (path, mtime)."""
    abs_path = Path(abs_path)
    key = str(abs_path)
    try:
        mtime = abs_path.stat().st_mtime_ns
    except OSError:
        _cache.pop(key, None)
        raise FileNotFoundError(f"source file not found: {rel_path}") from None
    cached = _cache.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    cfgs = _build_file(abs_path, rel_path)
    _cache[key] = (mtime, cfgs)
    return cfgs


def cfg_at_line(abs_path: Path, rel_path: str, line: int) -> "FunctionCFG":
    """The CFG of the function defined at (or innermost containing) a line."""
    cfgs = file_cfgs(abs_path, rel_path)
    exact = [cfg for cfg in cfgs if cfg.line == line]
    if exact:
        return exact[0]
    containing = [cfg for cfg in cfgs if cfg.line <= line <= cfg.end_line]
    if containing:
        return max(containing, key=lambda cfg: cfg.line)
    raise ValueError(f"no function definition at {rel_path}:{line}")


def clear_cache(paths: Optional[list] = None) -> int:
    """Drop cached file CFGs; None clears everything. Returns entries dropped."""
    if paths is None:
        count = len(_cache)
        _cache.clear()
        return count
    count = 0
    for path in paths:
        if _cache.pop(str(path), None) is not None:
            count += 1
    return count
