"""SWT-bench (test-generation) runner.

Task: the agent writes a TEST that reproduces the bug (fails on buggy code,
passes after the gold fix). Reuses the SWE-Pro checkout + agent-invocation
plumbing; the task-specific pieces are: the test-gen prompt (prompt.py), a
TEST-ONLY diff harvest, and the two-run SWT grader (grade.py).

Conditions:
  baseline        -- no tools, plain test-gen prompt.
  cfg_perrep_flow -- flow-first: the agent works in the buggy function's CFG
                     (flow_of/flow_plan/flow_check), no code map.
  dfg_perrep_flow -- flow-first: same, but the buggy function's DEF-USE
                     (value-flow) graph instead of the CFG (Python/beniget).

Usage:
  python -m experiments.swtbench.run_experiment \
    --ids-file results/_flowpilot_ids.json \
    --conditions baseline cfg_perrep_flow \
    --exp-name swt10 --concurrency 2 --grade
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import sys
from pathlib import Path

STUDY_ROOT = Path(__file__).resolve().parents[2]
if str(STUDY_ROOT) not in sys.path:
    sys.path.insert(0, str(STUDY_ROOT))

from experiments.swebench_pro import instances, checkout, agent  # noqa: E402
from experiments.swebench_pro import run_experiment as swe  # noqa: E402
from experiments.swtbench import prompt as swt_prompt  # noqa: E402
from experiments.swtbench import grade as swt_grade  # noqa: E402

CLAUDE_BIN = swe.CLAUDE_BIN


# -- test-only diff harvest ---------------------------------------------------

_TEST_HINTS = ("/test/", "/tests/", "test_", "_test.py", "/testing/")


def _is_test_path(p: str) -> bool:
    q = "/" + p
    base = p.rsplit("/", 1)[-1]
    return (any(h in q for h in ("/test/", "/tests/", "/testing/"))
            or base.startswith("test_") or base.endswith("_test.py")
            or base == "conftest.py")


def harvest_test_patch(inst: dict, wt: Path) -> str:
    """The agent's TEST diff: worktree-vs-base, keeping ONLY test-file sections.

    Inverse of checkout.diff_excluding_tests. We stage everything (minus the
    tool dirs), diff vs base, then keep only per-file sections whose path looks
    like a test."""
    from experiments.swebench_pro.checkout import _run
    base = inst["base_commit"]
    if _run(["git", "cat-file", "-e", base + "^{commit}"], cwd=wt,
            check=False).returncode != 0:
        base = "HEAD"
    _run(["git", "add", "-A", "--",
          ":(exclude).perrep", ":(exclude).repr_index", ":(exclude).codegraph"],
         cwd=wt, check=False)
    res = _run(["git", "diff", "--cached", base, "--",
                ":(exclude).perrep", ":(exclude).repr_index",
                ":(exclude).codegraph"], cwd=wt, check=False)
    full = res.stdout or ""
    # keep only test-file sections
    out, keep = [], False
    for line in full.splitlines(keepends=True):
        if line.startswith("diff --git "):
            parts = line.split()
            p = parts[2][2:] if len(parts) >= 3 and parts[2].startswith("a/") \
                else ""
            keep = _is_test_path(p)
        if keep:
            out.append(line)
    return "".join(out)


def condition_flow_v3(condition: str) -> bool:
    """flow3 (v3) test-gen arm: '<rep>_perrep_flow3' (cfg/dfg/cfgdfg).

    A NEW ARM (the v2 '_perrep_flow' conditions stay byte-identical): same
    map-free flow surface + TESTGEN server semantics, plus REPR_FLOW_V3=1
    (add_function op, fault_site, HEAD snapshots, merge-on-replan, graded
    verdicts), NO legacy-verify pin, and the flowfix-skill protocol prompt.
    Exact-membership check so it is never confused with the v2 conditions
    ('_perrep_flow' is a PREFIX of '_perrep_flow3')."""
    return condition in ("cfg_perrep_flow3", "dfg_perrep_flow3",
                         "cfgdfg_perrep_flow3")


def run_cell(inst: dict, condition: str, results_dir: Path, args) -> dict:
    iid = inst["instance_id"]
    run_dir = (results_dir / ("%s__%s" % (iid, condition))).resolve()
    if (run_dir / "grade.json").is_file() and not args.force:
        return {**json.loads((run_dir / "grade.json").read_text()),
                "_skipped": True}

    # TOKEN GUARD (same as swebench_pro.run_cell): a mid-batch OAuth expiry
    # turns every remaining cell into a 1-turn 401 failure. Skip WITHOUT
    # artifacts so a post-re-login resume re-runs the cell cleanly.
    mins = swe._token_minutes() if args.agent == "claude" else None
    if mins is not None and mins < 3:
        print("[swt] TOKEN EXPIRED/EXPIRING (%.0f min) -- skipping %s__%s "
              "without artifacts; re-login and resume" % (mins, iid, condition))
        return {"instance_id": iid, "condition": condition,
                "resolved": False, "_token_skipped": True}
    run_dir.mkdir(parents=True, exist_ok=True)

    # flow-first: cfg = control-flow blocks/edges, dfg = def-use value flow,
    # cfgdfg = BOTH graphs (the combined arm). All Python here
    # (beniget/stdlib-ast engines; no Joern needed).
    flow_first = condition in ("cfg_perrep_flow", "dfg_perrep_flow",
                               "cfgdfg_perrep_flow")
    rep = condition.split("_", 1)[0] if flow_first else None
    # flow3 (v3): a NEW arm -- the v2 branches above stay untouched.
    flow_v3 = condition_flow_v3(condition)
    if flow_v3:
        rep = condition.split("_", 1)[0]

    try:
        wt = checkout.prepare_worktree(inst, suffix=condition)
        mcp_config = None
        flow_first_tools = None
        if flow_first:
            # symbol index (so flow_of resolves the report's symbols); NO map.
            swe.ensure_repr_index(inst, wt, rep=rep)
            # TEST-GEN mode: flow_plan records the delta a FIX would make
            # (the contract the test asserts is missing); flow_check /
            # flow_verify prove the SOURCE flow is untouched and the diff is
            # test-files-only. All three verification tools are REQUIRED by
            # the prompt (v2 fix: they were optional and unused).
            mcp_config = swe.write_mcp_config(rep, wt, run_dir,
                                              flow_first=True,
                                              flow_testgen=True)
            flow_first_tools = ("mcp__%s_perrep__flow_of" % rep,
                                "mcp__%s_perrep__flow_plan" % rep,
                                "mcp__%s_perrep__flow_check" % rep,
                                "mcp__%s_perrep__flow_verify" % rep)
        if flow_v3:
            # flow3: same symbol-index-only, map-free setup as flow_first;
            # server runs in TESTGEN mode PLUS REPR_FLOW_V3=1 (and without
            # the legacy-verify pin). flow_first_tools stays None so the v2
            # prompt builder emits the plain task body, no v2 flow steps.
            swe.ensure_repr_index(inst, wt, rep=rep)
            mcp_config = swe.write_mcp_config(rep, wt, run_dir,
                                              flow_first=True,
                                              flow_testgen=True,
                                              flow_v3=True)
        # isolate history so the agent can't recover the gold test via git.
        checkout.isolate_worktree_history(wt)
    except Exception as e:
        (run_dir / "setup_error.txt").write_text(repr(e), "utf-8")
        return {"instance_id": iid, "condition": condition, "resolved": False,
                "score": 0.0, "reason": "setup_failed", "error": repr(e)}

    prompt = swt_prompt.build_prompt(inst, flow_first_tools=flow_first_tools)

    # flow3: the v2 task body (no v2 flow steps -- flow_first_tools is None)
    # wrapped by the v3 protocol: "/flowfix" + skill in the per-run
    # claude-home (default), or the protocol inlined per REPR_FLOW_SKILL=0.
    flow3_skill_md = None
    flow3_sentinel = None
    if flow_v3:
        from experiments.common import flow_protocol
        tools3 = flow_protocol.tool_names(rep)
        prompt = flow_protocol.build_user_prompt(
            "testgen", rep, tools3, prompt, agent=args.agent)
        if flow_protocol.skill_mode():
            flow3_skill_md = flow_protocol.build_skill_md("testgen", rep,
                                                          tools3)
        flow3_sentinel = flow_protocol.SENTINEL

    if args.agent == "claude":
        agent.run_agent(
            inst, run_dir, claude_bin=args.claude_bin, worktree=wt,
            model=args.model, effort=args.effort_flag,
            max_turns=args.max_turns,
            max_budget_usd=args.max_budget_usd,
            timeout_sec=args.timeout_sec, mcp_config=mcp_config,
            restrict_tools=(mcp_config is not None),
            flow_first_tools=flow_first_tools, prompt_override=prompt,
            skill_md=flow3_skill_md, sentinel=flow3_sentinel)
    else:
        from experiments.common import codex_agent
        codex_agent.run_agent(
            inst, run_dir, codex_bin=args.codex_bin, worktree=wt,
            model=args.model, effort=args.effort_flag,
            max_turns=args.max_turns,
            max_budget_usd=args.max_budget_usd,
            timeout_sec=args.timeout_sec, prompt=prompt,
            mcp_config=mcp_config, skill_md=flow3_skill_md,
            sentinel=flow3_sentinel,
            auth_file=(Path(args.codex_auth_file)
                       if args.codex_auth_file else None),
            auth_mode=args.codex_auth_mode)

    # flow3 ONLY: runner-side final plan-vs-code verdict (the v2 SWT cells
    # never had this harness-side record; adding it v3-only keeps them
    # untouched). extra_env mirrors the secbench call: the CLI must run in
    # TESTGEN + v3 mode to grade 'source flow unchanged + test-only diff'.
    if flow_v3 and rep is not None:
        from experiments import flowverify
        flowverify.record_final_verify(
            swe.VENV_PYTHON, STUDY_ROOT, rep, wt, run_dir,
            extra_env={"REPR_FLOW_TESTGEN": "1", "REPR_FLOW_V3": "1"})

    # harvest the agent's TEST as test_patch.diff (test files only)
    test_patch = harvest_test_patch(inst, wt)
    (run_dir / "test_patch.diff").write_text(test_patch, encoding="utf-8")

    rec = {"instance_id": iid, "condition": condition, "rep": rep,
           "test_patch_bytes": len(test_patch)}
    ds = getattr(args, "dataset", "pro")
    if ds in ("lite", "verified"):
        # lite/verified: swt_grade.grade is SWE-Pro-docker-specific
        # (dockerhub_tag), so grading is DEFERRED to the official SWT-bench
        # harness via grade_official.py, which later merges real verdicts
        # into this file. The stub is LOAD-BEARING for resume: cells key
        # done-ness on grade.json existing (see the top of run_cell).
        g = {"instance_id": iid, "condition": condition, "dataset": ds,
             "pending_official": True, "resolved": None}
        (run_dir / "grade.json").write_text(json.dumps(g), "utf-8")
        rec.update(g)
    elif args.grade:
        if not test_patch.strip():
            g = {"resolved": False, "score": 0.0, "reason": "no_test_patch"}
            (run_dir / "grade.json").write_text(json.dumps(g), "utf-8")
        else:
            g = swt_grade.grade(inst, run_dir)
        rec.update({k: g.get(k) for k in (
            "resolved", "score", "reason", "n_f2p", "n_p2f", "n_f2f")})
    if not args.keep_worktrees:
        checkout.cleanup_worktree(inst, suffix=condition)
    return rec


def main(argv=None):
    p = argparse.ArgumentParser("swtbench.run_experiment")
    p.add_argument("--n", type=int, default=10)
    p.add_argument("--instance-ids", nargs="*", default=None)
    p.add_argument("--ids-file", default=None)
    p.add_argument("--conditions", nargs="+",
                   default=["baseline", "cfg_perrep_flow"])
    p.add_argument("--exp-name", default="swtbench")
    p.add_argument("--concurrency", type=int, default=2)
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
    p.add_argument("--grade", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--keep-worktrees", action="store_true")
    p.add_argument("--dataset", default="pro", choices=["pro", "lite", "verified"],
                   help="which benchmark: 'pro' (SWE-bench Pro, default; SWT "
                        "grading via the SWE-Pro docker grader), 'lite' "
                        "(official SWE-bench Lite -- classic schema; grading "
                        "deferred to grade_official.py, cells write a "
                        "pending-official stub grade.json), or 'verified' "
                        "(official SWE-bench Verified -- same deferred-official "
                        "grading path as lite)")
    a = p.parse_args(argv)
    if a.model is None:
        a.model = ("gpt-5.4-mini" if a.agent == "codex"
                   else "claude-haiku-4-5")

    # dataset swap (same precedent as swebench_pro.run_experiment --dataset
    # verified): 'lite' rebinds this module's `instances` global to the
    # official SWE-bench Lite loader (classic schema: no dockerhub_tag /
    # before_repo_set_cmd / requirements / interface -- prompt.py drops the
    # missing sections silently). Import is LAZY: the lite module is being
    # built in parallel, and the default 'pro' path must never touch it.
    global instances
    if a.dataset == "lite":
        from experiments.swebench_lite import instances as _linst
        instances = _linst
    elif a.dataset == "verified":
        from experiments.swebench_verified import instances as _vinst
        instances = _vinst

    ids = a.instance_ids
    if a.ids_file:
        ids = json.loads(Path(a.ids_file).read_text("utf-8"))
    insts = instances.load_subset(ids) if ids else instances.load_all()[: a.n]
    results_dir = STUDY_ROOT / "results" / a.exp_name
    results_dir.mkdir(parents=True, exist_ok=True)

    cells = [(inst, cond) for cond in a.conditions for inst in insts]
    print("[swt] %d instances x %d conditions = %d cells (concurrency %d)"
          % (len(insts), len(a.conditions), len(cells), a.concurrency))

    results = []
    with cf.ThreadPoolExecutor(max_workers=a.concurrency) as ex:
        futs = {ex.submit(run_cell, inst, cond, results_dir, a): (inst, cond)
                for inst, cond in cells}
        done = 0
        for fut in cf.as_completed(futs):
            inst, cond = futs[fut]
            done += 1
            try:
                rec = fut.result()
            except Exception as e:
                rec = {"instance_id": inst["instance_id"], "condition": cond,
                       "resolved": False, "reason": "cell_crash", "error": repr(e)}
            results.append(rec)
            mark = "OK " if rec.get("resolved") else "x  "
            print("[%d/%d] %s %s %s" % (done, len(cells), mark, cond,
                                        inst["instance_id"][:48]))

    (results_dir / "_summary.json").write_text(json.dumps(results, indent=2),
                                               "utf-8")
    print("\n=== resolve rate by condition ===")
    for cond in a.conditions:
        rc = [r for r in results if r.get("condition") == cond]
        n = len(rc)
        res = sum(1 for r in rc if r.get("resolved"))
        print("  %-20s %d/%d" % (cond, res, n))


if __name__ == "__main__":
    main()
