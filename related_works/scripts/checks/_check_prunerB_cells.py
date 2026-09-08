"""Integrity + engagement check for baseline_prunerB cells (SWE-Pruner Route B).

Per cell: CLI build, pruned-tool calls (with / without a context_focus_question),
native tool calls that should be impossible (Bash/Read/Grep/Glob/Edit/Write),
pruner feedback strings seen by the model, pruned-vs-unpruned observation sizes,
turns/tokens/cost, grade.  Runs fine over the UNC path (no WSL needed).

Usage: python _check_prunerB_cells.py results/<exp_dir> [prunerB_events.jsonl]
"""
import glob
import json
import os
import re
import statistics as st
import sys

exp = sys.argv[1] if len(sys.argv) > 1 else "results/swe1_prunerB_smoke"
FILTERED = "Filtered some unrelevant parts judged by your context_focus_question"
ALLREL = "All outputs are judged as relevent!"
PERR = "[Pruner Error]"
NATIVE = {"Bash", "Read", "Grep", "Glob", "Edit", "Write", "MultiEdit", "NotebookEdit"}
rows = []
for c in sorted(glob.glob(os.path.join(exp, "*__baseline_prunerB"))):
    iid = os.path.basename(c).rsplit("__", 1)[0]
    ver = None
    wq = wnq = native = 0
    filt = allrel = perr = 0
    sizes_q, sizes_nq = [], []
    results = {}
    seen = set()
    tp = os.path.join(c, "transcript.jsonl")
    if os.path.exists(tp):
        for line in open(tp, encoding="utf-8", errors="replace"):
            if ver is None:
                mm = re.search(r'"version":\s*"([0-9.]+)"', line)
                if mm:
                    ver = mm.group(1)
            try:
                j = json.loads(line)
            except Exception:
                continue
            m = j.get("message") or {}
            if j.get("type") == "assistant":
                for b in m.get("content") or []:
                    if not isinstance(b, dict) or b.get("type") != "tool_use" or b.get("id") in seen:
                        continue
                    seen.add(b.get("id"))
                    n = b.get("name", "")
                    inp = b.get("input") or {}
                    if n.startswith("mcp__pruned_tools__"):
                        q = bool((inp.get("context_focus_question") or "").strip())
                        wq += q
                        wnq += (not q)
                        results[b.get("id")] = q
                    elif n in NATIVE:
                        native += 1
            elif j.get("type") == "user" and isinstance(m.get("content"), list):
                for b in m["content"]:
                    if not isinstance(b, dict) or b.get("type") != "tool_result":
                        continue
                    x = b.get("content")
                    text = x if isinstance(x, str) else ("".join(y.get("text", "") for y in x if isinstance(y, dict)) if isinstance(x, list) else "")
                    filt += FILTERED in text
                    allrel += ALLREL in text
                    perr += PERR in text
                    if b.get("tool_use_id") in results:
                        (sizes_q if results[b["tool_use_id"]] else sizes_nq).append(len(text))
    patch = open(os.path.join(c, "patch.diff"), encoding="utf-8", errors="replace").read() if os.path.exists(os.path.join(c, "patch.diff")) else ""
    try:
        r = json.load(open(os.path.join(c, "result.json")))
        u = r.get("usage") or {}
        tok = u.get("input_tokens", 0) + u.get("output_tokens", 0) + u.get("cache_read_input_tokens", 0)
        out = u.get("output_tokens", 0)
        cost = r.get("total_cost_usd")
        turns = r.get("num_turns")
        sub = r.get("subtype")
    except Exception:
        tok = out = cost = turns = sub = None
    try:
        grade = json.load(open(os.path.join(c, "grade.json"))).get("resolved")
    except Exception:
        grade = None
    rows.append(dict(cell=iid[-24:], build=ver, wq=wq, wnq=wnq, native=native, filt=filt, allrel=allrel, perr=perr,
                     mq=int(st.median(sizes_q)) if sizes_q else None, mnq=int(st.median(sizes_nq)) if sizes_nq else None,
                     turns=turns, tok=tok, out=out, cost=cost, sub=sub, grade=grade, patch=len(patch)))
print("%-26s %-8s %5s %5s %6s %5s %6s %4s %7s %7s %5s %9s %6s %7s %-14s %-6s %s" % (
    "cell", "build", "q", "noq", "native", "filt", "allrel", "err", "med_q", "med_noq", "turns", "counted", "out", "cost", "subtype", "res", "patch"))
for r in rows:
    print("%-26s %-8s %5s %5s %6s %5s %6s %4s %7s %7s %5s %9s %6s %7s %-14s %-6s %s" % (
        r["cell"], r["build"], r["wq"], r["wnq"], r["native"], r["filt"], r["allrel"], r["perr"], r["mq"], r["mnq"],
        r["turns"], r["tok"], r["out"], ("%.3f" % r["cost"]) if r["cost"] is not None else None, r["sub"], r["grade"], r["patch"]))
if rows:
    n = len(rows)
    print("\ncells %d | build 2.1.220: %d | pruned-tool calls with question: %d / %d (%.0f%%) | native tool calls: %d | cells with >=1 question: %d | feedback filtered/allrel/err: %d/%d/%d" % (
        n, sum(1 for r in rows if r["build"] == "2.1.220"), sum(r["wq"] for r in rows), sum(r["wq"] + r["wnq"] for r in rows),
        100.0 * sum(r["wq"] for r in rows) / max(1, sum(r["wq"] + r["wnq"] for r in rows)), sum(r["native"] for r in rows),
        sum(1 for r in rows if r["wq"]), sum(r["filt"] for r in rows), sum(r["allrel"] for r in rows), sum(r["perr"] for r in rows)))
