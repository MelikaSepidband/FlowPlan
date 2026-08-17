"""Test-generation prompts for the SWT-bench arm.

The agent must WRITE A TEST that reproduces the bug -- it fails on the current
(buggy) code and will pass once the bug is fixed. The agent NEVER sees the gold
code fix or the gold test. It must NOT fix the bug itself (that would make its
own test pass on the buggy code and defeat the reproduction).

baseline: problem statement + a plain 'write a failing test' instruction.
flow3 arms wrap this same task body with the flowfix-skill protocol prompt
(experiments.common.flow_protocol); build_prompt itself always emits the
plain task body (flow_first_tools stays None).
"""


def build_prompt(inst: dict, flow_first_tools=None) -> str:
    parts = ["# Reproducing-test generation task", ""]
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
