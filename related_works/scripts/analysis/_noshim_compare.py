"""Is the no-execution saving the prompt, or the missing shim?

Three arms on the same instances:
  shim baseline    swe30_shim/baseline          shim ON,  tests allowed
  no-shim baseline swe30_baseline_fix/baseline  shim OFF, tests allowed  (no sentence)
  no-execution     swe30_noexec/baseline_noexec shim OFF, tests forbidden (sentence)

The no-shim baseline isolates the environment: whatever it saves against the
shim baseline is the shim, not the prompt. What no-execution saves against the
NO-SHIM baseline is the sentence's own effect.

Usage: .venv/bin/python _noshim_compare.py > _noshim_compare.txt
"""
import glob
import json
import os
import re

PRICE = {"in": 1.0, "out": 5.0, "cr": 0.10, "cc": 2.00}
TEST_RE = re.compile(r"pytest|unittest|python -m test|tox\b")
ARMS = [
    ("shim baseline (tests allowed)", "swe30_shim", "baseline"),
    ("no-shim baseline (tests allowed)", "swe30_baseline_fix", "baseline"),
    ("no-execution (tests forbidden)", "swe30_noexec", "baseline_noexec"),
]


def scan(cell):
    tests, seen, models = 0, set(), set()
    path = os.path.join(cell, "transcript.jsonl")
    if not os.path.exists(path):
        return tests, models
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("type") != "assistant":
                continue
            msg = rec.get("message") or {}
            if msg.get("model"):
                models.add(msg["model"])
            for blk in msg.get("content") or []:
                if not (isinstance(blk, dict) and blk.get("type") == "tool_use"
                        and blk.get("name") == "Bash"):
                    continue
                key = (msg.get("id"), blk.get("id"))
                if key in seen:
                    continue
                seen.add(key)
                if TEST_RE.search(blk.get("input", {}).get("command", "")):
                    tests += 1
    return tests, models


def load(exp, cond):
    out = {}
    for cell in glob.glob("results/%s/*__%s" % (exp, cond)):
        gpath, rpath = (os.path.join(cell, f) for f in ("grade.json", "result.json"))
        if not os.path.exists(gpath):
            continue
        grade = json.load(open(gpath))
        res = (json.load(open(rpath))
               if os.path.exists(rpath) and os.path.getsize(rpath) else {})
        u = res.get("usage", {})
        counted = (u.get("input_tokens", 0) + u.get("output_tokens", 0)
                   + u.get("cache_read_input_tokens", 0))
        if grade.get("quarantined") or counted == 0:
            continue
        tests, models = scan(cell)
        out[os.path.basename(cell).rsplit("__", 1)[0]] = dict(
            res=bool(grade.get("resolved")), turns=res.get("num_turns") or 0,
            tests=tests, models=models,
            inp=u.get("input_tokens", 0), out=u.get("output_tokens", 0),
            cr=u.get("cache_read_input_tokens", 0),
            cc=u.get("cache_creation_input_tokens", 0))
    return out


data = {name: load(exp, cond) for name, exp, cond in ARMS}
ids = sorted(set.intersection(*[set(d) for d in data.values()]))
print("instances common to all three arms: %d" % len(ids))
for name, d in data.items():
    models = sorted({m for i in ids for m in d[i]["models"]})
    print("  %-34s cells=%d models=%s" % (name, len(d), models))


def agg(d):
    n = len(ids)
    tot = lambda f: sum(d[i][f] for i in ids)
    counted = tot("inp") + tot("out") + tot("cr")
    usd = (tot("inp") * PRICE["in"] + tot("out") * PRICE["out"]
           + tot("cr") * PRICE["cr"] + tot("cc") * PRICE["cc"]) / 1e6
    return dict(res=sum(d[i]["res"] for i in ids), counted=counted / n,
                usd=usd / n, turns=tot("turns") / n, tests=tot("tests"),
                out=tot("out") / n, cr=tot("cr") / n, cc=tot("cc") / n)


A = {name: agg(d) for name, d in data.items()}
base = A["shim baseline (tests allowed)"]
print("\n%-34s %8s %10s %9s %7s %7s" % ("arm", "resolved", "counted", "$/inst", "turns", "tests"))
for name in A:
    a = A[name]
    print("%-34s %5d/%-2d %9.3fM %8.4f %7.1f %7d"
          % (name, a["res"], len(ids), a["counted"] / 1e6, a["usd"], a["turns"], a["tests"]))

noshim = A["no-shim baseline (tests allowed)"]
noexec = A["no-execution (tests forbidden)"]
print("\nratios vs SHIM baseline:   no-shim %.3fx tokens, %.3fx $ | no-exec %.3fx tokens, %.3fx $"
      % (noshim["counted"] / base["counted"], noshim["usd"] / base["usd"],
         noexec["counted"] / base["counted"], noexec["usd"] / base["usd"]))
print("ratio  vs NO-SHIM baseline: no-exec %.3fx tokens, %.3fx $  <-- the sentence's own effect"
      % (noexec["counted"] / noshim["counted"], noexec["usd"] / noshim["usd"]))
