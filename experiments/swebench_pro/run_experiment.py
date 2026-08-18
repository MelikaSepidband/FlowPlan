"""SWE-bench Pro experiment runner: agent edit + docker test-grade per cell.

For each (instance, condition): check out the repo at base_commit, [for a perrep
condition] build the rep's .perrep/<rep>.db map on the worktree + attach the rep
MCP server, run the claude agent (editing files), extract the patch, then grade
in Docker (apply patch + run fail_to_pass/pass_to_pass). Score = resolved or not.

Conditions:
  baseline                          -- claude alone, no MCP servers
  <rep>_perrep   (ast/callgraph/    -- our fixed perrep server for <rep>
   deps/docs/hybrid)                   (the trimmed "new" versions)

Usage:
  .venv/bin/python -m experiments.swebench_pro.run_experiment \\
      --n 10 --conditions baseline ast_perrep deps_perrep \\
      --exp-name swebench_pilot --concurrency 3 --grade
"""

import argparse
import concurrent.futures as cf
import json
import os
import subprocess
import sys
import time
from pathlib import Path

STUDY_ROOT = Path(__file__).resolve().parents[2]
if str(STUDY_ROOT) not in sys.path:
    sys.path.insert(0, str(STUDY_ROOT))

from experiments.swebench_pro import instances, checkout, agent, grade  # noqa: E402

VENV_PYTHON = STUDY_ROOT / ".venv" / "bin" / "python"
CLAUDE_BIN = str(Path.home() / ".local" / "bin" / "claude")
# "cfgdfg" is the COMBINED flow arm (flow-first only: both graphs in flow_of,
# dual op vocabulary) -- it has no map/dyn/nlplan variants.
PERREP_REPS = ("ast", "callgraph", "deps", "docs", "hybrid", "cfg", "dfg",
               "cfgdfg")

# target instance_id -> injected cross-instance memory block (populated in main()
# when the 'memory_hybrid' condition is requested; empty dict otherwise).
_MEMORY_BLOCKS: dict[str, str] = {}

# External-tool baseline: colbymchenry/codegraph, run with ITS OWN whole-tree
# .codegraph/ index + Node MCP server (mirrors the LinuxFLBench 'codegraph'
# condition). NOT a perrep server; uses a neutral prompt so the steering is
# codegraph's own.
CODEGRAPH_NODE_BIN = "/home/tempadmin/.nvm/versions/node/v24.15.0/bin/node"
CODEGRAPH_CLI = "/home/tempadmin/codegraph_ext/dist/bin/codegraph.js"
CODEGRAPH_NODE_DIR = "/home/tempadmin/.nvm/versions/node/v24.15.0/bin"


def condition_rep(condition: str) -> str | None:
    """'<rep>_perrep[_dyn|_flowplan[_blocks]|_flow3|_flow|_rq2plan|_nlplan]'
    -> rep."""
    # strip modifiers right-to-left: _blocks is a modifier on _flowplan.
    if condition.endswith("_flowplan_blocks"):
        condition = condition[: -len("_blocks")]
    # _flow_nofix is the pre-fix flow-first A/B variant (strip the modifier)
    if condition.endswith("_flow_nofix"):
        condition = condition[: -len("_nofix")]
    # RQ2 ablation arms (map-free): _nlplan = forced natural-language plan (no
    # flow_check); _flowonly = flow_of ONLY, no plan, no verify (the lower-bound
    # control). (The CHECKED graph-plan arm is _flow itself, whose prompt already
    # forces the flow_plan->edit->flow_check loop.)
    # _flow3 (the v3 arm) is checked BEFORE _flow: '_perrep_flow' is a PREFIX
    # of '_perrep_flow3', and the flow3 rule must win.
    for suffix in ("_dyn", "_flowplan", "_flowonly", "_flow3", "_flow",
                   "_nlplan"):
        if condition.endswith("_perrep" + suffix):
            condition = condition[: -len(suffix)]
            break
    if condition.endswith("_perrep"):
        rep = condition[: -len("_perrep")]
        if rep in PERREP_REPS:
            return rep
    return None


def condition_flow_first(condition: str) -> bool:
    """True for the FLOW-FIRST arm (both the current and the pre-fix _nofix
    A/B variant): agent works IN the flow graph (flow_of + flow_plan +
    flow_check), NO code map. cfg = control-flow blocks/edges; dfg = def-use
    value flow (the _nofix A/B variant stays cfg-only)."""
    if (condition.endswith("_perrep_flow_nofix")
            and condition_rep(condition) == "cfg"):
        return True
    return (condition.endswith("_perrep_flow")
            and condition_rep(condition) in ("cfg", "dfg", "cfgdfg"))


def condition_flow_v3(condition: str) -> bool:
    """True for the v3 ("flow3") flow-first arm: '<rep>_perrep_flow3'.

    A NEW ARM, never a rewrite of v2 (user hard rule): same map-free
    flow_of/flow_plan/flow_check/flow_verify surface as '_perrep_flow', plus
    REPR_FLOW_V3=1 server semantics (add_function op, per-delta fault_site,
    HEAD snapshots, merge-on-replan, graded verdicts), the flowfix-skill
    prompt, and NO REPR_FLOW_LEGACY_VERIFY pin. Exact-suffix check:
    '_perrep_flow' is a PREFIX of '_perrep_flow3', so the existing
    endswith('_perrep_flow') checks never match a flow3 condition."""
    return (condition.endswith("_perrep_flow3")
            and condition_rep(condition) in ("cfg", "dfg", "cfgdfg"))


def condition_flow_nofix(condition: str) -> bool:
    """True for the PRE-FIX flow-first A/B variant (REPR_FLOW_NOFIX=1): flow_of
    without class-expansion / error-catching, to isolate the fix's effect."""
    return (condition.endswith("_perrep_flow_nofix")
            and condition_rep(condition) == "cfg")


def condition_nlplan(condition: str) -> bool:
    """RQ2 ABLATION arm, MAP-FREE: '<rep>_perrep_nlplan'.

    Identical map-free flow_of surface and forced 'plan-before-edit, revise'
    protocol as the flow-first arm, EXCEPT the plan is stated in NATURAL
    LANGUAGE in the prompt and there is NO flow_check (no machine
    verification). Isolates whether the win comes from the machine-checkable
    GRAPH contract vs merely 'plan first'. flow_plan/flow_check NOT registered."""
    return (condition.endswith("_perrep_nlplan")
            and condition_rep(condition) in ("cfg", "dfg"))


def condition_flowonly(condition: str) -> bool:
    """RQ2 LOWER-BOUND control, MAP-FREE: '<rep>_perrep_flowonly'.

    Map-free flow_of ONLY -- NO forced planning and NO verification. The
    prompt just says 'see the flow, find the spot, edit'. Isolates whether the
    flow VIEW alone helps, separate from any plan/verify discipline (the
    flow_of-only < nlplan < flow ladder). Same flow_of-only server as nlplan;
    flow_plan/flow_check NOT registered."""
    return (condition.endswith("_perrep_flowonly")
            and condition_rep(condition) in ("cfg", "dfg"))


def condition_dyn(condition: str) -> bool:
    """True for the dynamic (execution-trace overlay) arm of a perrep rep."""
    return condition.endswith("_perrep_dyn") and condition_rep(condition) is not None


def condition_flowplan(condition: str) -> bool:
    """True for the graph-mediated-editing arm (flow_plan + flow_check).

    Covers both the base '<rep>_perrep_flowplan' and the stronge
    '<rep>_perrep_flowplan_blocks' variant (see condition_cfg_blocks)."""
    base = condition
    if base.endswith("_flowplan_blocks"):
        base = base[: -len("_blocks")]
    return (base.endswith("_perrep_flowplan")
            and condition_rep(condition) in ("cfg", "dfg"))


def condition_cfg_blocks(condition: str) -> bool:
    """True for the STRONGER flowplan arm: the agent also sees the real
    block-level CFG (cfg_view). '<rep>_perrep_flowplan_blocks'."""
    return (condition.endswith("_perrep_flowplan_blocks")
            and condition_rep(condition) == "cfg")


import shutil
import threading

# serialize .repr_index builds per base_commit so parallel rep cells on the same
# instance don't build the (expensive) index concurrently.
_index_locks: dict[str, threading.Lock] = {}
_index_locks_guard = threading.Lock()

# PROCESS-WIDE lock around the actual backend build. The symbols backend uses a
# tree-sitter Parser (pyo3/Rust) cached in a MODULE-LEVEL dict shared across ALL
# threads; tree-sitter Parser is "unsendable" (not thread-safe), so two cells
# building symbols concurrently -> pyo3 PanicException "_native::Parser is
# unsendable, but sent to another thread" -> crashes the cells / run. The
# per-instance _index_lock only serializes the SAME base_commit; different
# instances still collided. This global lock serializes the build step across the
# whole orchestrator. (Builds are fast for symbols-only ast/deps, so serializing
# them costs little; before the jedi fix the slow jedi build masked this race.)
_build_lock = threading.Lock()


def _index_lock(inst: dict) -> threading.Lock:
    key = checkout.repr_index_cache_key(inst).name
    with _index_locks_guard:
        lk = _index_locks.get(key)
        if lk is None:
            lk = _index_locks[key] = threading.Lock()
    return lk


# The .repr_index backends each rep ACTUALLY reads (verified from perrep/build_*):
#   ast, deps  -> symbols only
#   docs       -> symbols + docs
#   callgraph  -> symbols + callgraph (the jedi pipeline)
#   hybrid     -> symbols + callgraph + docs
# Building the callgraph backend runs jedi.Script.goto/.infer on EVERY call site
# (tens of thousands on ansible/openlibrary), spawning a compiled-inference
# subprocess that can DEADLOCK on its pipe with no timeout -> wedges the whole
# batch. So build ONLY what the rep needs: ast/deps never trigger jedi.
_REP_BACKENDS = {
    "ast": ("symbols",),
    "deps": ("symbols",),
    "docs": ("symbols", "docs"),
    "callgraph": ("symbols", "callgraph"),
    "hybrid": ("symbols", "callgraph", "docs"),
    # cfg/dfg parse Python source on the fly (stdlib-ast / beniget engines);
    # only the symbols index is needed for name resolution + map skeleton.
    "cfg": ("symbols",),
    "dfg": ("symbols",),
    "cfgdfg": ("symbols",),
}


def ensure_repr_index(inst: dict, worktree: Path, rep: str | None = None) -> None:
    """Make worktree/.repr_index present with the backends ``rep`` needs, building
    each ONCE per base_commit and caching it. When ``rep`` is None, build all
    three (back-compat). For ast/deps this builds symbols ONLY -- no jedi callgraph
    pass -> no jedi deadlock (the cause of the batch hang)."""
    needed = _REP_BACKENDS.get(rep, ("symbols", "callgraph", "docs"))
    cache = checkout.repr_index_cache_key(inst)
    db_name = {"symbols": "symbols.db", "callgraph": "callgraph.db",
               "docs": "docs.db"}
    with _index_lock(inst):
        missing = [b for b in needed if not (cache / db_name[b]).is_file()]
        if missing:
            from indexer.backends import get_backend
            tmp_idx = worktree / ".repr_index"
            tmp_idx.mkdir(parents=True, exist_ok=True)
            # symbols is a prerequisite for callgraph/docs builders; build first.
            order = [b for b in ("symbols", "callgraph", "docs") if b in missing]
            # serialize the build across the whole process (shared tree-sitte
            # parser cache is not thread-safe).
            with _build_lock:
                for b in order:
                    get_backend(b).build(worktree, tmp_idx)
            cache.mkdir(parents=True, exist_ok=True)
            for f in tmp_idx.glob("*.db"):
                shutil.copy2(f, cache / f.name)
    # copy the needed cached DBs into the worktree
    wt_idx = worktree / ".repr_index"
    wt_idx.mkdir(parents=True, exist_ok=True)
    for b in needed:
        src = cache / db_name[b]
        if src.is_file() and not (wt_idx / db_name[b]).is_file():
            shutil.copy2(src, wt_idx / db_name[b])


def build_rep_map(rep: str, inst: dict, worktree: Path) -> dict:
    """Build .perrep/<rep>.db on the worktree, reusing the cached .repr_index."""
    from perrep.build import build_rep as _build
    out = worktree / ".perrep" / ("%s.db" % rep)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.is_file() and out.stat().st_size > 4096:
        return {"rep": rep, "cached": True}
    ensure_repr_index(inst, worktree, rep=rep)
    return _build(worktree, rep, repr_index=worktree / ".repr_index")


def write_mcp_config(rep: str, worktree: Path, run_dir: Path,
                     dyn: bool = False, flowplan: bool = False,
                     cfg_blocks: bool = False, flow_first: bool = False,
                     flow_nofix: bool = False,
                     flow_of_only: bool = False,
                     flow_verify: bool = True,
                     flow_testgen: bool = False,
                     flow_v3: bool = False) -> Path:
    """The --mcp-config json launching our perrep server for <rep>."""
    cfg = {
        "mcpServers": {
            "%s_perrep" % rep: {
                "command": str(VENV_PYTHON),
                "args": ["-m", "servers.perrep"],
                "env": {
                    "REPO_ROOT": str(worktree),
                    "REPO_INDEX_DIR": str(worktree / ".repr_index"),
                    "RUN_DIR": str(run_dir),  # parity with LinuxFLBench
                    "PYTHONPATH": str(STUDY_ROOT),
                    "REPR_PERREP_REP": rep,
                    # forward REPR_CONCISE (explore returns a map, not full
                    # source) from the orchestrator env if set.
                    "REPR_CONCISE": os.environ.get("REPR_CONCISE", ""),
                    # dyn arm: expose the failing-test execution-overlay tools.
                    # The sanitized trace lives in RUN_DIR (outside the
                    # agent-readable worktree) -- REPR_TRACE_FILE points the
                    # tools at it.
                    "REPR_TRACE": "1" if dyn else "",
                    "REPR_TRACE_FILE": str(run_dir / "trace.json") if dyn else "",
                    # flowplan arm: expose flow_plan + flow_check (graph-
                    # mediated editing). flow_plan writes its snapshot to
                    # RUN_DIR/flow_plan.json.
                    "REPR_FLOWEDIT": "1" if flowplan else "",
                    # stronger flowplan arm: also expose cfg_view (the real
                    # block-level CFG boxes-and-arrows).
                    "REPR_CFG_BLOCKS": "1" if cfg_blocks else "",
                    # flow-first arm: expose ONLY flow_of/flow_plan/flow_check
                    # (no explore map, no native tools). The flow IS the surface.
                    "REPR_FLOW_FIRST": "1" if flow_first else "",
                    # A/B: pre-fix flow_of (no class-expansion / error-catch).
                    "REPR_FLOW_NOFIX": "1" if flow_nofix else "",
                    # RQ2 NL-plan ablation: map-free but expose ONLY flow_of
                    # (no flow_plan/flow_check) -- the plan is natural language
                    # in the prompt, with no machine verification.
                    "REPR_FLOW_OF_ONLY": "1" if flow_of_only else "",
                    # LFLB passes flow_verify=False (localization, no edits).
                    "REPR_FLOW_VERIFY": "" if flow_verify else "0",
                    # SWT (test-gen) mode: plan = the delta a FIX would make;
                    # check/verify prove the SOURCE flow is untouched and the
                    # diff is test-files-only.
                    "REPR_FLOW_TESTGEN": "1" if flow_testgen else "",
                    # flow3 (v3) arm: v3 tool semantics (add_function op,
                    # fault_site, HEAD snapshots, merge-on-replan, graded
                    # verdicts, replace arg on flow_plan). Default off ->
                    # exact v2 behavior.
                    "REPR_FLOW_V3": "1" if flow_v3 else "",
                    # user decision 2026-08-05: agent-facing verification runs
                    # the PRE-FIX behavior (no by-name resolution fallback,
                    # ordinal dfg classifier) for parity with the Haiku-v2
                    # runs; the fixed code paths remain active for offline
                    # analysis, where this env is not set. flow3 does NOT pin
                    # legacy verify (the v3 verifier IS the arm); the v2 arms
                    # keep their pin.
                    "REPR_FLOW_LEGACY_VERIFY": "" if flow_v3 else "1",
                },
            }
        }
    }
    p = run_dir / "mcp.json"
    p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return p


def write_codegraph_mcp_config(worktree: Path, run_dir: Path) -> Path:
    """--mcp-config launching the EXTERNAL colbymchenry/codegraph Node server.

    It reads the worktree's prebuilt .codegraph/codegraph.db (cwd inherited from
    the claude subprocess = the worktree). PATH carries the nvm bin dir so node
    resolves its own runtime libs."""
    cfg = {
        "mcpServers": {
            "codegraph": {
                "type": "stdio",
                "command": CODEGRAPH_NODE_BIN,
                "args": [CODEGRAPH_CLI, "serve", "--mcp"],
                "env": {
                    "PATH": "%s:/usr/bin:/bin" % CODEGRAPH_NODE_DIR,
                    "REPO_ROOT": str(worktree),
                },
            }
        }
    }
    p = run_dir / "mcp.json"
    p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return p


def build_codegraph_index(inst: dict, worktree: Path) -> dict:
    """Build the external tool's .codegraph/codegraph.db on the worktree, cached
    per repo@base_commit (like .repr_index). The tool indexes the whole tree."""
    cache = checkout.CACHE_ROOT / "codegraph_index" / \
        checkout.repr_index_cache_key(inst).name
    db_rel = Path(".codegraph") / "codegraph.db"
    wt_db = worktree / db_rel
    if wt_db.is_file() and wt_db.stat().st_size > 4096:
        return {"cached": "worktree"}
    if (cache / "codegraph.db").is_file():
        wt_db.parent.mkdir(parents=True, exist_ok=True)
        for f in cache.glob("*"):
            shutil.copy2(f, wt_db.parent / f.name)
        return {"cached": "snapshot"}
    # build fresh with the external CLI, cwd = worktree. 'init' does a from-
    # scratch index (the worktree has no prior .codegraph); 'index' would erro
    # with "not initialized". (init has no -q flag; that's only on 'index'.)
    env = dict(os.environ)
    env["PATH"] = "%s:%s" % (CODEGRAPH_NODE_DIR, env.get("PATH", ""))
    # a stale/partial .codegraph from an aborted run makes 'init' refuse; wipe it
    if wt_db.parent.exists():
        shutil.rmtree(wt_db.parent, ignore_errors=True)
    proc = subprocess.run(
        [CODEGRAPH_NODE_BIN, CODEGRAPH_CLI, "init", str(worktree)],
        cwd=str(worktree), env=env, capture_output=True, text=True,
        timeout=3600)
    if not wt_db.is_file():
        raise RuntimeError(
            "codegraph init did not produce %s (rc=%s stderr=%r)" % (
                wt_db, proc.returncode, (proc.stderr or "")[-300:]))
    cache.mkdir(parents=True, exist_ok=True)
    for f in wt_db.parent.glob("*"):
        if f.is_file():
            shutil.copy2(f, cache / f.name)
    return {"cached": "built"}


def _token_minutes() -> float | None:
    """Minutes until the Claude OAuth token expires; None if unreadable."""
    try:
        cred = Path.home() / ".claude" / ".credentials.json"
        exp = (json.loads(cred.read_text("utf-8")).get("claudeAiOauth") or {}
               ).get("expiresAt")
        if not exp:
            return None
        return (exp - time.time() * 1000) / 60000.0
    except Exception:
        return None


def run_cell(inst: dict, condition: str, results_dir: Path, args) -> dict:
    """One (instance, condition) cell: checkout -> [map] -> agent -> grade."""
    iid = inst["instance_id"]
    run_id = "%s__%s" % (iid, condition)
    run_dir = (results_dir / run_id).resolve()

    # resume: skip if already graded
    if (run_dir / "grade.json").is_file() and not args.force:
        rec = json.loads((run_dir / "grade.json").read_text("utf-8"))
        rec["_skipped"] = True
        return rec

    # TOKEN GUARD (2026-08-11, after TWO mid-batch expiries each burned an
    # arm's tail as 1-turn 401 cells): if the OAuth token is dead or about to
    # die, do NOT start the cell and write NO artifacts -- a later resume
    # re-runs it cleanly after re-login. Cheap (one small file read per cell).
    mins = _token_minutes() if args.agent == "claude" else None
    if mins is not None and mins < 3:
        print("[swebench] TOKEN EXPIRED/EXPIRING (%.0f min) -- skipping %s "
              "without artifacts; re-login and resume" % (mins, run_id))
        return {"instance_id": iid, "condition": condition,
                "resolved": False, "_token_skipped": True}
    run_dir.mkdir(parents=True, exist_ok=True)

    rep = condition_rep(condition)
    mcp_config = None
    map_info = None
    try:
        # LEAKAGE FIX (2026-07-27, revised after checking the OFFICIAL harness):
        # before_repo_set_cmd is a GRADING-time command (scaleapi/
        # SWE-bench_Pro-os create_entryscript runs it inside the eval
        # container, after the candidate patch) -- so the solving worktree
        # simply never runs it (apply_gold_tests=False default). The oracle
        # tests are therefore never laid down; no stripping needed.
        leaky = getattr(args, "keep_gold_tests", False)
        wt = checkout.prepare_worktree(inst, suffix=condition,
                                       apply_gold_tests=leaky)
        # Two complementary countermeasures (both grading is unaffected --
        # the eval container re-lays the tests itself):
        #  1. we did NOT run before_repo_set_cmd, so no FIX-side test was laid
        #     down; but 2. a gold-test path that ALREADY EXISTS at base_commit
        #     (the fix MODIFIES it) is on disk from the plain checkout, so we
        #     still remove those paths from the solving tree.
        if not leaky:
            stripped = checkout.strip_gold_tests(inst, wt)
            (run_dir / "stripped_gold_tests.json").write_text(
                json.dumps(stripped), "utf-8")
        if condition == "codegraph":
            map_info = build_codegraph_index(inst, wt)
            mcp_config = write_codegraph_mcp_config(wt, run_dir)
        elif rep is not None:
            dyn = condition_dyn(condition)
            flowplan = condition_flowplan(condition)
            cfg_blocks = condition_cfg_blocks(condition)
            flow_first = condition_flow_first(condition)
            flow_nofix = condition_flow_nofix(condition)
            nlplan = condition_nlplan(condition)
            flowonly = condition_flowonly(condition)
            flow_v3 = condition_flow_v3(condition)
            # RQ2 ablation arms (_nlplan, _flowonly) share the flow-first
            # MAP-FREE base but expose ONLY flow_of. _nlplan forces an NL plan;
            # _flowonly forces nothing. The CHECKED graph-plan arm is _flow.
            # flow3 shares the flow-first map-free base (symbols index only).
            map_free = flow_first or nlplan or flowonly or flow_v3
            if map_free:
                # NO rep map (the flow is the surface, not a code map). Only the
                # symbol index is needed so flow_of can resolve the report's
                # named functions to their CFGs.
                ensure_repr_index(inst, wt, rep=rep)
                map_info = {"rep": rep, "flow_first": True, "no_map": True}
            else:
                map_info = build_rep_map(rep, inst, wt)
            if dyn:
                from experiments.swebench_pro import trace as trace_mod
                trace = trace_mod.collect_trace(inst, run_dir / "trace.json")
                (run_dir / "trace_status.json").write_text(
                    json.dumps({k: trace.get(k) for k in
                                ("ok", "reason", "test_id", "n_files")}),
                    "utf-8")
            mcp_config = write_mcp_config(rep, wt, run_dir, dyn=dyn,
                                          flowplan=flowplan,
                                          cfg_blocks=cfg_blocks,
                                          # flow3 reuses the flow-first
                                          # registration (REPR_FLOW_FIRST=1)
                                          flow_first=flow_first or flow_v3,
                                          flow_nofix=flow_nofix,
                                          flow_of_only=nlplan or flowonly,
                                          flow_v3=flow_v3)
        # Isolate git history LAST (after maps/index/trace are built from the
        # clean tree): the shared partial clone's object DB holds the fix
        # commit (fetched by past leaky runs, or lazily fetchable), so
        # without isolation `git show <fix-sha>:<path>` recovers the oracle
        # -- the documented SWE-bench cheating vector (arXiv 2607.07946: 63%
        # of Opus Pro resolutions; DeepSWE's 'Sanitized' remedy = prune
        # future commits, which the single-snapshot re-init achieves while
        # keeping git for patch extraction). Skipped by --keep-gold-tests.
        if not leaky:
            new_base = checkout.isolate_worktree_history(wt)
            (run_dir / "isolated_base.txt").write_text(new_base, "utf-8")
            # per-cell regression guard: refuse to run a contaminated cell.
            check = checkout.verify_no_oracle(inst, wt)
            (run_dir / "oracle_check.json").write_text(
                json.dumps(check), "utf-8")
            if not check["clean"]:
                raise RuntimeError("oracle leakage detected: %s" % check)
    except Exception as e:
        (run_dir / "setup_error.txt").write_text(repr(e), "utf-8")
        return {"instance_id": iid, "condition": condition, "score": 0.0,
                "resolved": False, "reason": "setup_failed", "error": repr(e)}

    # Keep the rep/codegraph MCP tools ACTIVE (never demoted to deferred) by
    # capping the built-in toolset with --tools. Then the agent calls
    # <rep>_explore organically -- no ToolSearch round-trip, no steer prompt
    # needed. Only the tool-bearing conditions get the restriction.
    is_tool_cell = (mcp_config is not None)
    restrict = is_tool_cell and not args.no_restrict_tools

    # Optional belt-and-suspenders steer (off by default now that --tools keeps
    # the tools active); kept behind --steer for ablations.
    sys_prompt_file = None
    if args.steer:
        from experiments.swebench_pro import stee
        sp = steer.system_prompt_for(condition, tools_active=restrict)
        if sp:
            sys_prompt_file = run_dir / "system_prompt.md"
            sys_prompt_file.write_text(sp, encoding="utf-8")

    # memory_hybrid: baseline agent + a block of structurally-retrieved past
    # fixes injected via the system prompt. Leakage-guarded (temporal + self)
    # inside memory_bank.retrieve. Empty block => behaves exactly like baseline.
    if condition in ("memory_hybrid", "memory_strategy", "memory_text",
                     "memory_combo", "memory_rerank"):
        block = _MEMORY_BLOCKS.get(iid, "")
        if block:
            sys_prompt_file = run_dir / "system_prompt.md"
            sys_prompt_file.write_text(block, encoding="utf-8")
        (run_dir / "memory_injected.txt").write_text(
            block or "(no memory retrieved)", encoding="utf-8")

    is_flow_first = condition_flow_first(condition)
    is_nlplan = condition_nlplan(condition)
    is_flowonly = condition_flowonly(condition)
    is_flow_v3 = condition_flow_v3(condition)
    # Map-free arms (flow-first + flow3 + the RQ2 nlplan/flowonly ablations)
    # have NO explore/map tool -- the flow IS the surface -- so they must NOT
    # get the explore nudge or the Grep-blocking explore-gate (that would tell
    # the agent to call a nonexistent cfg_explore and block its Grep).
    is_map_free = is_flow_first or is_nlplan or is_flowonly or is_flow_v3

    # The rep's PRIMARY explore tool's full MCP name -- used by both the
    # user-prompt nudge (lever 2) and the explore-gate hook (lever 3).
    explore_tool = None
    if rep is not None and not is_map_free:
        explore_tool = "mcp__%s_perrep__%s_explore" % (rep, rep)
    elif condition == "codegraph":
        explore_tool = "mcp__codegraph__codegraph_explore"

    # Adoption levers for tool cells (the pilot showed Haiku ignores the stee
    # and never calls the map -- 0 MCP calls). On by default for tool cells:
    #   lever 2 (--prompt-nudge): the explore-first instruction goes in the USER
    #     prompt's Step 0, where Haiku weights it most.
    #   lever 3 (--explore-gate): a PreToolUse hook DENIES Read/Grep/Glob until
    #     <rep>_explore has been called once. Strongest guarantee the map is used.
    user_explore_tool = None
    gate_settings = None
    if is_tool_cell and explore_tool:
        if not args.no_prompt_nudge:
            user_explore_tool = explore_tool
        if not args.no_explore_gate and args.agent == "claude":
            gate_rep = rep if rep is not None else "codegraph"
            gate_settings = agent.write_gate_settings(
                run_dir, gate_rep, explore_tool, worktree=wt)

    # dyn arms: Step-1 user-prompt nudge naming the rep's overlay tool (the
    # dyn steer paragraph alone got 0 overlay calls in the smoke cells --
    # same lesson as the explore nudge). Tool names must match the server's
    # REPR_TRACE registration (cfg -> executed_path, dfg -> executed_flows).
    user_dyn_tool = None
    if rep is not None and condition_dyn(condition) and not args.no_prompt_nudge:
        dyn_native = "executed_path" if rep == "cfg" else "executed_flows"
        user_dyn_tool = "mcp__%s_perrep__%s" % (rep, dyn_native)

    # flowplan arm: Step-2 user-prompt nudge naming the flow_plan/flow_check
    # tools (graph-mediated editing). Same adoption lever as dyn/explore.
    # The stronger (_blocks) arm also names cfg_view so the agent reads the
    # real block-level CFG before planning.
    user_flow_tools = None
    if (rep is not None and condition_flowplan(condition)
            and not args.no_prompt_nudge):
        view_tool = ("mcp__%s_perrep__cfg_view" % rep
                     if condition_cfg_blocks(condition) else None)
        user_flow_tools = ("mcp__%s_perrep__flow_plan" % rep,
                           "mcp__%s_perrep__flow_check" % rep,
                           view_tool)

    # flow-first arm: Step-0 user-prompt nudge naming flow_of (the entry point),
    # plan_tool, check_tool, verify_tool. The flow is the ONLY surface -- no
    # explore. flow_verify is the FINAL verificator: whole final change vs the
    # whole committed plan (flow_check is per-edit only).
    user_flow_first = None
    if is_flow_first and not args.no_prompt_nudge:
        user_flow_first = ("mcp__%s_perrep__flow_of" % rep,
                           "mcp__%s_perrep__flow_plan" % rep,
                           "mcp__%s_perrep__flow_check" % rep,
                           "mcp__%s_perrep__flow_verify" % rep)

    # RQ2 NL-plan ablation: map-free flow_of only, forced natural-language plan.
    user_nl_plan = None
    if condition_nlplan(condition) and not args.no_prompt_nudge:
        user_nl_plan = "mcp__%s_perrep__flow_of" % rep

    # RQ2 flow_of-only control: map-free flow_of only, NO plan, NO verify.
    user_flow_only = None
    if condition_flowonly(condition) and not args.no_prompt_nudge:
        user_flow_only = "mcp__%s_perrep__flow_of" % rep

    # flow3 (v3): the protocol text is delivered as the flowfix SKILL written
    # into the per-run claude-home (user prompt = "/flowfix" + task body);
    # REPR_FLOW_SKILL=0 inlines the same protocol above the task body. The
    # task body carries ONLY the map-free rules -- the v3 steps live in
    # flow_protocol, NOT in build_prompt (the v2 text stays untouched).
    flow3_prompt = None
    flow3_skill_md = None
    flow3_sentinel = None
    if is_flow_v3 and rep is not None:
        from experiments.common import flow_protocol
        tools3 = flow_protocol.tool_names(rep)
        body = agent.build_prompt(inst, map_free_rules=True)
        flow3_prompt = flow_protocol.build_user_prompt(
            "repair", rep, tools3, body, agent=args.agent)
        if flow_protocol.skill_mode():
            flow3_skill_md = flow_protocol.build_skill_md("repair", rep,
                                                          tools3)
        flow3_sentinel = flow_protocol.SENTINEL

    if args.agent == "claude":
        agent.run_agent(
            inst, run_dir, claude_bin=args.claude_bin, worktree=wt,
            model=args.model, effort=args.effort_flag,
            max_turns=args.max_turns,
            max_budget_usd=args.max_budget_usd,
            timeout_sec=args.timeout_sec, mcp_config=mcp_config,
            system_prompt_file=sys_prompt_file, restrict_tools=restrict,
            explore_tool=user_explore_tool, dyn_tool=user_dyn_tool,
            flow_tools=user_flow_tools, flow_first_tools=user_flow_first,
            nl_plan_tool=user_nl_plan, flow_only_tool=user_flow_only,
            prompt_override=flow3_prompt, skill_md=flow3_skill_md,
            sentinel=flow3_sentinel, gate_settings_file=gate_settings)
    else:
        from experiments.common import codex_agent
        codex_prompt = flow3_prompt or agent.build_prompt(
            inst, explore_tool=user_explore_tool, dyn_tool=user_dyn_tool,
            flow_tools=user_flow_tools, flow_first_tools=user_flow_first,
            nl_plan_tool=user_nl_plan, flow_only_tool=user_flow_only)
        if sys_prompt_file is not None:
            extra_prompt = sys_prompt_file.read_text(encoding="utf-8")
            marker = "$flowfix\n\n"
            if codex_prompt.startswith(marker):
                codex_prompt = (marker + extra_prompt + "\n\n"
                                + codex_prompt[len(marker):])
            else:
                codex_prompt = extra_prompt + "\n\n" + codex_prompt
        codex_agent.run_agent(
            inst, run_dir, codex_bin=args.codex_bin, worktree=wt,
            model=args.model, effort=args.effort_flag,
            max_turns=args.max_turns,
            max_budget_usd=args.max_budget_usd,
            timeout_sec=args.timeout_sec, prompt=codex_prompt,
            mcp_config=mcp_config, skill_md=flow3_skill_md,
            sentinel=flow3_sentinel,
            auth_file=(Path(args.codex_auth_file)
                       if args.codex_auth_file else None),
            auth_mode=args.codex_auth_mode)
        patch = checkout.diff_excluding_tests(inst, wt)
        (run_dir / "patch.diff").write_text(patch, encoding="utf-8")

    # measurement: record the FINAL plan-vs-code verdict for flow cells (same
    # check as the agent-facing flow_verify tool), whether or not the agent
    # called it -- run BEFORE the worktree is cleaned up.
    if is_flow_first and rep is not None:
        from experiments import flowverify
        flowverify.record_final_verify(VENV_PYTHON, STUDY_ROOT, rep, wt,
                                       run_dir)
    # flow3: same runner-side final verify, under v3 semantics (extra_env
    # carries REPR_FLOW_V3; the legacy pin is deliberately absent).
    if is_flow_v3 and rep is not None:
        from experiments import flowverify
        flowverify.record_final_verify(VENV_PYTHON, STUDY_ROOT, rep, wt,
                                       run_dir,
                                       extra_env={"REPR_FLOW_V3": "1"})

    rec = {"instance_id": iid, "condition": condition, "rep": rep,
           "map_edges": (map_info or {}).get("edges")}
    if args.grade:
        g = grade.grade(inst, run_dir)
        rec.update({k: g.get(k) for k in (
            "resolved", "score", "reason", "n_passed", "n_required",
            "n_missing")})
    if not args.keep_worktrees:
        checkout.cleanup_worktree(inst, suffix=condition)
    return rec


def clean_user_site() -> list[str]:
    """Layer 3 of the contamination fix: delete stale editable-install pointers
    that accumulate in the SHARED ~/.local/site-packages across runs (a prio
    cell's `pip install -e .` leaves an easy-install.pth / *.egg-link pointing at
    THAT cell's worktree; a later cell's python that doesn't hono
    PYTHONNOUSERSITE then imports the package from a long-dead sibling worktree).
    Removing them before each batch means no stale pointer can survive. Returns
    the list of removed file paths (for transparency)."""
    import sysconfig
    removed = []
    candidates = set()
    # the standard user-site dir + a couple of common version-specific ones
    try:
        candidates.add(Path(sysconfig.get_path("purelib", "posix_user")))
    except Exception:
        pass
    home = Path.home()
    for v in ("python3.12", "python3.11", "python3.10"):
        candidates.add(home / ".local" / "lib" / v / "site-packages")
    for site in candidates:
        if not site.is_dir():
            continue
        for pat in ("easy-install.pth", "*.egg-link", "__editable__*", "*.pth"):
            for f in site.glob(pat):
                # only nuke editable/egg pointers; leave real namespace .pth
                # packages (e.g. sphinxcontrib_*-nspkg.pth) alone unless they
                # reference a swebench worktree.
                try:
                    txt = f.read_text(encoding="utf-8", errors="ignore") \
                        if f.suffix in (".pth", ".egg-link") else ""
                except Exception:
                    txt = ""
                is_editable = f.name in ("easy-install.pth",) or \
                    f.suffix == ".egg-link" or f.name.startswith("__editable__") \
                    or "swebench_cache/worktrees" in txt
                if is_editable:
                    try:
                        f.unlink()
                        removed.append(str(f))
                    except Exception:
                        pass
    return removed


def main(argv=None):
    p = argparse.ArgumentParser(prog="swebench_pro.run_experiment")
    p.add_argument("--n", type=int, default=10, help="number of instances")
    p.add_argument("--instance-ids", nargs="*", default=None)
    p.add_argument("--ids-file", default=None,
                   help="JSON file with a list of instance_ids to run")
    p.add_argument("--conditions", nargs="+",
                   default=["baseline"])
    p.add_argument("--exp-name", default="swebench_pro")
    p.add_argument("--concurrency", type=int, default=3)
    p.add_argument("--agent", choices=["claude", "codex"], default="claude")
    p.add_argument("--model", default=None)
    p.add_argument("--effort-flag", default="low")
    p.add_argument("--max-turns", type=int, default=80)
    p.add_argument("--max-budget-usd", type=float, default=2.0)
    p.add_argument("--timeout-sec", type=int, default=1500)
    p.add_argument("--claude-bin", default=CLAUDE_BIN)
    p.add_argument("--codex-bin", default=os.environ.get("REPR_CODEX_BIN",
                                                         "codex"))
    p.add_argument("--codex-auth-file", default=None)
    p.add_argument("--codex-auth-mode", choices=["auto", "chatgpt", "api-key"],
                   default="auto")
    p.add_argument("--grade", action="store_true",
                   help="docker test-grade each run after the agent")
    p.add_argument("--force", action="store_true", help="re-run graded cells")
    p.add_argument("--keep-worktrees", action="store_true")
    p.add_argument("--steer", action="store_true",
                   help="ALSO add the early-explore steer system prompt "
                        "(off by default; --tools restriction already keeps the "
                        "MCP tools active)")
    p.add_argument("--no-restrict-tools", action="store_true",
                   help="do NOT cap built-in tools with --tools (lets MCP tools "
                        "demote to deferred -- the un-fixed behavior)")
    p.add_argument("--no-prompt-nudge", action="store_true",
                   help="adoption lever 2 OFF: do NOT add the explore-first "
                        "Step-0 instruction to the USER prompt for tool cells")
    p.add_argument("--no-explore-gate", action="store_true",
                   help="adoption lever 3 OFF: do NOT install the PreToolUse hook "
                        "that denies Read/Grep/Glob until <rep>_explore is called")
    p.add_argument("--keep-gold-tests", action="store_true",
                   help="do NOT strip the harness-laid gold test files from "
                        "the agent worktree (reproduces pre-2026-07-27 "
                        "leaky behavior; for ablation only)")
    p.add_argument("--no-clean-user-site", action="store_true",
                   help="do NOT wipe stale ~/.local editable-install pointers "
                        "before the batch (contamination-hardening layer 3)")
    p.add_argument("--dataset", default="pro", choices=["pro", "verified"],
                   help="which benchmark: 'pro' (SWE-bench Pro, default) or "
                        "'verified' (SWE-bench Verified -- raw issue, no "
                        "interface field, so the agent must localize)")
    a = p.parse_args(argv)
    if a.model is None:
        a.model = ("gpt-5.4-mini" if a.agent == "codex"
                   else "claude-haiku-4-5")

    # TOKEN-FRESHNESS GUARD: a mid-batch OAuth expiry silently turns cells into
    # 1-turn 401 "successes" that corrupt the results (this exact thing happened
    # on the n=70 run). Warn loudly if the token will likely die before the batch
    # finishes. Does NOT abort (the user may still want to start), but the warning
    # makes the risk explicit so they can re-login first.
    try:
        import json as _json
        cred = Path.home() / ".claude" / ".credentials.json"
        exp = (_json.loads(cred.read_text("utf-8")).get("claudeAiOauth") or {}
               ).get("expiresAt")
        if exp and a.agent == "claude":
            mins = (exp - time.time() * 1000) / 60000.0
            print("[swebench] OAuth token: ~%.0f min remaining" % mins)
            if mins < 90:
                print("[swebench] *** WARNING: token expires in %.0f min. A long "
                      "batch may 401 mid-run (cells become 1-turn auth failures). "
                      "Re-login (`claude`) before launching if this batch is "
                      "large. ***" % mins)
    except Exception:
        pass

    # dataset swap: 'verified' uses the SWE-bench Verified loader (raw issue,
    # NO interface/requirements -> agent must localize). checkout/agent/grade
    # consume the same instance shape (repo, repo_url, base_commit, problem_*).
    global instances
    if a.dataset == "verified":
        from experiments.swebench_verified import instances as _vinst
        instances = _vinst

    ids = a.instance_ids
    if a.ids_file:
        ids = json.loads(Path(a.ids_file).read_text("utf-8"))
    if ids:
        insts = instances.load_subset(ids)
    else:
        insts = instances.load_all()[: a.n]
    results_dir = STUDY_ROOT / "results" / a.exp_name
    results_dir.mkdir(parents=True, exist_ok=True)

    # Precompute cross-instance memory blocks if the memory condition is present.
    # Memory pool = ALL instances of the same repo(s) as the targets (so a target
    # can recall temporally-prior siblings); the retriever's temporal+self guard
    # prevents leakage. Blocks keyed by target instance_id.
    global _MEMORY_BLOCKS
    _mem_conds = {"memory_hybrid", "memory_strategy", "memory_text",
                  "memory_combo", "memory_rerank"}
    if _mem_conds & set(a.conditions):
        from experiments.swebench_verified.memory_bank import (
            HybridMemory, TextMemory, format_memory_block,
            format_memory_block_distilled, format_memory_block_combo,
            format_memory_block_rerank)
        target_repos = set(i["repo"] for i in insts)
        pool = [r for r in instances.load_all() if r["repo"] in target_repos]
        rerank_arm = "memory_rerank" in a.conditions
        combo_arm = "memory_combo" in a.conditions
        text_arm = "memory_text" in a.conditions
        # per-repo pools so the structural index resolves correctly
        by_repo = {}
        for repo in target_repos:
            by_repo[repo] = [r for r in pool if r["repo"] == repo]
        if rerank_arm:
            # STRUCT-ONLY LLM rerank: canonical retriever's top-10 candidates,
            # one LLM call picks the most transferable, K=1 distilled card.
            # No text retrieval anywhere (user design).
            print("[memory] building STRUCT-ONLY LLM-rerank memory "
                  "(ast+cg+deps+docs top-10 -> LLM pick -> K=1 card)")
            struct_mems = {rp: HybridMemory(rows) for rp, rows in by_repo.items()}
            id2repo = {i["instance_id"]: i["repo"] for i in insts}
            for inst in insts:
                rp = id2repo[inst["instance_id"]]
                _MEMORY_BLOCKS[inst["instance_id"]] = format_memory_block_rerank(
                    struct_mems[rp], None, inst["instance_id"])
        elif combo_arm:
            print("[memory] building COMBINED (structural hybrid + text) memory")
            struct_mems = {rp: HybridMemory(rows) for rp, rows in by_repo.items()}
            text_mems = {rp: TextMemory(rows) for rp, rows in by_repo.items()}
            id2repo = {i["instance_id"]: i["repo"] for i in insts}
            for inst in insts:
                rp = id2repo[inst["instance_id"]]
                _MEMORY_BLOCKS[inst["instance_id"]] = format_memory_block_combo(
                    struct_mems[rp], text_mems[rp], inst["instance_id"])
        else:
            print("[memory] building %s memory over %d pool instances"
                  % ("TEXT" if text_arm else "hybrid-structural", len(pool)))
            _mem = TextMemory(pool) if text_arm else HybridMemory(pool)
            distilled = ("memory_strategy" in a.conditions) or text_arm
            for inst in insts:
                if distilled:
                    _MEMORY_BLOCKS[inst["instance_id"]] = \
                        format_memory_block_distilled(_mem, inst["instance_id"],
                                                      k=1)
                else:
                    _MEMORY_BLOCKS[inst["instance_id"]] = format_memory_block(
                        _mem, inst["instance_id"], k=3)
        got = sum(1 for v in _MEMORY_BLOCKS.values() if v)
        print("[memory] retrieved non-empty memory for %d/%d targets"
              % (got, len(insts)))

    # HARDEN contamination: wipe stale shared-user-site editable pointers before
    # the batch (layer 3; layers 1+2 -- PYTHONNOUSERSITE + per-cell PYTHONUSERBASE
    # -- are set per cell in agent.py).
    if not a.no_clean_user_site:
        removed = clean_user_site()
        print("[swebench] cleaned %d stale ~/.local editable pointer(s)%s"
              % (len(removed), (": " + ", ".join(Path(r).name for r in removed))
                 if removed else ""))

    cells = [(inst, cond) for cond in a.conditions for inst in insts]
    print("[swebench] %d instances x %d conditions = %d cells (concurrency %d)"
          % (len(insts), len(a.conditions), len(cells), a.concurrency))

    results = []
    with cf.ThreadPoolExecutor(max_workers=a.concurrency) as ex:
        futs = {ex.submit(run_cell, inst, cond, results_dir, a): (inst, cond)
                for inst, cond in cells}
        done = 0
        for fut in cf.as_completed(futs):
            inst, cond = futs[fut]
            try:
                rec = fut.result()
            except Exception as e:
                rec = {"instance_id": inst["instance_id"], "condition": cond,
                       "score": 0.0, "resolved": False, "error": repr(e)}
            results.append(rec)
            done += 1
            res = rec.get("resolved")
            mark = "OK " if res else ("skip" if rec.get("_skipped") else "x  ")
            print("[%d/%d] %s %-14s %s" % (
                done, len(cells), mark, cond, inst["instance_id"][:48]))

    # per-condition resolve rate
    print("\n=== resolve rate by condition ===")
    for cond in a.conditions:
        rs = [r for r in results if r.get("condition") == cond]
        n = len(rs)
        solved = sum(1 for r in rs if r.get("resolved"))
        print("  %-16s %d/%d = %.1f%%" % (
            cond, solved, n, 100.0 * solved / n if n else 0.0))
    (results_dir / "_summary.json").write_text(
        json.dumps(results, indent=2), "utf-8")


if __name__ == "__main__":
    main()
