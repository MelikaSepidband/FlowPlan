"""Paired report for the SeeRepo arm (swe30_seerepo) vs the shim baseline.

Works on a partial run. Splits cells by whether the agent actually called the
graph tool, which is the precondition for the paper's mechanism.
"""
import glob
import json
import os
from collections import Counter

R = "/home/tempadmin/Code_rep_mcp_tools"
COND = "__baseline_seerepo"


def L(p):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return {}


def tk(r):
    u = r.get("usage") or {}
    return dict(i=u.get("input_tokens") or 0, o=u.get("output_tokens") or 0,
                cr=u.get("cache_read_input_tokens") or 0,
                cc=u.get("cache_creation_input_tokens") or 0)


rows = []
edges = Counter()
for d in sorted(glob.glob(os.path.join(R, "results", "swe30_seerepo", "*/"))):
    name = os.path.basename(d.rstrip("/"))
    if not name.endswith(COND):
        continue
    g = L(os.path.join(d, "grade.json"))
    if not g:
        continue
    key = name[:-len(COND)]
    bd = os.path.join(R, "results", "swe30_shim", key + "__baseline")
    bg = L(os.path.join(bd, "grade.json"))
    if not bg:
        continue
    calls = []
    tj = os.path.join(d, "seerepo", "seerepo_tools.jsonl")
    if os.path.exists(tj):
        for l in open(tj, encoding="utf-8"):
            j = json.loads(l)
            calls.append(j)
            edges[j.get("edge_type")] += 1
    el = os.path.join(d, "seerepo_elisions.jsonl")
    rows.append(dict(repo=key.split("instance_")[-1].split("__")[0],
                     n_calls=len(calls), quar=bool(g.get("quarantined")),
                     res=bool(g.get("resolved")), bres=bool(bg.get("resolved")),
                     r=L(os.path.join(d, "result.json")), b=L(os.path.join(bd, "result.json")),
                     elis=sum(1 for _ in open(el, encoding="utf-8")) if os.path.exists(el) else 0,
                     gi=L(os.path.join(d, "seerepo", "graph_info.json"))))

n = len(rows)
used = [x for x in rows if x["n_calls"]]
print("paired cells %d | quarantined %d" % (n, sum(1 for x in rows if x["quar"])))
print("graph tool used in %d/%d cells (%.0f%%) | calls %.2f/cell | edge types %s" % (
    len(used), n, 100 * len(used) / n if n else 0,
    sum(x["n_calls"] for x in rows) / n if n else 0, dict(edges)))
print("graph build: %.0fs mean, %.0fs max | elisions %.1f/cell (cells with a call: %.1f)" % (
    sum((x["gi"].get("build_seconds") or 0) for x in rows) / n,
    max((x["gi"].get("build_seconds") or 0) for x in rows),
    sum(x["elis"] for x in rows) / n,
    (sum(x["elis"] for x in used) / len(used)) if used else 0))


def agg(sel, key):
    t = dict(i=0, o=0, cr=0, cc=0); usd = 0.0; tur = 0; res = 0
    for x in sel:
        r = x[key]; u = tk(r)
        for k in t:
            t[k] += u[k]
        usd += r.get("total_cost_usd") or 0
        tur += r.get("num_turns") or 0
        res += x["res"] if key == "r" else x["bres"]
    m = len(sel)
    return dict(n=m, res=res, counted=(t["i"] + t["o"] + t["cr"]) / m,
                full=(t["i"] + t["o"] + t["cr"] + t["cc"]) / m,
                usd=usd / m, turns=tur / m, cc=t["cc"] / m, o=t["o"] / m)


def block(label, sel):
    if not sel:
        return
    A, B = agg(sel, "r"), agg(sel, "b")
    print("\n%s (n=%d)" % (label, len(sel)))
    print("  %-10s %-9s %-10s %-10s %-9s %s" % ("arm", "resolved", "counted", "full", "$/inst", "turns"))
    for nm, a in (("baseline", B), ("SeeRepo", A)):
        print("  %-10s %-9s %-10.3fM %-10.3fM $%-8.3f %.1f" % (
            nm, "%d/%d" % (a["res"], a["n"]), a["counted"] / 1e6, a["full"] / 1e6, a["usd"], a["turns"]))
    print("  ratios: counted %.2fx | full %.2fx | $ %.2fx | turns %.2fx" % (
        A["counted"] / B["counted"], A["full"] / B["full"], A["usd"] / B["usd"], A["turns"] / B["turns"]))


block("ALL PAIRED CELLS", rows)
block("cells WHERE THE GRAPH WAS USED", used)
block("cells with NO graph call", [x for x in rows if not x["n_calls"]])
byrepo = {}
for x in rows:
    e = byrepo.setdefault(x["repo"], [0, 0, 0, 0])
    e[0] += 1; e[1] += x["res"]; e[2] += x["bres"]; e[3] += 1 if x["n_calls"] else 0
print("\nby repo (n, seerepo resolved, baseline resolved, cells using graph):",
      {k: tuple(v) for k, v in sorted(byrepo.items())})
