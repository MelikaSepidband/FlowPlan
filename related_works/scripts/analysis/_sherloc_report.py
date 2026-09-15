"""Paired report for the SHERLOC arm (swe30_sherloc) vs the shim baseline."""
import glob
import json
import os
import sys
from collections import Counter

R = "/home/tempadmin/Code_rep_mcp_tools"


def L(p):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return {}


def tok(r):
    u = r.get("usage") or {}
    return dict(i=u.get("input_tokens") or 0, o=u.get("output_tokens") or 0,
                cr=u.get("cache_read_input_tokens") or 0,
                cc=u.get("cache_creation_input_tokens") or 0)


def arm(exp, cond):
    cells = {}
    for d in sorted(glob.glob(os.path.join(R, "results", exp, "*/"))):
        name = os.path.basename(d.rstrip("/"))
        if not name.endswith("__" + cond):
            continue
        g = L(os.path.join(d, "grade.json"))
        if not g:
            continue
        cells[name[:-(len(cond) + 2)]] = dict(
            res=bool(g.get("resolved")), quar=bool(g.get("quarantined")),
            r=L(os.path.join(d, "result.json")), dir=d)
    return cells


sh, bl = arm("swe30_sherloc", "baseline_sherloc"), arm("swe30_shim", "baseline")
common = sorted(set(sh) & set(bl))
print("sherloc %d | baseline %d | paired %d | quarantined %d" % (
    len(sh), len(bl), len(common), sum(1 for k in sh if sh[k]["quar"])))


def agg(cells, keys):
    t = dict(i=0, o=0, cr=0, cc=0); usd = 0.0; turns = 0; res = 0
    for k in keys:
        c = cells[k]; u = tok(c["r"])
        for x in t:
            t[x] += u[x]
        usd += c["r"].get("total_cost_usd") or 0
        turns += c["r"].get("num_turns") or 0
        res += 1 if c["res"] else 0
    n = len(keys)
    return dict(n=n, res=res, counted=t["i"] + t["o"] + t["cr"],
                full=t["i"] + t["o"] + t["cr"] + t["cc"], usd=usd,
                turns=turns / n if n else 0, **t)


A, B = agg(sh, common), agg(bl, common)
print("\n%-12s %-9s %-11s %-11s %-10s %-8s %-8s %s" % (
    "arm", "resolved", "counted", "full", "$/inst", "turns", "out-tok", "cache_write"))
for nm, a in (("baseline", B), ("SHERLOC", A)):
    print("%-12s %-9s %-11.2fM %-11.2fM $%-9.3f %-8.1f %-8dK %.0fK" % (
        nm, "%d/%d" % (a["res"], a["n"]), a["counted"] / 1e6, a["full"] / 1e6,
        a["usd"] / a["n"], a["turns"], a["o"] / 1000, a["cc"] / 1000))
print("\nratios vs baseline: counted %.2fx | full %.2fx | $ %.2fx | turns %.2fx | output %.2fx" % (
    A["counted"] / B["counted"], A["full"] / B["full"], A["usd"] / B["usd"],
    A["turns"] / B["turns"], A["o"] / B["o"]))

stage_usd = stage_counted = 0
stats = []
for k in common:
    l = L(os.path.join(sh[k]["dir"], "sherloc", "localization.json"))
    u = l.get("usage") or {}
    stage_usd += l.get("total_cost_usd") or 0
    stage_counted += (u.get("input_tokens") or 0) + (u.get("output_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
    stats.append((k.split("instance_")[-1].split("__")[0], l.get("status"),
                  l.get("n_tool_calls") or 0, len(l.get("locations") or [])))
print("\nlocalization stage: $%.2f of $%.2f (%.0f%%) | %.2fM of %.2fM counted (%.0f%%)" % (
    stage_usd, A["usd"], 100 * stage_usd / A["usd"], stage_counted / 1e6,
    A["counted"] / 1e6, 100 * stage_counted / A["counted"]))
print("stage status:", dict(Counter(s[1] for s in stats)),
      "| mean calls %.1f | mean locations %.1f" % (
          sum(s[2] for s in stats) / len(stats), sum(s[3] for s in stats) / len(stats)))

byrepo = {}
for k in common:
    repo = k.split("instance_")[-1].split("__")[0]
    e = byrepo.setdefault(repo, [0, 0, 0, 0.0, 0.0])
    e[0] += 1; e[1] += sh[k]["res"]; e[2] += bl[k]["res"]
    e[3] += sh[k]["r"].get("total_cost_usd") or 0
    e[4] += bl[k]["r"].get("total_cost_usd") or 0
print("\n%-16s %-4s %-9s %-9s %-9s %s" % ("repo", "n", "sherloc", "baseline", "$sherloc", "$baseline"))
for repo, e in sorted(byrepo.items()):
    print("%-16s %-4d %-9s %-9s $%-8.2f $%.2f" % (repo, e[0], "%d res" % e[1], "%d res" % e[2], e[3] / e[0], e[4] / e[0]))

flips = [(k.split("instance_")[-1][:24], bl[k]["res"], sh[k]["res"]) for k in common if bl[k]["res"] != sh[k]["res"]]
print("\nflips base->sherloc: gained %d, lost %d" % (
    sum(1 for f in flips if f[2]), sum(1 for f in flips if not f[2])))
for f in flips:
    print("   %-26s %s -> %s" % f)
