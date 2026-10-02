# FlowPlan

**Reducing the token cost of coding agents with abstraction-guided planning and verification.**

LLM-based coding agents resolve repository issues through a long interaction
loop, and the growing trajectory is replayed at every model call. FlowPlan
lets the agent make its key repair decisions on a program abstraction instead
of on raw source text. For the function under investigation it builds a
**per-function control-flow graph (CFG) or data-flow graph (DFG)**. The agent

1. **locates** the candidate fault on a graph element,
2. **plans** the repair as explicit *graph deltas* before touching the code,
3. **edits** the source and **checks** that the rebuilt graph changed as planned,
4. **verifies** the whole plan against the finished patch before it stops.

The plan makes the repair intent explicit before editing, and the check gives
a machine-checkable completion criterion, which replaces much of the
re-reading and re-testing an agent otherwise does after an edit.

This repository is the replication package of the paper. It contains

- the FlowPlan MCP server (five tools) and the `/flowfix` skill,
- the evaluation harness for three benchmarks (SWE-bench Pro, SWE-bench
  Verified, SecBench),
- the benchmark samples (instance ids) we evaluated on,
- our reproductions of five token-reduction methods on the same agent
  ([related_works/README.md](related_works/README.md)).

## How FlowPlan works

FlowPlan is attached to an otherwise vanilla **Claude Code** session through
the Model Context Protocol (MCP). The agent keeps its standard file-reading,
search, editing, and shell tools.

| tool | phase | what it does |
|---|---|---|
| `flow_of` | locate | resolves a function name through the symbol index and returns its CFG or DFG |
| `flow_locate` | locate | anchors the candidate fault on a graph element (block, edge, def-use relation, or line) and validates the reference |
| `flow_plan` | plan | records the intended repair as typed graph deltas (e.g. `add_guard`, `change_condition`, `rewire_edge`, `no_flow_change`, `add_function`) and snapshots the fingerprint of each target function at `HEAD` |
| `flow_check` | edit and check | rebuilds the graph of an edited function and compares the observed delta with the planned one (`exact` / `same_family` / `mismatch`) |
| `flow_verify` | verify | runs the same comparison over the complete plan and audits the working tree for modified functions the plan does not cover |

The four-phase policy (locate, plan, edit-and-check, verify) is delivered as a
Claude Code **skill**: the user prompt starts with `/flowfix`, and the skill
body is generated at run time into the per-run
`claude-home/skills/flowfix/SKILL.md` from a single source
(`experiments/common/flow_protocol.py`). The policy is advisory: no hook forces
a tool call, disables a native tool, or prevents the agent from stopping.

Python graphs are derived from the abstract syntax tree
(`servers/cfg_server`, `servers/dfg_server`). C and C++ graphs are extracted
from a [Joern](https://joern.io) code-property graph restricted to the
functions on the crash stack (`servers/common/joern*.py`). Function names are
resolved through a tree-sitter symbol index built once per repository commit,
outside the agent session (`indexer/`).

## Conditions

Every benchmark instance is run once per condition in a fresh headless Claude
Code session.

| condition | name in the paper | agent |
|---|---|---|
| `baseline` | vanilla agent | Claude Code with its default tools, no MCP server |
| `cfg_perrep_flow3` | FlowPlan (CFG) | default tools + the five flow tools over control-flow graphs + `/flowfix` |
| `dfg_perrep_flow3` | FlowPlan (DFG) | default tools + the five flow tools over data-flow graphs + `/flowfix` |

`cfgdfg_perrep_flow3` serves both graphs at once; it is available in the code
but is not part of the paper's evaluation. The five compared methods are
further conditions of the same runners (`baseline_noexec`, `baseline_eet` /
`baseline_eetB`, `baseline_sherloc`, `baseline_agentdiet`,
`baseline_seerepo`); see [related_works/README.md](related_works/README.md).

## Benchmarks

| benchmark | task | language | sample | instance ids |
|---|---|---|---|---|
| [SWE-bench Pro](https://github.com/scaleapi/SWE-bench_Pro-os) | issue repair | Python | 158 of the 266 public Python instances | `data/ids/swebench_pro_158.json` |
| [SWE-bench Verified](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified) | issue repair | Python | 218 of 500 | `data/ids/swebench_verified_218.json` |
| SecBench | security patching (sanitizer crash + proof-of-concept input) | C/C++ | 169 of the 300 evaluation instances | `data/ids/secbench_169.json` |

Each sample is sized for a 95% confidence level and a 5% margin of error;
random draws use seed 42. The instance records are in
`samples/swebench_pro_n266/` (all 266 public Python instances; the 108
outside the evaluation sample are the pool the EET experience base is built
from, see `data/eet/_eet_split.json`) and `samples/secbench_patch_n169/`.
SWE-bench Verified instances are read from the Hugging Face dataset.

All conditions receive the same task description: `problem_statement`,
`requirements`, and `interface` on SWE-bench Pro; `problem_statement` on
SWE-bench Verified; the bug report and the sanitizer report on SecBench. No
condition receives the reference patch or the names of the fail-to-pass
tests. Patches are graded with each benchmark's official evaluation harness.

The paper's runs use Claude Code 2.1.220 with `claude-haiku-4-5` and
`claude-sonnet-5`, both at low reasoning effort.

## Requirements

- **Linux or WSL2** (the harness uses `fcntl`, process groups, POSIX paths).
- **Python 3.12** with a venv at the repo root (`.venv/`; the runners spawn
  `.venv/bin/python` for the MCP server subprocess).
- **git** and network access to github.com (repositories are cloned and
  cached under `~/swebench_cache` and `~/secbench_cache`).
- **Claude Code CLI**, the native Linux install (default
  `~/.local/bin/claude`; pass `--claude-bin <path>` to pin a build), logged
  in. On WSL, do not use a Windows npm-installed `claude`; it breaks skill
  and MCP delivery.
- **Docker** for grading. The grading images
  (`jefzda/sweap-images:<tag>`, the SWE-bench images, and
  `hwiwonlee/secb.eval.x86_64.<id>`) are pulled or built on demand.
- **SWE-bench Pro grading:** a checkout of the official harness
  (<https://github.com/scaleapi/SWE-bench_Pro-os>; its `run_scripts/` and
  `dockerfiles/`), pointed to by `SWEBENCH_PRO_HARNESS`.
- **SWE-bench Verified:** the dataset
  (`huggingface-cli download princeton-nlp/SWE-bench_Verified --repo-type dataset`)
  and, for grading, the official `swebench` package installed for the system
  `python3` (we used 4.1.0).
- **SecBench:** the [Joern](https://joern.io) CLI (we used 4.0.557) at
  `~/tools/joern-cli` and a JDK 21 at `~/tools/jdk-21.0.5+11`.

## Setup

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e .        # installs the pinned dependencies only
claude                            # once, to log in (native Linux CLI)
```

| variable | default | meaning |
|---|---|---|
| `SWEBENCH_CACHE` | `~/swebench_cache` | clone / worktree / index cache (SWE-bench Pro and Verified) |
| `SWEBENCH_PRO_HARNESS` | `~/code_rep_data/swebench_pro/official_harness` | official SWE-bench Pro harness checkout (needed for `--grade`) |
| `SWEBENCH_VERIFIED_PARQUET` | Hugging Face hub cache | test-split parquet of SWE-bench Verified |
| `SECBENCH_SAMPLES_DIR` | `samples/secbench_patch_n169` | SecBench instance records |
| `REPR_TEST_SHIM` | off | `1` gives each cell its own python/pytest environment for the agent's test runs (used for the SWE-bench runs) |

## Running

Each runner executes `instances x conditions` cells and writes one directory
per cell under `results/<exp-name>/<instance>__<condition>/`. Cells that
already have a `grade.json` are skipped, so re-running the same command
resumes an interrupted batch. Use `--model claude-sonnet-5` for the Sonnet
runs. To smoke-test, point `--ids-file` at a JSON list with a single id.

**SWE-bench Pro**

```bash
REPR_TEST_SHIM=1 .venv/bin/python -m experiments.swebench_pro.run_experiment \
  --ids-file data/ids/swebench_pro_158.json \
  --conditions baseline cfg_perrep_flow3 dfg_perrep_flow3 \
  --exp-name pro158_haiku --model claude-haiku-4-5 --effort-flag low \
  --concurrency 2 --grade
```

**SWE-bench Verified** (same runner with `--dataset verified`; graded
afterwards with the official SWE-bench harness)

```bash
REPR_TEST_SHIM=1 .venv/bin/python -m experiments.swebench_pro.run_experiment \
  --dataset verified --ids-file data/ids/swebench_verified_218.json \
  --conditions baseline cfg_perrep_flow3 dfg_perrep_flow3 \
  --exp-name verified218_haiku --model claude-haiku-4-5 --effort-flag low \
  --concurrency 2

.venv/bin/python -m experiments.swebench_verified.grade_official \
  --results-dir results/verified218_haiku \
  --conditions baseline cfg_perrep_flow3 dfg_perrep_flow3 \
  --run-id verified218_haiku --cache-level base --max-workers 4
```

The grader writes the resolved instance ids per condition to
`results/<exp-name>/official_grade_summary.json`. Because these cells are
graded afterwards, they carry no `grade.json`; to resume an interrupted
Verified batch, pass an `--ids-file` with the ids whose cell has no
`result.json` yet.

**SecBench** (keep `--concurrency 1`: each cell boots its own Joern server)

```bash
.venv/bin/python -m experiments.secbench.run_experiment \
  --ids-file data/ids/secbench_169.json \
  --conditions baseline cfg_perrep_flow3 dfg_perrep_flow3 \
  --exp-name sec169_haiku --model claude-haiku-4-5 --effort-flag low \
  --concurrency 1 --grade
```

### Session limits

Each session is capped at 80 model turns, $2, and 25 minutes on SWE-bench Pro
and Verified, and at 40 turns, $1.50, and 25 minutes on SecBench. These are
the runners' defaults (`--max-turns`, `--max-budget-usd`, `--timeout-sec`)
and are the same for every condition. A run that reaches a limit is graded
on the patch it produced up to that point.

### Cell artifacts and token accounting

Each cell directory contains `prompt.md` (the exact user prompt), `mcp.json`,
`transcript.jsonl`, `meta.json`, `result.json` (the CLI's final usage
record), the patch (`patch.diff`, or `model_patch.diff` on SecBench), and
`grade.json`. FlowPlan cells also contain `mcp_calls.jsonl`,
`flow_locate.json`, `flow_plan.json`, and `flow_verify_final.json`.

Token consumption is the sum of four provider-reported components over all
model calls of a cell: `input_tokens`, `output_tokens`,
`cache_read_input_tokens`, and `cache_creation_input_tokens` (cache write).
Auxiliary model calls that a compared method requires, such as a separate
localization stage or reflection calls, are merged into the cell's
`result.json`.

### Login expiry

Long batches can outlive the CLI's OAuth token. The runners check the
remaining token lifetime before each cell and skip the cell without writing
artifacts when the token is about to expire; log in again (`claude`) and
re-run the same command to resume.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

The tests are hermetic: MCP tool semantics, the policy and skill text,
condition parsing, and the C/Joern query surface through fake clients (live
Joern tests are skipped when Joern is absent).

## Repository layout

```
experiments/
  common/flow_protocol.py     the /flowfix policy + skill builder (single source)
  common/joern_server.py      per-cell joern --server manager (SecBench)
  common/testenv_ladder.py    interpreter-compatible test environment for a cell
  flowverify.py               final plan-vs-code verdict recorder
  swebench_pro/               SWE-bench runner (Pro and Verified), leak-free checkout, Pro grading
  swebench_verified/          Verified instance loader + official-harness grading
  secbench/                   SecBench runner, CPG build, sanitizer grading
servers/
  perrep/                     the MCP server entry point (python -m servers.perrep)
  common/flowedit*.py         the five flow tools + fingerprint / delta engine
  cfg_server/                 Python CFG engine (stdlib ast + networkx)
  dfg_server/                 Python def-use engine (beniget + gast)
  common/joern*.py            C/C++ path (Joern client, snippet-CPG fingerprints)
  sherloc_server/, seerepo_server/, pruner_server/   servers of the compared methods
indexer/                      symbol index builder (function-name resolution)
perrep/                       engine support imported by the server
samples/                      benchmark instance records (SWE-bench Pro, SecBench)
data/ids/                     evaluation samples (instance ids)
data/eet/                     EET experience bases built from held-out instances
tools/                        assets of the compared methods
related_works/                the compared methods: what each one is, pins, launch lines
tests/
```

`data/ids/` also keeps the subsets used in earlier pilot runs
(`swe_*.json`, `sec_*.json`, `swe30_related_works.json`); the paper's
evaluation uses the three files listed under [Benchmarks](#benchmarks).
