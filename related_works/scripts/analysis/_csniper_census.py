"""Tool-use census for the ContextSniper rerun (swe30_csniper220) vs baseline.

Usage: .venv/bin/python _csniper_census.py > _csniper_census.json

Per cell: distinct tool_use calls by tool name (their MCP tools search_code /
edit_file / ..., native Bash / Read / Grep / Glob / Edit), turns, resolved,
and the same for the baseline cell of the same instance. Reads only the
transcripts of graded cells.
"""
import collections
import glob
import json
import os


def census(cell):
    counts, seen = collections.Counter(), set()
    path = os.path.join(cell, "transcript.jsonl")
    if not os.path.exists(path):
        return counts
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except Exception:
                continue
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
                name = blk.get("name", "?")
                if name.startswith("mcp__"):
                    name = "mcp:" + name.rsplit("__", 1)[-1]
                counts[name] += 1
    return counts


out = {}
for exp, cond in (("swe30_csniper220", "baseline_csniper"), ("swe30_shim", "baseline")):
    rows = {}
    for cell in glob.glob("results/%s/*__%s" % (exp, cond)):
        if not os.path.exists(os.path.join(cell, "grade.json")):
            continue
        grade = json.load(open(os.path.join(cell, "grade.json")))
        if grade.get("quarantined"):
            continue
        res = json.load(open(os.path.join(cell, "result.json")))
        rows[os.path.basename(cell).rsplit("__", 1)[0]] = dict(
            res=bool(grade.get("resolved")), turns=res.get("num_turns"),
            tools=dict(census(cell)))
    out[exp] = rows
ids = sorted(i for i in out["swe30_csniper220"] if i in out["swe30_shim"])
tot = collections.Counter()
for i in ids:
    tot.update(out["swe30_csniper220"][i]["tools"])
base_tot = collections.Counter()
for i in ids:
    base_tot.update(out["swe30_shim"][i]["tools"])
print(json.dumps(dict(n=len(ids), csniper_tools=dict(tot), baseline_tools_same_ids=dict(base_tot),
                      per_cell={i[9:40]: out["swe30_csniper220"][i] for i in ids}), indent=1))
