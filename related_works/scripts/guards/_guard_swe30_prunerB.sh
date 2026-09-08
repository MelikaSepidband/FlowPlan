#!/usr/bin/env bash
# Self-healing guard for swe30_prunerB (SWE-Pruner Route B: the paper's pruning
# mechanism + their experiment template inside Claude Code; REPR_TEST_SHIM=1,
# haiku/low, pinned CLI 2.1.220). Keeps the skimmer service alive, and starts
# the 30-cell run ONLY after (a) swe30_agentsmd has 30 graded cells (user
# sequencing) and (b) the smoke marker results/.swe30_prunerB_smoke_ok exists.
cd /home/tempadmin/Code_rep_mcp_tools || exit 1
LOG=swe30_prunerB_guard.log
STATE=results/.swe30_prunerB_guard_state
TARGET=30
CLAUDE_PIN=/home/tempadmin/.local/share/claude/versions/2.1.220
VENV=/home/tempadmin/Code_rep_mcp_tools/tools/swepruner_venv

exec 9>/tmp/swe30_prunerB_guard.lock
if ! flock -n 9; then
  sleep 660
  exit 0
fi

start_skimmer() {
  (cd tools/swe-pruner/swe-pruner && nohup setsid "$VENV/bin/swe-pruner" \
      --model-path /home/tempadmin/Code_rep_mcp_tools/tools/models/code-pruner --port 8000 \
      >> /home/tempadmin/Code_rep_mcp_tools/pruner_server.log 2>&1 < /dev/null &)
  sleep 45
}

# skimmer keepalive (needed for in-flight cells regardless of runner state)
if ! curl -sf -m 5 http://127.0.0.1:8000/health >/dev/null 2>&1; then
  echo "$(date '+%m-%d %H:%M') skimmer down -> relaunch" >> "$LOG"
  start_skimmer
fi

G=$(ls results/swe30_prunerB/*/grade.json 2>/dev/null | wc -l)
if [ "$G" -ge "$TARGET" ]; then
  echo "$(date '+%m-%d %H:%M') DONE $G/$TARGET" >> "$LOG"
  sleep 60
  exit 0
fi

# sequencing gate: wait for swe30_agentsmd to finish and for the smoke marker
A=$(ls results/swe30_agentsmd/*/grade.json 2>/dev/null | wc -l)
if [ "$A" -lt 30 ] || [ ! -f results/.swe30_prunerB_smoke_ok ]; then
  echo "$(date '+%m-%d %H:%M') waiting: agentsmd $A/30, smoke_ok=$([ -f results/.swe30_prunerB_smoke_ok ] && echo yes || echo no)" >> "$LOG"
  sleep 60
  exit 0
fi

if pgrep -f "exp-nam[e] swe30_prunerB" >/dev/null; then
  echo "$(date '+%m-%d %H:%M') alive $G/$TARGET" >> "$LOG"
else
  read -r lastG stuck < "$STATE" 2>/dev/null || { lastG=-1; stuck=0; }
  if [ "$G" = "$lastG" ]; then stuck=$((stuck+1)); else stuck=1; fi
  echo "$G $stuck" > "$STATE"
  if [ "$stuck" -ge 3 ]; then
    for d in results/swe30_prunerB/*/; do
      [ -d "$d" ] || continue
      if [ ! -f "$d/grade.json" ]; then
        name=$(basename "$d")
        printf '{"instance_id": "%s", "resolved": false, "error": "quarantined: repeated runner crash at this cell", "quarantined": true}\n' \
          "$name" > "$d/grade.json"
        echo "$(date '+%m-%d %H:%M') QUARANTINE $name" >> "$LOG"
      fi
    done
    echo "0 0" > "$STATE"
  fi
  echo "$(date '+%m-%d %H:%M') runner dead at $G/$TARGET -> (re)launch (stuck=$stuck)" >> "$LOG"
  nohup setsid env REPR_TEST_SHIM=1 .venv/bin/python -m experiments.swebench_pro.run_experiment \
    --ids-file results/_swe30_flow_ids.json \
    --conditions baseline_prunerB \
    --exp-name swe30_prunerB \
    --model claude-haiku-4-5 \
    --effort-flag low \
    --concurrency 2 --grade \
    --claude-bin "$CLAUDE_PIN" \
    >> swe30_prunerB.log 2>&1 < /dev/null &
fi
sleep 660
