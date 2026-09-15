"""Paired report for the CoderMind / RPG-Encoder arm vs the shim baseline.

Usage: .venv/bin/python _codermind_report.py [exp_name]   (default swe30_codermind)

Works on a partial run. Reports the agent session alone (result_main.json)
and the whole cell (result.json = agent + the encode / incremental-update
sessions their encoder ran with headless claude), and splits cells by
whether the agent actually called an rpg-tools MCP tool.
"""
import glob
import json
import os
import sys
from collections import Counter

R = "/home/tempadmin/Code_rep_mcp_tools"
EXP = sys.argv[1] if len(sys.argv) > 1 else "swe30_codermind"
COND = "__baseline_codermind"


def L(p):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return {}


def tk(r):
    u = (r or {}).get("usage") or {}
    return dict(i=u.get("input_tokens") or 0, o=u.get("output_tokens") or 0,
                cr=u.get("cache_read_input_tokens") or 0,
                cc=u.get("cache_creation_input_tokens") or 0)


def rpg_calls(d):
    """rpg-tools calls the agent made: from the harvested transcript."""
    calls = Counter()
    tp = os.path.join(d, "transcript.jsonl")
    if os.path.exists(tp):
        for line in open(tp, encoding="utf-8", errors="replace"):
            try:
                j = json.loads(line)
            except Exception:
                continue
            if j.get("type") != "assistant":
                continue
            for b in (j.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use" and \
                        str(b.get("name", "")).startswith("mcp__rpg-tools__"):
                    calls[b["name"].replace("mcp__rpg-tools__", "")] += 1
    return calls


rows = []
tools = Counter()
for d in sorted(glob.glob(os.path.join(R, "results", EXP, "*/"))):
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
    c = rpg_calls(d)
    tools.update(c)
    prep = L(os.path.join(d, "codermind", "prepare.json"))
    r_all = L(os.path.join(d, "result.json"))
    r_main = L(os.path.join(d, "result_main.json")) or r_all
    rows.append(dict(repo=key.split("instance_")[-1].split("__")[0], key=key,
                     n_calls=sum(c.values()), quar=bool(g.get("quarantined")),
                     res=bool(g.get("resolved")), bres=bool(bg.get("resolved")),
                     r=r_main, rall=r_all, b=L(os.path.join(bd, "result.json")),
                     prep=prep))

n = len(rows)
if not n:
    print("no paired graded cells under results/%s" % EXP)
    sys.exit(0)
used = [x for x in rows if x["n_calls"]]
print("exp %s | paired cells %d | quarantined %d" % (EXP, n, sum(1 for x in rows if x["quar"])))
print("rpg-tools used in %d/%d cells (%.0f%%) | calls %.2f/cell | by tool %s" % (
    len(used), n, 100 * len(used) / n, sum(x["n_calls"] for x in rows) / n, dict(tools)))
modes = Counter((x["prep"].get("rpg") or {}).get("mode") for x in rows)
oks = sum(1 for x in rows if (x["prep"].get("rpg") or {}).get("ok"))
side_calls = [((x["prep"].get("side") or {}).get("api_calls") or 0) for x in rows]
side_cost = [((x["prep"].get("side") or {}).get("total_cost_usd") or 0) for x in rows]
side_secs = [((x["prep"].get("rpg") or {}).get("seconds") or 0) for x in rows]
print("RPG step: modes %s | ok %d/%d | encoder claude calls %.1f/cell (max %d) | encoder $%.3f/cell (max $%.3f) | %.0fs/cell (max %.0fs)" % (
    dict(modes), oks, n, sum(side_calls) / n, max(side_calls), sum(side_cost) / n, max(side_cost),
    sum(side_secs) / n, max(side_secs)))


def agg(sel, key):
    t = dict(i=0, o=0, cr=0, cc=0); usd = 0.0; tur = 0; res = 0
    for x in sel:
        r = x[key]; u = tk(r)
        for k in t:
            t[k] += u[k]
        usd += r.get("total_cost_usd") or 0
        tur += r.get("num_turns") or 0
        res += x["res"] if key in ("r", "rall") else x["bres"]
    m = len(sel)
    return dict(n=m, res=res, counted=(t["i"] + t["o"] + t["cr"]) / m,
                full=(t["i"] + t["o"] + t["cr"] + t["cc"]) / m,
                usd=usd / m, turns=tur / m, cc=t["cc"] / m, o=t["o"] / m)


def block(label, sel):
    if not sel:
        return
    B, A, W = agg(sel, "b"), agg(sel, "r"), agg(sel, "rall")
    print("\n%s (n=%d)" % (label, len(sel)))
    print("  %-26s %-9s %-10s %-10s %-9s %s" % ("arm", "resolved", "counted", "full", "$/inst", "turns"))
    for nm, a in (("baseline", B), ("CoderMind, agent session", A), ("CoderMind, whole cell", W)):
        print("  %-26s %-9s %-10.3fM %-10.3fM $%-8.3f %.1f" % (
            nm, "%d/%d" % (a["res"], a["n"]), a["counted"] / 1e6, a["full"] / 1e6, a["usd"], a["turns"]))
    for nm, a in (("agent session", A), ("whole cell", W)):
        print("  ratios %-14s counted %.2fx | full %.2fx | $ %.2fx | turns %.2fx" % (
            nm, a["counted"] / B["counted"], a["full"] / B["full"], a["usd"] / B["usd"], a["turns"] / B["turns"]))


block("ALL PAIRED CELLS", rows)
block("cells WHERE rpg-tools WAS USED", used)
block("cells with NO rpg-tools call", [x for x in rows if not x["n_calls"]])
byrepo = {}
for x in rows:
    e = byrepo.setdefault(x["repo"], [0, 0, 0, 0])
    e[0] += 1; e[1] += x["res"]; e[2] += x["bres"]; e[3] += 1 if x["n_calls"] else 0
print("\nby repo (n, codermind resolved, baseline resolved, cells using rpg-tools):",
      {k: tuple(v) for k, v in sorted(byrepo.items())})
