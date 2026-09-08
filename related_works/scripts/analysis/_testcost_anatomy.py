"""How much does a test run actually cost in our shim baseline?

The no-execution arm cut 12% where the paper cut 56-62%. To explain the gap
honestly we need the size of the thing being removed. For the shim baseline
(swe30_shim/baseline) measure:

  - test commands, and how many cells run any
  - characters of tool_result returned BY test commands vs all other tools
  - the share of total observation text that test output represents
  - turns spent on test calls
  - what a test command actually returns (first line of a sample)

Then the same for the no-execution arm, to show what changed.

Usage: .venv/bin/python _testcost_anatomy.py > _testcost_anatomy.txt
"""
import glob
import json
import os
import re
from collections import Counter

TEST_RE = re.compile(r"pytest|unittest|python -m test|tox\b")
ARMS = [("shim baseline", "swe30_shim", "baseline"),
        ("no-execution", "swe30_noexec", "baseline_noexec")]


def analyse(cell):
    """Map tool_use id -> (is_test, tool) then size each tool_result."""
    is_test, tool_of, seen = {}, {}, set()
    turns_with_test = set()
    lines = []
    with open(os.path.join(cell, "transcript.jsonl"), encoding="utf-8",
              errors="replace") as fh:
        for line in fh:
            try:
                lines.append(json.loads(line))
            except Exception:
                pass
    for rec in lines:
        if rec.get("type") != "assistant":
            continue
        msg = rec.get("message") or {}
        for blk in msg.get("content") or []:
            if not (isinstance(blk, dict) and blk.get("type") == "tool_use"):
                continue
            key = (msg.get("id"), blk.get("id"))
            if key in seen:
                continue
            seen.add(key)
            cmd = (blk.get("input") or {}).get("command", "") or ""
            test = blk.get("name") == "Bash" and bool(TEST_RE.search(cmd))
            is_test[blk.get("id")] = test
            tool_of[blk.get("id")] = blk.get("name")
            if test:
                turns_with_test.add(msg.get("id"))
    chars = Counter()
    samples = []
    for rec in lines:
        if rec.get("type") != "user":
            continue
        msg = rec.get("message") or {}
        for blk in msg.get("content") or []:
            if not (isinstance(blk, dict) and blk.get("type") == "tool_result"):
                continue
            tid = blk.get("tool_use_id")
            content = blk.get("content")
            if isinstance(content, list):
                text = "".join(p.get("text", "") for p in content
                               if isinstance(p, dict))
            else:
                text = content if isinstance(content, str) else ""
            bucket = "test" if is_test.get(tid) else "other"
            chars[bucket] += len(text)
            chars[bucket + "_calls"] += 1
            if bucket == "test" and len(samples) < 3:
                samples.append(text[:160].replace("\n", " | "))
    return dict(tests=sum(1 for v in is_test.values() if v),
                test_turns=len(turns_with_test),
                test_chars=chars["test"], other_chars=chars["other"],
                test_results=chars["test_calls"], samples=samples)


for label, exp, cond in ARMS:
    rows, samples = [], []
    for cell in sorted(glob.glob("results/%s/*__%s" % (exp, cond))):
        if not os.path.exists(os.path.join(cell, "transcript.jsonl")):
            continue
        r = analyse(cell)
        rows.append(r)
        if len(samples) < 4:
            samples.extend(r["samples"][:1])
    n = len(rows)
    t_chars = sum(r["test_chars"] for r in rows)
    o_chars = sum(r["other_chars"] for r in rows)
    tests = sum(r["tests"] for r in rows)
    print("=== %s (%d cells)" % (label, n))
    print("  test commands: %d total, %.1f per cell, in %d/%d cells"
          % (tests, tests / n, sum(1 for r in rows if r["tests"]), n))
    print("  turns containing a test call: %.1f per cell"
          % (sum(r["test_turns"] for r in rows) / n))
    print("  observation characters: test %.2fM (%.1f%% of all), other %.2fM"
          % (t_chars / 1e6, 100 * t_chars / max(1, t_chars + o_chars), o_chars / 1e6))
    print("  mean characters returned per test command: %d"
          % (t_chars / max(1, sum(r["test_results"] for r in rows))))
    print("  sample test output:")
    for s in samples[:4]:
        print("    | %s" % s)
    print()
