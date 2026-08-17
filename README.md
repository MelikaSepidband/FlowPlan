# Flow Graph Tools

Flow-first code representations — control-flow graphs (CFG) and data-flow graphs
(DFG) — served to a coding agent as MCP tools, evaluated on three editing
benchmarks against a tool-less baseline.

Each benchmark × condition cell runs the **Claude Code** agent (default model
`claude-haiku-4-5`, low reasoning effort) on a leak-free checkout of the buggy
repository. In the flow conditions the agent has **no code map and no repo-wide
search tool from us** — its window into the code is the function-level flow
graph, plus its own built-in tools.

## Conditions

| condition | tool surface |
|---|---|
| `baseline` | no MCP tools (agent's built-in Read/Grep/Edit only) |
| `cfg_perrep_flow3` | 5 flow tools over the **control-flow** graph |
| `dfg_perrep_flow3` | 5 flow tools over the **def-use (data-flow)** graph |
| `cfgdfg_perrep_flow3` | 5 flow tools over **both** graphs |

The five MCP tools: `flow_of` (get a function's graph), `flow_locate` (declare
the faulty spot on the graph), `flow_plan` (record the planned graph delta),
`flow_check` (verify an edit produced the planned delta), `flow_verify` (final
whole-plan re-verification against the finished code).

The protocol is delivered as a **skill**: the user prompt starts with
`/flowfix`, and the skill body (generated at runtime into the per-run
`claude-home/skills/flowfix/SKILL.md`) instructs: localize in the graph →
declare the fault site → plan the graph delta → make the smallest code edit →
re-verify the graph. Per-task variants exist for repair (SWE-bench Pro),
test generation (SWT-bench protocol), and C security patching (SecBench).

## Benchmarks

| benchmark | task | language | instances shipped |
|---|---|---|---|
| SWE-bench Pro | bug repair | Python | `samples/swebench_pro_n253` (253) |
| SWT-bench protocol on SWE-bench Pro | test generation | Python | the same 253 files — a different task on the same bugs |
| SecBench | security patch (sanitizer crash) | C/C++ | `samples/secbench_patch_n169` (169) |

**About the test-generation benchmark:** we follow the official
[SWT-bench](https://arxiv.org/abs/2406.12952) task formulation and grading
protocol (two test-suite runs — buggy tree vs. gold-fixed tree; success
requires an agent-introduced fail→pass test and no pass→fail regression), but
we apply it to **SWE-bench Pro instances** rather than the original SWT-bench
dataset (which is derived from classic SWE-bench). Rationale: SWE-bench Pro is
the contamination-resistant successor, and sharing one instance distribution
between the repair and test-generation tasks keeps the cross-task comparison
clean. Consequently these numbers are **not comparable to the official
SWT-bench leaderboard**.

The subsets we actually ran are in `data/ids/`: SWE-bench Pro on 70 of the 253
(`swe_70.json`, built as 30 + 20 + 20), test generation on 50 of the 253
(`swt_50.json`, built as 30 + 20), SecBench on 50 of the 169 (`sec_50.json`).
The SWE and SWT subsets were sampled independently, so they are different
task lists (18 ids appear in both).

## Requirements

- **Linux or WSL2** (the harness uses `fcntl`, process groups, POSIX paths).
- **Python 3.12** with a venv at the repo root (`.venv/` — the runners spawn
  `.venv/bin/python` for the MCP server subprocess).
- **git** and network access to github.com (repository checkouts are cloned and
  cached).
- **Claude Code CLI** — the **native Linux** install (default
  `~/.local/bin/claude`, override with `CLAUDE_BIN`), logged in (`claude` once,
  complete OAuth). On WSL, do **not** use a Windows npm-installed `claude`; it
  breaks skill and MCP delivery.
- **Docker** (grading only).
- **SecBench only:** [Joern](https://joern.io) CLI and a JDK 21 — set
  `JOERN_CLI_DIR` and `JOERN_JAVA_HOME` if not at the defaults
  (`~/tools/joern-cli`, `~/tools/jdk-21.0.5+11`).
- **SWE-bench Pro / SWT-bench grading only:** the official SWE-bench Pro
  harness checkout (its `run_scripts/<instance>/` and `dockerfiles/`), pointed
  to by `SWEBENCH_PRO_HARNESS`. Grading docker images
  (`jefzda/sweap-images:<tag>`, `hwiwonlee/secb.eval.x86_64.<id>`) are pulled
  on demand.

## Setup

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e .        # installs the pinned dependencies
claude                            # once, to log in (native Linux CLI)
```

Environment variables (all optional unless noted):

| variable | default | meaning |
|---|---|---|
| `CLAUDE_BIN` | `~/.local/bin/claude` | Claude Code CLI binary |
| `SWEBENCH_CACHE` | `~/swebench_cache` | clone/worktree/index cache (SWE/SWT) |
| `SWEBENCH_PRO_HARNESS` | — | official harness dir (**required for `--grade`** on SWE/SWT) |
| `SECBENCH_SAMPLES_DIR` | `samples/secbench_patch_n169` | SecBench instance dir |
| `JOERN_CLI_DIR` | `~/tools/joern-cli` | Joern install (SecBench) |
| `JOERN_JAVA_HOME` | `~/tools/jdk-21.0.5+11` | JDK for Joern (SecBench) |
| `REPR_TEST_SHIM` | off | `1` = per-cell python/pytest shim venv (**use for SWE/SWT**) |

## Running

Each runner executes `instances × conditions` cells, writes one directory per
cell under `results/<exp-name>/<instance>__<condition>/`, and **skips cells
that already have a `grade.json`** — re-running the same command resumes an
interrupted batch.

SWE-bench Pro (repair):

```bash
REPR_TEST_SHIM=1 .venv/bin/python -m experiments.swebench_pro.run_experiment \
  --ids-file data/ids/swe_70.json \
  --conditions baseline cfg_perrep_flow3 dfg_perrep_flow3 cfgdfg_perrep_flow3 \
  --exp-name swe70 --effort-flag low --concurrency 2 --grade
```

SWT-bench (test generation; graded by the official two-run protocol —
the metric is *reproduction*, not resolution):

```bash
REPR_TEST_SHIM=1 .venv/bin/python -m experiments.swtbench.run_experiment \
  --ids-file data/ids/swt_50.json \
  --conditions baseline cfg_perrep_flow3 dfg_perrep_flow3 cfgdfg_perrep_flow3 \
  --exp-name swt50 --effort-flag low --concurrency 2 --grade
```

SecBench (C security patching; keep `--concurrency 1` — each cell boots its
own Joern server):

```bash
.venv/bin/python -m experiments.secbench.run_experiment \
  --ids-file data/ids/sec_50.json \
  --conditions baseline cfg_perrep_flow3 dfg_perrep_flow3 cfgdfg_perrep_flow3 \
  --exp-name sec50 --effort-flag low --concurrency 1 --grade
```

Tip: smoke-test one instance first by pointing `--ids-file` at a JSON list
with a single id.

### Cell artifacts

Each cell directory contains `prompt.md` (exact user prompt), `mcp.json`,
`transcript.jsonl`, `mcp_calls.jsonl`, `flow_plan.json` / `flow_locate.json` /
`flow_verify_final.json` (flow arms), `model_patch.diff`, `result.json`
(token usage/cost), and `grade.json`.

Token accounting used in our tables: `input + output + cache_read`
(cache-creation tokens excluded).

### OAuth note

Long batches outlive the CLI's OAuth token. The runners check remaining token
lifetime before each cell and **skip without writing artifacts** when it is
about to expire — re-login (`claude`) and re-run the same command to resume.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

Hermetic: MCP tool semantics, protocol/skill text, condition parsing,
grading logic, and the C/Joern query surface (via fake clients; live Joern
tests skip when Joern is absent).

## Repository layout

```
experiments/
  common/flow_protocol.py   # the /flowfix protocol + skill builder (single source)
  common/joern_server.py    # per-cell joern --server manager (SecBench)
  flowverify.py             # final plan-vs-code verdict recorder
  swebench_pro/             # repair runner + leak-free checkout + docker grading
  swtbench/                 # test-gen runner + official two-run grader
  secbench/                 # C security runner + CPG build + sanitizer grading
servers/
  perrep/                   # the MCP server entrypoint (python -m servers.perrep)
  common/flowedit*.py       # the 5 flow tools + fingerprint/delta engine
  cfg_server/               # Python CFG engine (stdlib ast + networkx)
  dfg_server/               # Python def-use engine (beniget + gast)
  common/joern*.py          # C/C++ path (Joern client, snippet-CPG fingerprints)
indexer/                    # symbols.db builder (function-name resolution)
perrep/                     # engine support imported by the server
samples/                    # benchmark instance data
data/ids/                   # canonical instance subsets
tests/
```
