#!/usr/bin/env bash
# CMV faithful arm (arXiv 2602.22402, claude-code-cmv v2.1.1): the tool's own
# auto-trim hooks + the stop-and-resume relay (tools/cmv_relay_hook.sh), so
# every trim is followed by `claude -p --resume` on the trimmed transcript --
# the tool's `cmv trim --latest` workflow, headless. Replaces swe30_cmv, whose
# in-process trims never reached the model (audit 2026-09-03).
# Paper config (REPR_TEST_SHIM=1, haiku/low, baseline tools) on the PINNED CLI
# build 2.1.220 -- the build of the swe30_shim baseline (user rule 2026-09-03:
# related work runs on 2.1.220, not the baseline on a newer build).
# Gate: the tool's default 600KB transcript size; export CMV_SIZE_THRESHOLD
# (bytes) for a lower-gate variant (goes through their config.json knob).
cd /home/tempadmin/Code_rep_mcp_tools || exit 1
CLAUDE_PIN=/home/tempadmin/.local/share/claude/versions/2.1.220
DONE=$(ls results/swe30_cmv2/*/grade.json 2>/dev/null | wc -l)
[ "$DONE" -ge 30 ] && exit 0
REPR_TEST_SHIM=1 .venv/bin/python -m experiments.swebench_pro.run_experiment \
  --ids-file results/_swe30_flow_ids.json \
  --conditions baseline_cmv2 \
  --exp-name swe30_cmv2 \
  --model claude-haiku-4-5 \
  --effort-flag low \
  --concurrency 2 --grade \
  --claude-bin "$CLAUDE_PIN" \
  >> swe30_cmv2.log 2>&1
