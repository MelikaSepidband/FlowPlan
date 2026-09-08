#!/usr/bin/env bash
# CMV relay hook -- the faithful in-run instantiation of claude-code-cmv
# (arXiv 2602.22402) for the baseline_cmv2 arm.
#
# WHY A RELAY (audit 2026-09-03 of swe30_cmv): CMV's PostToolUse hook rewrites
# the session JSONL on disk, but a running Claude Code process keeps its
# messages in memory and never re-reads that file -- the trimmed transcript is
# only ever loaded by the NEXT `claude --resume` (which is exactly the tool's
# own `cmv trim --latest` workflow: "exit the session, then cmv trim --latest"
# = snapshot, trim, relaunch with --resume). In swe30_cmv the hook trimmed 9/30
# transcripts (295 stubs) yet the live API context never shrank (post-trim
# calls 88-136K context, 0 cache-read-zero calls) -> the row was vacuous.
#
# WHAT THIS DOES: runs the tool's OWN `auto-trim --check-size` (their gate,
# their trimmer, byte-identical) and, iff it rewrote the transcript, tells
# Claude Code to stop (`continue: false`) and leaves a marker; the runner then
# resumes the same session (`claude -p --resume <sid>`), which loads the
# TRIMMED transcript -- the trim reaches the model, as the paper intends.
#
# Their ~/.cmv (config, auto-trim log, auto-backups) is scoped per cell via
# HOME=$RUN_DIR/cmv-home (os.homedir() honours $HOME on Linux), so cells never
# share a log and every pre-trim transcript is kept for the T3 dual-accounting
# audit (cmv_pretrim_<leg>.jsonl). CMV_SIZE_THRESHOLD (bytes) overrides their
# 600KB default gate through their own config.json knob (sizeThresholdBytes);
# unset = the tool's default.
set -u
# node + the CMV entry point are exported by the harness (agent.py:
# CMV_NODE_BIN / CMV_ENTRY, overridable with FGT_NODE_BIN / FGT_CMV_ENTRY)
NODE=${CMV_NODE_BIN:-node}
CMV=${CMV_ENTRY:-}
IN=$(cat)
RUN_DIR="${RUN_DIR:-}"
[ -n "$RUN_DIR" ] && [ -d "$RUN_DIR" ] || exit 0
[ -f "$CMV" ] || exit 0

read -r TP SID < <(printf '%s' "$IN" | /usr/bin/python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
print((d.get("transcript_path") or "-").replace(" ", "%20"), d.get("session_id") or "-")
')
TP=${TP//%20/ }
[ "$TP" != "-" ] && [ -f "$TP" ] || exit 0

CMVHOME="$RUN_DIR/cmv-home"
mkdir -p "$CMVHOME/.cmv"
THRESH=600000
if [ -n "${CMV_SIZE_THRESHOLD:-}" ]; then
  THRESH=$CMV_SIZE_THRESHOLD
  [ -f "$CMVHOME/.cmv/config.json" ] || \
    printf '{"autoTrim": {"sizeThresholdBytes": %s}}\n' "$THRESH" > "$CMVHOME/.cmv/config.json"
fi

BEFORE=$(stat -c %s "$TP" 2>/dev/null || echo 0)
# same gate their --check-size applies; checked here first so the pre-trim
# copy is only taken when a trim is actually due (~1ms otherwise, like theirs)
[ "$BEFORE" -ge "$THRESH" ] || exit 0

LEG=$(ls "$RUN_DIR"/cmv_pretrim_*.jsonl 2>/dev/null | wc -l)
LEG=$((LEG + 1))
cp -p "$TP" "$RUN_DIR/cmv_pretrim_$LEG.jsonl" 2>/dev/null

printf '%s' "$IN" | HOME="$CMVHOME" "$NODE" "$CMV" auto-trim --check-size >/dev/null 2>&1
AFTER=$(stat -c %s "$TP" 2>/dev/null || echo 0)

# Relay (stop + resume) only when the trim removed at least CMV_RELAY_MIN_PCT
# percent of the transcript -- the tool's own documented rule ("sessions with
# <5% reduction: trimming is not recommended", docs/CACHE_IMPACT_ANALYSIS.md).
# Smoke 2026-09-04: once the file sits near its trimmed floor (assistant text
# + stubs + per-line metadata), their gate re-trips on every tool call with
# 2-5% trims; restarting the session on each of those is a relay artifact,
# not their method, so such trims stay in place exactly as their hook leaves
# them (no effect until a later resume) and the session continues.
MINPCT=${CMV_RELAY_MIN_PCT:-5}
if [ "$AFTER" -lt "$BEFORE" ]; then
  TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  STUBS=$(grep -o '\[Trimmed' "$TP" 2>/dev/null | wc -l)
  RELAY=false
  [ $(( (BEFORE - AFTER) * 100 )) -ge $(( MINPCT * BEFORE )) ] && RELAY=true
  REC=$(printf '{"leg": %s, "session_id": "%s", "transcript_path": "%s", "before_bytes": %s, "after_bytes": %s, "stubs_on_disk": %s, "threshold_bytes": %s, "min_pct": %s, "relay": %s, "ts": "%s"}' \
        "$LEG" "$SID" "$TP" "$BEFORE" "$AFTER" "$STUBS" "$THRESH" "$MINPCT" "$RELAY" "$TS")
  printf '%s\n' "$REC" >> "$RUN_DIR/cmv_trims.jsonl"
  if [ "$RELAY" = true ]; then
    printf '%s\n' "$REC" > "$RUN_DIR/cmv_relay_pending.json"
    # Claude Code hook protocol: continue=false halts the session after this
    # hook; the runner resumes it on the trimmed transcript.
    printf '{"continue": false, "stopReason": "CMV auto-trim rewrote the session transcript (%s -> %s bytes); the harness resumes this session on the trimmed context."}\n' "$BEFORE" "$AFTER"
  else
    rm -f "$RUN_DIR/cmv_pretrim_$LEG.jsonl"
  fi
else
  rm -f "$RUN_DIR/cmv_pretrim_$LEG.jsonl"
fi
exit 0
