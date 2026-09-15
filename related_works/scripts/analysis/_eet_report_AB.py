"""Paired report for the two EET arms against the shim baseline.

  swe30_eet   arm A - their shipped 205-record base (SWE-bench Lite/Verified)
  swe30_eetB  arm B - a 108-record base built from OUR held-out trajectories
                      with THEIR pipeline (frozen split results/_eet_split.json)

Everything except the experience base is identical between the arms, so the
difference between them isolates base quality. Cells where retrieval did not
fire are pure baseline (their own coupling disables the confidence hook), so
they are reported separately -- pooling them dilutes the effect in whichever
arm fires less often.
"""
import glob
import json
import os
import re
from collections import Counter

R = "/home/tempadmin/Code_rep_mcp_tools"
ARMS = [("arm A (their base)", "swe30_eet", "__baseline_eet"),
        ("arm B (our base)", "swe30_eetB", "__baseline_eetB")]


def L(p):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return {}


def counted(r):
    u = (r or {}).get("usage") or {}
    return (u.get("input_tokens", 0) + u.get("output_tokens", 0)
            + u.get("cache_read_input_tokens", 0))


def full(r):
    u = (r or {}).get("usage") or {}
    return counted(r) + u.get("cache_creation_input_tokens", 0)


def scores_of(d):
    out = []
    t = os.path.join(d, "transcript.jsonl")
    if not os.path.exists(t):
        return out
    for line in open(t, encoding="utf-8", errors="replace"):
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("type") != "assistant":
            continue
        for b in (rec.get("message") or {}).get("content") or []:
            if isinstance(b, dict):
                x = b.get("text") or b.get("thinking") or ""
                for m in re.finditer(r"CONFIDENCE_SCORE:\s*(\d{1,3})", x):
                    out.append(int(m.group(1)))
    return out


def collect(exp, suffix):
    rows = []
    for d in sorted(glob.glob(os.path.join(R, "results", exp, "*/"))):
        name = os.path.basename(d.rstrip("/"))
        if not name.endswith(suffix):
            continue
        g = L(os.path.join(d, "grade.json"))
        if not g:
            continue
        key = name[: -len(suffix)]
        bd = os.path.join(R, "results", "swe30_shim", key + "__baseline")
        bg, br = L(os.path.join(bd, "grade.json")), L(os.path.join(bd, "result.json"))
        if not bg:
            continue
        ret = L(os.path.join(d, "eet", "retrieval.json"))
        ev = []
        ep = os.path.join(d, "eet", "events.jsonl")
        if os.path.exists(ep):
            ev = [json.loads(l) for l in open(ep, encoding="utf-8") if l.strip()]
        sc = scores_of(d)
        m = (ret.get("matches") or [{}])[0] if ret.get("matches") else {}
        repo = key.split("instance_")[-1].split("-")[0]
        rows.append(dict(key=key, repo=repo, quar=bool(g.get("quarantined")),
                         res=bool(g.get("resolved")), bres=bool(bg.get("resolved")),
                         r=L(os.path.join(d, "result.json")), b=br,
                         fired=bool(ret.get("fired")), sim=m.get("similarity"),
                         match=m.get("issue_id"),
                         same_repo=bool(m.get("issue_id") and repo in m["issue_id"]),
                         checks=sum(1 for e in ev if e.get("emitted") == "confidence_check"),
                         subs=sum(1 for e in ev if e.get("emitted") == "submission"),
                         scores=sc, maxscore=max(sc) if sc else None))
    return rows


def agg(sel, key):
    n = len(sel)
    if not n:
        return None
    return dict(n=n,
                res=sum(1 for x in sel if (x["res"] if key == "r" else x["bres"])),
                counted=sum(counted(x[key]) for x in sel) / n,
                full=sum(full(x[key]) for x in sel) / n,
                usd=sum((x[key].get("total_cost_usd") or 0) for x in sel) / n,
                turns=sum((x[key].get("num_turns") or 0) for x in sel) / n)


def block(label, sel):
    if not sel:
        print("\n%s: no cells" % label)
        return
    A, B = agg(sel, "r"), agg(sel, "b")
    print("\n%s (n=%d)" % (label, len(sel)))
    print("  %-10s %-9s %-11s %-11s %-9s %s" % ("arm", "resolved", "counted", "full", "$/inst", "turns"))
    print("  %-10s %-9s %-11.3fM %-11.3fM $%-8.3f %.1f" % (
        "baseline", "%d/%d" % (B["res"], B["n"]), B["counted"] / 1e6, B["full"] / 1e6, B["usd"], B["turns"]))
    print("  %-10s %-9s %-11.3fM %-11.3fM $%-8.3f %.1f" % (
        "EET", "%d/%d" % (A["res"], A["n"]), A["counted"] / 1e6, A["full"] / 1e6, A["usd"], A["turns"]))
    print("  ratios: counted %.2fx | full %.2fx | $ %.2fx | turns %.2fx" % (
        A["counted"] / B["counted"], A["full"] / B["full"], A["usd"] / B["usd"], A["turns"] / B["turns"]))


data = {}
for label, exp, suffix in ARMS:
    rows = collect(exp, suffix)
    data[exp] = rows
    fired = [x for x in rows if x["fired"]]
    print("=" * 78)
    print("%s  --  %s" % (label, exp))
    print("=" * 78)
    print("paired cells %d | quarantined %d" % (len(rows), sum(1 for x in rows if x["quar"])))
    if fired:
        print("retrieval fired %d/%d (%.0f%%) | same-repo %d/%d | distinct experiences %d | median sim %.3f"
              % (len(fired), len(rows), 100 * len(fired) / max(1, len(rows)),
                 sum(1 for x in fired if x["same_repo"]), len(fired),
                 len(set(x["match"] for x in fired)),
                 sorted(x["sim"] for x in fired)[len(fired) // 2]))
        ch = sum(x["checks"] for x in fired)
        sb = sum(x["subs"] for x in fired)
        ms = [x["maxscore"] for x in fired if x["maxscore"] is not None]
        print("confidence: %d checks over the firing cells, %d submission prompts, "
              "%d/%d cells reached >=81 (max score seen %s)"
              % (ch, sb, sum(1 for m in ms if m >= 81), len(ms), max(ms) if ms else None))
    block("ALL PAIRED CELLS", rows)
    block("cells WHERE RETRIEVAL FIRED (both levers active)", fired)
    block("cells with NO retrieval (pure baseline by their coupling)",
          [x for x in rows if not x["fired"]])
    byrepo = {}
    for x in rows:
        e = byrepo.setdefault(x["repo"], [0, 0, 0, 0])
        e[0] += 1; e[1] += x["res"]; e[2] += x["bres"]; e[3] += 1 if x["fired"] else 0
    print("\nby repo (n, EET resolved, baseline resolved, fired):",
          {k: tuple(v) for k, v in sorted(byrepo.items())})
    print()

# head-to-head on the instances where BOTH arms retrieved
A = {x["key"]: x for x in data["swe30_eet"]}
B = {x["key"]: x for x in data["swe30_eetB"]}
both = [k for k in A if k in B and A[k]["fired"] and B[k]["fired"]]
print("=" * 78)
print("HEAD-TO-HEAD on instances where BOTH arms retrieved (n=%d)" % len(both))
print("=" * 78)
if both:
    for nm, D in (("arm A", A), ("arm B", B)):
        sel = [D[k] for k in both]
        a = agg(sel, "r")
        print("  %-8s resolved %d/%d | counted %.3fM | $%.3f | turns %.1f | same-repo %d/%d" % (
            nm, a["res"], a["n"], a["counted"] / 1e6, a["usd"], a["turns"],
            sum(1 for x in sel if x["same_repo"]), len(sel)))
    bl = agg([A[k] for k in both], "b")
    print("  %-8s resolved %d/%d | counted %.3fM | $%.3f | turns %.1f" % (
        "baseline", bl["res"], bl["n"], bl["counted"] / 1e6, bl["usd"], bl["turns"]))
else:
    print("  none")
print()
print("NOTE: cells where retrieval did not fire run as pure baseline (their coupling "
      "disables the confidence hook), so the ALL-CELLS rows dilute the mechanism in "
      "whichever arm fires less often. The FIRED rows are the mechanism's effect.")
