"""Run SecBench (patch task) arms on the leak-free harness.

Supports `baseline` and `cfg_perrep` / `cfg_perrep_dyn` (the dynamic arm's
signal is the sanitizer crash stack, see trace.py). Reuses the swebench_pro
agent runner for the Claude Code loop; SecBench-specific pieces are the
checkout (git-history isolated), the crash-stack trace, the prompt, and
grading via the official `secb patch|build|repro` in the vendor image.

    python -m experiments.secbench.run_experiment \
        --conditions baseline cfg_perrep_dyn --n 10 --exp-name sec_pilot --grade
"""

import argparse
import json
import os
from pathlib import Path

import re

import hashlib

from experiments.secbench import agent as sec_agent
from experiments.secbench import checkout as sec_checkout
from experiments.secbench import instances as sec_inst
from experiments.secbench import joern as sec_joern
from experiments.secbench import trace as sec_trace
from experiments.swebench_pro import agent as swe_agent

STUDY_ROOT = Path(__file__).resolve().parents[2]
VENV_PYTHON = STUDY_ROOT / ".venv" / "bin" / "python"
# "cfgdfg" is the COMBINED flow arm (flow-first only; both graphs in flow_of).
PERREP_REPS = ("cfg", "dfg", "callgraph", "cfgdfg")
# the dyn (execution-overlay) tool each rep exposes as its Step-1 nudge target.
_DYN_NUDGE_TOOL = {"cfg": "crash_path", "dfg": "crash_path",
                   "callgraph": "crash_callpath"}
# The NATIVE LINUX claude. Bare "claude" in WSL resolves to a Windows shim
# (/mnt/c/.../npm/claude) that runs claude.exe -> the agent sees the checkout
# as a \\wsl.localhost\... UNC path and CANNOT run the Linux --settings hook o
# reach the Linux MCP server -> 0 gate denials, 0 MCP calls (the SecBench
# adoption bug). SWE-Pro uses this same Linux binary.
CLAUDE_BIN = str(Path.home() / ".local" / "bin" / "claude")


def is_baseline_dyn(cond: str) -> bool:
    """The no-representation dynamic control: the crash-stack tools ONLY,
    no AST/CFG/callgraph map. Isolates the dynamic signal from any rep."""
    return cond == "baseline_dyn"


def condition_rep(cond: str) -> str | None:
    c = cond
    # strip modifier suffixes: _flowplan_dyn is the combined arm (plan/verify
    # + crash-overlay localization). _flow3 (the v3 arm) is checked BEFORE
    # _flow: '_perrep_flow' is a PREFIX of '_perrep_flow3'.
    for suffix in ("_flowplan_dyn", "_dyn", "_flowplan", "_flowonly",
                   "_flow3", "_flow", "_nlplan"):
        if c.endswith("_perrep" + suffix):
            c = c[:-len(suffix)]
            break
    if c.endswith("_perrep"):
        rep = c[:-len("_perrep")]
        if rep in PERREP_REPS:
            return rep
    return None


def condition_dyn(cond: str) -> bool:
    """True for any arm carrying the crash-overlay (dyn) tools: the pure dyn
    arm and the combined flowplan+dyn arm."""
    return ((cond.endswith("_perrep_dyn") or cond.endswith("_perrep_flowplan_dyn"))
            and condition_rep(cond) is not None)


def condition_flowplan(cond: str) -> bool:
    """Graph-mediated-editing arm on SecBench (C flow_plan + flow_check, the
    fingerprint is Joern-backed). cfg only. Covers the base flowplan arm AND
    the combined flowplan+dyn arm."""
    return ((cond.endswith("_perrep_flowplan")
             or cond.endswith("_perrep_flowplan_dyn"))
            and condition_rep(cond) == "cfg")


def condition_flow_first(cond: str) -> bool:
    """FLOW-FIRST arm on SecBench: the agent works IN the flow graph (flow_of +
    flow_plan + flow_check), with NO code map. '<rep>_perrep_flow'. cfg =
    control-flow (blocks/edges), dfg = data-flow (def-use); both answer C via
    the per-instance Joern server the setup already boots."""
    return (cond.endswith("_perrep_flow")
            and condition_rep(cond) in ("cfg", "dfg", "cfgdfg"))


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
    """RQ2 LOWER-BOUND control on SecBench, MAP-FREE: '<rep>_perrep_flowonly'.

    Map-free flow_of ONLY (Joern-backed on C) -- NO forced plan, NO verify.
    The prompt just says 'see the flow, find the spot, edit'. Isolates whethe
    the flow VIEW alone helps on security patching, separate from any plan/
    verify discipline. flow_plan/flow_check are NOT registered."""
    return (cond.endswith("_perrep_flowonly")
            and condition_rep(cond) in ("cfg", "dfg"))


def _pick_port(iid: str, condition: str) -> int:
    """A deterministic, per-(instance,condition) Joern port in [8800, 9800)."""
    h = int(hashlib.sha1((iid + condition).encode()).hexdigest()[:6], 16)
    return 8800 + (h % 1000)


def write_mcp_config(rep: str, wt: Path, run_dir: Path, dyn: bool,
                     joern_port: int | None, flowplan: bool = False,
                     flow_first: bool = False,
                     flow_of_only: bool = False,
                     flow_v3: bool = False) -> Path:
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
            "REPR_TRACE": "1" if dyn else "",
            "REPR_TRACE_FILE": str(run_dir / "trace.json") if dyn else "",
            "REPR_TRACE_KIND": "sanitizer" if dyn else "",
            "JOERN_SERVER_URL": joern_url,
            "JOERN_USER": "repr" if joern_url else "",
            "JOERN_PASSWORD": "repr-study" if joern_url else "",
            # graph-mediated editing on C: flow_plan + flow_check, with a
            # Joern-backed (c2cpg batch) CFG fingerprint for the C round-trip.
            "REPR_FLOWEDIT": "1" if flowplan else "",
            # flow-first: expose ONLY flow_of/flow_plan/flow_check (no map).
            # flow3 reuses the same registration.
            "REPR_FLOW_FIRST": "1" if (flow_first or flow_v3) else "",
            # RQ2 flow_of-only control: map-free, expose ONLY flow_of (no
            # flow_plan/flow_check) -- no plan, no verify.
            "REPR_FLOW_OF_ONLY": "1" if flow_of_only else "",
            # flow3 (v3) arm: v3 tool semantics (add_function op, fault_site,
            # HEAD snapshots, merge-on-replan, graded verdicts, replace arg
            # on flow_plan). Default off -> exact v2 behavior.
            "REPR_FLOW_V3": "1" if flow_v3 else "",
            # agent-facing verification: PRE-FIX behavior (Haiku-v2 parity);
            # see swebench_pro.write_mcp_config for rationale. flow3 does NOT
            # pin legacy verify (the v3 verifier IS the arm); v2 keeps it.
            "REPR_FLOW_LEGACY_VERIFY": "" if flow_v3 else "1",
        },
    }}}
    p = run_dir / "mcp.json"
    p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return p


def write_dynonly_config(wt: Path, run_dir: Path) -> Path:
    """mcp.json for baseline_dyn: the dynonly server (crash tools only)."""
    cfg = {"mcpServers": {"dynonly": {
        "command": str(VENV_PYTHON), "args": ["-m", "servers.dynonly_server"],
        "env": {
            "REPO_ROOT": str(wt),
            "REPO_INDEX_DIR": str(wt / ".repr_index"),
            "RUN_DIR": str(run_dir),
            "PYTHONPATH": str(STUDY_ROOT),
            "REPR_TRACE": "1",
            "REPR_TRACE_FILE": str(run_dir / "trace.json"),
            "REPR_TRACE_KIND": "sanitizer",
        },
    }}}
    p = run_dir / "mcp.json"
    p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return p


def _redact_frame_locations(report: str) -> str:
    """Strip the ``/path/file.c:line`` from each stack frame, keeping the
    function name + sanitizer summary. Forces the dyn agent to call crash_path
    for the executed locations instead of reading them off the prompt."""
    # "#3 0x.. in func /src/x/file.c:123:5" -> "#3 in func <location via crash_path>"
    out = re.sub(
        r"(#\d+\s+0x[0-9a-f]+\s+in\s+\S+)\s+/\S+?:\d+(?::\d+)?",
        r"\1 <location via crash_path>", report)
    # also the SUMMARY line's file:line
    out = re.sub(r"(SUMMARY:.*?)\s+/\S+?:\d+(?::\d+)?", r"\1", out)
    return out


def build_prompt(inst: dict, explore_tool=None, dyn_tool=None,
                 flow_tools=None, flow_first_tools=None,
                 flow_only_tool=None) -> str:
    parts = ["# Security patch task", ""]
    if flow_only_tool:
        # RQ2 lower-bound control (flowonly): map-free flow_of ONLY, no plan,
        # no verify -- "see the crashing function's flow, find the spot, patch".
        parts += [
            "## Work IN THE FLOW (there is no code map)",
            "Fix this vulnerability by working in the crashing function's "
            "flow, not by reading the whole repo. Find the ONE part of the "
            "flow that, if changed, stops the crash -- do not rewrite the "
            "whole function.",
            "1. (REQUIRED FIRST) Call `%s` with the function/symbol names from "
            "the sanitizer report / vulnerability description. It returns each "
            "function's real flow graph. There is no explore/map tool; this "
            "flow is your surface." % flow_only_tool,
            "2. Read the flow to find where the fix goes (most C security "
            "fixes add a bounds/null/overflow check), then make the SMALL C "
            "edit there (Read the file for the exact lines, then Edit). Do not "
            "restructure code the flow didn't point you to. End with a "
            "non-empty patch.",
            "",
        ]
    if flow_first_tools:
        of_tool, plan_tool, check_tool = flow_first_tools[:3]
        verify_tool = (flow_first_tools[3]
                       if len(flow_first_tools) > 3 else None)
        # combined arm first -- "dfg_perrep" is a substring of "cfgdfg_perrep"
        if "cfgdfg_perrep" in of_tool:
            parts += [
                "## Work IN THE FLOW (there is no code map)",
                "Fix this vulnerability by working in the crashing function's "
                "TWO flow graphs -- its CONTROL-FLOW GRAPH (branches and "
                "paths) and its VALUE-FLOW (def-use) GRAPH (where sizes, "
                "lengths, indices and pointers are defined and used) -- not "
                "by reading the whole repo. Find the ONE wrong element in "
                "whichever graph shows it; do not rewrite the whole function.",
                "1. (REQUIRED FIRST) Call `%s` with the function/symbol names "
                "from the sanitizer report / vulnerability description. It "
                "returns BOTH graphs of each function: the control-flow "
                "graph (basic blocks + true/false/loop edges) and the "
                "def-use graph (every definition and every use of each "
                "value). There is no explore/map tool; these flows are your "
                "surface." % of_tool,
                "2. IF THE BUG IS NOT IN THE FLOW, LOCALIZE FIRST: the report "
                "may not name the faulty function, a name may not resolve, "
                "or neither graph may contain the faulty region. Do NOT "
                "force a fix into a flow that does not show the bug. Fall "
                "back to your own tools -- Grep for the crash frames / "
                "suspect identifiers, Read the candidate files -- until you "
                "find the faulty function, then call `%s` on THAT function "
                "and work in its flows." % of_tool,
                "3. Read the two graphs together to locate the fix: most C "
                "security fixes ADD A GUARD -- a bounds/null/overflow check "
                "dominating the crashing statement (control-flow view) -- "
                "and the def-use view shows WHICH value (length/index/size) "
                "reaches the crash unchecked, so the guard tests the right "
                "thing. Call `%s` to record the change, picking the op from "
                "the graph where the bug lives (control-flow: add_guard / "
                "remove_branch / change_condition / rewire_edge; data-flow: "
                "add_def_use / remove_def_use / move_def_use; or "
                "no_flow_change). Plan EVERY function you intend to touch in "
                "this one call." % plan_tool,
                "4. Make the SMALL C edit at that spot (Read the file for "
                "the exact lines, then Edit). Do not restructure code the "
                "flows didn't point you to.",
                "5. Call `%s` to confirm your edit made the flow change you "
                "planned (it checks the graph your op belongs to). If "
                "match=false, fix and re-check once." % check_tool,
            ] + ([
                "6. (REQUIRED LAST) When you believe the fix is complete and "
                "BEFORE you stop, call `%s` (no arguments). It re-checks "
                "EVERY planned delta against the FINAL code and lists any "
                "source edits made OUTSIDE the plan. If a delta reports "
                "match=false or an unplanned edit was accidental, fix that "
                "and call it once more. A pass is necessary but not "
                "sufficient -- make sure the guard actually covers the bad "
                "input, then stop with a non-empty patch." % verify_tool,
                "",
            ] if verify_tool else [
                "A match is necessary but not sufficient -- make sure the "
                "guard actually covers the bad input, then stop with a "
                "non-empty patch.",
                "",
            ])
        elif "dfg_perrep" in of_tool:
            parts += [
                "## Work IN THE FLOW (there is no code map)",
                "Fix this vulnerability by working in the crashing function's "
                "VALUE-FLOW (def-use) GRAPH, not by reading the whole repo. "
                "Find the ONE value relation that, if changed, stops the "
                "crash -- do not rewrite the whole function.",
                "1. (REQUIRED FIRST) Call `%s` with the function/symbol names "
                "from the sanitizer report / vulnerability description. It "
                "returns each function's real def-use graph -- every "
                "definition of a variable (size, length, index, pointer) and "
                "every place its value is used. There is no explore/map tool; "
                "this flow is your surface." % of_tool,
                "2. IF THE BUG IS NOT IN THE FLOW, LOCALIZE FIRST: the report "
                "may not name the faulty function, a name may not resolve, or "
                "the graphs you got back may not contain the faulty value "
                "relation. Do NOT force a fix into a flow that does not show "
                "the bug. Fall back to your own tools -- Grep for the crash "
                "frames / suspect identifiers, Read the candidate files -- "
                "until you find the faulty function, then call `%s` on THAT "
                "function and work in its flow." % of_tool,
                "3. Read the flow to locate the fix: most C security bugs are "
                "a value used UNCHECKED (a length/index/size that reaches a "
                "read/write/alloc without a bounds or null check on the path "
                "from its definition). Find that def->use edge and add the "
                "check, or repoint the use to a correct definition. Call `%s` "
                "to record that one data-flow change (op add_def_use / "
                "remove_def_use / move_def_use + function + anchor + "
                "rationale). Plan EVERY function you intend to touch in this "
                "one call." % plan_tool,
                "4. Make the SMALL C edit at that def/use site (Read the file "
                "for the exact lines, then Edit). Do not restructure code the "
                "flow didn't point you to.",
                "5. Call `%s` to confirm your edit made the planned data-flow "
                "change. If match=false, fix and re-check once." % check_tool,
            ] + ([
                "6. (REQUIRED LAST) When you believe the fix is complete and "
                "BEFORE you stop, call `%s` (no arguments). It re-checks "
                "EVERY planned delta against the FINAL code and lists any "
                "source edits made OUTSIDE the plan. If a delta reports "
                "match=false or an unplanned edit was accidental, fix that "
                "and call it once more. A pass is necessary but not "
                "sufficient -- make sure the check actually covers the bad "
                "value, then stop with a non-empty patch." % verify_tool,
                "",
            ] if verify_tool else [
                "A match is necessary but not sufficient -- make sure the "
                "check actually covers the bad value, then stop with a "
                "non-empty patch.",
                "",
            ])
        else:
            parts += [
                "## Work IN THE FLOW (there is no code map)",
                "Fix this vulnerability by working in the crashing function's "
                "CONTROL-FLOW GRAPH, not by reading the whole repo. Find the ONE "
                "part of the flow that, if changed, stops the crash -- do not "
                "rewrite the whole function.",
                "1. (REQUIRED FIRST) Call `%s` with the function/symbol names from "
                "the sanitizer report / vulnerability description. It returns each "
                "function's real control-flow graph (basic blocks + true/false/loop "
                "edges). There is no explore/map tool; this flow is your surface."
                % of_tool,
                "2. IF THE BUG IS NOT IN THE FLOW, LOCALIZE FIRST: the report "
                "may not name the faulty function, a name may not resolve, or "
                "the graphs you got back may not contain the faulty region. "
                "Do NOT force a fix into a flow that does not show the bug. "
                "Fall back to your own tools -- Grep for the crash frames / "
                "suspect identifiers, Read the candidate files -- until you "
                "find the faulty function, then call `%s` on THAT function "
                "and work in its flow." % of_tool,
                "3. Read the flow to locate the fix: most C security fixes ADD A "
                "GUARD -- a bounds/null/overflow check (an `if (...) "
                "return/break/goto`) that dominates the crashing statement. Call "
                "`%s` to record that one control-flow change (op + function + "
                "anchor + rationale). Plan EVERY function you intend to touch "
                "in this one call." % plan_tool,
                "4. Make the SMALL C edit at that flow locus (Read the file for the "
                "exact lines, then Edit). Do not restructure code the flow didn't "
                "point you to.",
                "5. Call `%s` to confirm your edit made the planned control-flow "
                "change. If match=false, fix and re-check once." % check_tool,
            ] + ([
                "6. (REQUIRED LAST) When you believe the fix is complete and "
                "BEFORE you stop, call `%s` (no arguments). It re-checks "
                "EVERY planned delta against the FINAL code and lists any "
                "source edits made OUTSIDE the plan. If a delta reports "
                "match=false or an unplanned edit was accidental, fix that "
                "and call it once more. A pass is necessary but not "
                "sufficient -- make sure the guard actually covers the bad "
                "input, then stop with a non-empty patch." % verify_tool,
                "",
            ] if verify_tool else [
                "A match is necessary but not sufficient -- make sure the "
                "guard actually covers the bad input, then stop with a "
                "non-empty patch.",
                "",
            ])
    if explore_tool:
        parts += [
            "## Step 0 (localize with the code map)",
            "Before grepping/reading broadly, call `%s` with terms from the "
            "vulnerability (crashing function names, the sanitizer kind) to "
            "find the relevant C source in one call." % explore_tool, ""]
    if dyn_tool:
        parts += [
            "## Step 1 (REQUIRED before editing): read the recorded crash path",
            "The sanitizer's crash was recorded for this exact code. Call "
            "`%s` to see the ordered stack the crashing input actually "
            "executed (innermost frame = the crashing statement) plus any "
            "allocation site. The fix almost always lies on this path or at "
            "the allocation site — patch there, not at a lexically similar "
            "but off-path location." % dyn_tool, ""]
    if flow_tools:
        plan_tool, check_tool = flow_tools[0], flow_tools[1]
        parts += [
            "## Step 2 (REQUIRED): plan the control-flow change, THEN edit, "
            "THEN verify",
            "Most C security fixes ADD A GUARD -- a bounds/null/overflow check "
            "(an `if (...) return/break/goto`) that dominates the crashing "
            "statement so the bad path is no longer taken. Fix this by first "
            "deciding how the crashing function's control flow must change, "
            "then projecting it to code:",
            "1. Call `%s` ONCE with the delta your fix makes to the crashing "
            "function's control-flow graph (usually op 'add_guard'; also "
            "remove_branch, change_condition, rewire_edge; or no_flow_change "
            "for a pure expression fix). Plan on the function ON THE CRASH PATH "
            "(the innermost frame or allocation site%s), NOT a lexically "
            "similar function elsewhere. Give the exact function name, the "
            "anchor file:line on the crash path, and a one-sentence rationale "
            "tying it to the sanitizer report."
            % (plan_tool, " you saw in Step 1" if dyn_tool else ""),
            "2. Make the C edit that realizes that plan.",
            "3. Call `%s` on the function you edited. It rebuilds that "
            "function's control-flow graph from your edited C source (via "
            "Joern) and tells you whether the realized delta MATCHES your plan. "
            "If match=false, your edit did not add the guard you intended -- "
            "reconsider and fix it, then re-check once." % check_tool,
            "IMPORTANT: match=true only confirms your edit made the graph "
            "change you PLANNED -- it does NOT prove the vulnerability is "
            "fixed. A verified guard on the wrong function, or a guard that is "
            "necessary but insufficient, will still crash. After match=true, "
            "re-read the crash path and confirm your guard actually dominates "
            "the crashing statement and covers the bad input; only then stop.",
            "",
        ]
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
    mins = _token_minutes() if args.agent == "claude" else None
    if mins is not None and mins < 3:
        print("[sec] TOKEN EXPIRED/EXPIRING (%.0f min) -- skipping %s__%s "
              "without artifacts; re-login and resume" % (mins, iid, condition))
        return {"instance_id": iid, "condition": condition,
                "resolved": False, "_token_skipped": True}
    run_dir.mkdir(parents=True, exist_ok=True)

    rep = condition_rep(condition)
    dyn = condition_dyn(condition)
    flowplan = condition_flowplan(condition)
    flow_first = condition_flow_first(condition)
    flowonly = condition_flowonly(condition)
    flow_v3 = condition_flow_v3(condition)
    # flowonly shares the flow-first MAP-FREE + CPG setup (no rep map; symbol
    # index + Joern CPG so flow_of resolves C funcs); it just exposes flow_of
    # ONLY and its prompt forces no plan/verify. flow3 shares the same
    # map-free + CPG setup (its extras ride on env REPR_FLOW_V3).
    map_free = flow_first or flowonly or flow_v3
    bdyn = is_baseline_dyn(condition)
    joern_port = _pick_port(iid, condition)
    try:
        wt = sec_checkout.prepare_checkout(inst, suffix=condition)
        chk = sec_checkout.verify_no_oracle(inst, wt)
        (run_dir / "oracle_check.json").write_text(json.dumps(chk), "utf-8")
        if not chk["clean"]:
            raise RuntimeError("oracle leak: %s" % chk)
        mcp_config = None
        cpg_path = None
        if bdyn:
            # baseline_dyn: crash-stack tools ONLY, no representation map.
            # Build callgraph.db (cheap, no Joern) so crash_callpath can
            # annotate edges; collect the trace; mount the dynonly server.
            from indexer.backends import get_backend
            (wt / ".repr_index").mkdir(exist_ok=True)
            os.environ.setdefault("REPR_CALLGRAPH_FAST", "1")
            get_backend("symbols").build(wt, wt / ".repr_index")
            get_backend("callgraph").build(wt, wt / ".repr_index")
            t = sec_trace.collect_trace(inst, run_dir / "trace.json",
                                        repo_root=wt)
            (run_dir / "trace_status.json").write_text(
                json.dumps({k: t.get(k) for k in
                            ("ok", "reason", "sanitizer",
                             "n_crash_in_repo")}), "utf-8")
            mcp_config = write_dynonly_config(wt, run_dir)
        elif rep is not None:
            from indexer.backends import get_backend
            (wt / ".repr_index").mkdir(exist_ok=True)
            get_backend("symbols").build(wt, wt / ".repr_index")
            # callgraph's crash_callpath reads .repr_index/callgraph.db (the
            # raw call graph), so build that backend for the callgraph rep.
            if rep == "callgraph":
                os.environ.setdefault("REPR_CALLGRAPH_FAST", "1")
                get_backend("callgraph").build(wt, wt / ".repr_index")
            # flow-first / flowonly: NO rep map (the flow is the surface).
            # Symbol index + CPG below are still built so flow_of can resolve
            # the report's symbols and render their C CFGs.
            if not map_free:
                # repository-map arms only (not part of this release: the
                # flow arms never build a map)
                from perrep import build as perrep_build
                perrep_build.build_rep(wt, rep, repr_index=wt / ".repr_index")
            # Build the Joern CPG (scoped to the crash-stack dirs) so the C/C++
            # control-flow tools (get_cfg/cfg_paths/dominators) and the block
            # overlay (executed_path/covered_blocks) actually work -- the
            # pure-Python engine cannot parse C. Skipped only if --no-joern.
            if not getattr(args, "no_joern", False):
                cpg_path = wt / ".repr_index" / "cpg" / "inst.cpg.bin"
                crec = sec_joern.build_cpg(inst, wt, cpg_path,
                                           heap_gb=args.cpg_heap_gb,
                                           timeout=args.cpg_timeout)
                (run_dir / "cpg_build.json").write_text(json.dumps(crec),
                                                        "utf-8")
                if not crec.get("ok"):
                    cpg_path = None  # tools fall back to notes; run continues
            if dyn:
                t = sec_trace.collect_trace(inst, run_dir / "trace.json",
                                            repo_root=wt)
                (run_dir / "trace_status.json").write_text(
                    json.dumps({k: t.get(k) for k in
                                ("ok", "reason", "sanitizer",
                                 "n_crash_in_repo")}), "utf-8")
            mcp_config = write_mcp_config(rep, wt, run_dir, dyn,
                                          joern_port if cpg_path else None,
                                          flowplan=flowplan,
                                          flow_first=flow_first,
                                          flow_of_only=flowonly,
                                          flow_v3=flow_v3)
    except Exception as e:
        (run_dir / "setup_error.txt").write_text(repr(e), "utf-8")
        return {"instance_id": iid, "condition": condition, "resolved": False,
                "score": 0.0, "reason": "setup_failed", "error": repr(e)}

    # flow-first / flowonly have NO explore/map tool -> no explore nudge, gate.
    explore_tool = (("mcp__%s_perrep__%s_explore" % (rep, rep))
                    if rep and not map_free else None)
    dyn_tool = ("mcp__%s_perrep__%s" % (rep, _DYN_NUDGE_TOOL.get(rep,
                "crash_path"))) if dyn else None
    gate_settings = None
    if rep is not None and not map_free and not args.no_explore_gate:
        gate_settings = swe_agent.write_gate_settings(
            run_dir, rep, explore_tool, worktree=wt)
    if bdyn:
        # baseline_dyn: no map to explore-gate; just nudge to crash_path.
        dyn_tool = "mcp__dynonly__crash_path"
    flow_tools = None
    if flowplan and rep is not None:
        flow_tools = ("mcp__%s_perrep__flow_plan" % rep,
                      "mcp__%s_perrep__flow_check" % rep)
    flow_first_tools = None
    if flow_first and rep is not None:
        flow_first_tools = ("mcp__%s_perrep__flow_of" % rep,
                            "mcp__%s_perrep__flow_plan" % rep,
                            "mcp__%s_perrep__flow_check" % rep,
                            "mcp__%s_perrep__flow_verify" % rep)
    flow_only_tool = None
    if flowonly and rep is not None:
        flow_only_tool = "mcp__%s_perrep__flow_of" % rep
    prompt = build_prompt(inst, explore_tool=explore_tool, dyn_tool=dyn_tool,
                          flow_tools=flow_tools,
                          flow_first_tools=flow_first_tools,
                          flow_only_tool=flow_only_tool)

    # flow3: the v2 task body (no v2 flow steps -- flow_first_tools is None
    # for flow3 cells) wrapped by the v3 protocol: "/flowfix" + skill in the
    # per-run claude-home (default), or inlined per REPR_FLOW_SKILL=0.
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
        if args.agent == "claude":
            sec_agent.run_agent(
                inst, run_dir, claude_bin=args.claude_bin, worktree=wt,
                model=args.model, effort=args.effort_flag,
                max_turns=args.max_turns,
                max_budget_usd=args.max_budget_usd,
                timeout_sec=args.timeout_sec, prompt=prompt,
                mcp_config=mcp_config,
                restrict_tools=(mcp_config is not None),
                settings_file=gate_settings, skill_md=flow3_skill_md,
                sentinel=flow3_sentinel)
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
        # measurement: record the FINAL plan-vs-code verdict for flow cells.
        # Runs HERE (not after) so the per-cell Joern server is still up --
        # the C dfg fingerprint needs the live CPG.
        if flow_first and rep is not None:
            from experiments import flowverify
            joern_env = {}
            if cpg_path is not None:
                joern_env = {"JOERN_SERVER_URL":
                             "http://127.0.0.1:%d" % joern_port,
                             "JOERN_USER": "repr",
                             "JOERN_PASSWORD": "repr-study"}
            flowverify.record_final_verify(VENV_PYTHON, STUDY_ROOT, rep, wt,
                                           run_dir, extra_env=joern_env)
        # flow3: same record, with extra_env gaining REPR_FLOW_V3 (and no
        # legacy pin -- record_final_verify only forwards extra_env).
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
        from experiments.linuxflbench.joern_server import JoernServer
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
    p.add_argument("--agent", choices=["claude", "codex"], default="claude")
    p.add_argument("--model", default=None)
    p.add_argument("--effort-flag", default="low")
    p.add_argument("--max-turns", type=int, default=40)
    p.add_argument("--max-budget-usd", type=float, default=1.5)
    p.add_argument("--timeout-sec", type=int, default=1500)
    p.add_argument("--claude-bin", default=CLAUDE_BIN)
    p.add_argument("--codex-bin", default=os.environ.get("REPR_CODEX_BIN",
                                                         "codex"))
    p.add_argument("--codex-auth-file", default=None)
    p.add_argument("--codex-auth-mode", choices=["auto", "chatgpt", "api-key"],
                   default="auto")
    p.add_argument("--grade", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--keep-checkouts", action="store_true")
    p.add_argument("--no-explore-gate", action="store_true",
                   help="disable the explore-gate adoption lever (ablation)")
    p.add_argument("--no-joern", action="store_true",
                   help="skip the Joern CPG build (cfg tools no-op on C -- "
                        "map-only ablation; NOT the correct static/dyn arm)")
    p.add_argument("--cpg-heap-gb", type=int, default=8,
                   help="JVM heap for c2cpg CPG builds")
    p.add_argument("--cpg-timeout", type=int, default=1800,
                   help="timeout (s) for a c2cpg build")
    p.add_argument("--joern-heap-gb", type=int, default=6,
                   help="JVM heap for the per-cell joern --server")
    a = p.parse_args(argv)
    if a.model is None:
        a.model = ("gpt-5.4-mini" if a.agent == "codex"
                   else "claude-haiku-4-5")

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
