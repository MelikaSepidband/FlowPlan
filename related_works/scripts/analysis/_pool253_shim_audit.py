"""Which cells behind the pooled SWE-bench Pro tables ran with the test shim?

The 253-instance table pools several experiment directories. For every
directory that holds baseline / cfg_perrep_flow3 / dfg_perrep_flow3 cells,
report per condition: cell count, how many carry a test_shim block in
meta.json, resolves, and per-instance counted tokens + dollars -- so the
pooled 3.71M / 1.73M / 1.71M rows can be traced to shim or no-shim cells.

Usage: .venv/bin/python _pool253_shim_audit.py > _pool253_shim_audit.txt
"""
import glob
import json
import os
from collections import defaultdict

PRICE = {"in": 1.0, "out": 5.0, "cr": 0.10, "cc": 2.00}
CONDS = ("baseline", "cfg_perrep_flow3", "dfg_perrep_flow3",
         "cfgdfg_perrep_flow3", "cfg_perrep_flow", "dfg_perrep_flow")


def cell_rows(cell):
    gpath, rpath = (os.path.join(cell, f) for f in ("grade.json", "result.json"))
    if not os.path.exists(gpath):
        return None
    grade = json.load(open(gpath))
    res = (json.load(open(rpath))
           if os.path.exists(rpath) and os.path.getsize(rpath) else {})
    u = res.get("usage", {})
    counted = (u.get("input_tokens", 0) + u.get("output_tokens", 0)
               + u.get("cache_read_input_tokens", 0))
    if grade.get("quarantined") or counted == 0:
        return None
    meta = {}
    mpath = os.path.join(cell, "meta.json")
    if os.path.exists(mpath):
        try:
            meta = json.load(open(mpath))
        except Exception:
            meta = {}
    usd = (u.get("input_tokens", 0) * PRICE["in"]
           + u.get("output_tokens", 0) * PRICE["out"]
           + u.get("cache_read_input_tokens", 0) * PRICE["cr"]
           + u.get("cache_creation_input_tokens", 0) * PRICE["cc"]) / 1e6
    return dict(shim="test_shim" in meta, res=bool(grade.get("resolved")),
                counted=counted, usd=usd,
                iid=os.path.basename(cell).rsplit("__", 1)[0])


groups = defaultdict(list)
for exp in sorted(glob.glob("results/*/")):
    for cell in glob.glob(os.path.join(exp, "*")):
        if not os.path.isdir(cell):
            continue
        name = os.path.basename(cell)
        if "__" not in name:
            continue
        cond = name.rsplit("__", 1)[1]
        if cond not in CONDS:
            continue
        row = cell_rows(cell)
        if row:
            groups[(os.path.basename(exp.rstrip("/")), cond)].append(row)

print("%-42s %-20s %5s %6s %6s %9s %8s" % (
    "experiment", "condition", "cells", "shim", "resolved", "counted", "$/inst"))
totals = defaultdict(lambda: defaultdict(list))
for (exp, cond), rows in sorted(groups.items()):
    n = len(rows)
    shim = sum(r["shim"] for r in rows)
    print("%-42s %-20s %5d %6s %6d %8.2fM %8.3f" % (
        exp, cond, n, "%d/%d" % (shim, n), sum(r["res"] for r in rows),
        sum(r["counted"] for r in rows) / n / 1e6,
        sum(r["usd"] for r in rows) / n))
    key = "shim" if shim == n else ("no-shim" if shim == 0 else "mixed")
    totals[cond][key].extend(rows)

print("\nPooled by condition and shim status (unique instances):")
for cond in sorted(totals):
    for key in sorted(totals[cond]):
        rows = totals[cond][key]
        seen, uniq = set(), []
        for r in rows:
            if r["iid"] in seen:
                continue
            seen.add(r["iid"])
            uniq.append(r)
        n = len(uniq)
        print("  %-20s %-8s cells=%3d unique=%3d resolved=%3d counted=%.2fM $%.3f" % (
            cond, key, len(rows), n, sum(r["res"] for r in uniq),
            sum(r["counted"] for r in uniq) / n / 1e6,
            sum(r["usd"] for r in uniq) / n))
