"""Regrade swe30_agentsmd (or any exp dir) cells whose docker grading failed
(docker_pull_failed / docker_unavailable) during the 2026-09-03 WSL vsock /
credential-helper window. Agent artifacts (patch.diff, result.json) are
intact -- only the grading step is redone, in place, via grade.grade().

Usage: .venv/bin/python _regrade_agentsmd_pullfail.py [results/exp_dir]
"""
import glob
import json
import sys
from pathlib import Path

from experiments.swebench_pro import grade, instances

exp = sys.argv[1] if len(sys.argv) > 1 else "results/swe30_agentsmd"
cells = []
for g in glob.glob(exp + "/*/grade.json"):
    try:
        j = json.load(open(g))
    except Exception:
        cells.append(Path(g).parent)
        continue
    if j.get("reason") in ("docker_pull_failed", "docker_unavailable"):
        cells.append(Path(g).parent)

ids = sorted({c.name.rsplit("__", 1)[0] for c in cells})
by = {i["instance_id"]: i for i in instances.load_subset(ids)}
print("regrading %d cells (%d instances) in %s" % (len(cells), len(ids), exp), flush=True)
for c in sorted(cells):
    iid = c.name.rsplit("__", 1)[0]
    inst = by.get(iid)
    if inst is None:
        print("SKIP (no instance record):", c.name, flush=True)
        continue
    rec = grade.grade(inst, c)
    print("%s -> resolved=%s reason=%s" % (c.name[-60:], rec.get("resolved"), rec.get("reason")), flush=True)
print("done", flush=True)
