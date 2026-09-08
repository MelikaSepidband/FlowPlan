#!/usr/bin/env bash
# Self-healing guard for swe30_csniper220: the REPAIRED ContextSniper FILTER
# comparator (arXiv 2607.01916) on the baseline's own CLI build 2.1.220
# (user rule 2026-09-03: related work runs on 2.1.220). Repairs vs
# swe30_csniper (see agent.py "FIDELITY REPAIR" block): rg installed, mock
# embedder so the backend's write API works, per-cell runtime dir, their
# runner's retrieval knobs. REPR_TEST_SHIM=1, haiku/low, concurrency 1
# (plugin backend on fixed ports 8090/1833). Single-pattern pgrep.
cd /home/tempadmin/Code_rep_mcp_tools || exit 1
LOG=swe30_csniper220_guard.log
STATE=results/.swe30_csniper220_guard_state
TARGET=30
CLAUDE_PIN=/home/tempadmin/.local/share/claude/versions/2.1.220

exec 9>/tmp/swe30_csniper220_guard.lock
if ! flock -n 9; then
  sleep 660
  exit 0
fi

G=$(ls results/swe30_csniper220/*/grade.json 2>/dev/null | wc -l)
if [ "$G" -ge "$TARGET" ]; then
  echo "$(date '+%m-%d %H:%M') DONE $G/$TARGET" >> "$LOG"
  sleep 60
  exit 0
fi

if pgrep -f "exp-nam[e] swe30_csniper220" >/dev/null; then
  echo "$(date '+%m-%d %H:%M') alive $G/$TARGET" >> "$LOG"
else
  read -r lastG stuck < "$STATE" 2>/dev/null || { lastG=-1; stuck=0; }
  if [ "$G" = "$lastG" ]; then stuck=$((stuck+1)); else stuck=1; fi
  echo "$G $stuck" > "$STATE"
  if [ "$stuck" -ge 3 ]; then
    for d in results/swe30_csniper220/*/; do
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
  # stale plugin backend from a killed cell would hold ports 8090/1833
  pkill -f "contextsniper-runtime/server/app.py" 2>/dev/null
  pkill -f "agfs-server" 2>/dev/null
  # never relaunch into an expired login (2026-09-03: a dead OAuth session
  # burns a cell as an empty patch); wait for the user to `claude login`.
  if ! "$CLAUDE_PIN" auth status 2>/dev/null | grep -q '"loggedIn": true'; then
    echo "$(date '+%m-%d %H:%M') runner dead at $G/$TARGET but NOT LOGGED IN -> waiting for claude login" >> "$LOG"
    sleep 660
    exit 0
  fi
  echo "$(date '+%m-%d %H:%M') runner dead at $G/$TARGET -> relaunch (stuck=$stuck)" >> "$LOG"
  nohup setsid env REPR_TEST_SHIM=1 .venv/bin/python -m experiments.swebench_pro.run_experiment \
    --ids-file results/_swe30_flow_ids.json \
    --conditions baseline_csniper \
    --exp-name swe30_csniper220 \
    --model claude-haiku-4-5 \
    --effort-flag low \
    --concurrency 1 --grade \
    --claude-bin "$CLAUDE_PIN" \
    >> swe30_csniper220.log 2>&1 < /dev/null &
fi
sleep 660
