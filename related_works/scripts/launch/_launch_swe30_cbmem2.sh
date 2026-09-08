#!/usr/bin/env bash
# Codebase-Memory MCP fidelity rerun (arXiv 2603.27277, baseline_cbmem2):
# the tool AS ITS INSTALLER CONFIGURES CLAUDE CODE (server name
# codebase-memory-mcp, shipped skill + 3 graph agents + SessionStart /
# SubagentStart / PreToolUse Grep|Glob / PostToolUse Read hooks), short
# project name, NO extra steer sentence. Paper config (REPR_TEST_SHIM=1,
# haiku/low, unrestricted baseline toolset + the MCP tools) on the PINNED CLI
# build 2.1.220 (the swe30_shim baseline's build; user rule 2026-09-03).
#
# Usage: ./_launch_swe30_cbmem2.sh [smoke]   -- "smoke" = 1 instance, exp swe1_cbmem2_smoke
cd /home/tempadmin/Code_rep_mcp_tools || exit 1
CLAUDE_PIN=/home/tempadmin/.local/share/claude/versions/2.1.220
if [ "${1:-}" = "smoke" ]; then
  IDS=results/_swe30_base_smoke_id.json; EXP=${2:-swe1_cbmem2_smoke}; CONC=1
else
  IDS=results/_swe30_flow_ids.json; EXP=swe30_cbmem2; CONC=2
fi
# refuse to start on a dead OAuth session (1-turn 401 cells corrupt the grid)
LEFT=$(python3 - <<'EOF'
import json, os, time
try:
    d = json.load(open(os.path.expanduser("~/.claude/.credentials.json")))["claudeAiOauth"]
    print(int((d["expiresAt"] / 1000 - time.time()) / 60) if d.get("accessToken") else -1)
except Exception:
    print(-1)
EOF
)
if [ "$LEFT" -lt 90 ]; then
  echo "OAuth token has $LEFT min left (or no token): re-login with \`claude\` (/login) in WSL first." >&2
  exit 2
fi
REPR_TEST_SHIM=1 nohup setsid .venv/bin/python -m experiments.swebench_pro.run_experiment \
  --ids-file "$IDS" \
  --conditions baseline_cbmem2 \
  --exp-name "$EXP" \
  --model claude-haiku-4-5 \
  --effort-flag low \
  --concurrency "$CONC" --grade \
  --claude-bin "$CLAUDE_PIN" \
  >> "$EXP.log" 2>&1 < /dev/null &
echo "launched $EXP (pid $!) -> $EXP.log"
