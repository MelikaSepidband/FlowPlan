"""Data-flow-graph MCP server (study condition C3).

Five representation tools per STUDY_PLAN.md section 2 (C3) plus the section
12.2 ``reindex`` tool: ``def_use``, ``backward_slice``, ``forward_slice``,
``data_flow_between``, and ``reindex``. Two engines sit behind the frozen
tool surface (INTERFACES.md section 12.1):

- When ``JOERN_SERVER_URL`` is set, ``def_use`` and the slice and flow tools
  route through ``servers.common.joern.JoernClient`` (identifier ``ddgIn``
  walks and reachableByFlows over the server-side CPG); items carry
  ``"engine": "joern"`` and no scope key.
- Otherwise -- and as the fallback whenever the configured Joern server is
  unreachable -- the pure-Python beniget engine (servers.dfg_server.engine)
  answers from per-module def-use chains, always fresh by file mtime; items
  carry ``"engine": "beniget"`` and ``"scope": "intra-module"``.

NOTE: do not add ``from __future__ import annotations`` here -- tool
implementation annotations must be real objects for FastMCP schema
generation (INTERFACES.md section 8).
"""

import re
from typing import Optional

from servers.common.base import ReprServer
from servers.common.joern import JoernClient, JoernUnavailable
from servers.common.tracefile import (
    NO_TRACE_NOTE as _NO_TRACE_NOTE,
    lines_for as _trace_lines,
    load_trace as _load_trace,
)

from servers.dfg_server import engine

__all__ = [
    "def_use",
    "backward_slice",
    "forward_slice",
    "data_flow_between",
    "reindex",
    "executed_flows",
    "create_server",
]

# Frozen tool descriptions -- verb-first, one example each.
DEF_USE_DESCRIPTION = (
    "List def/use chain edges for the variables in one source file: every "
    "definition-to-use pair as a def-use edge between file:LINE:var anchors "
    "plus the mirrored use-def edge -- use this to see where a value is "
    "assigned and every place that assignment is read. The optional "
    "function and variable arguments filter to one enclosing function "
    "(name or dotted suffix) or one EXACT variable name -- the variable "
    "filter must be the precise identifier as written in the source "
    "(case-sensitive, no wildcards). Pass file as a repo-relative path. "
    "Example: def_use(file='src/core/session.py', variable='timeout'). "
    "An empty list means no matching edges were found for the engine's "
    "scope -- not an error; do not retry the same call."
)
BACKWARD_SLICE_DESCRIPTION = (
    "Trace the statements that influence a variable at a given line, "
    "walking the def-use graph backward up to depth hops -- use this to "
    "find where a bad value came from. Args: file (repo-relative path), "
    "line (the line where the variable is read/written), var (the EXACT "
    "identifier name at that line -- REQUIRED, case-sensitive, no "
    "wildcards; an empty or wrong var yields no slice), depth (max hops, "
    "default 10). Items carry file:line anchors and the source line text. "
    "Example: backward_slice(file='src/core/session.py', line=250, "
    "var='timeout', depth=10). An empty result is an engine coverage gap, "
    "NOT an error -- do not retry the same slice; adjust the line/var or "
    "move on."
)
FORWARD_SLICE_DESCRIPTION = (
    "Trace the statements influenced by a variable at a given line, "
    "following data flow forward -- use this to see the blast radius of "
    "changing a value. Args: file (repo-relative path), line (the line "
    "where the variable is defined/read), var (the EXACT identifier name "
    "at that line -- REQUIRED, case-sensitive, no wildcards; an empty or "
    "wrong var yields no slice), depth (default 10). Items carry file:line "
    "anchors and the source line text. Example: forward_slice("
    "file='src/core/session.py', line=250, var='timeout'). An empty result "
    "is an engine coverage gap, NOT an error -- do not retry the same "
    "slice repeatedly; adjust the line/var or move on."
)
DATA_FLOW_BETWEEN_DESCRIPTION = (
    "Find data-flow chains from variables whose name matches "
    "source_pattern to variables whose name matches sink_pattern "
    "(substring match on the identifier name) -- use this to check whether "
    "a value can reach a sensitive operation. Args: source_pattern and "
    "sink_pattern (both REQUIRED, non-empty), max_flows (max chains "
    "returned, default 5). Returns up to max_flows ordered file:LINE:var "
    "anchor chains. Example: data_flow_between(source_pattern='request', "
    "sink_pattern='query'). Cross-function flow discovery has a real miss "
    "rate; an empty result is a coverage gap, NOT an error -- do not retry "
    "the same patterns repeatedly."
)
REINDEX_DESCRIPTION = (
    "Refresh the dfg engine's view of the given repo-relative files by "
    "dropping and rebuilding their cached def-use graphs; with no paths, "
    "refresh every file currently reported stale. Returns exactly one "
    "item {reindexed: [paths], wall_ms: float}. Example: "
    "reindex(paths=['src/core/session.py'])."
)
EXECUTED_FLOWS_DESCRIPTION = (
    "Show which of a file's def/use pairs the FAILING TEST actually "
    "exercised -- the recorded execution of the bug's reproducing test run "
    "(collected before your session; the code is unmodified). Each def->use "
    "pair carries def_executed / use_executed / on_failing_path (both "
    "endpoints executed -- a line-level approximation of the value actually "
    "flowing during the failing run). Use it to find WHICH value flows the "
    "bug's execution actually took: a def whose uses were never reached, or "
    "a use fed by a def off the failing path, is a strong repair clue. Args: "
    "file (repo-relative path), optional function (name or dotted suffix) "
    "and variable (EXACT identifier) filters. Pairs on the failing path "
    "sort first. A note item means no execution trace is available -- not "
    "an error, do not retry. Example: "
    "executed_flows(file='src/core/session.py', function='connect')."
)


def _escape_cpgql(value: str) -> str:
    """Escape a string for embedding in a double-quoted CPGQL literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _file_regex(rel_path: str) -> str:
    """A CPGQL file-name regex that matches the file by basename.

    The scoped kernel CPGs are built with a subsystem directory (or single
    file) as the c2cpg root, so ``location.filename`` is stored relative to
    that root -- typically just the basename (e.g. ``tg3.c``), NOT the
    repo-relative path the tool caller supplies. Matching on the basename
    (anchored at the end) is robust to either build root. The dot is left as a
    regex wildcard because a literal ``\\.`` is an illegal escape inside a
    Scala string literal; matching ``c`` after any char is harmless for C
    source names.
    """
    basename = rel_path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return ".*" + _escape_cpgql(basename) + "$"


def _joern_backward_slice_query(
    rel_path: str, line: int, var: str, depth: int
) -> str:
    """CPGQL: backward data-dependence walk (``ddgIn``) from var@line.

    ``ddgIn`` is the data-dependence predecessor step; repeating it with an
    ``emit`` collects every statement that (transitively) influences the
    queried variable, up to ``depth`` hops. ``location.filename`` -- not
    ``file.name`` -- is used because every CFG/DDG node carries a location
    whereas ``file`` is empty on synthetic nodes.
    """
    name = _escape_cpgql(var)
    return (
        f'cpg.identifier.name("{name}").lineNumber({int(line)})'
        f'.where(_.file.name("{_file_regex(rel_path)}"))'
        f".repeat(_.ddgIn)(_.maxDepth({int(depth)}).emit).dedup"
        ".map(node => Map("
        '"code" -> node.code, '
        '"lineNumber" -> node.lineNumber.getOrElse(-1), '
        '"file" -> node.location.filename)).toJson'
    )


def _joern_forward_slice_query(rel_path: str, line: int, var: str) -> str:
    """CPGQL: forward data-dependence slice of var@line via reachableByFlows.

    This Joern build exposes ``ddgIn`` but NOT a ``ddgOut`` step, so the
    forward direction is computed as "every identifier in the same file that
    is data-flow-reachable FROM the source variable": the union of the path
    elements of all flows from the source to those identifiers. The source is
    bound with ``def`` (reachableByFlows requires a traversal source binding).
    ``depth`` does not apply -- reachableByFlows returns whole flows.
    """
    name = _escape_cpgql(var)
    regex = _file_regex(rel_path)
    return (
        f'def src = cpg.identifier.name("{name}").lineNumber({int(line)})'
        f'.where(_.file.name("{regex}")); '
        f'cpg.identifier.where(_.file.name("{regex}"))'
        ".reachableByFlows(src).flatMap(_.elements).dedup"
        ".map(node => Map("
        '"code" -> node.code, '
        '"lineNumber" -> node.lineNumber.getOrElse(-1), '
        '"file" -> node.location.filename)).toJson'
    )


#: Cap on raw def/use rows fetched from Joern before pairing into items.
_DEF_USE_ROW_CAP = 400


def _joern_def_use_query(
    rel_path: str, function: Optional[str], variable: Optional[str]
) -> str:
    """CPGQL: (var, def_line, use_line, file) rows for a file's identifiers.

    Each identifier's ``ddgIn`` step yields the statements whose definitions
    reach that use, i.e. one def->use pair per (definition, use) edge in the
    data-dependence graph. Optional filters narrow to one enclosing method
    (bare name; dotted suffixes are reduced to their last component, matching
    C symbol naming) or one exact identifier name.
    """
    if variable:
        base = (
            f'cpg.identifier.name("{_escape_cpgql(variable)}")'
            f'.where(_.file.name("{_file_regex(rel_path)}"))'
        )
    else:
        base = f'cpg.identifier.where(_.file.name("{_file_regex(rel_path)}"))'
    if function:
        method = function.rsplit(".", 1)[-1]
        base += f'.where(_.method.name("{_escape_cpgql(method)}"))'
    return (
        base
        + ".flatMap(u => u.ddgIn.map(d => Map("
        '"var" -> u.name, '
        '"use_line" -> u.lineNumber.getOrElse(-1), '
        '"def_line" -> d.lineNumber.getOrElse(-1), '
        '"file" -> u.location.filename)))'
        f".dedup.take({_DEF_USE_ROW_CAP}).toJson"
    )


def _joern_def_use_items(
    client: JoernClient,
    rel_path: str,
    function: Optional[str],
    variable: Optional[str],
) -> list[dict]:
    """Run the Joern def/use query and shape rows into def-use/use-def items."""
    payload = client.query_json(
        _joern_def_use_query(rel_path, function, variable)
    )
    if not isinstance(payload, list):
        raise JoernUnavailable(f"unexpected Joern def_use payload: {payload!r}")
    items: list[dict] = []
    seen: set[tuple[str, str, int, int]] = set()
    for row in payload:
        if not isinstance(row, dict):
            continue
        var = str(row.get("var") or "")
        def_line = row.get("def_line")
        use_line = row.get("use_line")
        if (
            not var
            or not isinstance(def_line, int)
            or def_line < 1
            or not isinstance(use_line, int)
            or use_line < 1
            or def_line == use_line
        ):
            continue
        node_file = str(row.get("file") or rel_path)
        key = (node_file, var, def_line, use_line)
        if key in seen:
            continue
        seen.add(key)
        def_anchor = f"{node_file}:{def_line}:{var}"
        use_anchor = f"{node_file}:{use_line}:{var}"
        common = {"var": var, "file": node_file, "engine": "joern"}
        items.append(
            {"from": def_anchor, "to": use_anchor, "kind": "def-use", **common}
        )
        items.append(
            {"from": use_anchor, "to": def_anchor, "kind": "use-def", **common}
        )
    items.sort(key=lambda item: (item["file"], item["from"]))
    return items


def _joern_slice_items(
    client: JoernClient,
    rel_path: str,
    line: int,
    var: str,
    depth: int,
    backward: bool,
) -> list[dict]:
    """Run a Joern data-dependence slice (backward ddgIn / forward flows)."""
    if backward:
        cpgql = _joern_backward_slice_query(rel_path, line, var, depth)
    else:
        cpgql = _joern_forward_slice_query(rel_path, line, var)
    payload = client.query_json(cpgql)
    if not isinstance(payload, list):
        raise JoernUnavailable(f"unexpected Joern slice payload: {payload!r}")
    items: list[dict] = []
    seen: set[tuple[str, int]] = set()
    for node in payload:
        if not isinstance(node, dict):
            continue
        node_line = node.get("lineNumber")
        if not isinstance(node_line, int) or node_line < 1:
            continue
        node_file = str(node.get("file") or rel_path)
        key = (node_file, node_line)
        if key in seen:
            continue
        seen.add(key)
        items.append(
            {
                "file": node_file,
                "line": node_line,
                "anchor": f"{node_file}:{node_line}",
                "code": str(node.get("code", "")),
                "engine": "joern",
            }
        )
    items.sort(key=lambda it: (it["file"], it["line"]))
    return items


def _joern_flow_items(
    client: JoernClient, source_pattern: str, sink_pattern: str, max_flows: int
) -> list[dict]:
    """Run reachableByFlows between name-pattern matches; return flow items."""
    src = _escape_cpgql(re.escape(source_pattern))
    snk = _escape_cpgql(re.escape(sink_pattern))
    cpgql = (
        f'def src = cpg.identifier.name(".*{src}.*"); '
        f'def snk = cpg.identifier.name(".*{snk}.*"); '
        f"snk.reachableByFlows(src).take({int(max_flows)})"
        ".map(flow => flow.elements.map(node => Map("
        '"code" -> node.code, '
        '"lineNumber" -> node.lineNumber.getOrElse(-1), '
        '"file" -> node.location.filename))).toJson'
    )
    payload = client.query_json(cpgql)
    if not isinstance(payload, list):
        raise JoernUnavailable(f"unexpected Joern flow payload: {payload!r}")
    items: list[dict] = []
    for flow in payload[:max_flows]:
        if not isinstance(flow, list) or not flow:
            continue
        anchors = [
            f"{node.get('file', '')}:{node.get('lineNumber', -1)}"
            for node in flow
            if isinstance(node, dict)
        ]
        if not anchors:
            continue
        items.append(
            {
                "source": anchors[0],
                "sink": anchors[-1],
                "chain": anchors,
                "length": len(anchors) - 1,
                "codes": [
                    str(node.get("code", ""))
                    for node in flow
                    if isinstance(node, dict)
                ],
                "engine": "joern",
            }
        )
    return items


def _beniget_slice_items(
    rel_path: str, dfg: "engine.ModuleDFG", rows: list[dict]
) -> list[dict]:
    """Convert engine slice rows to result items with anchors and code."""
    return [
        {
            "file": rel_path,
            "line": row["line"],
            "anchor": f"{rel_path}:{row['line']}",
            "code": dfg.code_line(row["line"]),
            "vars": row["vars"],
            "depth": row["depth"],
            "engine": engine.ENGINE_NAME,
            "scope": engine.SCOPE,
        }
        for row in rows
    ]


def _slice(
    file: str, line: int, var: str, depth: int, backward: bool
) -> list[dict]:
    """Shared backward/forward slice implementation with Joern routing."""
    if not (var or "").strip():
        raise ValueError("var must not be empty")
    if line < 1:
        raise ValueError("line must be >= 1")
    if depth < 0:
        raise ValueError("depth must be >= 0")
    abs_path, rel_path = engine.resolve_file(file)
    client = JoernClient()
    if client.available():
        try:
            # The Joern (C-capable) path owns the answer when configured. Its
            # result -- including an empty list when the var/line is outside
            # the loaded CPG's scope -- is returned as-is; we do NOT fall
            # through to the beniget engine, which cannot parse C.
            return _joern_slice_items(
                client, rel_path, line, var.strip(), depth, backward
            )
        except JoernUnavailable:
            pass  # server unreachable -> fall back to beniget (section 12.1)
    # Pure-Python engine. A non-Python source cannot be parsed by beniget, so
    # a *parse* failure (SyntaxError / decode error) becomes an empty list
    # rather than crashing the tool. A ValueError from slice_statements is a
    # legitimate "variable not found at this line" signal and is allowed to
    # propagate (the envelope reports it as an error, per the tool contract).
    try:
        dfg = engine.module_dfg(abs_path, rel_path)
    except (SyntaxError, UnicodeDecodeError):
        return []
    rows = engine.slice_statements(
        dfg, line, var.strip(), depth, forward=not backward
    )
    return _beniget_slice_items(rel_path, dfg, rows)


# -- tool implementations --------------------------------------------------------


def def_use(
    file: str, function: Optional[str] = None, variable: Optional[str] = None
) -> list[dict]:
    """Def/use chain edges of one module, optionally filtered."""
    abs_path, rel_path = engine.resolve_file(file)
    client = JoernClient()
    if client.available():
        try:
            # The Joern (C-capable) path owns the answer when configured;
            # its result -- including an empty list when the file is outside
            # the loaded CPG's scope -- is returned as-is (no beniget
            # fallthrough, which cannot parse C).
            return _joern_def_use_items(
                client,
                rel_path,
                (function or "").strip() or None,
                (variable or "").strip() or None,
            )
        except JoernUnavailable:
            pass  # server unreachable -> fall back to beniget (section 12.1)
    try:
        dfg = engine.module_dfg(abs_path, rel_path)
    except (SyntaxError, UnicodeDecodeError):
        # A non-Python source with no reachable Joern: empty rather than a
        # parse-error envelope (mirrors the slice tools' behavior).
        return []
    span = None
    if function is not None and function.strip():
        wanted = function.strip()
        matches = [
            fn
            for fn in dfg.functions
            if fn.name == wanted
            or fn.qualname == wanted
            or fn.qualname.endswith("." + wanted)
        ]
        if not matches:
            raise ValueError(
                f"no function matching {function!r} in {rel_path}; "
                "use find_symbol to locate the right name"
            )
        span = matches
    items: list[dict] = []
    for var, def_line, use_line in dfg.pairs:
        if variable is not None and variable.strip() and var != variable.strip():
            continue
        if span is not None and not any(
            fn.line <= def_line <= fn.end_line or fn.line <= use_line <= fn.end_line
            for fn in span
        ):
            continue
        def_anchor = f"{rel_path}:{def_line}:{var}"
        use_anchor = f"{rel_path}:{use_line}:{var}"
        common = {
            "var": var,
            "file": rel_path,
            "engine": engine.ENGINE_NAME,
            "scope": engine.SCOPE,
        }
        items.append(
            {"from": def_anchor, "to": use_anchor, "kind": "def-use", **common}
        )
        items.append(
            {"from": use_anchor, "to": def_anchor, "kind": "use-def", **common}
        )
    return items


def backward_slice(file: str, line: int, var: str, depth: int = 10) -> list[dict]:
    """Statements influencing var at file:line (BFS over the def-use graph)."""
    return _slice(file, line, var, depth, backward=True)


def forward_slice(file: str, line: int, var: str, depth: int = 10) -> list[dict]:
    """Statements influenced by var at file:line (BFS over the def-use graph)."""
    return _slice(file, line, var, depth, backward=False)


def data_flow_between(
    source_pattern: str, sink_pattern: str, max_flows: int = 5
) -> list[dict]:
    """file:line chains from source-matching names to sink-matching names."""
    source = (source_pattern or "").strip()
    sink = (sink_pattern or "").strip()
    if not source:
        raise ValueError("source_pattern must not be empty")
    if not sink:
        raise ValueError("sink_pattern must not be empty")
    if max_flows <= 0:
        return []
    client = JoernClient()
    if client.available():
        try:
            return _joern_flow_items(client, source, sink, max_flows)
        except JoernUnavailable:
            pass  # fall back to the pure-Python engine (section 12.1)
    flows: list[tuple[list, str]] = []
    for abs_path, rel_path in engine.iter_module_paths():
        try:
            dfg = engine.module_dfg(abs_path, rel_path)
        except SyntaxError:
            continue
        for chain in engine.find_flows(dfg, source, sink, max_flows):
            flows.append((chain, rel_path))
    flows.sort(key=lambda fc: (len(fc[0]), fc[1]))
    items: list[dict] = []
    for chain, rel_path in flows[:max_flows]:
        anchors: list[str] = []
        for anchor in chain:
            text = f"{rel_path}:{anchor.line}:{anchor.var}"
            if not anchors or anchors[-1] != text:
                anchors.append(text)
        items.append(
            {
                "source": anchors[0],
                "sink": anchors[-1],
                "chain": anchors,
                "length": len(anchors) - 1,
                "file": rel_path,
                "engine": engine.ENGINE_NAME,
                "scope": engine.SCOPE,
            }
        )
    return items


def reindex(paths: list[str] = []) -> list[dict]:
    """Refresh cached def-use graphs; one item {reindexed, wall_ms}."""
    refreshed, wall_ms = engine.refresh_paths(list(paths or []))
    return [
        {
            "reindexed": refreshed,
            "wall_ms": wall_ms,
            "engine": engine.ENGINE_NAME,
        }
    ]


def executed_flows(
    file: str, function: Optional[str] = None, variable: Optional[str] = None
) -> list[dict]:
    """The failing test's execution overlaid on a file's def/use pairs."""
    abs_path, rel_path = engine.resolve_file(file)
    trace = _load_trace()
    if trace is None or not trace.get("ok"):
        return [
            {
                "file": rel_path,
                "test": (trace or {}).get("test_id"),
                "reason": (trace or {}).get("reason"),
                "engine": engine.ENGINE_NAME,
                "note": _NO_TRACE_NOTE,
            }
        ]
    executed = _trace_lines(trace, rel_path)
    if executed is None:
        return [
            {
                "file": rel_path,
                "test": trace.get("test_id"),
                "engine": engine.ENGINE_NAME,
                "note": (
                    "this file was never executed by the failing test -- the "
                    "bug's execution path does not reach it"
                ),
            }
        ]
    try:
        dfg = engine.module_dfg(abs_path, rel_path)
    except (SyntaxError, UnicodeDecodeError):
        return []
    span = None
    if function is not None and function.strip():
        wanted = function.strip()
        matches = [
            fn
            for fn in dfg.functions
            if fn.name == wanted
            or fn.qualname == wanted
            or fn.qualname.endswith("." + wanted)
        ]
        if not matches:
            raise ValueError(
                f"no function matching {function!r} in {rel_path}; "
                "use find_symbol to locate the right name"
            )
        span = matches
    items: list[dict] = []
    for var, def_line, use_line in dfg.pairs:
        if variable is not None and variable.strip() and var != variable.strip():
            continue
        if span is not None and not any(
            fn.line <= def_line <= fn.end_line
            or fn.line <= use_line <= fn.end_line
            for fn in span
        ):
            continue
        def_exec = def_line in executed
        use_exec = use_line in executed
        items.append(
            {
                "var": var,
                "def": f"{rel_path}:{def_line}:{var}",
                "use": f"{rel_path}:{use_line}:{var}",
                "def_executed": def_exec,
                "use_executed": use_exec,
                "on_failing_path": def_exec and use_exec,
                "test": trace.get("test_id"),
                "file": rel_path,
                "engine": engine.ENGINE_NAME,
                "scope": engine.SCOPE,
            }
        )
    items.sort(
        key=lambda it: (
            not it["on_failing_path"],
            not it["def_executed"],
            it["def"],
        )
    )
    return items


def create_server() -> ReprServer:
    """Create the dfg server with its five representation tools."""
    server = ReprServer("dfg")
    server.tool(description=DEF_USE_DESCRIPTION)(def_use)
    server.tool(description=BACKWARD_SLICE_DESCRIPTION)(backward_slice)
    server.tool(description=FORWARD_SLICE_DESCRIPTION)(forward_slice)
    server.tool(description=DATA_FLOW_BETWEEN_DESCRIPTION)(data_flow_between)
    server.tool(description=REINDEX_DESCRIPTION)(reindex)
    return server
