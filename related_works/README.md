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
| `baseline_sherloc` | SHERLOC, arXiv 2606.24820 (EMNLP'26) | their four repository tools (`view_file`, `codebase_search`, `repo_tree`, `connected_tree`) served over MCP from their own executor (`servers/sherloc_server/`), their `system.yaml` prompt (`tools/sherloc_system.md`), and their loop-detection / final-turn behaviour as a PostToolUse hook; run as a headless Haiku **pre-stage** whose `<findings>` / `<locations>` hand-off is injected into the repair prompt under a neutral heading, its tokens merged into the cell |
| `baseline_agentdiet` | AgentDiet, arXiv 2509.23586 (FSE'26) | their reflection rule and prompts from the figshare artifact, replayed by a per-cell proxy (`tools/agentdiet_proxy.py`) that rewrites tool observations in the live request; their acceptance bar kept (>=400 tokens saved, or the step cut below 80% of its size); reflection model = Haiku via a headless `claude -p` call, since their GPT-4o mini needs an API key. Side-call usage merged into `result.json` |
| `baseline_seerepo` | SeeRepo, arXiv 2606.14061 (ASE'26) | their AST repository graph built per instance (`seerepo_arm.py`), their Graphviz PNG returned as a real **image** by an MCP tool (`servers/seerepo_server/`), their `SeeRepo.yaml` navigation instructions appended to the task prompt (`tools/seerepo_steer.md`), plus their one-look rule via `tools/seerepo_elide_proxy.py` (an older image becomes a placeholder once a newer one arrives) |
| `baseline_eet` / `baseline_eetB` | EET, arXiv 2601.05777 (ACL'26 Findings) | their retrieval code unmodified (TF-IDF cosine over the issue text, `top_k=1`, `min_similarity=0.15`) rendering their `<retrieved_experiences>` template into the task prompt, plus their confidence hook (`eet_hook.py`: key-step detection, `CONFIDENCE_SCORE:` parsing, `<submission_prompt>` at >=81, disabled when nothing was retrieved). **Two arms:** `baseline_eet` uses their shipped 205-record base; `baseline_eetB` uses a 108-record base built from our own held-out trajectories with their two-stage pipeline (`data/eet/`) |

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
| SHERLOC (NeMo-Skills) | `git clone --filter=blob:none --sparse https://github.com/NVIDIA-NeMo/Skills third_party/nemo_skills` then `git sparse-checkout set recipes/sherloc` | `c5b9e72` | pure-Python tools; no model or service needed. `servers/sherloc_server/_their.py` shims the `nemo_skills.utils` imports so their executor loads standalone |
| AgentDiet artifact | figshare 30073654 ("Artifact of AgentDiet") | md5 `30e838719cc5dc5a4a64bcb4f55fe572` | download in a browser (figshare blocks scripted fetch); the proxy reads their prompts and acceptance rule from it |
| SeeRepo | `git clone https://github.com/cslsolow/SeeRepo third_party/SeeRepo` | `30ba01c` | Apache-2.0. Needs Graphviz `dot` on PATH; without root, `apt-get download graphviz && dpkg -x` into `~/.local/graphviz` and set `GVBINDIR` |
| EET | `git clone https://github.com/IanWalls/EET third_party/EET` | `91d9936` | their `experience/store.py` is imported unmodified for retrieval; their shipped 205-record base is in the clone, our 108-record base ships in `data/eet/` |

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

`baseline_eetB` reads `data/eet/`: an experience base distilled from our own
baseline runs with EET's own two-stage pipeline, drawn only from the 108
instances of a frozen split (`data/eet/_eet_split.json`) that reserves 158
instances for evaluation now and later, so the base can never contain an
instance we test on.

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
experiments/swebench_pro/sherloc_stage.py, sherloc_hook.py   SHERLOC pre-stage + their loop/final-turn hook
experiments/swebench_pro/seerepo_arm.py    SeeRepo per-instance graph build + MCP config
experiments/swebench_pro/eet_arm.py, eet_hook.py             EET retrieval (lever A) + confidence gate (lever B)
servers/sherloc_server/            their four tools over MCP (+ `_their.py` import shim)
servers/seerepo_server/            their graph tool over MCP, PNG returned as an image
tools/agentdiet_proxy.py           AgentDiet reflection proxy (their rule, Haiku as the reflection model)
tools/seerepo_elide_proxy.py       SeeRepo one-look image rule
tools/sherloc_system.md            their system prompt, verbatim
tools/seerepo_steer.md             their SeeRepo.yaml navigation instructions
data/eet/                          the EET experience base we built + the frozen 158/108 test/base split
related_works/scripts/              the guard / launch / check / analysis scripts exactly as run
                                    (they carry the study machine's paths; kept as a record,
                                    not as a CLI)
```
