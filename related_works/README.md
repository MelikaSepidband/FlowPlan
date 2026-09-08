# Related-work arms

The published token-reduction methods we ran on **Claude Code** (claude-haiku-4-5,
low reasoning effort) on the same 30 SWE-bench Pro instances as the flow arms,
with the same graders and the same token convention. Each arm is one condition
of the ordinary runner; nothing else in the harness changes.

| condition | paper | mechanism as run |
|---|---|---|
| `baseline_noexec` | To Run or Not to Run, arXiv 2606.26978 (ISSTA'26) | their "Prohibited" condition: the sentence *"Do not run pytest, unittest, or project test scripts. Project dependencies are not installed."* appended to the task prompt, **and** the test shim OFF (`REPR_TEST_SHIM` unset) so the worktree really has no pytest / no dependencies |
| `baseline_prunerB` | SWE-Pruner, arXiv 2601.16746 | their skimmer checkpoint served over HTTP (threshold 0.5, 500-char gate, their feedback strings), their `pruner.yaml` instruction template as the system prompt (`tools/prunerB_system.md`), bash-only agent: native Bash/Read/Grep/Glob/Edit/Write disabled, every read and edit through the two pruned wrappers (`servers/pruner_server/server_b.py`) |
| `baseline_csniper` | ContextSniper, arXiv 2607.01916 | their unmodified Claude Code plugin (`--plugin-dir` + explicit MCP registration), FILTER condition of their released runner, their rendered code policy appended to the task prompt, ripgrep on PATH, their sentence-transformers embedder (`BAAI/bge-m3`) |
| `baseline_cbmem2` | Codebase-Memory MCP, arXiv 2603.27277 | the tool **as its installer configures Claude Code**: server under its own name, shipped skill, three graph agents and four hooks (`tools/cbmem_claude_payload/`), worktree pre-indexed with their CLI under a short project name, a warm per-cell daemon, no steer sentence of ours |
| `baseline_agentsmd` | AGENTS.md study, arXiv 2601.20404 (JAWs@ICSE'26) | one ~1,000-word CLAUDE.md per repository (`tools/claudemd_full/`), sectioned like the study's organic AGENTS.md files, laid at the repository root before the run and removed before patch extraction |
| `baseline_cmv2` | CMV, arXiv 2602.22402 | their PreCompact + PostToolUse trim hooks scoped per cell, with the PostToolUse trim routed through `tools/cmv_relay_hook.sh`: their `auto-trim --check-size` runs unchanged and, when it trims, the session is stopped and resumed on the trimmed transcript (their own `cmv trim --latest` workflow), legs merged into one `result.json` |

Results, per-class token and dollar tables, and a plain-language card per
method are on the *Related Works* page of the study.

## Third-party code (not vendored)

Clone under `third_party/` (gitignored) or point the `FGT_*` variables at
existing checkouts. Pins are the commits the results were produced with.

| tool | source | pin | notes |
|---|---|---|---|
| ContextSniper | `git clone https://gitcode.com/lukchiwang/ContextSniper third_party/ContextSniper` | `a411c6b` | build per its README (`.venv` python, `agfs/build/agfs-server`); needs `rg` (ripgrep 14.x) on PATH and the `BAAI/bge-m3` weights cached offline |
| claude-code-cmv | `git clone https://github.com/CosmoNaught/claude-code-cmv third_party/claude-code-cmv` | `ebec354` | `npm install && npm run build` -> `dist/index.js`; node 24 |
| swe-pruner | `git clone https://github.com/Ayanami1314/swe-pruner third_party/swe-pruner` | `96171b5` | skimmer checkpoint `ayanami-kitasan/code-pruner` (HF); serve with `swe-pruner --model-path <checkpoint> --port 8000` |
| codebase-memory-mcp | the tool's installer (github.com/DeusData/codebase-memory-mcp) | binary `0.10.8` | binary on PATH or `FGT_CBMEM_BIN` |

Environment overrides (all optional): `FGT_THIRD_PARTY`, `FGT_CSNIPER_ROOT`,
`FGT_CMV_ENTRY`, `FGT_NODE_BIN`, `FGT_CBMEM_BIN`, `FGT_PRUNER_URL`.

## Running

Same runner, same flags as the flow arms; one condition per invocation, e.g.

```bash
# no-execution: shim OFF (the paper's condition)
.venv/bin/python -m experiments.swebench_pro.run_experiment \
    --ids-file data/ids/swe30_related_works.json --conditions baseline_noexec \
    --exp-name swe30_noexec --model claude-haiku-4-5 --effort-flag low \
    --concurrency 2 --grade

# every other arm: shim ON, like the baseline they are compared with
REPR_TEST_SHIM=1 .venv/bin/python -m experiments.swebench_pro.run_experiment \
    --ids-file data/ids/swe30_related_works.json --conditions baseline_prunerB \
    --exp-name swe30_prunerB --model claude-haiku-4-5 --effort-flag low \
    --concurrency 2 --grade
```

`baseline_prunerB` needs the skimmer server up first; `baseline_csniper`,
`baseline_cbmem2` and `baseline_cmv2` need the third-party checkouts above.
Pin the Claude Code build with `--claude-bin` when comparing against a
baseline run on a specific release.

## Files

```
experiments/swebench_pro/agent.py, run_experiment.py   the six conditions (search "related-work")
servers/pruner_server/server_b.py   SWE-Pruner Route B wrapper tools (bash, cat)
tools/prunerB_system.md             their instruction template (system prompt)
tools/cbmem_claude_payload/         Codebase-Memory install payload (skill, agents, hooks, settings)
tools/claudemd_full/                the three AGENTS.md-sized CLAUDE.md files
tools/cmv_relay_hook.sh             CMV stop-and-resume relay around their auto-trim
data/ids/swe30_related_works.json   the 30 instance ids
related_works/scripts/              the guard / launch / check / analysis scripts exactly as run
                                    (they carry the study machine's paths; kept as a record,
                                    not as a CLI)
```
