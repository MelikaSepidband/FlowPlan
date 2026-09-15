#!/usr/bin/env bash
# One-cell smoke of EET arm B (baseline_eetB): their method unchanged, but with
# the experience base built from OUR held-out trajectories (frozen split, 108
# base instances). Runs the SAME instance as the arm-A smoke so the two are
# directly comparable.
#   results/swe1_eetB_smoke/*/eet/retrieval.json  which experience, similarity, store_kind
#   results/swe1_eetB_smoke/*/eet/events.jsonl    key steps, scores, submission prompts
cd /home/tempadmin/Code_rep_mcp_tools || exit 1
CLAUDE_PIN=/home/tempadmin/.local/share/claude/versions/2.1.220
# instance chosen because BOTH arms retrieve on it (results/_eet_retrieval_AB.json),
# so arm A vs arm B is a like-for-like paired comparison. Written by the caller.
:
:
cat results/_swe1_eetB_smoke_ids.json
nohup setsid env REPR_TEST_SHIM=1 .venv/bin/python -m experiments.swebench_pro.run_experiment \
  --ids-file results/_swe1_eetB_smoke_ids.json \
  --conditions baseline_eetB \
  --exp-name swe1_eetB_smoke \
  --model claude-haiku-4-5 \
  --effort-flag low \
  --concurrency 1 --grade \
  --claude-bin "$CLAUDE_PIN" \
  >> swe1_eetB_smoke.log 2>&1 < /dev/null &
echo "launched; tail -f swe1_eetB_smoke.log"
