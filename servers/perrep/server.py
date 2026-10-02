"""Per-rep codegraph MCP server: ``<rep>_explore`` + ``<rep>_node`` over the
rep's prebuilt map. Mirrors codegraph (one map, two tools), one rep per server.

The representation is chosen by ``REPR_PERREP_REP`` (callgraph/ast/deps/docs/
hybrid). The map is ``<REPO_ROOT>/.perrep/<rep>.db`` (or ``REPR_PERREP_DB``).

NOTE: no ``from __future__ import annotations`` -- FastMCP needs real annotations.
"""

import os
from pathlib import Path
from typing import Optional

from servers.common import env
from servers.common.base import ReprServer
from perrep.engine import Engine, config_for

__all__ = ["create_server", "current_rep", "get_engine"]


def current_rep() -> str:
    return os.environ.get("REPR_PERREP_REP", "callgraph").strip() or "callgraph"


def _db_path(rep: str) -> Path:
    p = os.environ.get("REPR_PERREP_DB", "").strip()
    if p:
        return Path(p)
    return env.repo_root() / ".perrep" / ("%s.db" % rep)


_engine_cache: dict = {}


def get_engine() -> Engine:
    rep = current_rep()
    db = _db_path(rep)
    key = (rep, str(db))
    eng = _engine_cache.get(key)
    if eng is None:
        eng = Engine(config_for(rep, db, repo_root=env.repo_root()))
        _engine_cache[key] = eng
    return eng


# -- the codegraph-style explanations, per rep -------------------------------

_EXPLORE_BLURB = {
    "callgraph": "ranked by the CALL graph (who calls whom)",
    "ast": "ranked by STRUCTURAL containment (enclosing/nested defs)",
    "deps": "ranked by the IMPORT/#include dependency graph",
    "docs": "ranked by documentation/text relevance",
    "hybrid": "ranked by the merged CALL + IMPORT graph",
    "cfg": "ranked by CONTROL FLOW content (branch conditions, loop headers)",
    "dfg": "ranked by VALUE FLOW (where a name is defined and where it is used)",
}
_NEIGHBOR_BLURB = {
    "callgraph": "who calls it and what it calls",
    "ast": "its enclosing def and its nested defs",
    "deps": "the files it imports and the files that import it",
    "docs": "its doc-comment and related documented symbols",
    "hybrid": "its callers/callees and importers",
    "cfg": "its enclosing def and nested defs, with each function's flow summary",
    "dfg": "the functions its values flow to and from",
}

EXPLORE_BUDGET = 12000
BODY_MAX_CHARS = 20000


def create_server() -> ReprServer:
    rep = current_rep()
    server = ReprServer("%s_perrep" % rep)

    # FLOW-FIRST mode (cfg/dfg): the agent works IN THE FLOW, not via a code
    # map. Expose ONLY flow_of (the report's named symbols' flow graph -- CFG
    # blocks+edges for cfg, def-use value flow for dfg -- the entry point) +
    # flow_plan + flow_check. No _explore/_node/_search/_files, no native
    # tools -- there is deliberately no repo map to browse. This is the
    # "change the part of the flow that fixes the bug" arm, distinct from the
    # map-plus-flowplan arm. flow_plan/flow_check are already rep-aware via
    # REPR_PERREP_REP (op vocabulary + fingerprint dispatch in flowedit).
    flow_first = os.environ.get("REPR_FLOW_FIRST", "").strip() == "1"
    # RQ2 NL-plan ablation: map-free, but expose ONLY flow_of -- the plan is
    # natural language in the prompt, so flow_plan/flow_check are withheld.
    flow_of_only = os.environ.get("REPR_FLOW_OF_ONLY", "").strip() == "1"
    if rep in ("cfg", "dfg", "cfgdfg") and (flow_first or flow_of_only):
        from servers.common import flowedit_tools as fe
        of_desc = {"dfg": fe.DFG_FLOW_OF_DESCRIPTION,
                   "cfgdfg": fe.CFGDFG_FLOW_OF_DESCRIPTION}.get(
                       rep, fe.FLOW_OF_DESCRIPTION)
        server.tool(description=of_desc)(fe.flow_of)
        if not flow_of_only:
            plan_desc = fe.FLOW_PLAN_DESCRIPTION
            check_desc = fe.FLOW_CHECK_DESCRIPTION
            verify_desc = fe.FLOW_VERIFY_DESCRIPTION
            # flow3 (REPR_FLOW_V3=1): SAME tool name + description, but the
            # v3 callable, whose schema adds the optional ``replace`` arg
            # (v3 re-plans MERGE by function; replace=true starts over).
            # flow3 ALSO gains flow_locate -- the protocol's separate step 2
            # (declare WHERE the flow is wrong; flow_plan inherits it as
            # each delta's fault_site) -- so a flow3 server exposes exactly
            # 5 tools where a v2 flow server keeps exactly 4.
            # Default off -> the exact v2 callable/schema, byte-identical.
            if os.environ.get("REPR_FLOW_V3", "").strip() == "1":
                server.tool(description=fe.FLOW_LOCATE_DESCRIPTION)(
                    fe.flow_locate)
                server.tool(description=plan_desc,
                            name="flow_plan")(fe.flow_plan_v3)
            else:
                server.tool(description=plan_desc)(fe.flow_plan)
            server.tool(description=check_desc)(fe.flow_check)
            # final verificator: the WHOLE final change vs the WHOLE plan
            # (flow_check is per-edit; this re-checks everything at the end).
            # REPR_FLOW_VERIFY=0 withholds it (LFLB: localization, no edits).
            if os.environ.get("REPR_FLOW_VERIFY", "").strip() != "0":
                server.tool(description=verify_desc)(fe.flow_verify)
        return server

    blurb = _EXPLORE_BLURB.get(rep, "ranked by the code graph")
    nblurb = _NEIGHBOR_BLURB.get(rep, "its neighbors")

    explore_desc = (
        "PRIMARY: %s_explore(query) -- pass the bug report's key terms or suspect "
        "symbol names. Returns, in ONE call, the most relevant code (%s, not just "
        "text) with its VERBATIM SOURCE grouped by file. Read-equivalent -- treat "
        "shown source as already read; do NOT re-open those files. Usually the "
        "ONLY call you need." % (rep, blurb)
    )
    node_desc = (
        "%s_node(target='file:line' or an exact symbol) -- the read tool. Returns "
        "the definition's VERBATIM SOURCE plus %s, from the %s map. Use this "
        "INSTEAD of the built-in Read for any anchor %s_explore surfaces. An "
        "ambiguous name returns every matching definition." % (rep, nblurb, rep, rep)
    )

    def _explore(query: str) -> list:
        q = (query or "").strip()
        if not q:
            raise ValueError("query must not be empty")
        eng = get_engine()
        if not eng.available():
            return [{"query": q, "rep": rep,
                     "body": "%s map unavailable -- run `python -m perrep.build "
                     "--rep %s` for this repo." % (rep, rep)}]
        res = eng.explore(q, max_files=12, max_chars=BODY_MAX_CHARS)
        return [{"query": q, "rep": rep, "n_symbols": res["n_symbols"],
                 "n_files": res["n_files"], "body": res["body"]}]

    def _node(target: str, max_lines: int = 80) -> list:
        eng = get_engine()
        if not eng.available():
            return [{"target": target,
                     "error": "%s map unavailable for this repo." % rep}]
        return eng.node(target, max_lines=max_lines)

    _explore.__name__ = "%s_explore" % rep
    _explore.__doc__ = ("Explore the codebase via the %s map: graph-ranked "
                        "relevant symbols + verbatim source, in one call." % rep)
    _explore._repr_budget = EXPLORE_BUDGET
    _node.__name__ = "%s_node" % rep
    _node.__doc__ = ("Read one definition's source + its %s-map neighbors. "
                     "Use INSTEAD of Read." % rep)
    _node._repr_budget = EXPLORE_BUDGET

    server.tool(description=explore_desc)(_explore)
    server.tool(description=node_desc)(_node)

    # codegraph's three GENERIC utility tools, rep-named for telemetry:
    # <rep>_search (FTS symbol lookup), <rep>_files (file tree + symbol counts),
    # <rep>_status (map health). These read the rep's own .perrep map.
    def _search(query: str, limit: int = 10) -> list:
        eng = get_engine()
        if not eng.available():
            return [{"query": query, "error": "%s map unavailable." % rep}]
        return eng.search_tool(query, limit=limit)

    def _files(under: Optional[str] = None) -> list:
        eng = get_engine()
        if not eng.available():
            return [{"error": "%s map unavailable." % rep}]
        return eng.files(under=under)

    def _status() -> list:
        eng = get_engine()
        if not eng.available():
            return [{"error": "%s map unavailable." % rep}]
        return [eng.status()]

    _search.__name__ = "%s_search" % rep
    _files.__name__ = "%s_files" % rep
    _status.__name__ = "%s_status" % rep
    # DROP the generic <rep>_search for deps + docs -- it's a strict SUBSET of a
    # better tool there:
    #   deps: deps_search returned locations-only vs deps_explore's source (A/B
    #         on n=10 showed 3.6 calls/run of pure redundancy -> removing it
    #         pushed the calls onto deps_explore, -13% tokens, recall held).
    #   docs: docs_search queries the SAME doc-FTS index as the docs-NATIVE
    #         search_docs but strips the doc-comment text; search_docs returns
    #         the same hits PLUS the doc text, so docs_search is the inferior
    #         duplicate. Keep search_docs, drop docs_search.
    #   hybrid: hybrid_search returns locations only; the native symbol_lookup
    #         returns name/file/line PLUS source+doc+anchor, and hybrid_explore
    #         returns source too -- so hybrid_search is the inferior duplicate.
    #         Keep symbol_lookup, drop hybrid_search.
    # callgraph + ast keep <rep>_search (their completed runs stay valid).
    if rep not in ("deps", "docs", "hybrid"):
        server.tool(description=(
            "%s_search(query) -- quick symbol lookup in the %s map, locations only "
            "(no source). Use %s_explore instead to get the actual code in one call."
            % (rep, rep, rep)))(_search)
    # A/B: ast now KEEPS <rep>_files (file_skeleton removed instead, above) to
    # test the reverse trim -- which of the two file-structure tools the agent
    # prefers. <rep>_files registers for all reps here.
    server.tool(description=(
        "%s_files(under=None) -- files in the %s map with their symbol "
        "counts. Faster than Glob for project layout; filter with a "
        "directory prefix." % (rep, rep)))(_files)
    server.tool(description=(
        "%s_status() -- %s map health: node/edge/file counts. Skip unless "
        "debugging the index." % (rep, rep)))(_status)

    # Each rep ALSO keeps its OWN representation-native tools (from the original
    # per-rep servers) so the reps stay genuinely DIFFERENT -- not 5 copies of
    # codegraph's generic surface. Codegraph's explore+node+search+files+status
    # sit ON TOP of the rep's own specialized tools (callgraph ->
    # callers_of/callees_of/..., ast -> file_skeleton/astgrep_search/..., etc.).
    _register_native_tools(server, rep)
    return server


def _register_native_tools(server: "ReprServer", rep: str) -> None:
    """Add the rep's OWN tools from its original server, so each rep is distinct.

    Imports the original tool callables + descriptions and registers them on this
    server (the tools read the same REPO_ROOT/.repr_index they always did)."""
    if rep == "callgraph":
        from servers.callgraph_server import server as m
        tools = [(m.callers_of, m.CALLERS_OF_DESCRIPTION),
                 (m.callees_of, m.CALLEES_OF_DESCRIPTION),
                 (m.call_path, m.CALL_PATH_DESCRIPTION),
                 (m.function_info, m.FUNCTION_INFO_DESCRIPTION)]
        # dyn arm: crash_callpath is the callgraph-native execution overlay --
        # the sanitizer crash stack as a call chain, each edge checked vs the
        # static call graph. Exposed only for a sanitizer-kind trace.
        if (os.environ.get("REPR_TRACE", "").strip() == "1"
                and os.environ.get("REPR_TRACE_KIND", "").strip()
                == "sanitizer"):
            tools.append((m.crash_callpath, m.CRASH_CALLPATH_DESCRIPTION))
    elif rep == "ast":
        from servers.ast_server import tools as t
        from servers import ast_server as m   # descriptions live in __init__
        # A/B variant (ast10e): keep BOTH file tools (file_skeleton restored +
        # ast_files on below), but DROP astgrep_search -- test whether the
        # structural code-pattern search is pulling its weight (it was 1.9/run).
        tools = [(t.file_skeleton, m.FILE_SKELETON_DESCRIPTION),
                 (t.ts_query, m.TS_QUERY_DESCRIPTION),
                 (t.parse_file, m.PARSE_FILE_DESCRIPTION)]
    elif rep == "deps":
        from servers.deps_server import server as m
        tools = [(m.module_deps, m.MODULE_DEPS_DESCRIPTION),
                 (m.dependency_path, m.DEPENDENCY_PATH_DESCRIPTION),
                 (m.layer_summary, m.LAYER_SUMMARY_DESCRIPTION),
                 (m.cycles, m.CYCLES_DESCRIPTION)]
    elif rep == "docs":
        from servers.docs_server import server as m
        # A/B (docs trim): DROP search_docs + symbol_doc, both redundant with the
        # codegraph pair:
        #   search_docs(query) FTS's the SAME doc index as docs_explore but
        #     returns doc-hits WITHOUT source -> a strict subset of docs_explore.
        #     (It was the most-used tool at 7.9/run; we test whether forcing the
        #     agent onto docs_explore -- which returns doc text AND source -- cuts
        #     the ~21 MCP calls/run that made docs expensive.)
        #   symbol_doc(name) returns one symbol's doc text only; docs_node
        #     already returns that symbol's SOURCE + doc neighbors, so symbol_doc
        #     is dominated by docs_node even after search_docs is gone.
        # Keep module_overview + repo_map (the structural doc-navigation tools).
        tools = [(m.module_overview, m.MODULE_OVERVIEW_DESCRIPTION),
                 (m.repo_map, m.REPO_MAP_DESCRIPTION)]
    elif rep == "hybrid":
        from servers.hybrid_server import server as m
        # A/B (hybrid trim): DROP symbol_lookup + neighborhood, both dominated by
        # hybrid_node (which returns the symbol's SOURCE + its calls/imports
        # neighbors in one call). Usage on the full 8-tool n=10 run confirmed
        # they're dead weight: symbol_lookup 1.0/run (1 of 10 runs), neighborhood
        # 0.1/run (1 of 10) -- the agent uses hybrid_node instead. We test whether
        # removing them tightens the surface like the docs trim did (which RAISED
        # recall + cut tokens). Keep impact_of (unique: transitive blast-radius)
        # + repo_map (PageRank file map).
        tools = [(m.impact_of, m.IMPACT_OF_DESCRIPTION),
                 (m.repo_map, m.REPO_MAP_DESCRIPTION)]
    elif rep == "cfg":
        from servers.cfg_server import server as m
        tools = [(m.get_cfg, m.GET_CFG_DESCRIPTION),
                 (m.cfg_paths, m.CFG_PATHS_DESCRIPTION),
                 (m.dominators, m.DOMINATORS_DESCRIPTION)]
        # dyn arm: the failing-test execution overlay (REPR_TRACE=1 is set by
        # the harness only for *_perrep_dyn conditions, which also collect the
        # trace pre-run; the static arm's tool surface stays unchanged).
        if os.environ.get("REPR_TRACE", "").strip() == "1":
            tools += [(m.executed_path, m.EXECUTED_PATH_DESCRIPTION),
                      (m.covered_blocks, m.COVERED_BLOCKS_DESCRIPTION)]
            # crash_path is the SecBench-native overlay (sanitizer stack);
            # exposed only when a stack-kind trace is present so it stays
            # silent on SWE-Pro coverage traces.
            if os.environ.get("REPR_TRACE_KIND", "").strip() == "sanitizer":
                tools.append((m.crash_path, m.CRASH_PATH_DESCRIPTION))
        # graph-mediated editing: flow_plan + flow_check + flow_verify
        # (cfg/dfg only).
        if os.environ.get("REPR_FLOWEDIT", "").strip() == "1":
            from servers.common import flowedit_tools as fe
            tools += [(fe.flow_plan, fe.FLOW_PLAN_DESCRIPTION),
                      (fe.flow_check, fe.FLOW_CHECK_DESCRIPTION),
                      (fe.flow_verify, fe.FLOW_VERIFY_DESCRIPTION)]
            # stronger arm: cfg_view surfaces the REAL block-level CFG
            # (boxes-and-arrows) so the agent SEES the control flow of the
            # function it will edit, not just the function-map flow summary.
            if os.environ.get("REPR_CFG_BLOCKS", "").strip() == "1":
                tools.append((fe.cfg_view, fe.CFG_VIEW_DESCRIPTION))
    elif rep == "dfg":
        from servers.dfg_server import server as m
        tools = [(m.def_use, m.DEF_USE_DESCRIPTION),
                 (m.backward_slice, m.BACKWARD_SLICE_DESCRIPTION),
                 (m.forward_slice, m.FORWARD_SLICE_DESCRIPTION),
                 (m.data_flow_between, m.DATA_FLOW_BETWEEN_DESCRIPTION)]
        # dyn arm: executed_flows is dfg-native (value flow on the failing
        # path); covered_blocks (from the cfg server, rep-agnostic) ranks a
        # file's functions by failing-run coverage.
        if os.environ.get("REPR_TRACE", "").strip() == "1":
            from servers.cfg_server import server as cfg_m
            tools += [(m.executed_flows, m.EXECUTED_FLOWS_DESCRIPTION),
                      (cfg_m.covered_blocks, cfg_m.COVERED_BLOCKS_DESCRIPTION)]
        if os.environ.get("REPR_FLOWEDIT", "").strip() == "1":
            from servers.common import flowedit_tools as fe
            tools += [(fe.flow_plan, fe.FLOW_PLAN_DESCRIPTION),
                      (fe.flow_check, fe.FLOW_CHECK_DESCRIPTION),
                      (fe.flow_verify, fe.FLOW_VERIFY_DESCRIPTION)]
    else:
        tools = []
    for impl, desc in tools:
        server.tool(description=desc)(impl)
