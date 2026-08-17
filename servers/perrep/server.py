"""Per-rep MCP server, flow-first surface only.

The representation is chosen by ``REPR_PERREP_REP`` (cfg/dfg/cfgdfg). This
repository ships only the flow-first tool surface (flow_of/flow_locate/
flow_plan/flow_check/flow_verify); the runners set ``REPR_FLOW_FIRST=1``
automatically. The map DB path env vars are kept for engine compatibility:
``<REPO_ROOT>/.perrep/<rep>.db`` (or ``REPR_PERREP_DB``).

NOTE: no ``from __future__ import annotations`` -- FastMCP needs real annotations.
"""

import os
from pathlib import Path

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
            # TEST-GEN mode (SWT, REPR_FLOW_TESTGEN=1): same three tools,
            # test-generation semantics -- the plan records the delta a FIX
            # would make, and check/verify prove the SOURCE flow is untouched
            # and the diff is test-files-only.
            testgen = os.environ.get("REPR_FLOW_TESTGEN", "").strip() == "1"
            plan_desc = (fe.TG_FLOW_PLAN_DESCRIPTION if testgen
                         else fe.FLOW_PLAN_DESCRIPTION)
            check_desc = (fe.TG_FLOW_CHECK_DESCRIPTION if testgen
                          else fe.FLOW_CHECK_DESCRIPTION)
            verify_desc = (fe.TG_FLOW_VERIFY_DESCRIPTION if testgen
                           else fe.FLOW_VERIFY_DESCRIPTION)
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

    raise RuntimeError(
        "this repository ships only the flow-first tool surface "
        "(flow_of/flow_locate/flow_plan/flow_check/flow_verify); the "
        "runners set REPR_FLOW_FIRST=1 automatically")
