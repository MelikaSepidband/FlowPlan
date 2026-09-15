"""Offline probe: run THEIR retrieval (their store.py, their experience base, their
defaults top_k=1 / min_similarity=0.15 / use_tfidf=True / search_by_issue) against the
problem statements of our 30 SWE-bench Pro instances, and report the fire rate.

Their agent disables the confidence-check lever entirely when nothing is retrieved
(experience_retrieval.py:531-535), so the fire rate bounds how much of the method can
act on our benchmark at all. No LLM calls.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EET = ROOT / "tools" / "eet_src" / "mini-swe-agent" / "src"
sys.path.insert(0, str(EET))
sys.path.insert(0, str(ROOT))

from minisweagent.experience.store import ExperienceStore  # noqa: E402

STORE = EET / "minisweagent" / "experience" / "extracted_experiences_summarized.jsonl"
TOP_K = 1
MIN_SIM = 0.15

store = ExperienceStore(str(STORE))
print("experience base: %d records from %s" % (len(store.get_all()), STORE.name))

from experiments.swebench_pro import instances as inst_mod  # noqa: E402

ids = json.load(open(ROOT / "results" / "_swe30_flow_ids.json"))
insts = {i["instance_id"]: i for i in inst_mod.load_subset(ids)}
rows = []
for iid in ids:
    inst = insts[iid]
    ps = inst.get("problem_statement") or ""
    # their call, their defaults (top_k*2 then truncate, as the agent does)
    res = store.search_by_issue_similarity(ps, top_k=TOP_K * 2, min_similarity=MIN_SIM,
                                           use_tfidf=True)
    res = res[:TOP_K]
    # also record the best similarity with NO threshold, to see how far below 0.15 we are
    allres = store.search_by_issue_similarity(ps, top_k=1, min_similarity=0.0, use_tfidf=True)
    best = allres[0][1] if allres else 0.0
    rows.append({"id": iid, "repo": iid.split("instance_")[-1].split("-")[0],
                 "ps_chars": len(ps), "fired": bool(res),
                 "sim": res[0][1] if res else None, "best_sim_unfiltered": best,
                 "matched": res[0][0].issue_id if res else (allres[0][0].issue_id if allres else None)})

fired = [r for r in rows if r["fired"]]
print("\nFIRE RATE at their min_similarity=%.2f: %d/%d cells (%.0f%%)" % (
    MIN_SIM, len(fired), len(rows), 100 * len(fired) / len(rows)))
sims = sorted(r["best_sim_unfiltered"] for r in rows)
print("best-match similarity across the 30 (no threshold): min %.3f  median %.3f  max %.3f" % (
    sims[0], sims[len(sims) // 2], sims[-1]))
for thr in (0.05, 0.10, 0.15, 0.20, 0.30):
    n = sum(1 for r in rows if r["best_sim_unfiltered"] >= thr)
    print("   cells with a match >= %.2f : %2d/30" % (thr, n))
print("\nper-cell:")
for r in sorted(rows, key=lambda x: -x["best_sim_unfiltered"]):
    print("  %-8s sim=%.3f %-6s ps=%5dch  <- %s" % (
        r["repo"][:8], r["best_sim_unfiltered"], "FIRE" if r["fired"] else "", r["ps_chars"],
        r["matched"]))
json.dump(rows, open(ROOT / "results" / "_eet_retrieval_probe.json", "w"), indent=2)
print("\nwrote results/_eet_retrieval_probe.json")
