"""Per-instance averages for every comparator arm we ran, paired against the
shim baseline on that arm's own cells. Tolerant of unreadable json."""
import glob
import json
import os

ARMS = [("swe30_sherloc", "__baseline_sherloc", "SHERLOC"),
        ("swe30_agentdiet", "__baseline_agentdiet", "AgentDiet"),
        ("swe30_seerepo", "__baseline_seerepo", "SeeRepo"),
        ("swe30_eet", "__baseline_eet", "EET arm A"),
        ("swe30_eetB", "__baseline_eetB", "EET arm B"),
        ("swe30_agentsmd", "__baseline_agentsmd", "AGENTS.md")]


def L(p):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return {}


def ct(x):
    u = (x or {}).get("usage") or {}
    return (u.get("input_tokens", 0) + u.get("output_tokens", 0)
            + u.get("cache_read_input_tokens", 0))


for exp, suf, label in ARMS:
    rows = []
    for d in sorted(glob.glob("results/%s/*/" % exp)):
        n = os.path.basename(d.rstrip("/"))
        if not n.endswith(suf):
            continue
        g = L(os.path.join(d, "grade.json"))
        if not g:
            continue
        key = n[: -len(suf)]
        b = "results/swe30_shim/%s__baseline" % key
        bg, br = L(b + "/grade.json"), L(b + "/result.json")
        if not bg:
            continue
        r = L(os.path.join(d, "result.json"))
        rows.append((bool(g.get("resolved")), bool(bg.get("resolved")), ct(r), ct(br),
                     r.get("total_cost_usd") or 0, br.get("total_cost_usd") or 0,
                     r.get("num_turns") or 0, br.get("num_turns") or 0,
                     bool(g.get("quarantined"))))
    n = len(rows)
    if not n:
        print("%-11s NO PAIRED CELLS" % label)
        continue
    f = lambda i: sum(x[i] for x in rows) / n
    print("%-11s n=%2d  %2d/%d (base %2d)  %.3fM (%.2fx)  $%.3f (%.2fx)  %.1f turns (%.2fx)  quar=%d"
          % (label, n, sum(x[0] for x in rows), n, sum(x[1] for x in rows),
             f(2) / 1e6, f(2) / f(3), f(4), f(4) / f(5), f(6), f(6) / f(7),
             sum(1 for x in rows if x[8])))
