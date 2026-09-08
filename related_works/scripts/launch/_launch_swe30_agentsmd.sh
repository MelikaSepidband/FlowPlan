#!/usr/bin/env bash
# AGENTS.md replication arm (arXiv 2601.20404): per-repo CLAUDE.md files sized
# and structured like the study's organic AGENTS.md files (tools/claudemd_full,
# ~1,100-1,400 words, build/test commands + architecture + conventions), laid
# at the REPOSITORY ROOT as project memory and removed before patch extraction.
# Paper config (REPR_TEST_SHIM=1, haiku/low, baseline tools) on the PINNED CLI
# build 2.1.220 -- the build of the swe30_shim baseline (user rule 2026-09-03:
# related work runs on 2.1.220, not the baseline on a newer build).
cd /home/tempadmin/Code_rep_mcp_tools || exit 1
CLAUDE_PIN=/home/tempadmin/.local/share/claude/versions/2.1.220
DONE=$(ls results/swe30_agentsmd/*/grade.json 2>/dev/null | wc -l)
[ "$DONE" -ge 30 ] && exit 0
REPR_TEST_SHIM=1 .venv/bin/python -m experiments.swebench_pro.run_experiment \
  --ids-file results/_swe30_flow_ids.json \
  --conditions baseline_agentsmd \
  --exp-name swe30_agentsmd \
  --model claude-haiku-4-5 \
  --effort-flag low \
  --concurrency 2 --grade \
  --claude-bin "$CLAUDE_PIN" \
  >> swe30_agentsmd.log 2>&1
