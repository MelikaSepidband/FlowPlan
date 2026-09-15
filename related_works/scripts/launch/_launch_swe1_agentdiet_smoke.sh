#!/usr/bin/env bash
# One-cell smoke of the AgentDiet arm (baseline_agentdiet) on the same
# qutebrowser instance as the SHERLOC smoke. Same flags as the 30-cell run.
# Writes results/swe1_agentdiet_smoke/. Check afterwards:
#   results/swe1_agentdiet_smoke/*/agentdiet_events.jsonl       per-turn decisions (too_short / erased / no_erase / request_rewritten)
#   results/swe1_agentdiet_smoke/*/agentdiet_reflections.jsonl  one record per reflection call with its usage
#   results/swe1_agentdiet_smoke/*/agentdiet_summary.json       their metrics (analysis_count, erase_tot_count, erase_in/out_tokens)
#   results/swe1_agentdiet_smoke/*/agentdiet_proxy.log          proxy stdout/stderr
#   results/swe1_agentdiet_smoke/*/result.json                  merged usage (key "agentdiet"); result_main.json = the agent session alone
cd /home/tempadmin/Code_rep_mcp_tools || exit 1
CLAUDE_PIN=/home/tempadmin/.local/share/claude/versions/2.1.220
printf '["instance_qutebrowser__qutebrowser-1a9e74bfaf9a9db2a510dc14572d33ded6040a57-v%s"]\n' \
  "$(python3 -c "import json;ids=json.load(open('results/_swe30_flow_ids.json'));print([i for i in ids if 'qutebrowser-1a9e74bf' in i][0].split('-v')[-1])")" \
  > results/_swe1_agentdiet_smoke_ids.json
cat results/_swe1_agentdiet_smoke_ids.json
nohup setsid env REPR_TEST_SHIM=1 .venv/bin/python -m experiments.swebench_pro.run_experiment \
  --ids-file results/_swe1_agentdiet_smoke_ids.json \
  --conditions baseline_agentdiet \
  --exp-name swe1_agentdiet_smoke \
  --model claude-haiku-4-5 \
  --effort-flag low \
  --concurrency 1 --grade \
  --claude-bin "$CLAUDE_PIN" \
  >> swe1_agentdiet_smoke.log 2>&1 < /dev/null &
echo "launched; tail -f swe1_agentdiet_smoke.log"
