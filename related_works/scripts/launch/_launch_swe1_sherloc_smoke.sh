#!/usr/bin/env bash
# One-cell smoke of the SHERLOC arm (baseline_sherloc) on a qutebrowser
# instance from the 30-set (smallest repository tree of the three repos).
# Same flags as the 30-cell run. Writes results/swe1_sherloc_smoke/.
# Check afterwards:
#   results/swe1_sherloc_smoke/*/sherloc/localization.json   status/reason, n_tool_calls, locations
#   results/swe1_sherloc_smoke/*/sherloc/sherloc_tools.jsonl  every tool call the server answered
#   results/swe1_sherloc_smoke/*/sherloc/sherloc_calls.jsonl  hook log (loop / final-turn injections)
#   results/swe1_sherloc_smoke/*/sherloc_injected.txt         what the repair session was given
#   results/swe1_sherloc_smoke/*/result.json                  merged usage (key "sherloc" = the stage)
cd /home/tempadmin/Code_rep_mcp_tools || exit 1
CLAUDE_PIN=/home/tempadmin/.local/share/claude/versions/2.1.220
printf '["instance_qutebrowser__qutebrowser-1a9e74bfaf9a9db2a510dc14572d33ded6040a57-v%s"]\n' \
  "$(python3 -c "import json;ids=json.load(open('results/_swe30_flow_ids.json'));print([i for i in ids if 'qutebrowser-1a9e74bf' in i][0].split('-v')[-1])")" \
  > results/_swe1_sherloc_smoke_ids.json
cat results/_swe1_sherloc_smoke_ids.json
nohup setsid env REPR_TEST_SHIM=1 .venv/bin/python -m experiments.swebench_pro.run_experiment \
  --ids-file results/_swe1_sherloc_smoke_ids.json \
  --conditions baseline_sherloc \
  --exp-name swe1_sherloc_smoke \
  --model claude-haiku-4-5 \
  --effort-flag low \
  --concurrency 1 --grade \
  --claude-bin "$CLAUDE_PIN" \
  >> swe1_sherloc_smoke.log 2>&1 < /dev/null &
echo "launched; tail -f swe1_sherloc_smoke.log"
