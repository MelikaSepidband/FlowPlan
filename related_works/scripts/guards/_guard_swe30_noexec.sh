#!/usr/bin/env bash
# Self-healing guard for swe30_noexec ("To Run or Not to Run", arXiv 2606.26978:
# baseline + their prompt-level prohibited-execution sentence; NO test shim (deps not installed, as in the paper),
# haiku/low, pinned CLI 2.1.220). Launches ONLY after swe30_prunerB has 30
# graded cells (one 30-cell run at a time on the subscription).
cd /home/tempadmin/Code_rep_mcp_tools || exit 1
LOG=swe30_noexec_guard.log
STATE=results/.swe30_noexec_guard_state
TARGET=30
CLAUDE_PIN=/home/tempadmin/.local/share/claude/versions/2.1.220

exec 9>/tmp/swe30_noexec_guard.lock
if ! flock -n 9; then
  sleep 660
  exit 0
fi

G=$(ls results/swe30_noexec/*/grade.json 2>/dev/null | wc -l)
if [ "$G" -ge "$TARGET" ]; then
  echo "$(date '+%m-%d %H:%M') DONE $G/$TARGET" >> "$LOG"
  sleep 60
  exit 0
fi

P=$(ls results/swe30_prunerB/*/grade.json 2>/dev/null | wc -l)
# gate relaxed 2026-09-03 18:20: Route B is complete except 8 OAuth-failed cells being redone;
# the user asked for this arm to start now (2 + 2 concurrent cells is fine).
if [ "$P" -lt 22 ] || [ ! -f results/.swe30_noexec_smoke_ok ]; then
  echo "$(date '+%m-%d %H:%M') waiting: prunerB $P/30, smoke_ok=$([ -f results/.swe30_noexec_smoke_ok ] && echo yes || echo no)" >> "$LOG"
  sleep 60
  exit 0
fi

if pgrep -f "exp-nam[e] swe30_noexec" >/dev/null; then
  echo "$(date '+%m-%d %H:%M') alive $G/$TARGET" >> "$LOG"
else
  read -r lastG stuck < "$STATE" 2>/dev/null || { lastG=-1; stuck=0; }
  if [ "$G" = "$lastG" ]; then stuck=$((stuck+1)); else stuck=1; fi
  echo "$G $stuck" > "$STATE"
  if [ "$stuck" -ge 12 ]; then
    for d in results/swe30_noexec/*/; do
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
  # EXACT paper condition (arXiv 2606.26978 'Prohibited'): the prompt sentence AND an
  # environment with project dependencies NOT installed -> no test shim (REPR_TEST_SHIM unset).
  nohup setsid .venv/bin/python -m experiments.swebench_pro.run_experiment \
    --ids-file results/_swe30_flow_ids.json \
    --conditions baseline_noexec \
    --exp-name swe30_noexec \
    --model claude-haiku-4-5 \
    --effort-flag low \
    --concurrency 2 --grade \
    --claude-bin "$CLAUDE_PIN" \
    >> swe30_noexec.log 2>&1 < /dev/null &
fi
sleep 660
