"""Test-generation prompts for the SWT-bench arm.

The agent must WRITE A TEST that reproduces the bug -- it fails on the current
(buggy) code and will pass once the bug is fixed. The agent NEVER sees the gold
code fix or the gold test. It must NOT fix the bug itself (that would make its
own test pass on the buggy code and defeat the reproduction).

baseline: problem statement + a plain 'write a failing test' instruction.
flow-first (v2, TEST-GEN protocol): the plan/verify loop is REQUIRED, with
test-generation semantics (REPR_FLOW_TESTGEN=1 on the server):
  flow_of     -- see the buggy function's flow graph(s).
  flow_plan   -- REQUIRED: record the delta a correct FIX would make; the
                 test asserts the behavior that delta would produce, so it
                 FAILS on the current code. Snapshots the source graphs.
  flow_verify -- REQUIRED LAST: proves the SOURCE flow is UNCHANGED (the
                 agent must not fix the bug) and the diff is test-files-only.
  flow_check  -- optional spot-check that a source function is untouched.
"""


def _flow_steps(of_tool, plan_tool, check_tool, verify_tool, surface_intro,
                graph_blurb, locate_blurb):
    """The shared 5-step TEST-GEN protocol; per-rep wording is injected."""
    steps = [
        "## Work IN THE FLOW (there is no code map)",
        surface_intro,
        "1. (REQUIRED FIRST) Call `%s` with the function/symbol names the "
        "bug report mentions. %s There is no explore/map tool; this flow is "
        "your surface." % (of_tool, graph_blurb),
        "2. IF THE BUG IS NOT IN THE FLOW, LOCALIZE FIRST: the report may "
        "name no function, a name may not resolve, or the graphs you got "
        "back may not contain the faulty region. Do NOT design the test off "
        "a flow that does not show the bug. Fall back to your own tools -- "
        "Grep for the error message / suspect identifiers, Read the "
        "candidate files -- until you find the faulty function, then call "
        "`%s` on THAT function and work from its flow." % of_tool,
        "3. (REQUIRED) %s Then call `%s` to record the flow delta a correct "
        "FIX would make (op + function + anchor + rationale). That delta is "
        "the CONTRACT your test checks: the test must FAIL on the current "
        "code BECAUSE the change is absent, and PASS once a fix makes it. "
        "This also snapshots the source graphs so your work can be "
        "verified." % (locate_blurb, plan_tool),
        "4. Write the test in the project's test suite so it drives "
        "execution through exactly that flow locus and asserts the "
        "CORRECT (currently-violated) behavior. Do NOT edit any non-test "
        "source file -- fixing the bug would make your test pass now and "
        "defeat reproduction. (`%s` can spot-check that a source function "
        "is still untouched.)" % check_tool,
    ]
    if verify_tool:
        steps.append(
            "5. (REQUIRED LAST) Before you stop, call `%s` (no arguments). "
            "It verifies the SOURCE flow of every planned function is "
            "UNCHANGED and that your diff touches ONLY test files, and "
            "lists your test edits. If it reports a source change or a "
            "non-test edit, revert that and call it once more. Then make "
            "sure the test really fails on the current code, and stop."
            % verify_tool)
    steps.append("")
    return steps


def build_prompt(inst: dict, flow_first_tools=None) -> str:
    parts = ["# Reproducing-test generation task", ""]
    if flow_first_tools:
        of_tool, plan_tool, check_tool = flow_first_tools[:3]
        verify_tool = (flow_first_tools[3]
                       if len(flow_first_tools) > 3 else None)
        # combined arm first -- "dfg_perrep" is a substring of "cfgdfg_perrep"
        if "cfgdfg_perrep" in of_tool:
            parts += _flow_steps(
                of_tool, plan_tool, check_tool, verify_tool,
                "To design a test that reproduces this bug, study the buggy "
                "function's TWO flow graphs -- its CONTROL-FLOW GRAPH (the "
                "branches and paths where the bug manifests) and its "
                "VALUE-FLOW (def-use) GRAPH (where values are defined and "
                "used) -- not the whole repo.",
                "It returns BOTH graphs of each function: the control-flow "
                "graph (basic blocks + true/false/loop edges) and the "
                "def-use graph (every definition and every use of each "
                "value).",
                "Read the two graphs together to find what the bug turns on "
                "-- the path/branch that misbehaves and the value that "
                "drives execution down it.")
        elif "dfg_perrep" in of_tool:
            parts += _flow_steps(
                of_tool, plan_tool, check_tool, verify_tool,
                "To design a test that reproduces this bug, study the buggy "
                "function's VALUE-FLOW (def-use) GRAPH -- where values are "
                "defined and used, so you can drive the wrong value into "
                "the faulty use -- not the whole repo.",
                "It returns each function's real def-use graph -- every "
                "definition of a variable and every place its value is "
                "used.",
                "Read the flow to find the def/use relation the bug turns "
                "on -- the input or state that makes a use read the wrong "
                "value; your test must set up THAT value.")
        else:
            parts += _flow_steps(
                of_tool, plan_tool, check_tool, verify_tool,
                "To design a test that reproduces this bug, study the buggy "
                "function's CONTROL-FLOW GRAPH -- the branches and paths "
                "where the bug manifests -- not the whole repo.",
                "It returns each function's real control-flow graph (basic "
                "blocks + true/false/loop edges).",
                "Read the flow to find the path/branch that misbehaves -- "
                "the input or state that drives execution down the faulty "
                "edge; your test must exercise THAT path.")
    parts += [
        "You are working in a checked-out Python repository. There is a bug "
        "described below. Your job is to WRITE A NEW TEST (or extend an "
        "existing test file) that REPRODUCES the bug: the test must FAIL on the "
        "current (buggy) code because of this bug, and would PASS once the bug "
        "is correctly fixed.",
        "",
        "## Bug description",
        "",
        str(inst.get("problem_statement") or "").strip(),
    ]
    req = str(inst.get("requirements") or "").strip()
    if req:
        parts += ["", "## Expected behavior (what a fix should achieve)", "", req]
    iface = str(inst.get("interface") or "").strip()
    if iface:
        parts += ["", "## Interface / hints", "", iface]
    parts += [
        "",
        "## Rules",
        "- Write your test in the project's test suite (a file under test/ or "
        "tests/, or a test_*.py file). Add a clearly-named test function that "
        "asserts the CORRECT behavior, so it fails now and passes after a fix.",
        "- Do NOT fix the bug. Do NOT edit non-test source files. If your test "
        "passes on the current code, it does not reproduce the bug -- make it "
        "assert the correct (currently-violated) behavior.",
        "- Make the test self-contained and deterministic; use the same imports "
        "and fixtures the surrounding tests use.",
        "- When done, stop. The harness extracts your test as a diff and checks "
        "it fails on the buggy code and passes on the fixed code.",
    ]
    return "\n".join(parts)
