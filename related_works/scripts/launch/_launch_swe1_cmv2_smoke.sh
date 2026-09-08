#!/usr/bin/env bash
# 1-cell smoke for the CMV faithful relay arm (baseline_cmv2) on pinned CLI
# 2.1.220 -- same instance the old swe1_cmv_smoke used. Run BEFORE the grid.
# What to check afterwards (results/swe1_cmv2_smoke/*__baseline_cmv2/):
#   cmv_trims.jsonl        -> a trim happened (only if the transcript passed
#                             the 600KB gate; export CMV_SIZE_THRESHOLD=200000
#                             for a forced-trim smoke of the relay mechanics)
#   result_leg1.json, result_leg2.json -> the session was resumed
#   result.json.cmv_relay  -> legs + trims ledger, usage summed over legs
#   transcript.jsonl       -> '[Trimmed' stubs present AND the first assistant
#                             line after the trim shows cache_read ~0 with a
#                             cache_creation write ~ the trimmed context
#                             (= the trim reached the model)
cd /home/tempadmin/Code_rep_mcp_tools || exit 1
CLAUDE_PIN=/home/tempadmin/.local/share/claude/versions/2.1.220
REPR_TEST_SHIM=1 .venv/bin/python -m experiments.swebench_pro.run_experiment \
  --ids-file results/_swe1_cmv2_smoke_id.json \
  --conditions baseline_cmv2 \
  --exp-name swe1_cmv2_smoke \
  --model claude-haiku-4-5 \
  --effort-flag low \
  --concurrency 1 --grade \
  --claude-bin "$CLAUDE_PIN" \
  >> swe1_cmv2_smoke.log 2>&1
