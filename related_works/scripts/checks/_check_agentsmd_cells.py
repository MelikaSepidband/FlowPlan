"""Integrity check for baseline_agentsmd cells (AGENTS.md replication arm).

Per cell: CLI build from the transcript, whether the repo-root CLAUDE.md text
reached the model's context (a distinctive phrase from the per-repo file is
looked for in the transcript), whether CLAUDE.md leaked into patch.diff,
whether the worktree copy was removed, turns/tokens/cost, and grade.

Usage: python _check_agentsmd_cells.py results/<exp_dir>
"""
import glob
import json
import os
import re
import sys

exp = sys.argv[1] if len(sys.argv) > 1 else "results/swe1_agentsmd_smoke"
MARKERS = {
    "ansible__ansible": "how a play runs",
    "internetarchive__openlibrary": "request lifecycle",
    "qutebrowser__qutebrowser": "generated files",
}
rows = []
for c in sorted(glob.glob(os.path.join(exp, "*__baseline_agentsmd"))):
    iid = os.path.basename(c).rsplit("__", 1)[0]
    m = re.match(r"instance_(.+?)-[0-9a-f]{40}-v", iid)
    repo = m.group(1) if m else "?"
    marker = MARKERS.get(repo, "CLAUDE.md")
    ver = None
    in_ctx = False
    calls = 0
    tp = os.path.join(c, "transcript.jsonl")
    if os.path.exists(tp):
        for line in open(tp, encoding="utf-8", errors="replace"):
            if ver is None:
                mm = re.search(r'"version":\s*"([0-9.]+)"', line)
                if mm:
                    ver = mm.group(1)
            if marker in line:
                in_ctx = True
            if '"type":"assistant"' in line or '"type": "assistant"' in line:
                calls += 1
    patch = open(os.path.join(c, "patch.diff"), encoding="utf-8", errors="replace").read() if os.path.exists(os.path.join(c, "patch.diff")) else ""
    leak = "CLAUDE.md" in patch
    try:
        meta = json.load(open(os.path.join(c, "meta.json")))
        wt = meta.get("worktree", "")
    except Exception:
        wt = ""
    wt_md = os.path.exists(os.path.join(wt, "CLAUDE.md")) if wt and os.path.isdir(wt) else None
    try:
        r = json.load(open(os.path.join(c, "result.json")))
        u = r.get("usage") or {}
        tok = (u.get("input_tokens", 0) + u.get("output_tokens", 0) + u.get("cache_read_input_tokens", 0))
        out = u.get("output_tokens", 0)
        cc = u.get("cache_creation_input_tokens", 0)
        cost = r.get("total_cost_usd")
        turns = r.get("num_turns")
        sub = r.get("subtype")
    except Exception:
        tok = out = cc = cost = turns = sub = None
    try:
        g = json.load(open(os.path.join(c, "grade.json")))
        grade = g.get("resolved")
    except Exception:
        grade = None
    rows.append((iid[-24:], repo, ver, in_ctx, leak, wt_md, turns, tok, out, cc, cost, sub, grade, len(patch)))
print("%-26s %-22s %-8s %-7s %-6s %-9s %5s %9s %7s %7s %7s %-14s %-8s %s" % (
    "cell", "repo", "build", "md_ctx", "leak", "wt_md_left", "turns", "counted", "out", "cc", "cost", "subtype", "resolved", "patch_bytes"))
for r in rows:
    print("%-26s %-22s %-8s %-7s %-6s %-9s %5s %9s %7s %7s %7s %-14s %-8s %s" % (
        r[0], r[1][:22], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], ("%.3f" % r[10]) if r[10] is not None else None, r[11], r[12], r[13]))
n = len(rows)
if n:
    print("\ncells %d | build 2.1.220: %d | CLAUDE.md in context: %d | patch leaks: %d | worktree copy left behind: %d" % (
        n, sum(1 for r in rows if r[2] == "2.1.220"), sum(1 for r in rows if r[3]), sum(1 for r in rows if r[4]), sum(1 for r in rows if r[5])))
