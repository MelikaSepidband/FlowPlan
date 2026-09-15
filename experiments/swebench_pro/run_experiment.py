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
# "calls" is the CALL-SITE flow arm (flow-first only: the function's call
# sites are the working surface) -- distinct from "callgraph", the old
# repo-level map rep; it has no map/dyn/nlplan variants.
PERREP_REPS = ("ast", "callgraph", "deps", "docs", "hybrid", "cfg", "dfg",
               "cfgdfg", "calls", "src", "loc")

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
    # of '_perrep_flow3', and the flow3 rule must win. Same ordering rule:
    # _flow3s (strict) before _flow3 before _flow.
    for suffix in ("_dyn", "_flowplan", "_flowonly", "_flow3slice",
                   "_flow3dyn", "_flow3s", "_flow3b", "_flow3c",
                   "_flow3rip", "_flow3r", "_flow3L", "_flow3", "_flow",
                   "_nlplan", "_graphfirst"):
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
    endswith('_perrep_flow') checks never match a flow3 condition.

    'ast' (ast_perrep_flow3): the SAME v3 protocol with the function's
    SYNTAX TREE as the working surface; 'calls' (calls_perrep_flow3): with
    the function's CALL SITES as the surface -- both flow3-only, there are
    no v2 ast/calls flow arms (condition_flow_first stays cfg/dfg/cfgdfg).

    '<rep>_perrep_flow3s' (STRICT, 2026-08-25 accuracy-lever experiment) is
    flow3 in every harness respect (same prompt, skill, tools, map-free
    build) and counts as flow3 here; condition_flow_strict adds the
    REPR_FLOW_STRICT=1 server env on top (fault_site hard gate + dfg
    no-change tripwire). A NEW ARM, never a rewrite: with the env unset the
    flow3 server behavior is byte-identical."""
    if (condition_flow_strict(condition) or condition_flow_slice(condition)
            or condition_flow_dyn3(condition)
            or condition_flow_bundle(condition)
            or condition_flow_comply(condition)
            or condition_flow_relay(condition)
            or condition_flow_lean(condition)
            or condition_flow_ripple(condition)):
        return True
    return (condition.endswith("_perrep_flow3")
            and condition_rep(condition) in ("cfg", "dfg", "cfgdfg", "ast",
                                             "calls", "src", "loc"))


def condition_flow_slice(condition: str) -> bool:
    """flow3 PLUS the interprocedural backward value slice (accuracy-lever
    rung 2): '<rep>_perrep_flow3slice'.

    Same 5-tool flow3 surface/skill on the PLAIN (non-strict) base, plus a
    6th tool flow_slice (REPR_FLOW_SLICE=1): where does the value at the
    fault site COME FROM, across callers' argument expressions and callees'
    returns (callgraph.db, fast resolver). dfg/cfgdfg only -- the slice is
    a def-use construct; the planned src text-dump control gets its own
    arm when built."""
    return (condition.endswith("_perrep_flow3slice")
            and condition_rep(condition) in ("dfg", "cfgdfg"))


def condition_flow_bundle(condition: str) -> bool:
    """flow3 OPTIMIZED variant (B/C/D analyses 2026-08-25):
    '<rep>_perrep_flow3b'.

    PLAIN flow3 base with two token optimizations: (1) BUNDLED VIEW
    (REPR_FLOW_BUNDLE=1) -- flow_of returns the graph AND the function's
    numbered source in one response, displacing the follow-up Read
    round-trips (edit phase = 75% of spend, 87% of reads are re-reads);
    (2) a hardened STOP RULE in the skill (87% of cells kept re-checking
    after a passing flow_verify, mean 4.7 tool calls, never converting).
    Every flow3 view rep except src/loc (their view already is the
    source)."""
    return (condition.endswith("_perrep_flow3b")
            and condition_rep(condition) in ("cfg", "dfg", "cfgdfg",
                                             "ast", "calls"))


def condition_flow_lean(condition: str) -> bool:
    """flow3 LEAN variant (2026-08-25 waste audit): '<rep>_perrep_flow3L'.

    PLAIN flow3 base plus REPR_FLOW_LEAN=1 server responses: (1) a hardened
    STOP in the flow_verify PASS note (post-verify tails: SWE 446K/cell in
    88% of verify-reaching cells, 94% never touching the patch; SWT 145K;
    Sec 47K) with an escape hatch for the measured 1-6% of tails that
    really edit, and (2) batch/one-plan nudges in flow_plan/flow_check
    next-fields (66% of SWE flow_checks follow zero edits ~70K/cell; Sec
    2.07 plans/cell ~34K/cell; inert on SWT, 1.07 checks/cell). Response
    text ONLY -- no skill addendum, and NO bundled view (REPR_FLOW_BUNDLE
    stays off), so the stop rule is measured alone for the first time
    (every prior stop datapoint was joint bundle+stop). Same rep set as
    flow3b. SWT/SecBench harness wiring deferred -- the server-side gate
    is shared; those harnesses gain their env additions later."""
    return (condition.endswith("_perrep_flow3L")
            and condition_rep(condition) in ("cfg", "dfg", "cfgdfg",
                                             "ast", "calls"))


def condition_flow_relay(condition: str) -> bool:
    """flow3 PLAN-COMMIT RELAY variant (2026-08-26): '<rep>_perrep_flow3r'.

    PLAIN flow3 protocol split into TWO sequential CLI sessions sharing one
    worktree + RUN_DIR (the tool server keeps flow_plan.json on disk, so a
    fresh session sees the committed plan): stage 1 = locate + plan only
    (read-only toolset, no Edit/Write); stage 2 = a FRESH session seeded
    with the committed plan + planner's brief + pinned numbered source,
    doing edit -> flow_check -> flow_verify. Motivation (measured on 259
    canonical flow cells): every post-plan turn re-pays the ~23-27K of
    pre-plan exploration context; discarding it at the plan commit bounds
    at 0.72-0.76x of flow3 uniformly across benchmarks. No server-side
    behavior change (no new env flag); the split is pure harness
    orchestration, byte-identical for every other arm."""
    return (condition.endswith("_perrep_flow3r")
            and condition_rep(condition) in ("cfg", "dfg", "cfgdfg",
                                             "ast", "calls"))


def condition_flow_ripple(condition: str) -> bool:
    """flow3 RIPPLE/co-change arm (user-ordered 2026-08-26, built despite
    the probe ladder's 33% agent-keyed recall): '<rep>_perrep_flow3rip'.

    PLAIN flow3 base + REPR_FLOW_RIPPLE=1: flow_check attaches, to every
    VERIFIED real change, the same-file functions referencing the names
    the edit touched (ranked, capped 6) -- as DATA with soft framing,
    never an instruction pack (flow3c regressed with imperative rules).
    cfg/dfg/cfgdfg."""
    return (condition.endswith("_perrep_flow3rip")
            and condition_rep(condition) in ("cfg", "dfg", "cfgdfg"))


def condition_flow_comply(condition: str) -> bool:
    """flow3b PLUS the compliance pack (E-analysis 2026-08-25):
    '<rep>_perrep_flow3c'.

    Builds ON flow3b (bundled view + stop rule -- best arm, 6/10 @1.46M)
    and adds the four compliance rules the middle-band failure reading
    derived: (1) type-change consumer trace -- server-side, flow_check
    appends same-file callers whenever a function's flow verifiably
    changed (REPR_FLOW_COMPLY=1); (2) minimal-diff string guard, (3)
    spec-sequence check, (4) interface-replacement rule -- skill text.
    Same rep set as flow3b."""
    return (condition.endswith("_perrep_flow3c")
            and condition_rep(condition) in ("cfg", "dfg", "cfgdfg",
                                             "ast", "calls"))


def condition_flow_dyn3(condition: str) -> bool:
    """flow3 PLUS the failing-test execution overlay (accuracy-lever rung
    3): '<rep>_perrep_flow3dyn'.

    PLAIN flow3 base (no strict, no slice) with the failing test's executed
    lines WOVEN INTO the ritual (REPR_FLOW_DYN=1): flow_of's cfg/dfg views
    carry [RAN] markers, and flow_locate says whether the declared fault
    site is ON the failing path. No new optional tools -- rungs 1-2 showed
    those get skipped. Traces come from trace.collect_trace (cached per
    instance); a cell without a usable trace runs as plain flow3 with an
    honest overlay-absent note. cfg/dfg/cfgdfg only."""
    return (condition.endswith("_perrep_flow3dyn")
            and condition_rep(condition) in ("cfg", "dfg", "cfgdfg"))


def condition_flow_strict(condition: str) -> bool:
    """flow3 PLUS strict server semantics: '<rep>_perrep_flow3s'.

    REPR_FLOW_STRICT=1: flow_plan REJECTS deltas lacking a valid fault_site
    (graph-grounded planning enforced) and a confirmed dfg no_flow_change
    verdict carries a tripwire note. cfg/dfg/cfgdfg only -- the strict arm
    exists to test whether flow-specific instruments buy accuracy, so the
    placebo views (src/loc) and the non-flow views stay out of scope."""
    return (condition.endswith("_perrep_flow3s")
            and condition_rep(condition) in ("cfg", "dfg", "cfgdfg"))


def condition_graphfirst(condition: str) -> bool:
    """flow3 PLUS the graph-first read gate: '<rep>_perrep_graphfirst'.

    Same 5-tool v3 surface/skill as flow3, but a PreToolUse hook DENIES
    Read/Grep/Glob until flow_plan has committed a plan -- the agent must
    diagnose and plan from the graph ALONE, then read only to transcribe
    the edit (the enforceable form of 'everything happens at the graph
    level before the final edit'). Bash is never gated (cat/sed block
    backfired; escapes are measured, not blocked). Extended 2026-08-24 to
    every flow3 view (the which-view-supports-blind-planning experiment);
    loc is deliberately excluded -- a 3-line location view has nothing to
    plan from, degenerate by construction."""
    return (condition.endswith("_perrep_graphfirst")
            and condition_rep(condition) in ("cfg", "dfg", "cfgdfg", "ast",
                                             "calls", "src"))


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
            and condition_rep(condition) in ("cfg", "dfg", "src"))


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
    # calls/src/loc parse Python source on the fly too; symbols only.
    "calls": ("symbols",),
    "src": ("symbols",),
    "loc": ("symbols",),
}


def ensure_repr_index(inst: dict, worktree: Path, rep: str | None = None,
                      extra_backends: tuple = ()) -> None:
    """Make worktree/.repr_index present with the backends ``rep`` needs, building
    each ONCE per base_commit and caching it. When ``rep`` is None, build all
    three (back-compat). For ast/deps this builds symbols ONLY -- no jedi callgraph
    pass -> no jedi deadlock (the cause of the batch hang).

    ``extra_backends`` adds backends beyond the rep's defaults (the flow3slice
    arm needs callgraph.db for caller lookup). A callgraph build requested
    this way ALWAYS uses the fast name+proximity resolver (REPR_CALLGRAPH_FAST)
    -- the jedi path deadlocks/takes ~11 min per large repo."""
    needed = tuple(_REP_BACKENDS.get(rep, ("symbols", "callgraph", "docs")))
    force_fast = "callgraph" in extra_backends and "callgraph" not in needed
    for b in extra_backends:
        if b not in needed:
            needed = needed + (b,)
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
                prev_fast = os.environ.get("REPR_CALLGRAPH_FAST")
                if force_fast:
                    os.environ["REPR_CALLGRAPH_FAST"] = "1"
                try:
                    for b in order:
                        get_backend(b).build(worktree, tmp_idx)
                finally:
                    if force_fast:
                        if prev_fast is None:
                            os.environ.pop("REPR_CALLGRAPH_FAST", None)
                        else:
                            os.environ["REPR_CALLGRAPH_FAST"] = prev_fast
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
                     flow_v3: bool = False,
                     flow_strict: bool = False,
                     flow_slice: bool = False,
                     flow_dyn: bool = False,
                     flow_bundle: bool = False,
                     flow_lean: bool = False,
                     flow_comply: bool = False,
                     flow_ripple: bool = False,
                     flow_testrec: bool = False,
                     flow_pathcond: bool = False,
                     flow_witness: bool = False,
                     run_dir_env: bool = False) -> Path:
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
                    "REPR_TRACE_FILE": (str(run_dir / "trace.json")
                                        if (dyn or flow_dyn) else ""),
                    # flow3dyn (rung 3): failing-test overlay WOVEN into the
                    # flow3 views/locate. Off -> flow3 byte-identical.
                    "REPR_FLOW_DYN": "1" if flow_dyn else "",
                    # flow3b: bundled view (graph + numbered source in one
                    # flow_of response). Off -> flow3 byte-identical.
                    "REPR_FLOW_BUNDLE": "1" if flow_bundle else "",
                    # flow3L: lean response levers (hardened stop on a
                    # verified PASS + batch/one-plan nudges). Off -> flow3
                    # byte-identical.
                    "REPR_FLOW_LEAN": "1" if flow_lean else "",
                    # flow3c: compliance pack (consumer-trace note on every
                    # verified flow change). Off -> byte-identical.
                    "REPR_FLOW_COMPLY": "1" if flow_comply else "",
                    # flow3rip: co-change candidates on verified changes.
                    "REPR_FLOW_RIPPLE": "1" if flow_ripple else "",
                    # flow3t (SWT): per-delta plan-echo test recipes +
                    # test-artifact lints. Off -> testgen byte-identical.
                    "REPR_FLOW_TESTREC": "1" if flow_testrec else "",
                    # flow3p (SWT, cfg-only): CFG reachability certificate
                    # on each recipe. Off -> flow3t byte-identical.
                    "REPR_FLOW_PATHCOND": "1" if flow_pathcond else "",
                    # flow3w: shadow-fix witness (buggy-vs-shadow runs,
                    # DFG sink diff on graph arms, raw dump on placebos).
                    "REPR_FLOW_WITNESS": "1" if flow_witness else "",
                    # in-flight-run pin: a run started before the
                    # 2026-08-28 file-scope change finishes under the
                    # semantics it started with (set in the launcher's
                    # environment; unset = the current file scope)
                    "REPR_WITNESS_ID_SCOPE": os.environ.get(
                        "REPR_WITNESS_ID_SCOPE", ""),
                    # flow3wc: the witness observes branch outcomes
                    # (CFG-selected decisions) instead of values
                    "REPR_WITNESS_CFG": os.environ.get(
                        "REPR_WITNESS_CFG", ""),
                    # the witness runs pytest with the CELL's interpreter
                    # (testenv/testshim under run_dir)
                    "REPR_RUN_DIR": str(run_dir) if (flow_witness
                                                     or run_dir_env)
                    else "",
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
                    # strict (flow3s) arm: fault_site hard gate on flow_plan
                    # + dfg no-change tripwire. Off -> flow3 byte-identical.
                    "REPR_FLOW_STRICT": "1" if flow_strict else "",
                    # slice (flow3slice) arm: 6th tool flow_slice -- the
                    # interprocedural backward value slice (rung 2). Off ->
                    # the exact 5-tool flow3 surface.
                    "REPR_FLOW_SLICE": "1" if flow_slice else "",
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


_RELAY_STAGE_FILES = ("result.json", "transcript.jsonl", "prompt.md",
                      "meta.json", "agent_summary.json", "stderr.log")


def _relay_stash_stage1(run_dir: Path) -> dict:
    """Rename stage-1 artifacts to *_stage1.* so the stage-2 run_agent call
    cannot overwrite them (agent.py writes fixed names). Returns paths."""
    out = {}
    for name in _RELAY_STAGE_FILES:
        src = run_dir / name
        if not src.is_file():
            continue
        stem, dot, ext = name.partition(".")
        dst = run_dir / ("%s_stage1%s%s" % (stem, dot, ext))
        try:
            dst.unlink()
        except OSError:
            pass
        src.rename(dst)
        out[name] = dst
    return out


def _relay_seed(worktree: Path, run_dir: Path, rep: str) -> dict | None:
    """Render the stage-2 seed (plan + pinned source) via flow_seed_cli in
    the same env contract flowverify uses. None on any failure -- the
    caller then falls back to a plain flow3 stage 2."""
    env = dict(os.environ)
    env.update({"REPO_ROOT": str(worktree),
                "REPO_INDEX_DIR": str(Path(worktree) / ".repr_index"),
                "RUN_DIR": str(run_dir),
                "REPR_PERREP_REP": rep,
                "PYTHONPATH": str(STUDY_ROOT),
                "PYTHONHASHSEED": "0",
                "REPR_FLOW_V3": "1"})
    try:
        proc = subprocess.run(
            [str(VENV_PYTHON), "-m", "servers.common.flow_seed_cli"],
            env=env, capture_output=True, text=True, timeout=120)
        seed = json.loads(proc.stdout.strip() or "{}")
    except (subprocess.SubprocessError, OSError, ValueError):
        return None
    try:
        (run_dir / "flow_seed.json").write_text(
            json.dumps(seed, indent=1), "utf-8")
    except OSError:
        pass
    return seed if seed.get("ok") else None


def _relay_stage1_brief(result_stage1: Path | None, cap: int = 2000) -> str:
    """The planner session's final text ('result' in the CLI JSON) -- a
    distilled implementation brief carried into stage 2."""
    if not result_stage1 or not Path(result_stage1).is_file():
        return ""
    try:
        obj = json.loads(Path(result_stage1).read_text("utf-8"))
    except (OSError, ValueError):
        return ""
    brief = obj.get("result") if isinstance(obj, dict) else None
    return (brief or "").strip()[:cap]


def _relay_merge_results(run_dir: Path, fallback: bool) -> None:
    """Write a MERGED result.json (stage1 + stage2 usage/cost/turns summed)
    so every existing report reads the cell's TRUE total. Per-stage
    originals stay in result_stage1.json / the stage-2 fields."""
    def _load(p: Path) -> dict:
        try:
            obj = json.loads(p.read_text("utf-8"))
            return obj if isinstance(obj, dict) else {}
        except (OSError, ValueError):
            return {}
    s1 = _load(run_dir / "result_stage1.json")
    s2 = _load(run_dir / "result.json")
    u1, u2 = (s1.get("usage") or {}), (s2.get("usage") or {})
    merged = dict(s2) if s2 else {}
    keys = ("input_tokens", "output_tokens", "cache_read_input_tokens",
            "cache_creation_input_tokens")
    merged["usage"] = {k: (u1.get(k, 0) or 0) + (u2.get(k, 0) or 0)
                       for k in keys}
    merged["total_cost_usd"] = ((s1.get("total_cost_usd") or 0.0)
                                + (s2.get("total_cost_usd") or 0.0))
    if "num_turns" in s1 or "num_turns" in s2:
        merged["num_turns"] = ((s1.get("num_turns") or 0)
                               + (s2.get("num_turns") or 0))
    merged["flow3r"] = {"fallback": fallback,
                        "stage1_usage": u1, "stage2_usage": u2,
                        "stage1_turns": s1.get("num_turns"),
                        "stage2_turns": s2.get("num_turns"),
                        "stage1_cost": s1.get("total_cost_usd"),
                        "stage2_cost": s2.get("total_cost_usd"),
                        "stage2_parseable": bool(s2)}
    (run_dir / "result.json").write_text(json.dumps(merged, indent=2),
                                         "utf-8")


def _flow3r_two_stage(inst: dict, run_dir: Path, wt: Path, args, *,
                      rep: str, mcp_config, sys_prompt_file, restrict: bool,
                      flow3_prompt: str, flow3_skill_md, flow3_sentinel,
                      body: str, tools3) -> None:
    """The flow3r plan-commit relay: two claude sessions, one worktree.

    Stage 1 (read-only toolset, no Edit/Write) localizes and commits the
    plan; stage 2 starts FRESH, seeded with the committed plan + the
    planner's brief + pinned numbered source, and implements it. The tool
    server needs no changes: flow_plan.json / fingerprints live in RUN_DIR
    on disk and flow_check/flow_verify reload them on every call. Turn and
    budget caps are a SPLIT of the flow3 cell's caps (never a raise). If
    stage 1 commits no plan (or the seed render fails), stage 2 runs the
    plain flow3 prompt/skill with the remaining caps -- recorded as
    fallback in result.json's flow3r block."""
    from experiments.common import flow_protocol
    run_dir = Path(run_dir).resolve()
    if not flow_protocol.skill_mode():
        raise RuntimeError(
            "flow3r requires skill mode (REPR_FLOW_SKILL=0 unsupported: "
            "the per-stage skills ARE the protocol delivery)")
    # stale relay state from an interrupted earlier attempt would poison
    # stage 2 (the worktree was rebuilt fresh): clear before stage 1.
    for stale in ("flow_plan.json", "flow_locate.json", "flow_seed.json"):
        try:
            (run_dir / stale).unlink()
        except OSError:
            pass
    s1_turns = max(12, args.max_turns * 3 // 8)
    s2_turns = max(20, args.max_turns - s1_turns)
    s1_budget = round(args.max_budget_usd * 0.35, 2)
    s2_budget = round(args.max_budget_usd - s1_budget, 2)

    stage1_skill = flow_protocol.build_relay_skill_md(
        "repair", rep, tools3, stage=1)
    agent.run_agent(
        inst, run_dir, claude_bin=args.claude_bin, worktree=wt,
        model=args.model, effort=args.effort_flag,
        max_turns=s1_turns, max_budget_usd=s1_budget,
        timeout_sec=args.timeout_sec, mcp_config=mcp_config,
        system_prompt_file=sys_prompt_file, restrict_tools=restrict,
        prompt_override=flow3_prompt, skill_md=stage1_skill,
        sentinel=flow3_sentinel,
        # plan-only stage: no Edit/Write -- the session cannot fix anything,
        # it can only look and commit the plan.
        tools_override="Read,Bash,Grep,Glob")
    stashed = _relay_stash_stage1(run_dir)

    seed = None
    if (run_dir / "flow_plan.json").is_file():
        seed = _relay_seed(wt, run_dir, rep)
    fallback = seed is None
    if fallback:
        stage2_prompt, stage2_skill = flow3_prompt, flow3_skill_md
    else:
        brief = _relay_stage1_brief(stashed.get("result.json"))
        sources = "\n\n".join((seed.get("sources") or {}).values())
        body2 = flow_protocol.build_relay_stage2_body(
            body, seed.get("plan") or "", brief, sources)
        stage2_prompt = flow_protocol.build_user_prompt(
            "repair", rep, tools3, body2, agent=args.agent)
        stage2_skill = flow_protocol.build_relay_skill_md(
            "repair", rep, tools3, stage=2)
    agent.run_agent(
        inst, run_dir, claude_bin=args.claude_bin, worktree=wt,
        model=args.model, effort=args.effort_flag,
        max_turns=s2_turns, max_budget_usd=s2_budget,
        timeout_sec=args.timeout_sec, mcp_config=mcp_config,
        system_prompt_file=sys_prompt_file, restrict_tools=restrict,
        prompt_override=stage2_prompt, skill_md=stage2_skill,
        sentinel=flow3_sentinel)
    _relay_merge_results(run_dir, fallback=fallback)


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
            graphfirst = condition_graphfirst(condition)
            # RQ2 ablation arms (_nlplan, _flowonly) share the flow-first
            # MAP-FREE base but expose ONLY flow_of. _nlplan forces an NL plan;
            # _flowonly forces nothing. The CHECKED graph-plan arm is _flow.
            # flow3 shares the flow-first map-free base (symbols index only);
            # graphfirst is flow3 + the read gate.
            map_free = flow_first or nlplan or flowonly or flow_v3 or graphfirst
            if map_free:
                # NO rep map (the flow is the surface, not a code map). Only the
                # symbol index is needed so flow_of can resolve the report's
                # named functions to their CFGs. The slice arm additionally
                # needs callgraph.db (caller lookup; fast resolver forced).
                ensure_repr_index(
                    inst, wt, rep=rep,
                    extra_backends=(("callgraph",)
                                    if condition_flow_slice(condition)
                                    else ()))
                map_info = {"rep": rep, "flow_first": True, "no_map": True}
            else:
                map_info = build_rep_map(rep, inst, wt)
            if dyn or condition_flow_dyn3(condition):
                from experiments.swebench_pro import trace as trace_mod
                trace = trace_mod.collect_trace(inst, run_dir / "trace.json")
                (run_dir / "trace_status.json").write_text(
                    json.dumps({k: trace.get(k) for k in
                                ("ok", "reason", "test_id", "n_files")}),
                    "utf-8")
            mcp_config = write_mcp_config(rep, wt, run_dir, dyn=dyn,
                                          flowplan=flowplan,
                                          cfg_blocks=cfg_blocks,
                                          # flow3/graphfirst reuse the
                                          # flow-first registration
                                          flow_first=(flow_first or flow_v3
                                                      or graphfirst),
                                          flow_nofix=flow_nofix,
                                          flow_of_only=nlplan or flowonly,
                                          flow_v3=flow_v3 or graphfirst,
                                          flow_strict=condition_flow_strict(
                                              condition),
                                          flow_slice=condition_flow_slice(
                                              condition),
                                          flow_dyn=condition_flow_dyn3(
                                              condition),
                                          # flow3c = flow3b + pack: the
                                          # bundle rides along
                                          flow_bundle=(
                                              condition_flow_bundle(condition)
                                              or condition_flow_comply(
                                                  condition)),
                                          # flow3L: response levers only;
                                          # the bundle deliberately stays
                                          # OFF (stop measured alone)
                                          flow_lean=condition_flow_lean(
                                              condition),
                                          flow_comply=condition_flow_comply(
                                              condition),
                                          flow_ripple=condition_flow_ripple(
                                              condition))
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
    # baseline_lean (2026-08-21 forensics): the tool-surface CONTROL -- plain
    # baseline (no MCP, no skill, same prompt) but with the SAME --tools
    # restriction the MCP arms get. The unrestricted baseline carries ~20K
    # more prefix tokens per API call (26 built-ins + skill/agent listings,
    # and no Grep/Glob at all), which alone accounts for ~40% of the
    # flow-arm token gap; this arm isolates the process effect from it.
    restrict = ((is_tool_cell and not args.no_restrict_tools)
                or condition == "baseline_lean")

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

    # -- published prompt-lever comparator arms (efficiency-baselines plan) --
    # Both papers append their clause AFTER the task block in the USER prompt,
    # so these arms use a prompt override, not the system prompt. One thing
    # changed vs baseline: the appended clause (plus, for TALE, the billed
    # single-turn budget-estimation call the method itself prescribes).
    comparator_prompt_override = None
    seerepo_mcp = None
    codermind_mcp, codermind_env = None, None
    eet_fired = False
    if condition == "baseline_beclause":
        # "Prompt-Induced Waste in Coding Agents" (arXiv 2608.01347) App. C
        # frozen bounded_efficiency clause, SHA-256 prefix 56954fa9ae17,
        # replayed verbatim.
        comparator_prompt_override = (
            agent.build_prompt(inst) + "\n\n" + _BOUNDED_EFFICIENCY_CLAUSE)
        (run_dir / "comparator_clause.txt").write_text(
            _BOUNDED_EFFICIENCY_CLAUSE, encoding="utf-8")
    elif condition == "baseline_noexec":
        # 2606.26978 prohibited-execution arm: baseline + their sentence
        # appended to the user prompt (prompt-level, their primary method).
        comparator_prompt_override = (
            agent.build_prompt(inst) + "\n\n" + _NOEXEC_CLAUSE)
        (run_dir / "comparator_clause.txt").write_text(
            _NOEXEC_CLAUSE, encoding="utf-8")
    elif condition == "baseline_tale":
        # TALE-EP (arXiv 2412.18547, ACL'25 Findings): Figure-5 zero-shot
        # budget estimation, then the Table-1/Figure-7c budget clause.
        budget = _tale_estimate_budget(inst, run_dir, args)
        comparator_prompt_override = (
            agent.build_prompt(inst)
            + "\n\nPlease complete the above task. Let's think step by step "
              "and use less than %d tokens:" % budget)
    elif condition in ("baseline_cbmem", "baseline_cbmem2"):
        # Codebase-Memory MCP (arXiv 2603.27277): pre-index the worktree
        # with the tool's own CLI, then one adoption steer sentence (our
        # known optional-tools-get-skipped failure mode; disclosed).
        # baseline_cbmem2 (2026-09-03 fidelity rerun, pinned CLI 2.1.220):
        # the tool AS SHIPPED for Claude Code -- server name, skill, agents
        # and hooks from its own installer (agent.py _lay_cbmem_payload), NO
        # extra steer sentence (the shipped SessionStart reminder + skill are
        # the tool's own steer), and a SHORT project name (--name <repo>):
        # the first arm derived the name from the 150-char worktree path,
        # which was echoed as a prefix on every qualified name and made up
        # 29% of all graph-result characters.
        import subprocess as _sp
        # the binary does NOT create these dirs itself, and the runtime dir
        # holds a UNIX SOCKET ("secure CLI coordination ... (endpoint)"
        # failure) -- sun_path caps at ~108 chars, so the runtime dir MUST
        # be a short path; keyed by a run_dir hash for per-cell uniqueness.
        import hashlib as _hl
        cbm_rt = Path("/tmp/cbmrt_%s" % _hl.md5(
            str(run_dir).encode()).hexdigest()[:10])
        (run_dir / "cbm_cache").mkdir(parents=True, exist_ok=True)
        cbm_rt.mkdir(parents=True, exist_ok=True)
        idx_env = dict(os.environ)
        idx_env["CBM_CACHE_DIR"] = str(run_dir / "cbm_cache")
        idx_env["CBM_RUNTIME_DIR"] = str(cbm_rt)
        idx_env["CBM_ALLOWED_ROOT"] = str(wt)
        idx_cmd = [str(agent.CBMEM_BIN), "cli", "index_repository",
                   "--repo-path", str(wt)]
        if condition == "baseline_cbmem2":
            idx_cmd += ["--name", (inst.get("repo") or "repo").split("/")[-1]]
        r = _sp.run(idx_cmd, env=idx_env, capture_output=True, text=True,
                    timeout=900)
        (run_dir / "cbmem_index.log").write_text(
            (r.stdout or "") + "\n---\n" + (r.stderr or ""), encoding="utf-8")
        if condition == "baseline_cbmem":
            comparator_prompt_override = (
                agent.build_prompt(inst)
                + "\n\nThis repo is already indexed in the cbmem graph: use "
                  "mcp__cbmem__search_graph, trace_path, get_code_snippet, "
                  "and query_graph to locate and understand code instead of "
                  "Read/Grep, falling back to files only where "
                  "mcp__cbmem__check_index_coverage reports a gap.")
        else:
            (run_dir / "cbmem2_arm.txt").write_text(
                "baseline_cbmem2: as-shipped Codebase-Memory install payload "
                "(skill + 3 agents + SessionStart/SubagentStart/PreToolUse "
                "Grep|Glob/PostToolUse Read hooks), server name "
                "codebase-memory-mcp, project name %s, baseline prompt with "
                "NO extra steer sentence.\nindex cmd: %s\n"
                % ((inst.get("repo") or "repo").split("/")[-1],
                   " ".join(idx_cmd)), encoding="utf-8")
    elif condition == "baseline_csniper":
        # ContextSniper FILTER condition (arXiv 2607.01916): their released
        # Claude runner APPENDS the rendered code policy to the task prompt
        # (CONTEXTSNIPER_APPEND_CODE_POLICY_TO_TASK=1 path); we do the same.
        policy = _csniper_policy()
        comparator_prompt_override = (
            agent.build_prompt(inst) + "\n\n" + policy)
        (run_dir / "csniper_policy.txt").write_text(policy, encoding="utf-8")
    elif condition == "baseline_sherloc":
        # SHERLOC (arXiv 2606.24820, EMNLP'26 author-reported): their
        # localization pass runs FIRST as its own headless Claude Code
        # session (their four tools over MCP, their prompt, their
        # loop-detection / final-turn interventions via hook), and its
        # <findings> + <locations> are appended to the baseline task prompt
        # of the repair session. Its usage is merged into result.json by
        # run_agent(pre_stage_dir=...). See sherloc_stage.py for the
        # disclosed deviations.
        from experiments.swebench_pro import sherloc_stage
        block, _stage = sherloc_stage.run_stage(
            inst, run_dir, wt, claude_bin=args.claude_bin, model=args.model,
            effort=args.effort_flag, max_budget_usd=args.max_budget_usd,
            timeout_sec=args.timeout_sec)
        comparator_prompt_override = (
            agent.build_prompt(inst) + (("\n\n" + block) if block else ""))
    elif condition == "baseline_seerepo":
        # SeeRepo (arXiv 2606.14061, ASE'26): their AST repository graph built
        # per cell with their build_graph(), their graph tool served over MCP
        # returning their Graphviz PNG as an IMAGE, and their SeeRepo.yaml
        # instructions appended to the baseline prompt. See seerepo_arm.py.
        from experiments.swebench_pro import seerepo_arm
        seerepo_mcp, steer = seerepo_arm.prepare(inst, run_dir, wt)
        comparator_prompt_override = agent.build_prompt(inst) + "\n\n" + steer
        (run_dir / "seerepo_steer.txt").write_text(steer, encoding="utf-8")
    elif condition == "baseline_codermind":
        # RPG-Encoder / CoderMind (arXiv 2602.02084, ICML'26): the tool AS
        # SHIPPED for Claude Code -- `cmind init --ai claude` in the worktree
        # (their .mcp.json / .claude settings+hook / commands), the RPG
        # encoded once per repository with their encoder (headless claude)
        # and moved to each instance commit with their incremental update.
        # Baseline prompt, no steer sentence of ours. See codermind_arm.py.
        from experiments.swebench_pro import codermind_arm
        codermind_mcp, codermind_env = codermind_arm.prepare(
            inst, run_dir, wt, claude_bin=args.claude_bin, model=args.model,
            effort=args.effort_flag)
    elif condition == "baseline_eet":
        # EET (arXiv 2601.05777, ACL'26 Findings): lever A -- their TF-IDF
        # store (imported unmodified) retrieves the top-1 experience object
        # from THEIR shipped 205-record base and renders THEIR
        # <retrieved_experiences> template into the task prompt. Lever B (the
        # confidence hook) is wired below, only when this fired. See eet_arm.py.
        from experiments.swebench_pro import eet_arm
        block, eet_info = eet_arm.prepare(inst, run_dir, store="theirs")
        eet_fired = bool(block)
        if block:
            comparator_prompt_override = agent.build_prompt(inst) + "\n\n" + block
    elif condition == "baseline_eetB":
        # EET arm B: their method UNCHANGED, but with an experience base built
        # from OUR held-out swe183 baseline trajectories using THEIR extraction +
        # summarization pipeline (_eet_build_base.py). Their intended setting --
        # a base that covers the target repositories -- versus arm A's shipped
        # base, which contains none of our three repos. Only the base differs.
        from experiments.swebench_pro import eet_arm
        block, eet_info = eet_arm.prepare(inst, run_dir, store="ours")
        eet_fired = bool(block)
        if block:
            comparator_prompt_override = agent.build_prompt(inst) + "\n\n" + block

    is_flow_first = condition_flow_first(condition)
    is_nlplan = condition_nlplan(condition)
    is_flowonly = condition_flowonly(condition)
    is_flow_v3 = condition_flow_v3(condition)
    is_graphfirst = condition_graphfirst(condition)
    # Map-free arms (flow-first + flow3 + graphfirst + the RQ2 nlplan/
    # flowonly ablations) have NO explore/map tool -- the flow IS the
    # surface -- so they must NOT get the explore nudge or the
    # Grep-blocking explore-gate (that would tell the agent to call a
    # nonexistent cfg_explore and block its Grep). graphfirst gets its OWN
    # gate (deny reads until flow_plan commits) below.
    is_map_free = (is_flow_first or is_nlplan or is_flowonly or is_flow_v3
                   or is_graphfirst)

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
    if (is_flow_v3 or is_graphfirst) and rep is not None:
        from experiments.common import flow_protocol
        tools3 = flow_protocol.tool_names(rep)
        body = agent.build_prompt(inst, map_free_rules=True)
        flow3_prompt = flow_protocol.build_user_prompt(
            "repair", rep, tools3, body, agent=args.agent)
        if flow_protocol.skill_mode():
            flow3_skill_md = flow_protocol.build_skill_md("repair", rep,
                                                          tools3)
            if is_graphfirst and flow3_skill_md:
                # the gate rule, mirrored in the hook's deny reason
                flow3_skill_md += (
                    "\nGRAPH-FIRST RULE (enforced): Read/Grep/Glob are "
                    "BLOCKED until the graph work is done -- ALL of: "
                    "(1) flow_of on the report's function names, (2) "
                    "flow_locate on the faulty spot the graph shows, (3) "
                    "flow_plan committing your deltas. Only then does "
                    "reading unlock, and only to make the edit. If the "
                    "graph truly cannot show the bug, still do the three "
                    "steps with your best hypothesis (a no_flow_change "
                    "delta with a one-line rationale is acceptable) -- "
                    "then read and re-plan (flow_plan merges "
                    "revisions).\n")
            if (condition_flow_bundle(condition)
                    or condition_flow_comply(condition)) and flow3_skill_md:
                # flow3b: explain the bundled view + the hardened stop
                # (the two measured optimizations)
                flow3_skill_md += (
                    "\nBUNDLED VIEW (this arm): flow_of returns the graph "
                    "AND the function's numbered source together -- do NOT "
                    "Read a function you already got from flow_of; work "
                    "from the bundled source.\n"
                    "STOP RULE (this arm): when flow_verify reports every "
                    "delta exact or same_family, the fix is VERIFIED -- "
                    "STOP IMMEDIATELY. Do not re-read files, re-run "
                    "searches, or re-check anything after a passing "
                    "flow_verify; extra checking after verification has "
                    "measurably never changed an outcome.\n")
            if condition_flow_ripple(condition) and flow3_skill_md:
                # flow3rip: explain the ripple field (data framing, no
                # imperatives -- the flow3c lesson)
                flow3_skill_md += (
                    "\nRIPPLE FIELD (this arm): when flow_check verifies "
                    "a real flow change, its result includes a 'ripple' "
                    "list -- same-file functions that reference the same "
                    "names your edit touched, with line numbers. This is "
                    "DATA, not a to-do list: most listed functions need "
                    "no change. Scan it once; only if your fix changed a "
                    "contract (signature, return shape, behavior) that a "
                    "listed function relies on, update that function "
                    "too. Do not re-read or edit unaffected ones.\n")
            if condition_flow_comply(condition) and flow3_skill_md:
                # the compliance pack: the four rules the middle-band
                # failure audit derived (each killed real cells)
                flow3_skill_md += (
                    "\nCOMPLIANCE RULES (this arm; each rule exists "
                    "because violating it failed real hidden tests):\n"
                    "1. TYPE-CHANGE CONSUMERS: if your edit changes the "
                    "TYPE or SHAPE of a value a function returns or "
                    "produces, follow EVERY consumer of that value and "
                    "convert them too (flow_check lists same-file callers "
                    "when your edit changes flow -- check each one).\n"
                    "2. MINIMAL STRING DIFFS: NEVER rewrite existing "
                    "string literals (messages, return strings) the task "
                    "does not explicitly name as wrong -- hidden tests "
                    "assert their exact wording. When ADDING a message, "
                    "copy the exact pattern and suffix used by sibling "
                    "branches of the same function.\n"
                    "3. SPEC SEQUENCES: when the task states an order or "
                    "an ONLY-list ('first X then Y', 'use A and B only'), "
                    "re-view the edited function with flow_of afterwards "
                    "and confirm its call sequence matches EXACTLY -- "
                    "remove calls the spec excludes, even if they seem "
                    "harmless.\n"
                    "4. REPLACEMENT MEANS REMOVAL: 'X instead of Y' means "
                    "Y goes away -- remove or make optional the old "
                    "parameters/calls and update every caller; never keep "
                    "them 'for backwards compatibility'.\n")
            if condition_flow_dyn3(condition) and flow3_skill_md:
                # rung-3 overlay arm: explain the markers the views already
                # carry (nothing optional to fetch).
                flow3_skill_md += (
                    "\nFAILING-TEST OVERLAY (this arm): the graphs "
                    "flow_of shows are annotated with [RAN] markers -- "
                    "the lines the bug's FAILING test actually executed "
                    "-- and flow_locate tells you whether your declared "
                    "fault site is ON that failing path. Use this: the "
                    "fix almost always lands on the failing path, so if "
                    "the spot you blame carries no [RAN] (or flow_locate "
                    "warns the line is never executed), re-diagnose "
                    "BEFORE planning. Plan deltas on [RAN] blocks/pairs "
                    "unless you can say why the fix belongs off-path "
                    "(e.g. code the fix must ADD).\n")
            if condition_flow_slice(condition) and flow3_skill_md:
                # rung-2 slice arm. v2 (2026-08-25): the slice is AUTO-
                # ATTACHED to flow_locate's result -- the first pilot showed
                # the standalone tool gets skipped (2/10 cells) -- so the
                # skill explains how to USE it, not how to fetch it.
                flow3_skill_md += (
                    "\nSLICE (this arm): flow_locate's result includes a "
                    "'slice' field -- the backward value slice of the "
                    "located spot: where the value COMES FROM, through "
                    "def-use flow AND across function boundaries (callers' "
                    "arguments, callees' returns). READ IT before "
                    "planning: if it shows the wrong value is PRODUCED in "
                    "another function, plan your delta at the PRODUCING "
                    "function, not at the symptom line. To trace a "
                    "different variable or spot, call the flow_slice tool "
                    "with 'file.py:LINE' and the variable name.\n")
        flow3_sentinel = flow_protocol.SENTINEL

    # graphfirst: the read gate -- PreToolUse denies Read/Grep/Glob until
    # RUN_DIR/flow_plan.json exists (written by the flow_plan tool itself,
    # so the gate cannot fail to open). Bash is deliberately NOT gated.
    if is_graphfirst:
        gate_settings = agent.write_graphfirst_gate_settings(run_dir)

    # RTK comparator arm (arXiv 2607.12161): the tool's own PreToolUse
    # Bash-rewrite hook, hook-only (no RTK.md prompt text), scoped per cell.
    if condition == "baseline_rtk":
        gate_settings = agent.write_rtk_settings(run_dir)

    # CMV comparator arm (arXiv 2602.22402): the tool's own PreCompact +
    # PostToolUse transcript-trim hooks, scoped per cell (absolute paths).
    if condition == "baseline_cmv":
        gate_settings = agent.write_cmv_settings(run_dir)

    # CMV faithful arm (baseline_cmv2, 2026-09-03): same tool hooks, but the
    # PostToolUse trim goes through the stop-and-resume relay so the trimmed
    # transcript reaches the model (swe30_cmv's in-process trims never did).
    if condition == "baseline_cmv2":
        gate_settings = agent.write_cmv2_settings(run_dir)

    # Squeez enforced arm (arXiv 2604.04979): PreToolUse Bash rewrite hook.
    if condition == "baseline_squeez":
        gate_settings = agent.write_squeez_settings(run_dir)

    # "More with Less" turn-control arms (arXiv 2510.16786): the paper's
    # per-tool-result reminder hook; cap = 75th percentile of the swe30
    # baseline's turn distribution (fixed arm) or its median with a
    # one-time buffer to the 75th (dynamic arm, their "50->75").
    turn_cap = None
    if condition in ("baseline_turn75", "baseline_turndyn"):
        turn_cap = _turn_cap_for(condition)
        gate_settings = agent.write_turn_reminder_settings(run_dir, turn_cap)

    # EET lever B: their confidence-check / submission-prompt hook, wired ONLY
    # when their retrieval fired for this instance (their own coupling --
    # experience_retrieval.py:531-535 falls back to plain DefaultAgent, with no
    # confidence checks, when nothing was retrieved).
    if condition in ("baseline_eet", "baseline_eetB") and eet_fired:
        gate_settings = agent.write_eet_settings(run_dir)

    if args.agent == "claude" and condition_flow_relay(condition):
        # flow3r: two-stage plan-commit relay (helper above). Uses the
        # already-built flow3 prompt/skill as its stage-1 base and its
        # no-plan fallback; body/tools3 come from the flow3 build block.
        _flow3r_two_stage(
            inst, run_dir, wt, args, rep=rep, mcp_config=mcp_config,
            sys_prompt_file=sys_prompt_file, restrict=restrict,
            flow3_prompt=flow3_prompt, flow3_skill_md=flow3_skill_md,
            flow3_sentinel=flow3_sentinel, body=body, tools3=tools3)
    elif args.agent == "claude":
        agent.run_agent(
            inst, run_dir, claude_bin=args.claude_bin, worktree=wt,
            model=args.model, effort=args.effort_flag,
            max_turns=(turn_cap if turn_cap else args.max_turns),
            max_budget_usd=args.max_budget_usd,
            timeout_sec=args.timeout_sec,
            # baseline_csniper: their MCP server via explicit config (the
            # --plugin-dir MCP path silently fails on CLI 2.1.236); passed
            # here (NOT via the shared mcp_config var) so is_tool_cell stays
            # False and no --tools restriction is added -- one-thing-changed
            # vs the unrestricted baseline.
            mcp_config=(agent.CSNIPER_MCP_CONFIG
                        if condition == "baseline_csniper"
                        else seerepo_mcp
                        if condition == "baseline_seerepo"
                        else codermind_mcp
                        if condition == "baseline_codermind"
                        else agent.CBMEM_MCP_CONFIG
                        if condition == "baseline_cbmem"
                        else agent.CBMEM2_MCP_CONFIG
                        if condition == "baseline_cbmem2"
                        else agent.PRUNER_MCP_CONFIG
                        if condition == "baseline_pruner"
                        else agent.PRUNERB_MCP_CONFIG
                        if condition == "baseline_prunerB" else mcp_config),
            system_prompt_file=(agent.PRUNERB_SYSTEM_PROMPT
                                if condition == "baseline_prunerB"
                                else sys_prompt_file),
            restrict_tools=restrict,
            explore_tool=user_explore_tool, dyn_tool=user_dyn_tool,
            flow_tools=user_flow_tools, flow_first_tools=user_flow_first,
            nl_plan_tool=user_nl_plan, flow_only_tool=user_flow_only,
            prompt_override=flow3_prompt or comparator_prompt_override,
            skill_md=flow3_skill_md,
            sentinel=flow3_sentinel, gate_settings_file=gate_settings,
            # baseline_pure: leave the CLI's stock deferred-tools/tool-search
            # behavior untouched (no ENABLE_TOOL_SEARCH=false, no --tools) --
            # "just plain Claude Code" apart from the web ban + budget caps.
            stock_tool_search=(condition == "baseline_pure"),
            rtk_path=(agent.RTK_BIN if condition == "baseline_rtk" else None),
            plugin_dir=(agent.CSNIPER_PLUGIN
                        if condition == "baseline_csniper" else None),
            lingua_proxy=(condition == "baseline_lingua"),
            selctx_proxy=(condition == "baseline_selctx"),
            mask_proxy=(condition == "baseline_mask"),
            cmv_relay=(condition == "baseline_cmv2"),
            turn_reminder_cap=turn_cap,
            turn_extension=(_turndyn_ext()
                            if condition == "baseline_turndyn" else None),
            cbmem_dirs=(condition in ("baseline_cbmem", "baseline_cbmem2")),
            cbmem_payload=(condition == "baseline_cbmem2"),
            claudemd_file=(_claudemd_for(iid)
                           if condition == "baseline_claudemd" else None),
            claudemd_root_file=(_claudemd_for(iid, "claudemd_full")
                                if condition == "baseline_agentsmd" else None),
            # baseline_pruner: the demo's only disallowed native tool is
            # Bash (Read/Grep/Glob stay open -- that bypass is part of the
            # original condition; do not "fix" it).
            extra_disallowed=("Bash" if condition == "baseline_pruner"
                              # prunerB: bash-only like the paper's scaffold --
                              # every read AND edit goes through the pruned
                              # wrappers (Edit needs a prior Read, which is
                              # disabled, so it is disabled too).
                              else "Bash,Read,Grep,Glob,Edit,Write,MultiEdit,NotebookEdit"
                              if condition == "baseline_prunerB" else None),
            squeez_task=((inst.get("problem_statement") or "")[:300]
                         if condition == "baseline_squeez" else None),
            # baseline_sherloc: fold the localization session's usage in
            pre_stage_dir=(run_dir / "sherloc"
                           if condition == "baseline_sherloc" else None),
            # baseline_agentdiet: their trajectory reduction as a per-cell
            # proxy; baseline prompt, baseline tools, nothing else changes.
            agentdiet_proxy=(condition == "baseline_agentdiet"),
            # baseline_seerepo: their one-look image rule via a per-cell proxy
            seerepo_proxy=(condition == "baseline_seerepo"),
            # baseline_codermind: `cmind` on PATH for their SessionStart hook,
            # CMIND_WORKSPACE for their MCP server
            extra_env=codermind_env)
        if condition == "baseline_codermind":
            # fold the encode / incremental-update sessions' usage into
            # result.json (their paper reports graph maintenance separately)
            agent._merge_side_calls(run_dir, run_dir / "codermind_side.jsonl",
                                    "codermind")
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
    if (is_flow_v3 or is_graphfirst) and rep is not None:
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


# -- comparator-arm helpers (efficiency-baselines plan, 2026-08-31) ----------
# Frozen clause from "Prompt-Induced Waste in Coding Agents" (arXiv
# 2608.01347 v5), Appendix C / Table 4, condition `bounded_efficiency`,
# SHA-256 prefix 56954fa9ae17. Verbatim; do not edit.
# "To Run or Not to Run" (arXiv 2606.26978, ISSTA'26): their PRIMARY
# prohibited-execution enforcement is this prompt-level sentence (verbatim).
# "More with Less" (arXiv 2510.16786) turn caps from the swe30_shim baseline's
# num_turns distribution (n=30, census 2026-09-03: min 7, p25 42, median 50,
# p75 62, max 79). Fixed arm = 75th percentile; dynamic arm = 50th percentile
# + one-time buffer to the 75th (the paper's "Dynamic 50->75").
_TURN75_CAP = 62
_TURNDYN_INITIAL = 50
_TURNDYN_EXT = 12


def _turn_cap_for(condition):
    """Initial cap per arm; TURN_SMOKE_CAP overrides (smokes only)."""
    env = os.environ.get("TURN_SMOKE_CAP")
    if env:
        return int(env)
    return _TURN75_CAP if condition == "baseline_turn75" else _TURNDYN_INITIAL


def _turndyn_ext():
    """Buffer size for the dynamic arm; TURN_SMOKE_EXT overrides (smokes)."""
    return int(os.environ.get("TURN_SMOKE_EXT", _TURNDYN_EXT))


_NOEXEC_CLAUSE = ("Do not run pytest, unittest, or project test scripts. "
                  "Project dependencies are not installed.")

_BOUNDED_EFFICIENCY_CLAUSE = (
    "Work efficiently: begin with the failing test and the most likely "
    "implementation files; inspect additional files only when evidence "
    "requires it; avoid unrelated cleanup; make the smallest sufficient "
    "change; run the relevant tests; stop as soon as the acceptance "
    "criteria pass.")

# TALE (arXiv 2412.18547, ACL'25 Findings) Figure-5 zero-shot budget
# estimation prompt. Verbatim (capital "Give" is genuine); do not edit.
_TALE_ESTIMATE_PROMPT = (
    "Task: Analyze the given question and estimate the minimum number of "
    "tokens required to generate a complete and accurate response. Please "
    "Give the response by strictly following this format: [[budget]], "
    "for example, Budget: [[12]].")


def _claudemd_for(iid, subdir=None):
    """Map instance id -> the per-repo generated CLAUDE.md file.

    subdir=None -> tools/claudemd (the short files of the first arm);
    subdir="claudemd_full" -> the AGENTS.md-sized files of baseline_agentsmd."""
    import re as _re
    m = _re.match(r"instance_(.+?)-[0-9a-f]{40}-v[0-9a-f]+$", iid)
    if not m:
        raise RuntimeError("cannot derive repo from instance id %r" % iid)
    base = (agent.CLAUDEMD_DIR if subdir is None
            else agent.STUDY_ROOT / "tools" / subdir)
    p = base / (m.group(1) + ".md")
    if not p.exists():
        raise RuntimeError("no CLAUDE.md authored for repo %r" % m.group(1))
    return p


_CSNIPER_POLICY_CACHE = None


def _csniper_policy():
    """Render ContextSniper's code-policy text exactly as their released
    Claude runner does (contextsniper-render-code-policy with the filtering
    section enabled). Static per config -- rendered once and cached."""
    global _CSNIPER_POLICY_CACHE
    if _CSNIPER_POLICY_CACHE is None:
        import subprocess
        env = dict(os.environ)
        env["PY_BIN"] = str(agent.CSNIPER_ROOT / ".venv" / "bin" / "python")
        env["CONTEXTSNIPER_DIR"] = str(agent.CSNIPER_ROOT)
        env["CONTEXTSNIPER_INJECT_FILTERING_PROMPT"] = "1"
        out = subprocess.run(
            [str(agent.CSNIPER_PLUGIN / "bin"
                 / "contextsniper-render-code-policy")],
            capture_output=True, text=True, env=env, timeout=60)
        if out.returncode != 0 or not out.stdout.strip():
            raise RuntimeError("csniper policy render failed: %s"
                               % out.stderr[:300])
        _CSNIPER_POLICY_CACHE = out.stdout.strip()
    return _CSNIPER_POLICY_CACHE


def _tale_estimate_budget(inst, run_dir, args):
    """TALE-EP phase 1: one single-turn, tool-less claude call estimating a
    token budget for this instance. The call's full JSON result (incl. usage)
    is saved to run_dir/tale_estimator.json so the arm's token bill can
    include it. Fallback budget on any failure: 2000 (recorded as such)."""
    import re
    import subprocess
    # Adaptation (disclosed): the Figure-5 Task text rides as the SYSTEM
    # prompt of the estimation call, with one added guard sentence --
    # coding-agent task prompts otherwise trigger task execution instead of
    # estimation (verified on a smoke). Question goes as the user message
    # in the Figure-7c "Q: ..." form.
    est_system = (_TALE_ESTIMATE_PROMPT
                  + " Do not attempt to answer the question itself.")
    out = {}
    budget = None
    try:
        cmd = [args.claude_bin, "-p", "Q: " + agent.build_prompt(inst),
               "--append-system-prompt", est_system,
               "--output-format", "json", "--strict-mcp-config",
               "--model", args.model, "--effort", args.effort_flag,
               "--permission-mode", "bypassPermissions",
               "--max-turns", "1", "--max-budget-usd", "0.25",
               "--tools", "", "--disallowedTools", "WebFetch,WebSearch"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
        out = json.loads(r.stdout or "{}")
        m = re.search(r"\[\[\s*(\d+)\s*\]\]", str(out.get("result", "")))
        if m:
            budget = int(m.group(1))
    except Exception as e:
        out = {"error": repr(e)}
    if not budget or budget <= 0:
        budget = 2000
        out["fallback_budget_used"] = True
    out["tale_budget"] = budget
    (run_dir / "tale_estimator.json").write_text(
        json.dumps(out), encoding="utf-8")
    return budget


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

    # Validate conditions BEFORE launching any cell (2026-08-19 review): a
    # misnamed arm (e.g. 'calls_perrep_flow', 'cfgdfg_perrep') parses to a
    # rep but matches no arm predicate, falls through to the MAP arm, and
    # perrep.build's SystemExit("unknown rep") -- a BaseException -- then
    # aborts the WHOLE batch mid-run instead of failing one cell.
    from perrep import build as perrep_build
    _literal = {"baseline", "baseline_lean", "baseline_pure", "codegraph",
                "memory_hybrid", "memory_strategy", "memory_text",
                "memory_combo", "memory_rerank",
                # published prompt-lever comparator arms (2026-08-31)
                "baseline_beclause", "baseline_tale", "baseline_rtk",
                "baseline_cmv", "baseline_cmv2", "baseline_csniper",
                "baseline_lingua",
                "baseline_cbmem", "baseline_cbmem2", "baseline_claudemd",
                "baseline_pruner", "baseline_squeez", "baseline_selctx",
                "baseline_agentsmd", "baseline_prunerB", "baseline_noexec",
                "baseline_mask", "baseline_turn75", "baseline_turndyn",
                "baseline_sherloc", "baseline_agentdiet", "baseline_seerepo", "baseline_eet", "baseline_eetB",
                "baseline_codermind"}
    for cond in a.conditions:
        if cond in _literal:
            continue
        rep = condition_rep(cond)
        if rep is None:
            raise SystemExit(
                "[swebench] unknown condition %r -- want one of %s or "
                "<rep>_perrep[_dyn|_flowplan[_blocks]|_flow|_flow3|_flow3s|"
                "_nlplan|_flowonly]" % (cond, sorted(_literal)))
        if (cond.endswith("_perrep_graphfirst")
                and not condition_graphfirst(cond)):
            raise SystemExit(
                "[swebench] condition %r: graphfirst supports the flow3 "
                "views (cfg/dfg/cfgdfg/ast/calls/src); loc is excluded "
                "(nothing to plan from)" % cond)
        if (cond.endswith("_perrep_flow3s")
                and not condition_flow_strict(cond)):
            raise SystemExit(
                "[swebench] condition %r: the strict arm (_flow3s) supports "
                "cfg/dfg/cfgdfg only (it tests flow-specific instruments; "
                "the other views have no graph to ground plans in)" % cond)
        if (cond.endswith("_perrep_flow3slice")
                and not condition_flow_slice(cond)):
            raise SystemExit(
                "[swebench] condition %r: the slice arm (_flow3slice) "
                "supports dfg/cfgdfg only (the slice is a def-use "
                "construct; the src text-dump control gets its own arm)"
                % cond)
        if (cond.endswith("_perrep_flow3dyn")
                and not condition_flow_dyn3(cond)):
            raise SystemExit(
                "[swebench] condition %r: the overlay arm (_flow3dyn) "
                "supports cfg/dfg/cfgdfg only (only flow views can carry "
                "an executed path)" % cond)
        if (cond.endswith("_perrep_flow3b")
                and not condition_flow_bundle(cond)):
            raise SystemExit(
                "[swebench] condition %r: the bundled arm (_flow3b) "
                "supports cfg/dfg/cfgdfg/ast/calls (src/loc views already "
                "ARE the source)" % cond)
        if (cond.endswith("_perrep_flow3L")
                and not condition_flow_lean(cond)):
            raise SystemExit(
                "[swebench] condition %r: the lean arm (_flow3L) supports "
                "cfg/dfg/cfgdfg/ast/calls (same rep set as flow3b)" % cond)
        if (cond.endswith("_perrep_flow3c")
                and not condition_flow_comply(cond)):
            raise SystemExit(
                "[swebench] condition %r: the compliance arm (_flow3c) "
                "supports cfg/dfg/cfgdfg/ast/calls" % cond)
        if (cond.endswith("_perrep_flow3rip")
                and not condition_flow_ripple(cond)):
            raise SystemExit(
                "[swebench] condition %r: the ripple arm (_flow3rip) "
                "supports cfg/dfg/cfgdfg" % cond)
        if cond.endswith("_perrep_flow3r"):
            if not condition_flow_relay(cond):
                raise SystemExit(
                    "[swebench] condition %r: the relay arm (_flow3r) "
                    "supports cfg/dfg/cfgdfg/ast/calls" % cond)
            if a.agent != "claude":
                raise SystemExit(
                    "[swebench] condition %r: the relay arm needs the "
                    "claude agent (two-stage CLI orchestration is not "
                    "wired for codex)" % cond)
        # everything that is not MAP-FREE (plain map, dyn, flowplan) builds
        # a .perrep/<rep>.db and therefore needs a map builder for the rep.
        is_map_free = (condition_flow_first(cond) or condition_flow_v3(cond)
                       or condition_nlplan(cond) or condition_flowonly(cond)
                       or condition_graphfirst(cond))
        if not is_map_free and rep not in perrep_build.REPS:
            raise SystemExit(
                "[swebench] condition %r needs a rep map, but rep %r has no "
                "map builder (flow3-only rep?). Did you mean "
                "'%s_perrep_flow3'?" % (cond, rep, rep))

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
