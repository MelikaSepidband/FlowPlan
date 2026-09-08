"""Final paired report for the no-execution arm (arXiv 2606.26978 'Prohibited').

Usage: .venv/bin/python _noexec_report.py > _noexec_report.txt

Pairs swe30_noexec (sentence + no test shim = deps not installed) against the
swe30_shim baseline (runnable tests) on the cells both produced. Integrity:
placeholder ('quarantined') grades and zero-usage cells are excluded and named.
Compliance: counts test commands (pytest/unittest/tox) in each arm's Bash calls.
"""
import glob
import json
import os
import re

PRICE = {"in": 1.0, "out": 5.0, "cr": 0.10, "cc": 2.00}  # $/MTok, 1h cache write
TEST_RE = re.compile(r"pytest|unittest|python -m test|tox\b")


def scan(cell):
    """(distinct builds, test-command count) from the cell transcript."""
    versions, tests, seen = set(), 0, set()
    path = os.path.join(cell, "transcript.jsonl")
    if not os.path.exists(path):
        return versions, tests
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("version"):
                versions.add(rec["version"])
            if rec.get("type") != "assistant":
                continue
            msg = rec.get("message") or {}
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
    return versions, tests


def load(exp, cond):
    cells, bad = {}, []
    for cell in glob.glob("results/%s/*__%s" % (exp, cond)):
        gpath, rpath = (os.path.join(cell, f) for f in ("grade.json", "result.json"))
        if not os.path.exists(gpath):
            continue
        grade = json.load(open(gpath))
        res = (json.load(open(rpath))
               if os.path.exists(rpath) and os.path.getsize(rpath) else {})
        usage = res.get("usage", {})
        counted = (usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
                   + usage.get("cache_read_input_tokens", 0))
        if grade.get("quarantined") or counted == 0:
            bad.append(os.path.basename(cell)[9:40])
            continue
        versions, tests = scan(cell)
        cells[os.path.basename(cell).rsplit("__", 1)[0]] = dict(
            res=bool(grade.get("resolved")), turns=res.get("num_turns") or 0,
            tests=tests, ver=",".join(sorted(versions)),
            inp=usage.get("input_tokens", 0), out=usage.get("output_tokens", 0),
            cr=usage.get("cache_read_input_tokens", 0),
            cc=usage.get("cache_creation_input_tokens", 0))
    return cells, bad


def agg(cells, ids):
    n = len(ids)
    tot = lambda f: sum(cells[i][f] for i in ids)
    counted = tot("inp") + tot("out") + tot("cr")
    usd = (tot("inp") * PRICE["in"] + tot("out") * PRICE["out"]
           + tot("cr") * PRICE["cr"] + tot("cc") * PRICE["cc"]) / 1e6
    return dict(n=n, res=sum(cells[i]["res"] for i in ids), inp=tot("inp") / n,
                out=tot("out") / n, cr=tot("cr") / n, cc=tot("cc") / n,
                counted=counted / n, full=(counted + tot("cc")) / n, usd=usd / n,
                turns=tot("turns") / n, tests=tot("tests"))


base, _ = load("swe30_shim", "baseline")
noex, bad = load("swe30_noexec", "baseline_noexec")
ids = sorted(i for i in noex if i in base)

print("no-exec valid cells: %d | invalid: %s | builds: %s"
      % (len(noex), bad or "none", sorted({c["ver"] for c in noex.values()})))
B, N = agg(base, ids), agg(noex, ids)
print("\nPAIRED n=%d" % len(ids))
head = "%-24s %8s %7s %8s %11s %9s %9s %9s %8s %6s %6s"
print(head % ("arm", "resolved", "input", "output", "cache_read", "cache_cr",
              "counted", "full", "USD", "turns", "tests"))
row = "%-24s %5d/%-2d %7.0f %8.0f %10.3fM %8.1fK %8.3fM %8.3fM %8.4f %6.1f %6d"
for name, a in (("baseline (shim on)", B), ("no-execution (exact)", N)):
    print(row % (name, a["res"], a["n"], a["inp"], a["out"], a["cr"] / 1e6,
                 a["cc"] / 1e3, a["counted"] / 1e6, a["full"] / 1e6, a["usd"],
                 a["turns"], a["tests"]))
print("\nratios: counted %.2fx full %.2fx USD %.2fx output %.2fx cache_read %.2fx "
      "cache_cr %.2fx turns %.2fx"
      % (N["counted"] / B["counted"], N["full"] / B["full"], N["usd"] / B["usd"],
         N["out"] / B["out"], N["cr"] / B["cr"], N["cc"] / B["cc"],
         N["turns"] / B["turns"]))
ratios = sorted((noex[i]["inp"] + noex[i]["out"] + noex[i]["cr"])
                / (base[i]["inp"] + base[i]["out"] + base[i]["cr"]) for i in ids)
print("per-cell: median %.2fx, below 1.0: %d/%d, min %.2f, max %.2f"
      % (ratios[len(ratios) // 2], sum(r < 1 for r in ratios), len(ratios),
         ratios[0], ratios[-1]))
print("overlap: both %d, noexec-only %d, baseline-only %d"
      % (sum(1 for i in ids if noex[i]["res"] and base[i]["res"]),
         sum(1 for i in ids if noex[i]["res"] and not base[i]["res"]),
         sum(1 for i in ids if base[i]["res"] and not noex[i]["res"])))
print("compliance: test commands baseline %d vs no-exec %d; cells with >=1: "
      "baseline %d vs no-exec %d"
      % (B["tests"], N["tests"], sum(1 for i in ids if base[i]["tests"]),
         sum(1 for i in ids if noex[i]["tests"])))
