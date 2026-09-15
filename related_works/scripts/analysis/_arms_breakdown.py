"""Per-instance token breakdown (input / output / cache_read / cache_creation)
for every arm in the artifact table, paired against the shim baseline."""
import glob
import json
import os

ARMS = [("swe30_shim", "__baseline", "baseline"),
        ("swe30_shim", "__cfg_perrep_flow3", "cfg flow3"),
        ("swe30_shim", "__dfg_perrep_flow3", "dfg flow3"),
        ("swe30_agentdiet", "__baseline_agentdiet", "AgentDiet"),
        ("swe30_eetB", "__baseline_eetB", "EET arm B"),
        ("swe30_eet", "__baseline_eet", "EET arm A"),
        ("swe30_sherloc", "__baseline_sherloc", "SHERLOC"),
        ("swe30_agentsmd", "__baseline_agentsmd", "AGENTS.md"),
        ("swe30_seerepo", "__baseline_seerepo", "SeeRepo")]


def L(p):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return {}


def parts(x):
    u = (x or {}).get("usage") or {}
    return (u.get("input_tokens", 0), u.get("output_tokens", 0),
            u.get("cache_read_input_tokens", 0), u.get("cache_creation_input_tokens", 0))


print("%-12s %8s %8s %11s %10s   %8s" % ("arm", "input", "output", "cache_read", "cache_cr", "counted"))
for exp, suf, label in ARMS:
    main, mainonly = [], []
    for d in sorted(glob.glob("results/%s/*/" % exp)):
        n = os.path.basename(d.rstrip("/"))
        if not n.endswith(suf):
            continue
        if not L(os.path.join(d, "grade.json")):
            continue
        main.append(parts(L(os.path.join(d, "result.json"))))
        mo = os.path.join(d, "result_main.json")
        if os.path.exists(mo):
            mainonly.append(parts(L(mo)))
    n = len(main)
    if not n:
        print("%-12s NO CELLS" % label)
        continue
    a = [sum(x[i] for x in main) / n for i in range(4)]
    print("%-12s %8.0f %8.0f %11.0f %10.0f   %8.3fM" % (label, a[0], a[1], a[2], a[3], (a[0] + a[1] + a[2]) / 1e6))
    if mainonly and label == "AgentDiet":
        m = len(mainonly)
        b = [sum(x[i] for x in mainonly) / m for i in range(4)]
        print("%-12s %8.0f %8.0f %11.0f %10.0f   %8.3fM  (agent session only)"
              % ("  ↳", b[0], b[1], b[2], b[3], (b[0] + b[1] + b[2]) / 1e6))
