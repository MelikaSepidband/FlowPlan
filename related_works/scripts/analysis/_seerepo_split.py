"""Did SeeRepo save tokens in the cells where the agent ACTUALLY called the graph?
Splits the swe30_seerepo arm by whether seerepo_tools.jsonl records >=1 call, and
pairs each subset against its own baseline cells."""
import glob
import json
import os

R = "/home/tempadmin/Code_rep_mcp_tools"


def L(p):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return {}


def tk(x):
    u = (x or {}).get("usage") or {}
    return (u.get("input_tokens", 0) + u.get("output_tokens", 0)
            + u.get("cache_read_input_tokens", 0))


def cc(x):
    return ((x or {}).get("usage") or {}).get("cache_creation_input_tokens", 0)


rows = []
for d in sorted(glob.glob(os.path.join(R, "results", "swe30_seerepo", "*__baseline_seerepo/"))):
    name = os.path.basename(d.rstrip("/"))
    g = L(os.path.join(d, "grade.json"))
    if not g:
        continue
    key = name[: -len("__baseline_seerepo")]
    b = os.path.join(R, "results", "swe30_shim", key + "__baseline")
    bg, br = L(b + "/grade.json"), L(b + "/result.json")
    if not bg:
        continue
    calls = 0
    tj = os.path.join(d, "seerepo", "seerepo_tools.jsonl")
    if os.path.exists(tj):
        calls = sum(1 for line in open(tj, encoding="utf-8") if line.strip())
    r = L(os.path.join(d, "result.json"))
    rows.append(dict(repo=key.split("instance_")[-1].split("-")[0], calls=calls,
                     res=bool(g.get("resolved")), bres=bool(bg.get("resolved")),
                     t=tk(r), bt=tk(br), cc=cc(r), bcc=cc(br),
                     u=r.get("total_cost_usd") or 0, bu=br.get("total_cost_usd") or 0,
                     n=r.get("num_turns") or 0, bn=br.get("num_turns") or 0))


def block(label, sel):
    if not sel:
        print("\n%s: none" % label)
        return
    m = len(sel)
    f = lambda k: sum(x[k] for x in sel) / m
    print("\n%s  (n=%d)" % (label, m))
    print("  resolved      %d/%d   (baseline %d/%d)" % (sum(x["res"] for x in sel), m,
                                                        sum(x["bres"] for x in sel), m))
    print("  counted       %.3fM vs %.3fM   = %.2fx" % (f("t") / 1e6, f("bt") / 1e6, f("t") / f("bt")))
    print("  cache_creation %.1fK vs %.1fK  = %.2fx" % (f("cc") / 1e3, f("bcc") / 1e3, f("cc") / f("bcc")))
    print("  dollars       $%.3f vs $%.3f  = %.2fx" % (f("u"), f("bu"), f("u") / f("bu")))
    print("  turns         %.1f vs %.1f    = %.2fx" % (f("n"), f("bn"), f("n") / f("bn")))
    print("  graph calls   %.1f/cell" % f("calls"))


used = [x for x in rows if x["calls"] > 0]
unused = [x for x in rows if x["calls"] == 0]
print("SeeRepo split by whether the agent called the graph tool")
block("CELLS THAT USED THE GRAPH", used)
block("CELLS THAT NEVER CALLED IT", unused)
block("ALL CELLS", rows)
print("\nper-cell detail (used):")
for x in sorted(used, key=lambda y: -y["calls"]):
    print("  %-9s calls=%-3d %.2fx tokens  %.2fx $  turns %d vs %d  %s" % (
        x["repo"][:9], x["calls"], x["t"] / x["bt"], x["u"] / x["bu"], x["n"], x["bn"],
        "resolved" if x["res"] else ""))
