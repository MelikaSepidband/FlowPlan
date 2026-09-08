"""Per-arm usage aggregates for the ten-arm scorecard artifact.

Usage: .venv/bin/python _scorecard_data.py > _scorecard_data.json

For every arm: valid cells (placeholder / zero-usage cells excluded and named),
CLI builds seen in the transcripts, per-instance means of the four usage
classes, counted / full / dollars, turns, resolves -- plus the same means for
the baseline restricted to the arm's own instance set (paired), so partial
arms (ContextSniper rerun still running) get an honest ratio.
"""
import glob
import json
import os

PRICE = {"in": 1.0, "out": 5.0, "cr": 0.10, "cc": 2.00}  # $/MTok, 1h cache write
ARMS = [
    ("baseline", "swe30_shim", "baseline"),
    ("cfg", "swe30_shim", "cfg_perrep_flow3"),
    ("dfg", "swe30_shim", "dfg_perrep_flow3"),
    ("noexec", "swe30_noexec", "baseline_noexec"),
    ("claudemd", "swe30_agentsmd", "baseline_agentsmd"),
    ("pruner", "swe30_prunerB", "baseline_prunerB"),
    ("csniper", "swe30_csniper220", "baseline_csniper"),
    ("cbmem", "swe30_cbmem2", "baseline_cbmem2"),
    ("cmv", "swe30_cmv2", "baseline_cmv2"),
    ("lingua", "swe30_lingua", "baseline_lingua"),
]


def versions(cell):
    seen = set()
    path = os.path.join(cell, "transcript.jsonl")
    if not os.path.exists(path):
        return seen
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"version"' not in line:
                continue
            try:
                v = json.loads(line).get("version")
            except Exception:
                continue
            if v:
                seen.add(v)
    return seen


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
        iid = os.path.basename(cell).rsplit("__", 1)[0]
        cells[iid] = dict(
            res=bool(grade.get("resolved")), turns=res.get("num_turns") or 0,
            cli=res.get("total_cost_usd", 0.0), ver=sorted(versions(cell)),
            inp=usage.get("input_tokens", 0), out=usage.get("output_tokens", 0),
            cr=usage.get("cache_read_input_tokens", 0),
            cc=usage.get("cache_creation_input_tokens", 0))
    return cells, bad


def agg(cells, ids):
    n = len(ids)
    if not n:
        return None
    tot = lambda f: sum(cells[i][f] for i in ids)
    counted = tot("inp") + tot("out") + tot("cr")
    usd = (tot("inp") * PRICE["in"] + tot("out") * PRICE["out"]
           + tot("cr") * PRICE["cr"] + tot("cc") * PRICE["cc"]) / 1e6
    return dict(n=n, res=sum(cells[i]["res"] for i in ids),
                inp=tot("inp") / n, out=tot("out") / n, cr=tot("cr") / n,
                cc=tot("cc") / n, counted=counted / n,
                full=(counted + tot("cc")) / n, usd=usd / n,
                cli=tot("cli") / n, turns=tot("turns") / n)


base, _ = load("swe30_shim", "baseline")
out = {}
for key, exp, cond in ARMS:
    cells, bad = load(exp, cond)
    ids = sorted(i for i in cells if i in base)
    builds = sorted({v for c in cells.values() for v in c["ver"]})
    ratios = sorted((cells[i]["inp"] + cells[i]["out"] + cells[i]["cr"])
                    / (base[i]["inp"] + base[i]["out"] + base[i]["cr"]) for i in ids)
    out[key] = dict(
        exp=exp, cond=cond, valid=len(cells), invalid=bad, builds=builds,
        arm=agg(cells, ids), base_same_ids=agg(base, ids),
        per_cell_median=(ratios[len(ratios) // 2] if ratios else None),
        below_one=sum(r < 1 for r in ratios),
        overlap=dict(both=sum(1 for i in ids if cells[i]["res"] and base[i]["res"]),
                     arm_only=sum(1 for i in ids if cells[i]["res"] and not base[i]["res"]),
                     base_only=sum(1 for i in ids if base[i]["res"] and not cells[i]["res"])))
print(json.dumps(out, indent=1))
