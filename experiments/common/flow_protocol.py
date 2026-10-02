"""v3 ("flow3") protocol text + flowfix skill delivery for the flow arms.

Used ONLY by the *_perrep_flow3 conditions (cfg/dfg/cfgdfg x SWE-bench
repair / SecBench security). HARD RULE: v3 is a new arm,
never a rewrite -- the v2 conditions' prompt text lives untouched in the
three harnesses; this module owns all v3 prompt/skill text.

The flow3 arms keep the v2 flow-first surface (flow_of / flow_plan /
flow_check / flow_verify, no code map), add flow_locate, and tighten the
protocol:

  step 0      localization-first ordering: do NOT plan before the bug is
              visible in a graph.
  flow_of     first, on the report's named symbols, with the Grep/Read
              fallback when the flow does not show the bug.
  flow_locate the SEPARATE step 2 (wrong-path localization): declare WHERE
              the flow is wrong (file:line + one-sentence reason) BEFORE
              planning; flow_plan inherits it as each delta's fault_site.
  flow_plan   every delta carries a ``fault_site`` (file:line of the faulty
              spot), inherited from flow_locate when absent;
              v3 ops add ``add_function``.
  edit        the smallest change.
  flow_check  at most ONE re-check on mismatch (no retry loops).
  flow_verify REQUIRED LAST; on failure fix and call once more.

Skill delivery (default): ``build_skill_md()`` wraps the protocol in the
``flowfix`` skill (frontmatter + a plain-text sentinel line) which the
harness writes into the per-run claude-home at ``skills/flowfix/SKILL.md``
-- NEVER
into the worktree, where it would leak into patch.diff -- and the use
prompt becomes ``/flowfix`` + blank line + task body. ``REPR_FLOW_SKILL=0``
switches to inline mode: the same protocol text is placed above the task
body instead (no skill file, no /flowfix line). After the run the harness
greps the transcript for the sentinel and records ``skill_expanded`` in
meta.json (telemetry only; never fails the cell).
"""

from __future__ import annotations

import os
from pathlib import Path

SKILL_NAME = "flowfix"
# The skill-expansion sentinel. It MUST survive markdown/skill rendering to
# be detectable in a captured transcript/stream -- an HTML comment does NOT
# (rendering drops it), which silently made skill_expanded false-negative.
# So the sentinel is a plain, highly distinctive substring, and it is carried
# by a VISIBLE body line (SENTINEL_LINE) that the model sees and echoes: no
# HTML comment, no leading '#', not inside a code fence.
SENTINEL = "FLOWFIX-PROTOCOL-V3"
# The rendered first body line of SKILL.md. Distinctive enough not to occur in
# normal prose or a task body, and it contains SENTINEL as a stable substring.
SENTINEL_LINE = "FLOWFIX-PROTOCOL-V3 (flow-graph-first editing skill active)."
TASKS = ("repair", "security")
REPS = ("cfg", "dfg", "cfgdfg")

_FLOW_TOOLS = ("flow_of", "flow_locate", "flow_plan", "flow_check",
               "flow_verify")


def tool_names(rep: str) -> tuple:
    """The five flow3 tools' full MCP names on <rep>'s perrep server."""
    if rep not in REPS:
        raise ValueError("unknown rep %r (want one of %s)" % (rep, REPS))
    return tuple("mcp__%s_perrep__%s" % (rep, t) for t in _FLOW_TOOLS)


def skill_mode() -> bool:
    """Skill delivery is the flow3 default; REPR_FLOW_SKILL=0 -> inline."""
    return os.environ.get("REPR_FLOW_SKILL", "").strip() != "0"


def _validate(task: str, rep: str) -> None:
    if task not in TASKS:
        raise ValueError("unknown task %r (want one of %s)" % (task, TASKS))
    if rep not in REPS:
        raise ValueError("unknown rep %r (want one of %s)" % (rep, REPS))


# -- per-(task, rep) wording ---------------------------------------------------
#
# Kept deliberately close to the v2 prompts of the two harnesses (SWE-Pro
# agent.build_prompt / secbench build_prompt) so
# a v3-vs-v2 comparison isolates the PROTOCOL changes, not wording drift.
# Dispatch is on the EXACT rep string, with "cfgdfg" its own key -- never on
# substring matching ("dfg_perrep" is a substring of "cfgdfg_perrep").

_INTRO = {
    ("repair", "cfg"):
        "You fix this bug by working in the function's CONTROL-FLOW GRAPH, "
        "not by reading the whole repo. Do NOT try to rewrite the whole "
        "function -- find the ONE part of the flow that, if changed, makes "
        "the failing behavior go away, and change only that.",
    ("repair", "dfg"):
        "You fix this bug by working in the function's VALUE-FLOW (def-use) "
        "GRAPH, not by reading the whole repo. Do NOT try to rewrite the "
        "whole function -- find the ONE value relation that, if changed, "
        "makes the failing behavior go away, and change only that.",
    ("repair", "cfgdfg"):
        "You fix this bug by working in the function's TWO flow graphs -- "
        "its CONTROL-FLOW GRAPH (branches and paths) and its VALUE-FLOW "
        "(def-use) GRAPH (where values are defined and used) -- not by "
        "reading the whole repo. Do NOT try to rewrite the whole function: "
        "find the ONE wrong element in whichever graph shows it, and change "
        "only that.",
    ("security", "cfg"):
        "Fix this vulnerability by working in the crashing function's "
        "CONTROL-FLOW GRAPH, not by reading the whole repo. Find the ONE "
        "part of the flow that, if changed, stops the crash -- do not "
        "rewrite the whole function.",
    ("security", "dfg"):
        "Fix this vulnerability by working in the crashing function's "
        "VALUE-FLOW (def-use) GRAPH, not by reading the whole repo. Find "
        "the ONE value relation that, if changed, stops the crash -- do not "
        "rewrite the whole function.",
    ("security", "cfgdfg"):
        "Fix this vulnerability by working in the crashing function's TWO "
        "flow graphs -- its CONTROL-FLOW GRAPH (branches and paths) and its "
        "VALUE-FLOW (def-use) GRAPH (where sizes, lengths, indices and "
        "pointers are defined and used) -- not by reading the whole repo. "
        "Find the ONE wrong element in whichever graph shows it; do not "
        "rewrite the whole function.",
}

_GRAPH_BLURB = {
    "cfg": "It returns each function's real control-flow graph -- its basic "
           "blocks and the true/false/loop edges between them.",
    "dfg": "It returns each function's real def-use graph -- every "
           "definition of a variable and every place its value is used.",
    "cfgdfg": "It returns BOTH graphs of each function: the control-flow "
              "graph (basic blocks + true/false/loop edges) and the def-use "
              "graph (every definition and every use of each value).",
}
# security keeps the v2 C wording: values are sizes/lengths/indices/pointers.
_SEC_DFG_BLURB = ("It returns each function's real def-use graph -- every "
                  "definition of a variable (size, length, index, pointer) "
                  "and every place its value is used.")

_LOCATE = {
    ("repair", "cfg"):
        "Read the flow to locate the fix: a branch/guard that should exist "
        "but doesn't (add it), a condition that's wrong (change it), or an "
        "edge going the wrong way (reroute it).",
    ("repair", "dfg"):
        "Read the flow to locate the fix: a value used without being "
        "checked/derived first (add the def or guard-def), a def computing "
        "the wrong value (change it), or a use reading a stale/wrong "
        "definition (repoint or move it).",
    ("repair", "cfgdfg"):
        "Read the two graphs together to locate the fix: a branch/guard "
        "that should exist but doesn't (add it), a wrong condition (change "
        "it), a value used without being checked/derived first (add the def "
        "or guard), or a use reading a stale/wrong definition (repoint it).",
    ("security", "cfg"):
        "Read the flow to locate the fix: most C security fixes ADD A GUARD "
        "-- a bounds/null/overflow check (an `if (...) return/break/goto`) "
        "that dominates the crashing statement.",
    ("security", "dfg"):
        "Read the flow to locate the fix: most C security bugs are a value "
        "used UNCHECKED (a length/index/size that reaches a read/write/"
        "alloc without a bounds or null check on the path from its "
        "definition). Find that def->use edge and add the check, or repoint "
        "the use to a correct definition.",
    ("security", "cfgdfg"):
        "Read the two graphs together to locate the fix: most C security "
        "fixes ADD A GUARD -- a bounds/null/overflow check dominating the "
        "crashing statement (control-flow view) -- and the def-use view "
        "shows WHICH value (length/index/size) reaches the crash unchecked, "
        "so the guard tests the right thing.",
}

# v3 op vocabulary = the v2 set + add_function (universal: cfg, dfg, cfgdfg).
_CFG_OP_LIST = ("add_guard / remove_branch / change_condition / "
                "change_block_structure / rewire_edge")
_DFG_OP_LIST = "add_def_use / remove_def_use / move_def_use"
_OPS = {
    "cfg": "control-flow op %s; add_function for a brand-new function; or "
           "no_flow_change" % _CFG_OP_LIST,
    "dfg": "data-flow op %s; add_function for a brand-new function; or "
           "no_flow_change" % _DFG_OP_LIST,
    "cfgdfg": "control-flow: %s; data-flow: %s; add_function for a "
              "brand-new function; or no_flow_change"
              % (_CFG_OP_LIST, _DFG_OP_LIST),
}

_DESCRIPTION = {
    # one line each, and no ": " inside (a plain YAML scalar must not carry
    # a colon-space -- it would break the frontmatter parse).
    "repair": "Flow-graph-first bug fixing -- get the bug in a graph, plan "
              "the flow delta with its fault site, edit small, verify last.",
    "security": "Flow-graph-first security patching -- crashing function's "
                "graph in view, plan the guard, edit small, verify last.",
}


def build_steps(task: str, rep: str, tools) -> str:
    """The v3 protocol text for one (task, rep) cell.

    ``tools`` is the 5-tuple (flow_of, flow_locate, flow_plan, flow_check,
    flow_verify) full MCP tool names (see tool_names()). Returns the
    numbered-step text WITHOUT the sentinel (the sentinel belongs to the
    skill wrapper only, so inline mode can never fake a positive
    skill_expanded)."""
    _validate(task, rep)
    tools = tuple(tools)
    if len(tools) < 5:
        raise ValueError(
            "tools must be (flow_of, flow_locate, flow_plan, flow_check, "
            "flow_verify)")
    of, locate, plan, check, verify = tools[:5]
    both = rep == "cfgdfg"
    surface = ("these flows are your surface" if both
               else "this flow is your surface")
    in_flow = "in its flows" if both else "in its flow"
    blurb = (_SEC_DFG_BLURB if (task == "security" and rep == "dfg")
             else _GRAPH_BLURB[rep])
    named = ("the function/symbol names from the sanitizer report / "
             "vulnerability description" if task == "security"
             else "the function/symbol names the bug report mentions")

    step0 = ("0. ORDER OF WORK -- localize first, plan second: see the "
             "graph, find the bug IN the graph, and only then commit a "
             "plan. Do NOT call flow_plan until you can see the bug in "
             "a graph.")

    step1 = ("1. (REQUIRED FIRST) Call `%s` with %s. %s There is no "
             "explore/map tool; %s." % (of, named, blurb, surface))

    graph_word = "graphs" if both else "graph"
    if task == "security":
        step2 = ("2. (REQUIRED) Call `%s` with the crash-path spot that "
                 "needs the guard -- the file:line the %s and the "
                 "sanitizer frames point at -- and one sentence on why the "
                 "flow is wrong there (reason=...). It validates the spot "
                 "against the current %s and records it; your flow_plan "
                 "deltas inherit it as their fault_site unless a delta "
                 "gives its own." % (locate, graph_word, graph_word))
    else:
        step2 = ("2. (REQUIRED) Call `%s` with the faulty spot the %s "
                 "shows (site='file.py:LINE') and one sentence on why the "
                 "flow is wrong there (reason=...). It validates the spot "
                 "against the current %s and records it; your flow_plan "
                 "deltas inherit it as their fault_site unless a delta "
                 "gives its own." % (locate, graph_word, graph_word))

    if task == "security":
        step3 = ("3. IF THE BUG IS NOT IN THE FLOW, LOCALIZE FIRST: the "
                 "report may not name the faulty function, a name may not "
                 "resolve, or the graphs you got back may not contain the "
                 "faulty region. Do NOT force a fix into a flow that does "
                 "not show the bug. Fall back to your own tools -- Grep for "
                 "the crash frames / suspect identifiers, Read the "
                 "candidate files -- until you find the faulty function, "
                 "then call `%s` on THAT function and work %s."
                 % (of, in_flow))
    else:
        step3 = ("3. IF THE BUG IS NOT IN THE FLOW, LOCALIZE FIRST: the "
                 "report may name no function, a name may not resolve, or "
                 "the graphs you got back may not contain the faulty "
                 "region. Do NOT force a fix into a flow that does not show "
                 "the bug. Fall back to your own tools -- Grep for the "
                 "error message / suspect identifiers, Read the candidate "
                 "files -- until you find the faulty function, then call "
                 "`%s` on THAT function and work %s." % (of, in_flow))

    step4 = ("4. %s Then call `%s` to record the change (%s; plus "
             "function + anchor + rationale). Plan EVERY function you "
             "intend to touch in this ONE call; if you later revise the "
             "plan, calling `%s` again replaces only the deltas of the "
             "functions you re-plan. In EVERY delta include `fault_site`"
             " -- the file:line of the exact faulty spot the graph "
             "shows (a delta without one inherits the spot you declared "
             "with `flow_locate`); for a no_flow_change delta still "
             "give the file:line plus a one-line reason why no flow "
             "changes." % (_LOCATE[(task, rep)], plan, _OPS[rep], plan))

    if task == "security":
        step5 = ("5. Make the SMALL C edit at that spot (Read the file for "
                 "the exact lines, then Edit). Do not restructure code the "
                 "flow didn't point you to.")
    else:
        step5 = ("5. Make the SMALL code edit at that spot (open the file "
                 "with Read to get the exact lines, then Edit). Do not "
                 "restructure code the flow didn't point you to.")

    step6 = ("6. Call `%s` to confirm your edit made the flow change "
             "you planned. If it reports a mismatch, fix the edit and "
             "re-check ONCE -- do not loop." % check)

    if task == "security":
        step7 = ("7. (REQUIRED LAST) When you believe the fix is complete "
                 "and BEFORE you stop, call `%s` (no arguments). It "
                 "re-checks EVERY planned delta against the FINAL code and "
                 "lists any source edits made OUTSIDE the plan. If it "
                 "reports a failure, fix that and call it once more. A pass "
                 "is necessary but not sufficient -- make sure the guard "
                 "actually covers the bad input, then stop with a non-empty "
                 "patch." % verify)
    else:
        step7 = ("7. (REQUIRED LAST) When you believe the fix is complete "
                 "and BEFORE you stop, call `%s` (no arguments). It "
                 "re-checks EVERY planned delta against the FINAL code and "
                 "lists any source edits you made OUTSIDE the plan. If it "
                 "reports a failure, fix that and call it once more. A pass "
                 "is necessary but not sufficient -- make sure the change "
                 "actually addresses the bug, then stop with a non-empty "
                 "patch." % verify)

    return "\n".join([
        "## Work IN THE FLOW (there is no code map)",
        _INTRO[(task, rep)],
        step0, step1, step2, step3, step4, step5, step6, step7,
    ])


def build_skill_md(task: str, rep: str, tools) -> str:
    """The flowfix SKILL.md: frontmatter (name + one-line description), then
    the plain-text sentinel line as the body's first (visible, rendered)
    line, then the protocol. The sentinel is a visible line -- not an HTML
    comment -- so it survives markdown/skill rendering into the transcript,
    which is what skill_expanded greps for."""
    _validate(task, rep)
    return "\n".join([
        "---",
        "name: %s" % SKILL_NAME,
        "description: %s" % _DESCRIPTION[task],
        "---",
        SENTINEL_LINE,
        build_steps(task, rep, tools),
        "",
    ])


def build_user_prompt(task: str, rep: str, tools, task_body: str,
                      agent: str = "claude") -> str:
    """The flow3 user prompt around ``task_body`` (the benchmark's own task
    text, WITHOUT any protocol steps). Skill mode (default): first line
    is the agent-native invocation (``/flowfix`` for Claude Code,
    ``$flowfix`` for Codex), followed by a blank line and task body; the
    protocol arrives via the skill.
    REPR_FLOW_SKILL=0: the protocol text inlined above the task body."""
    _validate(task, rep)
    if agent not in ("claude", "codex"):
        raise ValueError("unknown agent %r (want claude or codex)" % agent)
    if skill_mode():
        marker = "$" if agent == "codex" else "/"
        return "%s%s\n\n%s" % (marker, SKILL_NAME, task_body)
    return "%s\n\n%s" % (build_steps(task, rep, tools), task_body)


def write_skill(claude_home, skill_md: str) -> Path:
    """Write SKILL.md into the per-run claude-home (skills/flowfix/). The
    caller must pass the claude-home, NEVER the worktree -- a worktree file
    would leak into the harvested patch.diff."""
    path = Path(claude_home) / "skills" / SKILL_NAME / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(skill_md, encoding="utf-8")
    return path


def skill_expanded(transcript_path, sentinel: str = SENTINEL) -> bool:
    """Did the /flowfix skill actually expand into the conversation? True iff
    the transcript carries the sentinel (which lives ONLY in SKILL.md -- the
    user prompt never contains it, in either mode). Telemetry only; any read
    problem reports False rather than raising."""
    p = Path(transcript_path)
    try:
        return p.is_file() and sentinel in p.read_text(encoding="utf-8",
                                                       errors="replace")
    except OSError:
        return False
