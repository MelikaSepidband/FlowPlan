"""Freeze the durable TEST / BASE split over the 266 SWE-bench Pro python instances.

Why a frozen split: the EET arm-B experience base must never contain an instance
we later evaluate on. Building it from "everything except today's 30" would leak
the moment the test set grows to 158. So the split is decided once, written to a
file, and both the base builder and the arm read it.

Sizes (user's plan): 158 reserved for testing (the current 30 plus future
additions), 108 available for the experience base.

Assignment rule, in priority order:
  1. All 30 current test instances -> TEST (they are the evaluation set today).
  2. Instances with NO agent transcript can never contribute to the base
     (extraction needs a trajectory), so they go to TEST first -- this costs the
     base nothing and maximises the number of transcript-bearing instances left
     for it.
  3. Fill the remaining TEST slots from transcript-bearing instances, sampled
     deterministically (seed 42) and stratified by repo so neither side is
     skewed toward one project.
  4. Everything else -> BASE (of which only the transcript-bearing ones can
     actually be extracted from).

Usage: .venv/bin/python _freeze_eet_split.py [--write]
"""
import collections
import glob
import json
import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from experiments.swebench_pro import instances as I  # noqa: E402

OUT = ROOT / "results" / "_eet_split.json"
N_TEST = 158

allids = list(I.manifest_ids())
test30 = set(json.load(open(ROOT / "results" / "_swe30_flow_ids.json")))
have_tr = set(os.path.basename(d).replace("__baseline", "")
              for d in glob.glob(str(ROOT / "results" / "swe183" / "*__baseline")))


def repo(i):
    return i.split("instance_")[-1].split("-")[0]


assert test30 <= set(allids), "test30 not inside the manifest"
print("pool %d | with transcripts %d | without %d | current test set %d"
      % (len(allids), len(have_tr & set(allids)), len(set(allids) - have_tr), len(test30)))

test = set(test30)
# rule 2: transcript-less instances are free to reserve for testing
no_tr = [i for i in allids if i not in have_tr and i not in test]
for i in sorted(no_tr):
    if len(test) < N_TEST:
        test.add(i)
print("after reserving transcript-less instances: test=%d" % len(test))

# rule 3: fill the rest from transcript-bearing instances, stratified by repo
remaining = [i for i in allids if i not in test]
need = N_TEST - len(test)
if need > 0:
    by_repo = collections.defaultdict(list)
    for i in remaining:
        by_repo[repo(i)].append(i)
    total = len(remaining)
    rng = random.Random(42)
    picked = []
    for r in sorted(by_repo):
        share = round(need * len(by_repo[r]) / total)
        picked += rng.sample(sorted(by_repo[r]), min(share, len(by_repo[r])))
    # correct any rounding drift deterministically
    pool = [i for i in sorted(remaining) if i not in set(picked)]
    while len(picked) < need and pool:
        picked.append(pool.pop(0))
    picked = picked[:need]
    test |= set(picked)
print("after stratified fill: test=%d" % len(test))

base = [i for i in allids if i not in test]
base_extractable = [i for i in base if i in have_tr]

print()
print("TEST : %d  (transcript-bearing: %d)" % (len(test), len(set(test) & have_tr)))
print("BASE : %d  (extractable, i.e. has a transcript: %d)" % (len(base), len(base_extractable)))
print("  TEST by repo:", dict(collections.Counter(repo(i) for i in sorted(test))))
print("  BASE by repo:", dict(collections.Counter(repo(i) for i in base)))
print("  BASE-extractable by repo:", dict(collections.Counter(repo(i) for i in base_extractable)))
print()
print("checks:")
print("  current 30 all in TEST      :", test30 <= test)
print("  TEST and BASE disjoint      :", not (test & set(base)))
print("  TEST + BASE == pool         :", len(test) + len(base) == len(allids))
print("  no base instance in TEST    :", not (set(base_extractable) & test))

if "--write" in sys.argv:
    OUT.write_text(json.dumps({
        "created": "2026-09-10",
        "pool": len(allids),
        "n_test_reserved": len(test),
        "n_base": len(base),
        "n_base_extractable": len(base_extractable),
        "rule": ("current-30 first, then transcript-less instances (they can never "
                 "feed the base), then a seed-42 repo-stratified fill; remainder = base"),
        "seed": 42,
        "test_ids": sorted(test),
        "base_ids": sorted(base),
        "base_extractable_ids": sorted(base_extractable),
        "current_test30": sorted(test30),
    }, indent=2), encoding="utf-8")
    print("\nwrote", OUT)
else:
    print("\ndry run; pass --write to freeze")
