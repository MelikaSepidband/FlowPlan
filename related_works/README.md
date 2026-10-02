# Compared methods

Five published token-reduction methods, reproduced on **Claude Code** and run
on the same instances, models, session limits, graders, and token accounting
as FlowPlan. Each method is one condition of the ordinary runners; nothing
else in the harness changes. The methods intervene at different points of the
repair trajectory: before the session (SHERLOC), in the task prompt
(No-execution, EET), in the tool surface (SeeRepo), and in the message
history (AgentDiet).

| method | condition | paper | mechanism as run |
|---|---|---|---|
| No-execution | `baseline_noexec` | To Run or Not to Run, arXiv 2606.26978 (ISSTA'26) | their "Prohibited" condition: the sentence *"Do not run pytest, unittest, or project test scripts. Project dependencies are not installed."* appended to the task prompt; the harness installs no project dependencies |
| EET | `baseline_eet` / `baseline_eetB` | EET, arXiv 2601.05777 (ACL'26 Findings) | their retrieval code unmodified (TF-IDF cosine over the issue text, `top_k=1`, `min_similarity=0.15`) rendering their `<retrieved_experiences>` template into the task prompt, plus their confidence hook (`eet_hook.py`: key-step detection, `CONFIDENCE_SCORE:` parsing, `<submission_prompt>` at >=81, disabled when nothing was retrieved). `baseline_eet` uses the authors' released 205-record experience base; `baseline_eetB` uses a base built with their two-stage pipeline from our own baseline trajectories on held-out instances (`data/eet/`) |
| SHERLOC | `baseline_sherloc` | SHERLOC, arXiv 2606.24820 (EMNLP'26) | their four repository tools (`view_file`, `codebase_search`, `repo_tree`, `connected_tree`) served over MCP from their own executor (`servers/sherloc_server/`), their `system.yaml` prompt (`tools/sherloc_system.md`), and their loop-detection / final-turn behaviour as a PostToolUse hook; run as a headless **pre-stage** whose `<findings>` / `<locations>` hand-off is injected into the repair prompt under a neutral heading, its tokens merged into the cell |
| AgentDiet | `baseline_agentdiet` | AgentDiet, arXiv 2509.23586 (FSE'26) | their reflection rule and prompt, replayed by a per-cell proxy (`tools/agentdiet_proxy.py`) that rewrites tool observations in the live request; their acceptance bar kept (>=400 tokens saved, or the step cut below 80% of its size). The reflection model is the cell's own model through a headless `claude -p` call (theirs is an OpenAI model that needs an API key); the side-call usage is merged into `result.json` |
| SeeRepo | `baseline_seerepo` | SeeRepo, arXiv 2606.14061 (ASE'26) | their AST repository graph built per instance (`seerepo_arm.py`), their Graphviz PNG returned as a real **image** by an MCP tool (`servers/seerepo_server/`), their `SeeRepo.yaml` navigation instructions appended to the task prompt (`tools/seerepo_steer.md`), plus their one-look rule via `tools/seerepo_elide_proxy.py` (an older image becomes a placeholder once a newer one arrives) |

## Where each method runs

| condition | SWE-bench Pro | SWE-bench Verified | SecBench |
|---|---|---|---|
| `baseline_noexec` | yes | yes | yes |
| `baseline_eetB` | yes | | yes |
| `baseline_eet` | | yes | |
| `baseline_sherloc` | yes | yes | yes |
| `baseline_agentdiet` | yes | yes | yes |
| `baseline_seerepo` | yes | yes | |

**EET experience bases.** EET retrieves an experience record distilled from
earlier trajectories, so the base must not contain an evaluation instance.

- SWE-bench Pro (`baseline_eetB`):
  `data/eet/extracted_experiences_summarized_ours.jsonl`, 108 records from
  the 108 public Python instances outside the 158-instance evaluation sample
  (the frozen split is `data/eet/_eet_split.json`).
- SecBench (`baseline_eetB`):
  `data/eet/extracted_experiences_summarized_ours_sec169.jsonl`, 128 records
  from evaluation-split instances outside the 169-instance sample.
- SWE-bench Verified (`baseline_eet`): the authors' released base (205
  records, in their repository), which shares no instance with our
  218-instance sample.

**SecBench (C/C++).** Only the language-specific parts a method needs are
adapted; the intervention itself is unchanged.

- No-execution: the appended sentence is the C analog of theirs, *"Do not
  build the project, run its tests, or execute the proof-of-concept input.
  Build dependencies are not installed."*
- SHERLOC: their source-file filter keeps C/C++ extensions instead of Python
  ones, and `connected_tree` reads the `#include` graph instead of Python
  imports.
- EET: the key-step detector of the confidence hook also recognizes build and
  proof-of-concept commands (`EET_LANG=c`).
- AgentDiet: unchanged.
- SeeRepo is not run on SecBench: its repository graph is built from Python
  ASTs.

## Third-party code (not vendored)

The methods run the authors' own code. Clone it into the locations below
(all gitignored). Pins are the commits the paper's runs used.

| method | source | pin | location and notes |
|---|---|---|---|
| SHERLOC | `git clone https://github.com/NVIDIA-NeMo/Skills tools/sherloc_src` | `c5b9e72` | only `recipes/sherloc` is used; pure-Python tools, no model or service needed. `servers/sherloc_server/_their.py` shims the `nemo_skills.utils` import so their executor loads standalone. Override the location with `SHERLOC_SRC` |
| SeeRepo | `git clone https://github.com/cslsolow/SeeRepo tools/seerepo_src` | `30ba01c` | Apache-2.0. Needs the Graphviz `dot` binary; the harness looks for an extracted Graphviz at `~/.local/graphviz` (no root needed: `apt-get download` the Graphviz packages and `dpkg -x` them there), or at the root given by `SEEREPO_GRAPHVIZ` |
| EET | `git clone https://github.com/IanWalls/EET tools/eet_src` | `91d9936` | their `experience/store.py` is imported unmodified for retrieval; their released experience base is in the clone |
| AgentDiet | figshare 30073654 ("Artifact of AgentDiet") | md5 `30e838719cc5dc5a4a64bcb4f55fe572` | nothing to install: their reflection prompt and acceptance rule are carried by `tools/agentdiet_proxy.py` |

The Python packages these methods import are part of the pinned dependencies
in `pyproject.toml`.

## Running

Same runners and flags as the FlowPlan conditions (see the top-level
[README](../README.md)); one condition per invocation, for example:

```bash
# SWE-bench Pro
REPR_TEST_SHIM=1 .venv/bin/python -m experiments.swebench_pro.run_experiment \
    --ids-file data/ids/swebench_pro_158.json --conditions baseline_sherloc \
    --exp-name pro158_haiku_sherloc --model claude-haiku-4-5 --effort-flag low \
    --concurrency 2 --grade

# SWE-bench Verified (grade afterwards with experiments.swebench_verified.grade_official)
REPR_TEST_SHIM=1 .venv/bin/python -m experiments.swebench_pro.run_experiment \
    --dataset verified --ids-file data/ids/swebench_verified_218.json \
    --conditions baseline_eet \
    --exp-name verified218_haiku_eet --model claude-haiku-4-5 --effort-flag low \
    --concurrency 2

# SecBench
.venv/bin/python -m experiments.secbench.run_experiment \
    --ids-file data/ids/secbench_169.json --conditions baseline_agentdiet \
    --exp-name sec169_haiku_agentdiet --model claude-haiku-4-5 --effort-flag low \
    --concurrency 1 --grade
```

Notes:

- `baseline_noexec` on SWE-bench Pro with `claude-haiku-4-5` was launched
  without `REPR_TEST_SHIM`, so that the environment has no test runner either
  (their setting); every other run uses the same environment as the baseline
  it is compared with.
- `baseline_seerepo` builds one repository graph per instance. Set
  `SEEREPO_GRAPH_CACHE=results/<earlier exp-name>` to reuse the graphs of an
  earlier run (for example the Haiku run when launching the Sonnet run).
- Pin the Claude Code build with `--claude-bin` so that a method and the
  baseline it is compared with run on the same release.

## Additional arms (not in the paper)

Five further methods were run in a 30-instance pilot on SWE-bench Pro
(`data/ids/swe30_related_works.json`) and are not part of the paper's
evaluation. Their conditions remain available in the SWE-bench runner.

| condition | paper | mechanism as run |
|---|---|---|
| `baseline_prunerB` | SWE-Pruner, arXiv 2601.16746 | their skimmer checkpoint served over HTTP (threshold 0.5, 500-char gate, their feedback strings), their `pruner.yaml` instruction template as the system prompt (`tools/prunerB_system.md`), bash-only agent: native Bash/Read/Grep/Glob/Edit/Write disabled, every read and edit through the two pruned wrappers (`servers/pruner_server/server_b.py`) |
| `baseline_csniper` | ContextSniper, arXiv 2607.01916 | their unmodified Claude Code plugin (`--plugin-dir` + explicit MCP registration), FILTER condition of their released runner, their rendered code policy appended to the task prompt, ripgrep on PATH, their sentence-transformers embedder (`BAAI/bge-m3`) |
| `baseline_cbmem2` | Codebase-Memory MCP, arXiv 2603.27277 | the tool as its installer configures Claude Code: server under its own name, shipped skill, three graph agents and four hooks (`tools/cbmem_claude_payload/`), worktree pre-indexed with their CLI under a short project name, a warm per-cell daemon |
| `baseline_agentsmd` | AGENTS.md study, arXiv 2601.20404 (JAWs@ICSE'26) | one ~1,000-word CLAUDE.md per repository (`tools/claudemd_full/`), sectioned like the study's organic AGENTS.md files, laid at the repository root before the run and removed before patch extraction |
| `baseline_cmv2` | CMV, arXiv 2602.22402 | their PreCompact + PostToolUse trim hooks scoped per cell, with the PostToolUse trim routed through `tools/cmv_relay_hook.sh`: their `auto-trim --check-size` runs unchanged and, when it trims, the session is stopped and resumed on the trimmed transcript (their own `cmv trim --latest` workflow), legs merged into one `result.json` |

Third-party code for these arms:

| tool | source | pin | location and notes |
|---|---|---|---|
| ContextSniper | `git clone https://gitcode.com/lukchiwang/ContextSniper tools/ContextSniper` | `a411c6b` | build per its README (`.venv` python, `agfs/build/agfs-server`); needs `rg` (ripgrep 14.x) on PATH and the `BAAI/bge-m3` weights cached offline |
| claude-code-cmv | `git clone https://github.com/CosmoNaught/claude-code-cmv tools/claude-code-cmv` | `ebec354` | `npm install && npm run build` -> `dist/index.js`; node 24 at `~/.nvm/versions/node/v24.15.0/bin/node` |
| swe-pruner | `https://github.com/Ayanami1314/swe-pruner` | `96171b5` | skimmer checkpoint `ayanami-kitasan/code-pruner` (HF); serve with `swe-pruner --model-path <checkpoint> --port 8000` before the run (`PRUNER_URL` overrides the endpoint) |
| codebase-memory-mcp | the tool's installer (github.com/DeusData/codebase-memory-mcp) | binary `0.10.8` | binary at `~/.local/bin/codebase-memory-mcp` |

`related_works/scripts/` keeps the guard, launch, check, and analysis scripts
of that pilot exactly as they were run. They carry the study machine's paths
and are kept as a record, not as a CLI.

## Files

```
experiments/swebench_pro/agent.py, run_experiment.py   the conditions on SWE-bench Pro and Verified
experiments/secbench/agent.py, run_experiment.py       the conditions on SecBench
experiments/swebench_pro/sherloc_stage.py, sherloc_hook.py   SHERLOC pre-stage + their loop / final-turn hook
experiments/swebench_pro/seerepo_arm.py                SeeRepo per-instance graph build + MCP config
experiments/swebench_pro/eet_arm.py, eet_hook.py       EET retrieval + confidence gate
servers/sherloc_server/            their four tools over MCP (+ `_their.py` import shim)
servers/seerepo_server/            their graph tool over MCP, PNG returned as an image
tools/agentdiet_proxy.py           AgentDiet reflection proxy
tools/seerepo_elide_proxy.py       SeeRepo one-look image rule
tools/sherloc_system.md            their system prompt, verbatim
tools/seerepo_steer.md             their SeeRepo.yaml navigation instructions
data/eet/                          the EET experience bases we built + the frozen SWE-bench Pro split
servers/pruner_server/, tools/prunerB_system.md, tools/cbmem_claude_payload/,
tools/claudemd_full/, tools/cmv_relay_hook.sh           assets of the additional arms
```
