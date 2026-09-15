#!/usr/bin/env bash
# One-cell smoke of the EET arm (baseline_eet). Picks an instance where THEIR
# retrieval fires (so both levers are active -- their coupling disables the
# confidence hook when nothing is retrieved). Same flags as the 30-cell run.
# Check afterwards:
#   results/swe1_eet_smoke/*/eet/retrieval.json   which experience was injected, similarity
#   results/swe1_eet_smoke/*/eet/events.jsonl     every key step, score, submission prompt
#   results/swe1_eet_smoke/*/eet/state.json       their workflow_progress at the end
#   results/swe1_eet_smoke/*/prompt.md            the injected <retrieved_experiences> block
cd /home/tempadmin/Code_rep_mcp_tools || exit 1
CLAUDE_PIN=/home/tempadmin/.local/share/claude/versions/2.1.220
.venv/bin/python - <<'PY' > results/_swe1_eet_smoke_ids.json
import json, sys
sys.path.insert(0, ".")
from experiments.swebench_pro import eet_arm, instances
ids = json.load(open("results/_swe30_flow_ids.json"))
insts = {i["instance_id"]: i for i in instances.load_subset(ids)}
# the highest-similarity firing cell on qutebrowser (smallest repo -> fastest cell)
best, best_sim = None, -1.0
for iid in ids:
    if "qutebrowser" not in iid:
        continue
    r = eet_arm.retrieve(insts[iid].get("problem_statement") or "", iid)
    if r and r[0][1] > best_sim:
        best, best_sim = iid, r[0][1]
if best is None:                    # fall back to any firing cell
    for iid in ids:
        r = eet_arm.retrieve(insts[iid].get("problem_statement") or "", iid)
        if r and r[0][1] > best_sim:
            best, best_sim = iid, r[0][1]
print(json.dumps([best]))
sys.stderr.write("smoke instance: %s (similarity %.3f)\n" % (best, best_sim))
PY
cat results/_swe1_eet_smoke_ids.json
nohup setsid env REPR_TEST_SHIM=1 .venv/bin/python -m experiments.swebench_pro.run_experiment \
  --ids-file results/_swe1_eet_smoke_ids.json \
  --conditions baseline_eet \
  --exp-name swe1_eet_smoke \
  --model claude-haiku-4-5 \
  --effort-flag low \
  --concurrency 1 --grade \
  --claude-bin "$CLAUDE_PIN" \
  >> swe1_eet_smoke.log 2>&1 < /dev/null &
echo "launched; tail -f swe1_eet_smoke.log"
