"""SWE-bench Pro experiment runner: agent edit + docker test-grade per cell.

For each (instance, condition): check out the repo at base_commit, [for a flow3
condition] build the symbols index on the worktree + attach the rep's flow MCP
server, run the claude agent (editing files), extract the patch, then grade
in Docker (apply patch + run fail_to_pass/pass_to_pass). Score = resolved or not.

Conditions:
  baseline                          -- claude alone, no MCP servers
  <rep>_perrep_flow3                -- map-free flow-first v3 arm for <rep>
   (cfg/dfg/cfgdfg)                    (flow_of + flow_plan/flow_check/
                                        flow_verify, flowfix-skill prompt)

Usage:
  .venv/bin/python -m experiments.swebench_pro.run_experiment \\
      --n 10 --conditions baseline cfg_perrep_flow3 \\
      --exp-name swebench_pilot --concurrency 3 --grade
"""

import argparse
import concurrent.futures as cf
import json
import os
import sys
import time
from pathlib import Path

STUDY_ROOT = Path(__file__).resolve().parents[2]
if str(STUDY_ROOT) not in sys.path:
    sys.path.insert(0, str(STUDY_ROOT))

from experiments.swebench_pro import instances, checkout, agent, grade  # noqa: E402

VENV_PYTHON = STUDY_ROOT / ".venv" / "bin" / "python"
CLAUDE_BIN = os.environ.get("CLAUDE_BIN",
                            str(Path.home() / ".local" / "bin" / "claude"))
# This export supports EXACTLY these conditions (validated in main()).
ALLOWED_CONDITIONS = {"baseline", "cfg_perrep_flow3", "dfg_perrep_flow3",
                      "cfgdfg_perrep_flow3"}
# "cfgdfg" is the COMBINED flow arm (both graphs in flow_of, dual op
# vocabulary).
PERREP_REPS = ("cfg", "dfg", "cfgdfg")


def condition_rep(condition: str) -> str | None:
    """'<rep>_perrep[_flow3]' -> rep."""
    if condition.endswith("_perrep_flow3"):
        condition = condition[: -len("_flow3")]
    if condition.endswith("_perrep"):
        rep = condition[: -len("_perrep")]
        if rep in PERREP_REPS:
            return rep
    return None


def condition_flow_first(condition: str) -> bool:
    """The v2 flow-first arm is not in this export; always False. (Kept for
    import compatibility -- flow3 is detected by condition_flow_v3.)"""
    return False


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
    """Removed arm (pre-fix flow-first A/B variant); always False."""
    return False


def condition_nlplan(condition: str) -> bool:
    """Removed arm (RQ2 NL-plan ablation); always False."""
    return False


def condition_flowonly(condition: str) -> bool:
    """Removed arm (RQ2 flow_of-only control); always False."""
    return False


def condition_dyn(condition: str) -> bool:
    """Removed arm (execution-trace overlay); always False."""
    return False


def condition_flowplan(condition: str) -> bool:
    """Removed arm (graph-mediated-editing flowplan); always False."""
    return False


def condition_cfg_blocks(condition: str) -> bool:
    """Removed arm (flowplan_blocks); always False."""
    return False


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


# The .repr_index backends each rep ACTUALLY reads:
# cfg/dfg parse Python source on the fly (stdlib-ast / beniget engines);
# only the symbols index is needed for name resolution + map skeleton.
_REP_BACKENDS = {
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


def write_mcp_config(rep: str, worktree: Path, run_dir: Path,
                     flow_first: bool = False,
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
                    # flow-first arm: expose ONLY flow_of/flow_plan/flow_check
                    # (no explore map, no native tools). The flow IS the surface.
                    "REPR_FLOW_FIRST": "1" if flow_first else "",
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
        if rep is not None:
            # flow3 shares the flow-first MAP-FREE base (symbols index only):
            # NO rep map (the flow is the surface, not a code map). Only the
            # symbol index is needed so flow_of can resolve the report's
            # named functions to their CFGs.
            flow_v3 = condition_flow_v3(condition)
            ensure_repr_index(inst, wt, rep=rep)
            map_info = {"rep": rep, "flow_first": True, "no_map": True}
            mcp_config = write_mcp_config(rep, wt, run_dir,
                                          # flow3 reuses the flow-first
                                          # registration (REPR_FLOW_FIRST=1)
                                          flow_first=flow_v3,
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

    # Keep the rep MCP tools ACTIVE (never demoted to deferred) by capping the
    # built-in toolset with --tools. Then the agent calls the flow tools
    # organically -- no ToolSearch round-trip, no steer prompt needed. Only
    # the tool-bearing conditions get the restriction.
    is_tool_cell = (mcp_config is not None)
    restrict = is_tool_cell and not args.no_restrict_tools

    is_flow_v3 = condition_flow_v3(condition)
    # flow3 is MAP-FREE: NO explore/map tool -- the flow IS the surface -- so
    # there is no explore nudge and no Grep-blocking explore-gate here (those
    # belonged to the removed map arms).

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

    agent.run_agent(
        inst, run_dir, claude_bin=args.claude_bin, worktree=wt,
        model=args.model, effort=args.effort_flag,
        max_turns=args.max_turns,
        max_budget_usd=args.max_budget_usd,
        timeout_sec=args.timeout_sec, mcp_config=mcp_config,
        restrict_tools=restrict,
        prompt_override=flow3_prompt, skill_md=flow3_skill_md,
        sentinel=flow3_sentinel)

    # measurement: record the FINAL plan-vs-code verdict for flow cells (same
    # check as the agent-facing flow_verify tool), whether or not the agent
    # called it -- run BEFORE the worktree is cleaned up.
    # flow3: runner-side final verify, under v3 semantics (extra_env
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
    p.add_argument("--agent", choices=["claude"], default="claude")
    p.add_argument("--model", default=None)
    p.add_argument("--effort-flag", default="low")
    p.add_argument("--max-turns", type=int, default=80)
    p.add_argument("--max-budget-usd", type=float, default=2.0)
    p.add_argument("--timeout-sec", type=int, default=1500)
    p.add_argument("--claude-bin", default=CLAUDE_BIN)
    p.add_argument("--grade", action="store_true",
                   help="docker test-grade each run after the agent")
    p.add_argument("--force", action="store_true", help="re-run graded cells")
    p.add_argument("--keep-worktrees", action="store_true")
    p.add_argument("--no-restrict-tools", action="store_true",
                   help="do NOT cap built-in tools with --tools (lets MCP tools "
                        "demote to deferred -- the un-fixed behavior)")
    p.add_argument("--keep-gold-tests", action="store_true",
                   help="do NOT strip the harness-laid gold test files from "
                        "the agent worktree (reproduces pre-2026-07-27 "
                        "leaky behavior; for ablation only)")
    p.add_argument("--no-clean-user-site", action="store_true",
                   help="do NOT wipe stale ~/.local editable-install pointers "
                        "before the batch (contamination-hardening layer 3)")
    a = p.parse_args(argv)
    bad = sorted(set(a.conditions) - ALLOWED_CONDITIONS)
    if bad:
        raise SystemExit(
            "unsupported condition(s): %s -- this export supports exactly: %s"
            % (", ".join(bad), ", ".join(sorted(ALLOWED_CONDITIONS))))
    if a.model is None:
        a.model = "claude-haiku-4-5"

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

    ids = a.instance_ids
    if a.ids_file:
        ids = json.loads(Path(a.ids_file).read_text("utf-8"))
    if ids:
        insts = instances.load_subset(ids)
    else:
        insts = instances.load_all()[: a.n]
    results_dir = STUDY_ROOT / "results" / a.exp_name
    results_dir.mkdir(parents=True, exist_ok=True)

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
