#!/usr/bin/env bash
# Self-healing, IDLE-GATED guard for the Codebase-Memory as-shipped fidelity
# rerun (baseline_cbmem2; REPR_TEST_SHIM=1, haiku/low, pinned CLI 2.1.220).
#
# Why idle-gated: the tool's shipped hooks (hook-augment) hash their own
# 293 MB binary on every call under a 2-4.5 s deadline; on a loaded box
# (load 30 on 24 cores during the 2026-09-03 ContextSniper rerun) every hook
# call dies silently and the arm degrades to "bare MCP server". So this guard
# refuses to launch while ANY other run_experiment is alive or the 1-min
# load is above half the cores.
#
# Stages: (1) smoke4 = 1 instance; (2) proceed to the 30 only if smoke4's
# transcript carries the shipped "[codebase-memory]" SessionStart reminder
# (hooks verified live); otherwise write results/.swe30_cbmem2_HOLD and stop.
# Refuses to (re)launch on a dead OAuth session (would mint 401 cells).
cd /home/tempadmin/Code_rep_mcp_tools || exit 1
LOG=swe30_cbmem2_guard.log
STATE=results/.swe30_cbmem2_guard_state
HOLD=results/.swe30_cbmem2_HOLD
TARGET=30
CLAUDE_PIN=/home/tempadmin/.local/share/claude/versions/2.1.220
SMOKE=swe1_cbmem2_smoke4

exec 9>/tmp/swe30_cbmem2_guard.lock
if ! flock -n 9; then sleep 660; exit 0; fi
log() { echo "$(date '+%m-%d %H:%M') $*" >> "$LOG"; }

[ -f "$HOLD" ] && { log "HOLD present ($(cat "$HOLD" | head -c 120)) -> not launching"; sleep 660; exit 0; }

G=$(ls results/swe30_cbmem2/*/grade.json 2>/dev/null | wc -l)
if [ "$G" -ge "$TARGET" ]; then log "DONE $G/$TARGET"; sleep 60; exit 0; fi

if pgrep -f "exp-nam[e] swe30_cbmem2" >/dev/null || pgrep -f "exp-nam[e] $SMOKE" >/dev/null; then
  log "alive $G/$TARGET"; sleep 660; exit 0
fi

# --- gates ---------------------------------------------------------------
LEFT=$(python3 -c 'import json,os,time
try:
    d=json.load(open(os.path.expanduser("~/.claude/.credentials.json")))["claudeAiOauth"]
    print(int((d["expiresAt"]/1000-time.time())/60) if d.get("accessToken") else -1)
except Exception:
    print(-1)')
if [ "$LEFT" -lt 90 ]; then log "OAuth has $LEFT min -> NOT launching (re-login needed)"; sleep 660; exit 0; fi

# Count OTHER experiment runners. Must exclude this guard's own process tree:
# when the guard is invoked as `bash -lc '... run_experiment ...'` (or via the
# scheduled task's wrapper) its own command line contains the pattern and
# pgrep -f counts it, so the gate never opens (observed 19:07-19:47 with an
# idle box). Match the python module invocation only, and drop our own pid,
# our parent, and any pid whose cmdline is a shell running this guard.
OTHER=$(pgrep -f "\-m experiments\.(swebench_pro|secbench)\.run_experiment" \
        | grep -v -x -e "$$" -e "$PPID" | wc -l)
LOAD1=$(cut -d' ' -f1 /proc/loadavg | cut -d. -f1)
HALF=$(( $(nproc) / 2 ))
if [ "$OTHER" -gt 0 ] || [ "$LOAD1" -gt "$HALF" ]; then
  log "busy: other runners=$OTHER load1=$LOAD1 (>$HALF?) -> waiting for an idle box"; sleep 660; exit 0
fi

# --- stage 1: smoke4 -------------------------------------------------------
if ! ls results/$SMOKE/*/grade.json >/dev/null 2>&1; then
  log "idle -> launching $SMOKE"
  nohup setsid env REPR_TEST_SHIM=1 .venv/bin/python -m experiments.swebench_pro.run_experiment \
    --ids-file results/_swe30_base_smoke_id.json --conditions baseline_cbmem2 --exp-name "$SMOKE" \
    --model claude-haiku-4-5 --effort-flag low --concurrency 1 --grade --claude-bin "$CLAUDE_PIN" \
    >> "$SMOKE.log" 2>&1 < /dev/null &
  sleep 660; exit 0
fi

# --- stage 2: hooks live? --------------------------------------------------
REM=$(python3 _cbmem2_smoke_check.py "$SMOKE" 2>/dev/null | grep -o "reminder lines: [0-9]*" | grep -o "[0-9]*$" | head -1)
if [ -z "$REM" ] || [ "$REM" -lt 1 ]; then
  echo "smoke4 graded but the shipped SessionStart reminder never reached the model (reminder lines=${REM:-?}); check ~/.cache/codebase-memory-mcp/logs/hook-augment-timeouts.log and box load, then delete this file to retry" > "$HOLD"
  log "HOLD: smoke4 hooks silent (reminder lines=${REM:-?})"; sleep 660; exit 0
fi

# --- stage 3: the 30 --------------------------------------------------------
read -r lastG stuck < "$STATE" 2>/dev/null || { lastG=-1; stuck=0; }
if [ "$G" = "$lastG" ]; then stuck=$((stuck+1)); else stuck=1; fi
echo "$G $stuck" > "$STATE"
if [ "$stuck" -ge 3 ]; then
  for d in results/swe30_cbmem2/*/; do
    [ -d "$d" ] || continue
    if [ ! -f "$d/grade.json" ]; then
      name=$(basename "$d")
      printf '{"instance_id": "%s", "resolved": false, "error": "quarantined: repeated runner crash at this cell", "quarantined": true}\n' "$name" > "$d/grade.json"
      log "QUARANTINE $name"
    fi
  done
  echo "0 0" > "$STATE"
fi
log "smoke4 hooks live (reminder lines=$REM); launching swe30_cbmem2 at $G/$TARGET (stuck=$stuck)"
nohup setsid env REPR_TEST_SHIM=1 .venv/bin/python -m experiments.swebench_pro.run_experiment \
  --ids-file results/_swe30_flow_ids.json --conditions baseline_cbmem2 --exp-name swe30_cbmem2 \
  --model claude-haiku-4-5 --effort-flag low --concurrency 2 --grade --claude-bin "$CLAUDE_PIN" \
  >> swe30_cbmem2.log 2>&1 < /dev/null &
sleep 660
