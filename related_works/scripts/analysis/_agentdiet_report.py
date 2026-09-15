"""Paired report for the AgentDiet arm (swe30_agentdiet) vs the shim baseline.

Works on a partial run: pairs whatever cells are graded so far.
"""
import glob
import json
import os

R = "/home/tempadmin/Code_rep_mcp_tools"
COND = "__baseline_agentdiet"


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


cells = {}
for d in sorted(glob.glob(os.path.join(R, "results", "swe30_agentdiet", "*/"))):
    name = os.path.basename(d.rstrip("/"))
    if not name.endswith(COND):
        continue
    g = L(os.path.join(d, "grade.json"))
    if not g:
        continue
    cells[name[:-len(COND)]] = dict(
        res=bool(g.get("resolved")), quar=bool(g.get("quarantined")),
        r=L(os.path.join(d, "result.json")), m=L(os.path.join(d, "result_main.json")), d=d)

bl = {}
for k in cells:
    bd = os.path.join(R, "results", "swe30_shim", k + "__baseline")
    g = L(os.path.join(bd, "grade.json"))
    if g:
        bl[k] = dict(res=bool(g.get("resolved")), r=L(os.path.join(bd, "result.json")))

common = sorted(set(cells) & set(bl))
n = len(common)
print("agentdiet graded %d | paired with baseline %d | quarantined %d" % (
    len(cells), n, sum(1 for k in cells if cells[k]["quar"])))
if not n:
    raise SystemExit("no paired cells yet")


def agg(get):
    t = dict(i=0, o=0, cr=0, cc=0); usd = 0.0; tur = 0; res = 0
    for k in common:
        r, ok = get(k)
        u = tk(r)
        for x in t:
            t[x] += u[x]
        usd += r.get("total_cost_usd") or 0
        tur += r.get("num_turns") or 0
        res += 1 if ok else 0
    return t, usd, tur, res


tb, ub, turb, resb = agg(lambda k: (bl[k]["r"], bl[k]["res"]))
ta, ua, tura, resa = agg(lambda k: (cells[k]["r"], cells[k]["res"]))
tm, um, turm, _ = agg(lambda k: (cells[k]["m"], False))
bcd = (tb["i"] + tb["o"] + tb["cr"]) / n


def line(nm, t, usd, tur, res, base):
    cd = (t["i"] + t["o"] + t["cr"]) / n
    return "%-22s %-8s %-8.0f %-8.0f %-11.0f %-11.0f %-9.3fM %-8s $%-8.3f %.1f" % (
        nm, "%d/%d" % (res, n), t["i"] / n, t["o"] / n, t["cr"] / n, t["cc"] / n,
        cd / 1e6, "%.2fx" % (cd / base) if base else "1.00x", usd / n, tur / n)


print("\nPER-INSTANCE AVERAGES (n=%d paired cells)" % n)
print("%-22s %-8s %-8s %-8s %-11s %-11s %-10s %-8s %-9s %s" % (
    "arm", "resolved", "input", "output", "cache_read", "cache_creat", "counted", "vs base", "$/inst", "turns"))
print(line("baseline", tb, ub, turb, resb, 0))
print(line("AgentDiet agent-only", tm, um, turm, resa, bcd))
print(line("AgentDiet cell total", ta, ua, tura, resa, bcd))

fb = (tb["i"] + tb["o"] + tb["cr"] + tb["cc"]) / n
fa = (ta["i"] + ta["o"] + ta["cr"] + ta["cc"]) / n
print("\nfull (incl cache_creation): baseline %.3fM | AgentDiet %.3fM = %.2fx" % (fb / 1e6, fa / 1e6, fa / fb))
print("$/inst: baseline $%.3f | AgentDiet $%.3f = %.2fx" % (ub / n, ua / n, ua / ub))

sc = sd = er = 0
for k in common:
    ad = cells[k]["r"].get("agentdiet") or {}
    sc += ad.get("side_calls") or 0
    sd += ad.get("side_cost") or 0
    p = os.path.join(cells[k]["d"], "agentdiet_erasures.jsonl")
    if os.path.exists(p):
        er += sum(1 for _ in open(p, encoding="utf-8"))
print("reflection: %.1f calls/cell, $%.3f/cell (%.0f%% of cell cost) | erasures %.1f/cell" % (
    sc / n, sd / n, 100 * sd / ua, er / n))

byrepo = {}
for k in common:
    rp = k.split("instance_")[-1].split("__")[0]
    e = byrepo.setdefault(rp, [0, 0, 0])
    e[0] += 1; e[1] += cells[k]["res"]; e[2] += bl[k]["res"]
print("by repo (n, agentdiet resolved, baseline resolved):", {k: tuple(v) for k, v in sorted(byrepo.items())})
flips = [(k.split("instance_")[-1][:24], bl[k]["res"], cells[k]["res"]) for k in common if bl[k]["res"] != cells[k]["res"]]
print("flips: gained %d, lost %d" % (sum(1 for f in flips if f[2]), sum(1 for f in flips if not f[2])))
