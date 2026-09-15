#!/usr/bin/env bash
# Self-healing guard for swe30_eet (EET, arXiv 2601.05777 / ACL'26 Findings;
# REPR_TEST_SHIM=1, haiku/low, pinned CLI 2.1.220). Starts the 30-cell run ONLY
# after the smoke marker results/.swe30_eet_smoke_ok exists (user go).
# Always grep for "quarantined" before calling the run complete.
cd /home/tempadmin/Code_rep_mcp_tools || exit 1
LOG=swe30_eet_guard.log
STATE=results/.swe30_eet_guard_state
TARGET=30
CLAUDE_PIN=/home/tempadmin/.local/share/claude/versions/2.1.220

exec 9>/tmp/swe30_eet_guard.lock
if ! flock -n 9; then
  sleep 660
  exit 0
fi

G=$(ls results/swe30_eet/*/grade.json 2>/dev/null | wc -l)
if [ "$G" -ge "$TARGET" ]; then
  echo "$(date '+%m-%d %H:%M') DONE $G/$TARGET" >> "$LOG"
  sleep 60
  exit 0
fi

if [ ! -f results/.swe30_eet_smoke_ok ]; then
  echo "$(date '+%m-%d %H:%M') waiting: smoke_ok=no" >> "$LOG"
  sleep 60
  exit 0
fi

if pgrep -f "exp-nam[e] swe30_eet" >/dev/null; then
  echo "$(date '+%m-%d %H:%M') alive $G/$TARGET" >> "$LOG"
else
  read -r lastG stuck < "$STATE" 2>/dev/null || { lastG=-1; stuck=0; }
  if [ "$G" = "$lastG" ]; then stuck=$((stuck+1)); else stuck=1; fi
  echo "$G $stuck" > "$STATE"
  if [ "$stuck" -ge 12 ]; then
    for d in results/swe30_eet/*/; do
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
    --conditions baseline_eet \
    --exp-name swe30_eet \
    --model claude-haiku-4-5 \
    --effort-flag low \
    --concurrency 2 --grade \
    --claude-bin "$CLAUDE_PIN" \
    >> swe30_eet.log 2>&1 < /dev/null &
fi
sleep 660
