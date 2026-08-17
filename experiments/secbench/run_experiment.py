"""Run SecBench (patch task) arms on the leak-free harness.

Supports `baseline` and the flow3 flow-first arms (`cfg_perrep_flow3`,
`dfg_perrep_flow3`, `cfgdfg_perrep_flow3`), run with the Claude Code agent.
Reuses the swebench_pro agent runner for the Claude Code loop; the
SecBench-specific pieces are the checkout (git-history isolated), the prompt,
the per-instance Joern CPG + server the flow tools answer C through, and
grading via the official `secb patch|build|repro` in the vendor image.

    python -m experiments.secbench.run_experiment \
        --conditions baseline cfg_perrep_flow3 --n 10 --exp-name sec_pilot --grade
"""

import argparse
import json
import os
from pathlib import Path

import hashlib

from experiments.secbench import agent as sec_agent
from experiments.secbench import checkout as sec_checkout
from experiments.secbench import instances as sec_inst
from experiments.secbench import joern as sec_joern

STUDY_ROOT = Path(__file__).resolve().parents[2]
VENV_PYTHON = STUDY_ROOT / ".venv" / "bin" / "python"
# The ONLY conditions this export supports (validated in main()).
ALLOWED_CONDITIONS = {"baseline", "cfg_perrep_flow3", "dfg_perrep_flow3",
                      "cfgdfg_perrep_flow3"}
# "cfgdfg" is the COMBINED flow arm (flow-first only; both graphs in flow_of).
PERREP_REPS = ("cfg", "dfg", "cfgdfg")
# The NATIVE LINUX claude. Bare "claude" in WSL resolves to a Windows shim
# (/mnt/c/.../npm/claude) that runs claude.exe -> the agent sees the checkout
# as a \\wsl.localhost\... UNC path and CANNOT run the Linux --settings hook o
# reach the Linux MCP server -> 0 gate denials, 0 MCP calls (the SecBench
# adoption bug). SWE-Pro uses this same Linux binary.
CLAUDE_BIN = os.environ.get("CLAUDE_BIN",
                            str(Path.home() / ".local" / "bin" / "claude"))


def is_baseline_dyn(cond: str) -> bool:
    """Removed arm (kept for test imports): always False in this export."""
    return False


def condition_rep(cond: str) -> str | None:
    c = cond
    # strip the modifier suffix: only the flow3 (v3) flow-first arm exists in
    # this export -- '<rep>_perrep_flow3'.
    for suffix in ("_flow3",):
        if c.endswith("_perrep" + suffix):
            c = c[:-len(suffix)]
            break
    if c.endswith("_perrep"):
        rep = c[:-len("_perrep")]
        if rep in PERREP_REPS:
            return rep
    return None


def condition_dyn(cond: str) -> bool:
    """Removed arm (kept for test imports): always False in this export."""
    return False


def condition_flowplan(cond: str) -> bool:
    """Removed arm (kept for test imports): always False in this export."""
    return False


def condition_flow_first(cond: str) -> bool:
    """Removed arm (the v2 flow-first '<rep>_perrep_flow'; kept for test
    imports): always False in this export."""
    return False


def condition_flow_v3(cond: str) -> bool:
    """flow3 (v3) FLOW-FIRST arm on SecBench: '<rep>_perrep_flow3' fo
    cfg/dfg/cfgdfg, answering C via the same per-instance Joern CPG + serve
    the v2 flow path boots. A NEW ARM: same map-free surface as
    '_perrep_flow' plus REPR_FLOW_V3=1 semantics (add_function, fault_site,
    HEAD snapshots, merge-on-replan, graded verdicts), the flowfix-skill
    prompt, and NO REPR_FLOW_LEGACY_VERIFY pin. Exact-suffix check:
    '_perrep_flow' is a PREFIX of '_perrep_flow3', so v2's
    endswith('_perrep_flow') never matches a flow3 condition."""
    return (cond.endswith("_perrep_flow3")
            and condition_rep(cond) in ("cfg", "dfg", "cfgdfg"))


def condition_flowonly(cond: str) -> bool:
    """Removed arm (kept for test imports): always False in this export."""
    return False


def _pick_port(iid: str, condition: str) -> int:
    """A deterministic, per-(instance,condition) Joern port in [8800, 9800)."""
    h = int(hashlib.sha1((iid + condition).encode()).hexdigest()[:6], 16)
    return 8800 + (h % 1000)


def write_mcp_config(rep: str, wt: Path, run_dir: Path,
                     joern_port: int | None) -> Path:
    """mcp.json for a flow3 (map-free flow-first v3) cell."""
    # When a per-cell Joern server is booted (C/C++ CFG), point the perrep
    # server's cfg tools at it via JOERN_SERVER_URL. Matches the JoernServe
    # defaults (user 'repr', password 'repr-study').
    joern_url = ("http://127.0.0.1:%d" % joern_port) if joern_port else ""
    cfg = {"mcpServers": {"%s_perrep" % rep: {
        "command": str(VENV_PYTHON), "args": ["-m", "servers.perrep"],
        "env": {
            "REPO_ROOT": str(wt),
            "REPO_INDEX_DIR": str(wt / ".repr_index"),
            "RUN_DIR": str(run_dir),
            "PYTHONPATH": str(STUDY_ROOT),
            "REPR_PERREP_REP": rep,
            "REPR_TRACE": "",
            "REPR_TRACE_FILE": "",
            "REPR_TRACE_KIND": "",
            "JOERN_SERVER_URL": joern_url,
            "JOERN_USER": "repr" if joern_url else "",
            "JOERN_PASSWORD": "repr-study" if joern_url else "",
            "REPR_FLOWEDIT": "",
            # flow-first: expose ONLY flow_of/flow_plan/flow_check (no map).
            "REPR_FLOW_FIRST": "1",
            "REPR_FLOW_OF_ONLY": "",
            # flow3 (v3) arm: v3 tool semantics (add_function op, fault_site,
            # HEAD snapshots, merge-on-replan, graded verdicts, replace arg
            # on flow_plan).
            "REPR_FLOW_V3": "1",
            # flow3 does NOT pin legacy verify (the v3 verifier IS the arm).
            "REPR_FLOW_LEGACY_VERIFY": "",
        },
    }}}
    p = run_dir / "mcp.json"
    p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return p


def build_prompt(inst: dict) -> str:
    parts = ["# Security patch task", ""]
    parts += [
        "You are working in a checked-out C/C++ repository (the current "
        "directory). A real OSS-Fuzz vulnerability fires the "
        "`%s` sanitizer." % (inst.get("sanitizer") or "?"),
        "",
        "## Vulnerability",
        "",
        (inst.get("bug_report") or inst.get("bug_description") or "")[:8000],
        "",
        "## Sanitizer report",
        "",
        (inst.get("sanitizer_report") or "")[:4000],
        "",
        "## Rules",
        "- Make the MINIMAL change to non-test C/C++ source so the crash is no "
        "longer triggered. Do NOT edit tests or build files.",
        "- Edit files in the current directory; the harness harvests your git "
        "diff and grades it by rebuilding and re-running the sanitizer.",
        "- End with a non-empty change; then stop.",
    ]
    return "\n".join(parts)


def harvest_patch(inst: dict, wt: Path) -> str:
    """git diff of the agent's C/C++ edits vs the base snapshot (HEAD)."""
    import subprocess
    exts = ("*.c", "*.cpp", "*.h", "*.hpp", "*.cc", "*.hh")
    subprocess.run(["git", "-C", str(wt), "add", "-A"], capture_output=True)
    r = subprocess.run(
        ["git", "-C", str(wt), "diff", "--no-color", "--cached", "HEAD", "--",
         *exts], capture_output=True, text=True)
    return r.stdout or ""


def grade(inst: dict, patch: str, run_dir: Path) -> dict:
    """Grade via this repo's native SecBench grader (no external repo)."""
    from experiments.secbench import grade as sec_grade
    return sec_grade.grade_patch(inst, patch, run_dir)


def run_cell(inst: dict, condition: str, results_dir: Path, args) -> dict:
    iid = inst["instance_id"]
    run_dir = (results_dir / ("%s__%s" % (iid, condition))).resolve()
    if (run_dir / "grade.json").is_file() and not args.force:
        return {**json.loads((run_dir / "grade.json").read_text()),
                "_skipped": True}

    # TOKEN GUARD (same as swebench_pro/swtbench run_cell): a mid-batch OAuth
    # expiry turns remaining cells into 1-turn 401 failures. Skip WITHOUT
    # artifacts so a post-re-login resume re-runs the cell cleanly. (SecBench
    # cells are expensive -- per-cell Joern server + CPG build -- so burning
    # them on a dead token is especially wasteful.)
    from experiments.swebench_pro.run_experiment import _token_minutes
    mins = _token_minutes()
    if mins is not None and mins < 3:
        print("[sec] TOKEN EXPIRED/EXPIRING (%.0f min) -- skipping %s__%s "
              "without artifacts; re-login and resume" % (mins, iid, condition))
        return {"instance_id": iid, "condition": condition,
                "resolved": False, "_token_skipped": True}
    run_dir.mkdir(parents=True, exist_ok=True)

    rep = condition_rep(condition)
    flow_v3 = condition_flow_v3(condition)
    joern_port = _pick_port(iid, condition)
    try:
        wt = sec_checkout.prepare_checkout(inst, suffix=condition)
        chk = sec_checkout.verify_no_oracle(inst, wt)
        (run_dir / "oracle_check.json").write_text(json.dumps(chk), "utf-8")
        if not chk["clean"]:
            raise RuntimeError("oracle leak: %s" % chk)
        mcp_config = None
        cpg_path = None
        if rep is not None:
            # flow3 (flow-first, MAP-FREE): NO rep map (the flow is the
            # surface). Symbol index + CPG below are still built so flow_of
            # can resolve the report's symbols and render their C CFGs.
            from indexer.backends import get_backend
            (wt / ".repr_index").mkdir(exist_ok=True)
            get_backend("symbols").build(wt, wt / ".repr_index")
            # Build the Joern CPG (scoped to the crash-stack dirs) so the C/C++
            # control-flow tools actually work -- the pure-Python engine cannot
            # parse C. Skipped only if --no-joern.
            if not getattr(args, "no_joern", False):
                cpg_path = wt / ".repr_index" / "cpg" / "inst.cpg.bin"
                crec = sec_joern.build_cpg(inst, wt, cpg_path,
                                           heap_gb=args.cpg_heap_gb,
                                           timeout=args.cpg_timeout)
                (run_dir / "cpg_build.json").write_text(json.dumps(crec),
                                                        "utf-8")
                if not crec.get("ok"):
                    cpg_path = None  # tools fall back to notes; run continues
            mcp_config = write_mcp_config(rep, wt, run_dir,
                                          joern_port if cpg_path else None)
    except Exception as e:
        (run_dir / "setup_error.txt").write_text(repr(e), "utf-8")
        return {"instance_id": iid, "condition": condition, "resolved": False,
                "score": 0.0, "reason": "setup_failed", "error": repr(e)}

    prompt = build_prompt(inst)

    # flow3: the v2 task body wrapped by the v3 protocol: "/flowfix" + skill
    # in the per-run claude-home (default), or inlined per REPR_FLOW_SKILL=0.
    flow3_skill_md = None
    flow3_sentinel = None
    if flow_v3 and rep is not None:
        from experiments.common import flow_protocol
        tools3 = flow_protocol.tool_names(rep)
        prompt = flow_protocol.build_user_prompt(
            "security", rep, tools3, prompt, agent=args.agent)
        if flow_protocol.skill_mode():
            flow3_skill_md = flow_protocol.build_skill_md("security", rep,
                                                          tools3)
        flow3_sentinel = flow_protocol.SENTINEL

    def _run_the_agent():
        sec_agent.run_agent(
            inst, run_dir, claude_bin=args.claude_bin, worktree=wt,
            model=args.model, effort=args.effort_flag,
            max_turns=args.max_turns,
            max_budget_usd=args.max_budget_usd,
            timeout_sec=args.timeout_sec, prompt=prompt,
            mcp_config=mcp_config,
            restrict_tools=(mcp_config is not None),
            settings_file=None, skill_md=flow3_skill_md,
            sentinel=flow3_sentinel)
        # measurement: record the FINAL plan-vs-code verdict for flow cells.
        # Runs HERE (not after) so the per-cell Joern server is still up --
        # the C dfg fingerprint needs the live CPG. extra_env gains
        # REPR_FLOW_V3 (and no legacy pin -- record_final_verify only
        # forwards extra_env).
        if flow_v3 and rep is not None:
            from experiments import flowverify
            joern_env = {"REPR_FLOW_V3": "1"}
            if cpg_path is not None:
                joern_env.update({"JOERN_SERVER_URL":
                                  "http://127.0.0.1:%d" % joern_port,
                                  "JOERN_USER": "repr",
                                  "JOERN_PASSWORD": "repr-study"})
            flowverify.record_final_verify(VENV_PYTHON, STUDY_ROOT, rep, wt,
                                           run_dir, extra_env=joern_env)

    # Boot a per-cell Joern server preloaded with the CPG for the duration of
    # the agent run; the perrep server (launched by claude) reads
    # JOERN_SERVER_URL from its mcp.json env. Without a CPG, run plainly.
    if cpg_path is not None:
        from experiments.common.joern_server import JoernServer
        srv = JoernServer(cpg_path, port=joern_port,
                          heap_gb=args.joern_heap_gb,
                          log_path=run_dir / "joern.server.log")
        try:
            srv.start(ready_timeout=180.0)
            (run_dir / "joern_env.json").write_text(
                json.dumps(srv.client_env()), "utf-8")
            _run_the_agent()
        finally:
            srv.stop()
    else:
        _run_the_agent()

    patch = harvest_patch(inst, wt)
    (run_dir / "model_patch.diff").write_text(patch, encoding="utf-8")
    rec = {"instance_id": iid, "condition": condition, "rep": rep,
           "patch_bytes": len(patch)}
    if args.grade:
        if not patch.strip():
            g = {"resolved": False, "score": 0.0, "reason": "no_patch"}
        else:
            g = grade(inst, patch, run_dir)
        rec.update(g)
        (run_dir / "grade.json").write_text(json.dumps(rec, indent=2), "utf-8")
    if not args.keep_checkouts:
        sec_checkout.cleanup_checkout(inst, suffix=condition)
    return rec


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m experiments.secbench.run_experiment")
    p.add_argument("--conditions", nargs="+", default=["baseline"])
    p.add_argument("--n", type=int, default=10)
    p.add_argument("--instance-ids", nargs="*", default=None)
    p.add_argument("--ids-file", default=None,
                   help="JSON file with a list of instance_ids")
    p.add_argument("--exp-name", default="secbench")
    p.add_argument("--concurrency", type=int, default=1)
    p.add_argument("--agent", choices=["claude"], default="claude")
    p.add_argument("--model", default="claude-haiku-4-5")
    p.add_argument("--effort-flag", default="low")
    p.add_argument("--max-turns", type=int, default=40)
    p.add_argument("--max-budget-usd", type=float, default=1.5)
    p.add_argument("--timeout-sec", type=int, default=1500)
    p.add_argument("--claude-bin", default=CLAUDE_BIN)
    p.add_argument("--grade", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--keep-checkouts", action="store_true")
    p.add_argument("--no-joern", action="store_true",
                   help="skip the Joern CPG build (cfg tools no-op on C -- "
                        "map-only ablation; NOT the correct static arm)")
    p.add_argument("--cpg-heap-gb", type=int, default=8,
                   help="JVM heap for c2cpg CPG builds")
    p.add_argument("--cpg-timeout", type=int, default=1800,
                   help="timeout (s) for a c2cpg build")
    p.add_argument("--joern-heap-gb", type=int, default=6,
                   help="JVM heap for the per-cell joern --server")
    a = p.parse_args(argv)
    bad = [c for c in a.conditions if c not in ALLOWED_CONDITIONS]
    if bad:
        p.error("unsupported condition(s): %s (allowed: %s)"
                % (", ".join(bad), ", ".join(sorted(ALLOWED_CONDITIONS))))

    allrows = sec_inst.load_all()
    ids = a.instance_ids
    if a.ids_file:
        ids = json.loads(Path(a.ids_file).read_text(encoding="utf-8"))
    if ids:
        want = set(ids)
        rows = [r for r in allrows if r["instance_id"] in want]
    else:
        rows = allrows[:a.n]
    results_dir = STUDY_ROOT / "results" / a.exp_name
    results_dir.mkdir(parents=True, exist_ok=True)

    from concurrent.futures import ThreadPoolExecutor
    cells = [(r, c) for r in rows for c in a.conditions]
    print("[secbench] %d instances x %d conditions = %d cells"
          % (len(rows), len(a.conditions), len(cells)))
    results = []

    def _run(rc):
        r, c = rc
        try:
            return run_cell(r, c, results_dir, a)
        except Exception as e:  # keep the batch going
            return {"instance_id": r["instance_id"], "condition": c,
                    "resolved": False, "reason": "cell_crash", "error": repr(e)}

    with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
        for i, rec in enumerate(ex.map(_run, cells)):
            results.append(rec)
            mark = "OK" if rec.get("resolved") else "x"
            print("[%d/%d] %-2s %-16s %s"
                  % (i + 1, len(cells), mark, rec.get("condition"),
                     rec["instance_id"][:40]))

    if a.grade:
        from collections import Counter
        print("\n=== resolve rate by condition ===")
        for c in a.conditions:
            cs = [r for r in results if r.get("condition") == c]
            res = sum(bool(r.get("resolved")) for r in cs)
            print("  %-16s %d/%d" % (c, res, len(cs)))
    (results_dir / "_summary.json").write_text(json.dumps(results, indent=2),
                                               "utf-8")


if __name__ == "__main__":
    main()
