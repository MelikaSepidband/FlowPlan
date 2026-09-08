"""Run the claude agent on one SWE-bench Pro instance (host worktree).

The agent runs with cwd = the instance worktree and ``bypassPermissions`` so it
can EDIT files (unlike LinuxFLBench, where it only read + emitted JSON). It is
told to fix the bug described in ``problem_statement`` so the failing tests pass;
it must NOT edit the test files. We then extract its patch as the worktree diff
(excluding the harness-laid test files).

Reuses the linuxflbench claude-invocation conventions: ``claude -p`` with
``--output-format json``, a per-run ``CLAUDE_CONFIG_DIR`` seeded from the user's
credentials, optional ``--mcp-config`` for a rep server, token-freshness guard.
"""

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

from experiments.swebench_pro import checkout

STUDY_ROOT = Path(__file__).resolve().parents[2]
VENV_PYTHON = STUDY_ROOT / ".venv" / "bin" / "python"
GATE_HOOK = STUDY_ROOT / "experiments" / "swebench_pro" / "explore_gate_hook.py"


def _hook_cmd(env_pairs: dict, hook_path: Path) -> str:
    """`env K=V ... <venv-python> <hook>` -- hook commands run through a shell, so
    this carries config regardless of the parent env."""
    prefix = " ".join(["env"] + [shlex.quote("%s=%s" % kv) for kv in env_pairs.items()])
    return "%s %s %s" % (prefix, shlex.quote(str(VENV_PYTHON)), shlex.quote(str(hook_path)))


def write_gate_settings(run_dir: Path, rep: str, explore_tool: str,
                        worktree: Optional[Path] = None) -> Path:
    """settings.json wiring the MAP-SURFACED explore-gate (PreToolUse + PostToolUse,
    same hook script). Holds the closed-gate token economics (-21%) WITHOUT the
    empty patches.

    Design (revised 2026-06-22, final, after the gate-never-opened and
    edit-scoped misfires):
    - PostToolUse on the rep MCP tools: record the repo-relative file paths the
      map result surfaced, into RUN_DIR/.surfaced_files.
    - PreToolUse: DENY Read/Grep/Glob used to BROWSE (agent localizes via the
      MAP -> Read low / MCP high / -21%), but ALLOW a Read of a file the map has
      already SURFACED (so the agent can open the file it localized and edit it;
      this also satisfies Claude Code's Edit read-first rule -> no empty patch).
      Edit/Write are allowed and also pre-add their target to the allowlist.

    Net: Read rises only to ~the files the map pointed at (~2-3), not ~12 of
    browsing; the read-to-edit never dead-ends -> no empty patches. Bash is NOT
    matched (the cat/sed block backfired -- see memory swebench-bash-not-waste).
    The `worktree` arg is accepted for signature compatibility but unused now."""
    mcp_pat = "mcp__%s_perrep__.*" % rep
    pre_matcher = "Read|Grep|Glob|Edit|Write|MultiEdit|NotebookEdit|%s" % mcp_pat
    cmd = _hook_cmd(
        {"RUN_DIR": str(run_dir), "REPR_GATE_REP": rep,
         "REPR_GATE_TOOL": explore_tool}, GATE_HOOK)
    settings = {
        "hooks": {
            "PreToolUse": [
                {"matcher": pre_matcher,
                 "hooks": [{"type": "command", "command": cmd}]}],
            # PostToolUse only needs the MCP tools (to record surfaced paths).
            "PostToolUse": [
                {"matcher": mcp_pat,
                 "hooks": [{"type": "command", "command": cmd}]}],
        }
    }
    p = run_dir / "settings.json"
    p.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return p


# =============================================================================
# Related-work arms (the published token-reduction methods we ran on Claude
# Code against the flow arms; see related_works/README.md).
#
# Third-party code is NOT vendored. Clone it under third_party/ (or point the
# FGT_* variables anywhere) -- pins are listed in related_works/README.md:
#   third_party/ContextSniper     gitcode.com/lukchiwang/ContextSniper
#   third_party/claude-code-cmv   github.com/CosmoNaught/claude-code-cmv (built)
#   third_party/swe-pruner        github.com/Ayanami1314/swe-pruner (skimmer)
#   codebase-memory-mcp           the tool's own installer binary
# =============================================================================
TOOLS_DIR = STUDY_ROOT / "tools"
THIRD_PARTY = Path(os.environ.get("FGT_THIRD_PARTY", STUDY_ROOT / "third_party"))


def _which(name: str, fallback: Path) -> Path:
    found = shutil.which(name)
    return Path(found) if found else fallback


# -- ContextSniper (arXiv 2607.01916), condition baseline_csniper --------------
CSNIPER_ROOT = Path(os.environ.get("FGT_CSNIPER_ROOT", THIRD_PARTY / "ContextSniper"))
CSNIPER_PLUGIN = CSNIPER_ROOT / "claude-plugin"
# -- Codebase-Memory MCP (arXiv 2603.27277), condition baseline_cbmem2 ---------
CBMEM_BIN = Path(os.environ.get(
    "FGT_CBMEM_BIN",
    _which("codebase-memory-mcp", Path.home() / ".local" / "bin" / "codebase-memory-mcp")))
# the tool AS `codebase-memory-mcp install` configures Claude Code: skill,
# three graph agents, SessionStart/SubagentStart/PreToolUse(Grep|Glob)/
# PostToolUse(Read) hooks -- captured verbatim from `install -y` into a
# throwaway HOME (tools/cbmem_claude_payload/).
CBMEM_PAYLOAD_DIR = TOOLS_DIR / "cbmem_claude_payload"
# -- CLAUDE.md / AGENTS.md replication (arXiv 2601.20404), baseline_agentsmd ---
CLAUDEMD_FULL_DIR = TOOLS_DIR / "claudemd_full"
# -- SWE-Pruner (arXiv 2601.16746), condition baseline_prunerB -----------------
# Route B = their pruning mechanism + their experiment template inside Claude
# Code; every read goes through the pruned wrappers (native tools disabled).
# The skimmer is served separately (their `swe-pruner --model-path ... --port
# 8000`); FGT_PRUNER_URL points the wrappers at it.
PRUNERB_SYSTEM_PROMPT = TOOLS_DIR / "prunerB_system.md"
PRUNER_URL = os.environ.get("FGT_PRUNER_URL", "http://localhost:8000/prune")
# -- CMV (arXiv 2602.22402), condition baseline_cmv2 ---------------------------
CMV_ENTRY = Path(os.environ.get(
    "FGT_CMV_ENTRY", THIRD_PARTY / "claude-code-cmv" / "dist" / "index.js"))
NODE_BIN = Path(os.environ.get("FGT_NODE_BIN", _which("node", Path("/usr/bin/node"))))
# The relay hook runs the tool's own `auto-trim --check-size` byte-identically
# and, iff it trimmed, stops the session and leaves a marker; _cmv_relay_stages
# then resumes the SAME session on the trimmed transcript (their own
# `cmv trim --latest` workflow: exit, trim, relaunch with --resume).
CMV_RELAY_HOOK = TOOLS_DIR / "cmv_relay_hook.sh"
# Headless -p needs a prompt on every resumed leg (their interactive relaunch
# needs none). Kept neutral and task-free -- disclosed.
CMV_RESUME_PROMPT = "Continue the task from where you left off."
CMV_MAX_LEGS = 12


def _write_mcp(run_dir: Path, name: str, server: dict) -> Path:
    p = run_dir / ("mcp_%s.json" % name)
    p.write_text(json.dumps({"mcpServers": server}, indent=2), encoding="utf-8")
    return p


def write_prunerB_mcp_config(run_dir: Path) -> Path:
    """--mcp-config for baseline_prunerB: the two pruned wrapper tools
    (bash, cat) served by servers/pruner_server/server_b.py, calling the
    skimmer over HTTP with the paper's threshold 0.5 / 500-char gate."""
    (STUDY_ROOT / "results").mkdir(parents=True, exist_ok=True)
    return _write_mcp(run_dir, "prunerB", {"pruned_tools": {
        "command": str(VENV_PYTHON),
        "args": ["-m", "servers.pruner_server.server_b"],
        "env": {
            "PYTHONPATH": str(STUDY_ROOT),
            "PRUNER_URL": PRUNER_URL,
            "PRUNER_THRESHOLD": "0.5",
            "PRUNER_MIN_CHARS": "500",
            "PRUNER_CHUNK_OVERLAP": "50",
            "PRUNER_TIMEOUT": "120",
            "PRUNER_RETRIES": "3",
            "PRUNER_B_LOG": str(STUDY_ROOT / "results" / "prunerB_events.jsonl"),
        }}})


def write_csniper_mcp_config(run_dir: Path) -> Path:
    """--mcp-config for baseline_csniper: the plugin's own MCP server binary,
    registered explicitly (plugin-dir MCP loading fails silently on some CLI
    builds; hooks/skills still ride in via --plugin-dir)."""
    return _write_mcp(run_dir, "csniper", {"contextsniper": {
        "command": str(CSNIPER_PLUGIN / "bin" / "contextsniper-mcp"),
        "args": []}})


def write_cbmem2_mcp_config(run_dir: Path) -> Path:
    """--mcp-config for baseline_cbmem2: the binary under ITS OWN server name
    (the shipped agents reference mcp__codebase-memory-mcp__* tools)."""
    return _write_mcp(run_dir, "cbmem2", {"codebase-memory-mcp": {
        "command": str(CBMEM_BIN), "args": []}})


def _lay_cbmem_payload(run_dir: Path, home: Path) -> Path:
    """Reproduce the Codebase-Memory `install` payload inside the per-cell
    CLAUDE_CONFIG_DIR: skill (home/skills/codebase-memory/SKILL.md), the
    three graph agents (home/agents/*.md), the three hook scripts
    (home/hooks/*, BIN pointed at the real binary) and a settings file with
    the shipped hook wiring. The shipped settings.json refers to
    "$HOME/.claude/hooks/..."; HOME stays the real home under the harness, so
    the commands are rewritten to absolute per-cell paths. Returns the
    settings file path (passed via --settings)."""
    src = CBMEM_PAYLOAD_DIR
    for rel in ("skills/codebase-memory/SKILL.md",
                "agents/codebase-memory.md",
                "agents/codebase-memory-scout.md",
                "agents/codebase-memory-auditor.md"):
        dst = home / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text((src / rel).read_text(encoding="utf-8"),
                       encoding="utf-8")
    hooks_dir = home / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    for name in ("cbm-code-discovery-gate", "cbm-session-reminder",
                 "cbm-subagent-reminder"):
        body = (src / "hooks" / name).read_text(encoding="utf-8")
        body = re.sub(r"^BIN='.*'$", "BIN='%s'" % CBMEM_BIN, body, flags=re.M)
        p = hooks_dir / name
        p.write_text(body, encoding="utf-8")
        p.chmod(0o755)
    shipped = json.loads((src / "settings.json").read_text(encoding="utf-8"))

    def _abs(cmd: str) -> str:
        return '"%s"' % (hooks_dir / cmd.strip('"').rsplit("/", 1)[-1])
    for _event, entries in shipped.get("hooks", {}).items():
        for entry in entries:
            for h in entry.get("hooks", []):
                h["command"] = _abs(h["command"])
    p = run_dir / "cbmem_settings.json"
    p.write_text(json.dumps(shipped, indent=2), encoding="utf-8")
    return p


def write_cmv2_settings(run_dir: Path) -> Path:
    """settings.json for baseline_cmv2: CMV's PreCompact hook exactly as
    shipped (inert at our context sizes -- autocompact never fires) and its
    PostToolUse hook routed through the relay wrapper, which calls their
    `auto-trim --check-size` unchanged and adds the stop-and-resume step.
    Absolute node + dist/index.js paths instead of the bare `cmv` the
    installer writes -- their open issue #7: bare `cmv` is unresolvable in
    the non-interactive shells Claude Code spawns for hooks."""
    pre = "%s %s auto-trim" % (shlex.quote(str(NODE_BIN)),
                              shlex.quote(str(CMV_ENTRY)))
    post = "/bin/bash %s" % shlex.quote(str(CMV_RELAY_HOOK))
    settings = {
        "hooks": {
            "PreCompact": [
                {"matcher": "",
                 "hooks": [{"type": "command", "command": pre,
                            "timeout": 30}]}],
            "PostToolUse": [
                {"matcher": "",
                 "hooks": [{"type": "command", "command": post,
                            "timeout": 30}]}],
        }
    }
    p = run_dir / "settings.json"
    p.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return p


_NO_SUM_KEYS = {"contextWindow", "maxOutputTokens"}


def _sum_numeric(a, b):
    """Recursively add the numeric leaves of b into a (dicts merged by key;
    capacity fields and non-numeric leaves keep b's value)."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            if k in _NO_SUM_KEYS or k not in a:
                out[k] = v
            else:
                out[k] = _sum_numeric(a[k], v)
        return out
    if (isinstance(a, (int, float)) and isinstance(b, (int, float))
            and not isinstance(a, bool) and not isinstance(b, bool)):
        return a + b
    return b


def _cmv_relay_stages(run_dir, cmd, env, wt, rc, max_turns, max_budget_usd,
                      timeout_sec):
    """baseline_cmv2: after each leg, if the relay hook trimmed the transcript
    and halted the session (marker run_dir/cmv_relay_pending.json), resume
    the SAME session -- Claude Code loads the now-trimmed JSONL -- with the
    turn / $ budget that remains (never a raise). Legs are capped at
    CMV_MAX_LEGS; a leg that ends on an agent-outcome error (max turns /
    max budget) is final. Raw legs are kept as result_leg<n>.json and
    result.json becomes the merged record (usage / modelUsage / cost / turns
    / durations summed, outcome fields from the last leg), so every
    downstream reader sees the whole cell. Returns (rc, timed_out)."""
    def _parse(text):
        try:
            return json.loads(text or "{}")
        except Exception:
            return {}
    r = _parse((run_dir / "result.json").read_text("utf-8"))
    (run_dir / "result_leg1.json").write_text(json.dumps(r, indent=2), "utf-8")
    legs = [{"leg": 1, "subtype": r.get("subtype"),
             "num_turns": r.get("num_turns"), "rc": rc,
             "cost": r.get("total_cost_usd")}]
    merged = dict(r)
    turns_used = int(r.get("num_turns") or 1)
    cost_used = float(r.get("total_cost_usd") or 0.0)
    marker_p = run_dir / "cmv_relay_pending.json"
    leg, timed_out = 1, False
    while marker_p.is_file() and leg < CMV_MAX_LEGS:
        marker = _parse(marker_p.read_text("utf-8"))
        marker_p.unlink()
        sid = marker.get("session_id") or r.get("session_id")
        sub = str(r.get("subtype") or "")
        rem_turns = max_turns - turns_used
        rem_budget = round(max_budget_usd - cost_used, 2)
        if not sid or sub in ("error_max_turns", "error_max_budget_usd"):
            legs.append({"leg": leg + 1, "skipped": "final leg (%s)"
                         % (sub or "no session id")})
            break
        if rem_turns < 1 or rem_budget < 0.05:
            legs.append({"leg": leg + 1, "skipped": "budget exhausted "
                         "(turns_left=%d, usd_left=%.2f)"
                         % (rem_turns, rem_budget)})
            break
        leg += 1
        cmd2 = list(cmd)
        cmd2[cmd2.index("-p") + 1] = CMV_RESUME_PROMPT
        cmd2[cmd2.index("--max-turns") + 1] = str(rem_turns)
        cmd2[cmd2.index("--max-budget-usd") + 1] = str(rem_budget)
        cmd2 += ["--resume", sid]
        try:
            proc = subprocess.run(cmd2, cwd=str(wt), env=env,
                                  capture_output=True, text=True,
                                  timeout=timeout_sec, start_new_session=True)
            out, rc = proc.stdout or "", proc.returncode
            if proc.stderr:
                (run_dir / ("stderr_leg%d.log" % leg)).write_text(
                    proc.stderr, "utf-8")
        except subprocess.TimeoutExpired as e:
            out = (e.stdout if isinstance(e.stdout, str) else "") or ""
            rc, timed_out = -1, True
        (run_dir / ("result_leg%d.json" % leg)).write_text(out, "utf-8")
        r = _parse(out)
        legs.append({"leg": leg, "subtype": r.get("subtype"),
                     "num_turns": r.get("num_turns"), "rc": rc,
                     "cost": r.get("total_cost_usd"),
                     "resumed_session": sid, "trim": marker,
                     "max_turns": rem_turns, "max_budget_usd": rem_budget})
        for k in ("usage", "modelUsage", "total_cost_usd", "num_turns",
                  "duration_ms", "duration_api_ms"):
            if k in merged and k in r:
                merged[k] = _sum_numeric(merged[k], r[k])
            elif k in r:
                merged[k] = r[k]
        for k in ("subtype", "is_error", "stop_reason", "result",
                  "session_id", "terminal_reason", "permission_denials"):
            if k in r:
                merged[k] = r[k]
        turns_used += int(r.get("num_turns") or 1)
        cost_used += float(r.get("total_cost_usd") or 0.0)
        if timed_out:
            break
    trims = []
    try:
        for line in (run_dir / "cmv_trims.jsonl").read_text("utf-8").splitlines():
            if line.strip():
                trims.append(_parse(line))
    except OSError:
        pass
    merged["cmv_relay"] = {
        "legs": legs, "trims": trims, "n_legs": leg,
        "turns_used": turns_used, "cost_used": round(cost_used, 4),
        "max_turns": max_turns, "max_budget_usd": max_budget_usd,
        "resume_prompt": CMV_RESUME_PROMPT,
        "size_threshold_env": os.environ.get("CMV_SIZE_THRESHOLD")}
    (run_dir / "result.json").write_text(json.dumps(merged, indent=2), "utf-8")
    return rc, timed_out


def csniper_env(run_dir: Path, worktree: Optional[Path]) -> dict:
    """Per-cell environment for the ContextSniper plugin: their venv python
    + built AGFS backend, per-cell storage/runtime inside run_dir, the FILTER
    condition of their released Claude runner (filters + filtering prompt
    ON; the code policy is appended to the task prompt by run_experiment),
    their SWE-Pro runner's retrieval knobs, ripgrep on PATH, and their
    in-tree sentence-transformers embedder (default model BAAI/bge-m3, CPU,
    offline weights). Semantic FUSION in search_code stays OFF (their
    release default). Thread / batch caps only bound memory; they change
    speed, never retrieval output."""
    e = {
        "PY_BIN": str(CSNIPER_ROOT / ".venv" / "bin" / "python"),
        "AGFS_BIN": str(CSNIPER_ROOT / "agfs" / "build" / "agfs-server"),
        "CONTEXTSNIPER_DIR": str(CSNIPER_ROOT),
        "CONTEXTSNIPER_LOCAL_STORAGE_DIR": str(run_dir / "csniper_store"),
        "CONTEXTSNIPER_RETRIEVAL_SEMANTIC_ENABLED": "0",
        "CONTEXTSNIPER_PLUGIN_AUTO_START": "1",
        "CONTEXTSNIPER_PLUGIN_AUTO_STOP": "1",
        "CONTEXTSNIPER_FILTER_ENABLED": "1",
        "CONTEXTSNIPER_FILTER_NATIVE_READ": "1",
        "CONTEXTSNIPER_FILTER_NATIVE_BASH": "1",
        "CONTEXTSNIPER_INJECT_FILTERING_PROMPT": "1",
        "CONTEXTSNIPER_INJECT_CODE_POLICY_ON_SUBMIT": "0",
        "CONTEXTSNIPER_DISABLE_AFTER_TURN_EXTRACTION": "1",
        "CONTEXTSNIPER_CODE_TOGGLE": "true",
        "VECTOR_DB_TYPE": "memory",
        "CONTEXTSNIPER_SESSION_ID": run_dir.name,
        "CONTEXTSNIPER_PLUGIN_START_WAIT": "60",
        "MCP_TIMEOUT": "120000",
        "EMBEDDING_PROVIDER": "st",
        "ST_MODEL": "BAAI/bge-m3",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "TOKENIZERS_PARALLELISM": "false",
        "OMP_NUM_THREADS": "2",
        "MKL_NUM_THREADS": "2",
        "OPENBLAS_NUM_THREADS": "2",
        "TORCH_NUM_THREADS": "2",
        "CONTEXTSNIPER_CODE_SEARCH_EMBED_BATCH_TEXTS": "16",
        "CONTEXTSNIPER_CODE_SEARCH_EMBED_BATCH_TOKENS": "8000",
        "CONTEXTSNIPER_RUNTIME_DIR": str(run_dir / "csniper_runtime"),
        "CONTEXTSNIPER_STORAGE_BACKEND": "agfs",
        "CONTEXTSNIPER_CODE_TOGGLE_FORCE": "true",
        "CONTEXTSNIPER_RETRIEVAL_GRAPH_ENABLED": "1",
        "CONTEXTSNIPER_RETRIEVAL_SYMBOLIC_ENABLED": "1",
        "CONTEXTSNIPER_RETRIEVAL_FREQUENCY_ENABLED": "1",
        "CONTEXTSNIPER_CODE_SEARCH_CANDIDATE_MAX_FILES": "40",
        "CONTEXTSNIPER_CODE_SEARCH_EMBED_MAX_FILES": "20",
        "CONTEXTSNIPER_CODE_SEARCH_MAX_SNIPPETS": "250",
        "CONTEXTSNIPER_CODE_SEARCH_INGEST_CANDIDATES": "1",
        "CONTEXTSNIPER_CODE_SEARCH_INGEST_ASYNC": "1",
        "CONTEXTSNIPER_CODE_SEARCH_INGEST_MAX_FILES": "3",
        "CONTEXTSNIPER_CODE_SEARCH_INGEST_MAX_CHUNKS": "40",
        "CONTEXTSNIPER_CODE_SEARCH_INGEST_WORKERS": "2",
        "CONTEXTSNIPER_CODE_FUSE_MODE": "weighted_rrf",
        "CONTEXTSNIPER_CODE_FUSE_W_EMBED": "0.33",
        "CONTEXTSNIPER_CODE_FUSE_W_BM25": "0.17",
        "CONTEXTSNIPER_CODE_FUSE_W_CTAGS": "0.17",
        "CONTEXTSNIPER_CODE_FUSE_W_GRAPH": "0.33",
        "CONTEXTSNIPER_BOOTSTRAP_MAX_FILES": "40",
        "CONTEXTSNIPER_BOOTSTRAP_FULL_INDEX_CAP_FILES": "40",
        "CONTEXTSNIPER_SWE_COPY_AGFS_RUNTIME": "1",
        "CONTEXTSNIPER_ACCOUNT_ID": "acct-demo-r0",
        "CONTEXTSNIPER_USER_ID": "u-claude-r0",
        "CONTEXTSNIPER_AGENT_ID": "claude-code-r0",
        "NO_PROXY": "127.0.0.1,localhost,::1",
        "no_proxy": "127.0.0.1,localhost,::1",
    }
    if worktree is not None:
        e["CONTEXTSNIPER_WORKSPACE_ROOT"] = str(worktree)
    return e


def cbmem_env(run_dir: Path, worktree: Optional[Path]) -> dict:
    """Per-cell Codebase-Memory env (full isolation; the MCP server and the
    hooks inherit it and find the pre-built index). The runtime dir holds a
    unix socket (sun_path ~108-char limit) so it must be SHORT; keyed by a
    run_dir hash. The shipped hooks enforce a 2000 ms in-process deadline
    and exit silently past it; CBM_HOOK_DEADLINE_MS is the tool's own knob."""
    e = {
        "CBM_CACHE_DIR": str(run_dir / "cbm_cache"),
        "CBM_RUNTIME_DIR": "/tmp/cbmrt_%s" % hashlib.md5(
            str(run_dir).encode()).hexdigest()[:10],
        "CBM_LOG_LEVEL": "error",
        "CBM_HOOK_DEADLINE_MS": "4500",
    }
    if worktree is not None:
        e["CBM_ALLOWED_ROOT"] = str(worktree)
    return e


def build_prompt(inst: dict[str, Any], explore_tool: Optional[str] = None,
                 dyn_tool: Optional[str] = None,
                 flow_tools: Optional[tuple] = None,
                 flow_first_tools: Optional[tuple] = None,
                 nl_plan_tool: Optional[str] = None,
                 flow_only_tool: Optional[str] = None,
                 map_free_rules: bool = False) -> str:
    """The fix-the-bug task prompt.

    Default: problem_statement + requirements + interface.
    REPR_PROMPT_MODE:
      problem_only -> ONLY the problem statement (drop requirements AND
        interface -- both pre-supply spec + the fault LOCATION).
      no_interface -> problem_statement + requirements, but DROP interface
        (the field that names the file/function). Keeps the behavioral spec but
        removes the explicit fault LOCATION, so the agent must localize.

    ``explore_tool`` (e.g. "mcp__ast_perrep__ast_explore"): when set, the FIRST
    line of the USER prompt commands the agent to call it before any grep/Read.
    Adoption lever 2 -- Haiku weights the user turn far more than an appended
    system prompt, and the steered system prompt alone left the map unused (the
    0-MCP-call pilot). Tool conditions only; baseline passes None.

    ``dyn_tool`` (e.g. "mcp__cfg_perrep__executed_path"): the dyn arms'
    execution-overlay nudge. Same lesson, same lever: the dyn steer paragraph
    alone produced 0 overlay calls in the smoke cells, so the USER prompt adds
    a Step-1 naming the rep's overlay tool.

    ``flow_tools`` (plan_tool, check_tool): the graph-mediated-editing arm. When
    set, the USER prompt adds a Step-2 requiring the agent to commit its
    intended CFG/DFG delta with `flow_plan` BEFORE editing, then verify with
    `flow_check` AFTER editing (and revise once on mismatch). This is the
    study's core repair hypothesis -- planning the graph change first, then
    projecting it to code -- and the same adoption lever applies.
    """
    import os as _os
    mode = _os.environ.get("REPR_PROMPT_MODE", "").strip().lower()
    problem_only = mode == "problem_only"
    no_interface = mode == "no_interface"
    parts = ["# Bug-fix task", ""]
    if flow_first_tools:
        of_tool, plan_tool, check_tool = flow_first_tools[:3]
        verify_tool = flow_first_tools[3] if len(flow_first_tools) > 3 else None
        # NOTE: check the COMBINED arm before dfg -- "dfg_perrep" is a
        # substring of "cfgdfg_perrep".
        if "cfgdfg_perrep" in of_tool:
            parts += [
                "## Work IN THE FLOW (there is no code map)",
                "You fix this bug by working in the function's TWO flow "
                "graphs -- its CONTROL-FLOW GRAPH (branches and paths) and "
                "its VALUE-FLOW (def-use) GRAPH (where values are defined "
                "and used) -- not by reading the whole repo. Do NOT try to "
                "rewrite the whole function: find the ONE wrong element in "
                "whichever graph shows it, and change only that.",
                "1. (REQUIRED FIRST) Call `%s` with the function/symbol names "
                "the bug report mentions. It returns BOTH graphs of each "
                "function: the control-flow graph (basic blocks + "
                "true/false/loop edges) and the def-use graph (every "
                "definition and every use of each value). There is no "
                "explore/map tool; these flows are your surface." % of_tool,
                "2. IF THE BUG IS NOT IN THE FLOW, LOCALIZE FIRST: the report "
                "may name no function, a name may not resolve, or neither "
                "graph you got back may contain the faulty region. Do NOT "
                "force a fix into a flow that does not show the bug. Fall "
                "back to your own tools -- Grep for the error message / "
                "suspect identifiers, Read the candidate files -- until you "
                "find the faulty function, then call `%s` on THAT function "
                "and work in its flows." % of_tool,
                "3. Read the two graphs together to locate the fix: a "
                "branch/guard that should exist but doesn't (add it), a "
                "wrong condition (change it), a value used without being "
                "checked/derived first (add the def or guard), or a use "
                "reading a stale/wrong definition (repoint it). Call `%s` to "
                "record the change, picking the op from the graph where the "
                "bug lives (control-flow: add_guard / remove_branch / "
                "change_condition / change_block_structure / rewire_edge; "
                "data-flow: add_def_use / remove_def_use / move_def_use; or "
                "no_flow_change). Plan EVERY function you intend to touch in "
                "this one call." % plan_tool,
                "4. Make the SMALL code edit at that spot (open the file "
                "with Read to get the exact lines, then Edit). Do not "
                "restructure code the flows didn't point you to.",
                "5. Call `%s` to confirm your edit made the flow change you "
                "planned (it checks the graph your op belongs to). If "
                "match=false, fix the edit and re-check once." % check_tool,
            ] + ([
                "6. (REQUIRED LAST) When you believe the fix is complete and "
                "BEFORE you stop, call `%s` (no arguments). It re-checks "
                "EVERY planned delta against the FINAL code and lists any "
                "source edits you made OUTSIDE the plan. If a delta reports "
                "match=false or an unplanned edit was accidental, fix that "
                "and call it once more. A pass is necessary but not "
                "sufficient -- make sure the change actually addresses the "
                "bug, then stop with a non-empty patch." % verify_tool,
                "",
            ] if verify_tool else [
                "A match is necessary but not sufficient -- make sure the "
                "change actually addresses the bug, then stop with a "
                "non-empty patch.",
                "",
            ])
        elif "dfg_perrep" in of_tool:
            # dfg flow-first: same protocol, value-flow wording. The surface
            # is the def-use graph, the fix target is a def/use relation.
            parts += [
                "## Work IN THE FLOW (there is no code map)",
                "You fix this bug by working in the function's VALUE-FLOW "
                "(def-use) GRAPH, not by reading the whole repo. Do NOT try "
                "to rewrite the whole function -- find the ONE value relation "
                "that, if changed, makes the failing behavior go away, and "
                "change only that.",
                "1. (REQUIRED FIRST) Call `%s` with the function/symbol names "
                "the bug report mentions. It returns each function's real "
                "def-use graph -- every definition of a variable and every "
                "place its value is used. There is no explore/map tool; this "
                "flow is your surface." % of_tool,
                "2. IF THE BUG IS NOT IN THE FLOW, LOCALIZE FIRST: the report "
                "may name no function, a name may not resolve, or the graphs "
                "you got back may not contain the faulty value relation. Do "
                "NOT force a fix into a flow that does not show the bug. Fall "
                "back to your own tools -- Grep for the error message / "
                "suspect identifiers, Read the candidate files -- until you "
                "find the faulty function, then call `%s` on THAT function "
                "and work in its flow." % of_tool,
                "3. Read the flow to locate the fix: a value used without "
                "being checked/derived first (add the def or guard-def), a "
                "def computing the wrong value (change it), or a use reading "
                "a stale/wrong definition (repoint or move it). Call `%s` to "
                "record that one data-flow change (op add_def_use / "
                "remove_def_use / move_def_use + function + anchor + "
                "rationale). Plan EVERY function you intend to touch in this "
                "one call." % plan_tool,
                "4. Make the SMALL code edit at that def/use site (open the "
                "file with Read to get the exact lines, then Edit). Do not "
                "restructure code the flow didn't point you to.",
                "5. Call `%s` to confirm your edit made the data-flow change "
                "you planned. If match=false, fix the edit and re-check once."
                % check_tool,
            ] + ([
                "6. (REQUIRED LAST) When you believe the fix is complete and "
                "BEFORE you stop, call `%s` (no arguments). It re-checks "
                "EVERY planned delta against the FINAL code and lists any "
                "source edits you made OUTSIDE the plan. If a delta reports "
                "match=false or an unplanned edit was accidental, fix that "
                "and call it once more. A pass is necessary but not "
                "sufficient -- make sure the change actually addresses the "
                "bug, then stop with a non-empty patch." % verify_tool,
                "",
            ] if verify_tool else [
                "A match is necessary but not sufficient -- make sure the "
                "change actually addresses the bug, then stop with a "
                "non-empty patch.",
                "",
            ])
        else:
            parts += [
                "## Work IN THE FLOW (there is no code map)",
                "You fix this bug by working in the function's CONTROL-FLOW GRAPH, "
                "not by reading the whole repo. Do NOT try to rewrite the whole "
                "function -- find the ONE part of the flow that, if changed, makes "
                "the failing behavior go away, and change only that.",
                "1. (REQUIRED FIRST) Call `%s` with the function/symbol names the "
                "bug report mentions. It returns each function's real control-flow "
                "graph -- its basic blocks and the true/false/loop edges between "
                "them. There is no explore/map tool; this flow is your surface."
                % of_tool,
                "2. IF THE BUG IS NOT IN THE FLOW, LOCALIZE FIRST: the report "
                "may name no function, a name may not resolve, or the graphs "
                "you got back may not contain the faulty region. Do NOT force "
                "a fix into a flow that does not show the bug. Fall back to "
                "your own tools -- Grep for the error message / suspect "
                "identifiers, Read the candidate files -- until you find the "
                "faulty function, then call `%s` on THAT function and work in "
                "its flow." % of_tool,
                "3. Read the flow to locate the fix: a branch/guard that should "
                "exist but doesn't (add it), a condition that's wrong (change it), "
                "or an edge going the wrong way (reroute it). Call `%s` to record "
                "that one control-flow change (op + function + anchor + "
                "rationale). Plan EVERY function you intend to touch in this one "
                "call." % plan_tool,
                "4. Make the SMALL code edit at that flow locus (open the file with "
                "Read to get the exact lines, then Edit). Do not restructure code "
                "the flow didn't point you to.",
                "5. Call `%s` to confirm your edit made the control-flow change you "
                "planned. If match=false, fix the edit and re-check once."
                % check_tool,
            ] + ([
                "6. (REQUIRED LAST) When you believe the fix is complete and "
                "BEFORE you stop, call `%s` (no arguments). It re-checks EVERY "
                "planned delta against the FINAL code and lists any source "
                "edits you made OUTSIDE the plan. If a delta reports "
                "match=false or an unplanned edit was accidental, fix that and "
                "call it once more. A pass is necessary but not sufficient -- "
                "make sure the change actually addresses the bug, then stop "
                "with a non-empty patch." % verify_tool,
                "",
            ] if verify_tool else [
                "A match is necessary but not sufficient -- make sure the "
                "change actually addresses the bug, then stop with a "
                "non-empty patch.",
                "",
            ])
    if nl_plan_tool:
        # RQ2 ABLATION (nlplan): same map-free flow_of surface and same
        # 'plan-before-edit, then revise' discipline as the flow-first arm,
        # but the plan is NATURAL LANGUAGE written into the response and there
        # is NO flow_check verification. Isolates whether the win comes from a
        # machine-checkable graph contract vs merely planning first.
        parts += [
            "## Work IN THE FLOW (there is no code map)",
            "You fix this bug by working in the function's flow, not by reading "
            "the whole repo. Do NOT rewrite the whole function -- find the ONE "
            "part of the flow that, if changed, makes the failing behavior go "
            "away, and change only that.",
            "1. (REQUIRED FIRST) Call `%s` with the function/symbol names the "
            "bug report mentions. It returns each function's real flow graph. "
            "There is no explore/map tool; this flow is your surface."
            % nl_plan_tool,
            "2. (REQUIRED) Before editing, WRITE OUT YOUR PLAN in plain English: "
            "in 1-2 sentences, state exactly how the flow must change to fix "
            "the bug (which function, which line, and what change -- e.g. 'add "
            "a guard before line 88 so an empty input returns early'). Write "
            "this plan in your response before you touch any code.",
            "3. Make the SMALL code edit that realizes your plan (open the file "
            "with Read to get the exact lines, then Edit). Do not restructure "
            "code your plan didn't call for.",
            "4. (REQUIRED) After editing, RE-READ your edit against the plan you "
            "wrote: does the code change actually do what your plan said? If "
            "not, fix the edit once. Then make sure it truly addresses the bug "
            "and stop with a non-empty patch.",
            "",
        ]
    if flow_only_tool:
        # RQ2 lower-bound control: map-free flow_of surface with NO forced
        # planning and NO verification -- just "see the flow, find the spot,
        # edit". Isolates whether the flow VIEW alone helps, separate from any
        # plan/verify discipline (flow_of vs nlplan vs cfg_perrep_flow ladder).
        parts += [
            "## Work IN THE FLOW (there is no code map)",
            "You fix this bug by working in the function's flow, not by reading "
            "the whole repo. Do NOT rewrite the whole function -- find the ONE "
            "part of the flow that, if changed, makes the failing behavior go "
            "away, and change only that.",
            "1. (REQUIRED FIRST) Call `%s` with the function/symbol names the "
            "bug report mentions. It returns each function's real flow graph. "
            "There is no explore/map tool; this flow is your surface."
            % flow_only_tool,
            "2. Read the flow to find where the fix goes, then make the SMALL "
            "code edit there (open the file with Read to get the exact lines, "
            "then Edit). Do not restructure code the flow didn't point you to. "
            "End with a non-empty patch.",
            "",
        ]
    if explore_tool:
        parts += [
            "## Step 0 (REQUIRED FIRST): localize with the code map",
            "Before reading or grepping ANY file, your FIRST action must be to "
            "call `%s` with the bug's key terms or suspect symbol names. It "
            "returns the relevant code's verbatim source in one call (treat it "
            "as already read). Use the map to find where to edit; fall back to "
            "Read/Grep only for a detail it did not cover or to apply your edit."
            % explore_tool,
            "",
        ]
    if dyn_tool:
        parts += [
            "## Step 1 (REQUIRED before editing): check the recorded "
            "failing-test execution",
            "A run of the bug's FAILING test was recorded for this exact code "
            "state. After localizing and BEFORE writing your edit, call "
            "`%s` on your suspect code to see what the failing run actually "
            "executed. Code the failing test never reached is rarely the "
            "right edit site; a branch or value flow the run wrongly "
            "took/skipped usually is." % dyn_tool,
            "",
        ]
    if flow_tools:
        # flow_tools = (plan_tool, check_tool[, view_tool]); view_tool is the
        # stronger arm's cfg_view (real block-level CFG) or None.
        plan_tool, check_tool = flow_tools[0], flow_tools[1]
        view_tool = flow_tools[2] if len(flow_tools) > 2 else None
        parts += [
            "## Step 2 (REQUIRED): plan the graph change, THEN edit, THEN "
            "verify",
            "Fix this bug by first deciding HOW THE CODE'S FLOW MUST CHANGE, "
            "then projecting that decision into a code edit -- not by editing "
            "raw code and hoping. Concretely:",
        ]
        if view_tool:
            parts += [
                "NOTE: when you call `%s` (below), its response INCLUDES the "
                "REAL control-flow graph of each function you name -- its basic "
                "blocks and the true/false/loop edges between them. READ that "
                "graph: it shows exactly WHERE in the control flow the bug is "
                "(a missing guard is a branch that should exist but doesn't; a "
                "wrong path is an edge going the wrong way). If your planned "
                "delta doesn't fit the graph, call `%s` again with a corrected "
                "delta before editing. You may also call `%s` at any time to "
                "re-view a function's control-flow graph."
                % (plan_tool, plan_tool, view_tool),
            ]
        parts += [
            "1. From the bug report and the function you will fix, call "
            "`%s` ONCE with the delta your fix makes to the function's "
            "control-/data-flow graph (e.g. add_guard, remove_branch, "
            "change_condition, add_def_use, move_def_use; or no_flow_change if "
            "the fix is a pure expression/constant/signature edit). Give the "
            "exact function name, the anchor file:line, and a one-sentence "
            "rationale tying it to the bug." % plan_tool,
            "2. Make the code edit that realizes that plan.",
            "3. Call `%s` on each function you edited. It recomputes the "
            "function's graph from your edited source and tells you whether the "
            "realized delta MATCHES your plan. If match=false, your edit did "
            "not make the change you intended -- reconsider and fix it, then "
            "re-check (once). If match=true, you are done." % check_tool,
            "This plan-then-edit-then-verify loop is the required workflow for "
            "this task; do not skip flow_plan or flow_check.",
            "",
        ]
    parts += [
        "You are working in a checked-out Python repository. Fix the bug "
        "described below by EDITING the source files in the current directory. "
        "Your changes must make the failing tests pass without breaking the "
        "passing ones.",
        "",
        "## Problem statement",
        "",
        str(inst.get("problem_statement") or "").strip(),
    ]
    if not problem_only:
        req = str(inst.get("requirements") or "").strip()
        if req:
            parts += ["", "## Requirements", "", req]
        iface = str(inst.get("interface") or "").strip()
        if iface and not no_interface:
            parts += ["", "## Interface / hints", "", iface]
    parts += [
        "",
        "## Rules",
        "- Edit the SOURCE files needed to fix the bug. Do NOT edit any test "
        "files (paths under test/ or tests/, or files named test_*.py); the "
        "tests are fixed and define correctness.",
        "- Make the smallest change that satisfies the requirements.",
    ]
    # Rules tail, three variants keyed on what the cell actually has:
    #  - map-free flow arms (flow-first / nl-plan / flow-only): the flow rule;
    #  - gated MAP arms (explore_tool set -- the only cells where the
    #    explore-gate hook really blocks Read/Grep): the map/Grep-gate rules;
    #  - BASELINE (no tools at all): a neutral localize rule. Keying the map
    #    rules on explore_tool (not "everything non-map-free") fixes the bug
    #    where baseline fell into the map branch and ran with phantom-map
    #    rules ("code map (explore/node)", "Grep ... blocked on purpose")
    #    describing tools and gates the baseline cell never had.
    # ``map_free_rules`` (flow3): the caller wants ONLY the map-free rules
    # tail -- the protocol steps arrive separately (flowfix skill / inlined
    # v3 text), so none of the tool args above are set. Default False keeps
    # every existing prompt byte-identical.
    map_free = (bool(flow_first_tools) or bool(nl_plan_tool)
                or bool(flow_only_tool) or map_free_rules)
    if map_free:
        parts += [
            "- Use Read to open the exact file the flow points you to, then "
            "Edit it. You must end with a non-empty patch; never stop with the "
            "bug unfixed.",
        ]
    elif explore_tool:
        parts += [
            "- Localize using the code map (explore/node), NOT by Read/Grep/Glob "
            "to browse -- Grep/Glob and Reads of files the map hasn't shown you "
            "are blocked on purpose; the map already gives you the relevant "
            "source.",
            "- Once the map has shown you the file to fix, you MAY Read that file "
            "(Reads of map-surfaced files are allowed) and then Edit it -- this "
            "is the normal localize->read->edit flow. You must end with a "
            "non-empty patch; never stop with the bug unfixed.",
        ]
    else:
        parts += [
            "- Localize the bug with your own tools (Grep/Glob to search, Read "
            "to open files), then Edit the source. You must end with a "
            "non-empty patch; never stop with the bug unfixed.",
        ]
    parts += [
        "- When done, stop. The harness extracts your changes as a git diff.",
    ]
    return "\n".join(parts)


def _seed_claude_home(run_dir: Path) -> Path:
    """A per-run CLAUDE_CONFIG_DIR seeded with the user's credentials + login.

    CLAUDE_CONFIG_DIR needs BOTH ``.credentials.json`` (the OAuth token) and
    ``.claude.json`` (the account/login record) -- credentials alone yield
    "Not logged in". Copy both; ``settings.json`` too if present.
    """
    home = run_dir / "claude-home"
    home.mkdir(parents=True, exist_ok=True)
    src = Path.home() / ".claude"
    cred = src / ".credentials.json"
    if cred.is_file():
        shutil.copy2(cred, home / ".credentials.json")
    # the login/account record lives at ~/.claude.json (one level up)
    login = Path.home() / ".claude.json"
    if login.is_file():
        shutil.copy2(login, home / ".claude.json")
    settings = src / "settings.json"
    if settings.is_file():
        shutil.copy2(settings, home / "settings.json")
    return home


def _harvest_transcript(config_dir: Path, run_dir: Path,
                        started_at: float) -> None:
    """Copy the newest session transcript into run_dir/transcript.jsonl, for
    MCP/tool-usage telemetry (claude writes it under config_dir/projects/)."""
    projects = config_dir / "projects"
    if not projects.is_dir():
        return
    cands = sorted((p for p in projects.glob("**/*.jsonl") if p.is_file()),
                   key=lambda p: p.stat().st_mtime)
    if not cands:
        return
    fresh = [p for p in cands if p.stat().st_mtime >= started_at - 1.0]
    shutil.copy2((fresh or cands)[-1], run_dir / "transcript.jsonl")


# -- test-environment shim (REPR_TEST_SHIM=1, DEFAULT OFF) ---------------------
#
# Observed across baseline AND flow arms (swe30_flow3 / swe30_flow_v2): agents
# try to self-test and fail at the ENVIRONMENT level -- Ubuntu ships `python3`
# but no `python` on PATH, and the checkouts have no `pytest` entry point --
# so NO arm can run project unit tests and regressions ship unverified. The
# shim gives the agent's Bash subprocesses a working `python` and `pytest`.
# Everything lives under run_dir (never the worktree, so nothing can leak into
# patch.diff), and the flag defaults OFF so every completed run's env stays
# byte-identical; it will be enabled for the next full grid across ALL arms.


def _shim_setup_env() -> dict:
    """Env for the shim's own venv/pip setup subprocesses: never --user, never
    the shared study venv's site via PYTHONPATH."""
    e = dict(os.environ)
    e["PYTHONNOUSERSITE"] = "1"
    e["PIP_USER"] = "0"
    e.pop("PYTHONPATH", None)
    return e


def _venv_pytest_ok(venv_python: str, info: dict) -> bool:
    """True iff ``import pytest`` works in the shim venv, pip-installing pytest
    INTO the venv (never --user, never the study venv) when missing. Records
    the failure reason in ``info`` and never raises (offline pip must not
    crash the cell)."""
    probe = [venv_python, "-c", "import pytest"]
    try:
        if subprocess.run(probe, capture_output=True, text=True, timeout=120,
                          env=_shim_setup_env()).returncode == 0:
            return True
    except Exception as e:
        info["pytest_error"] = repr(e)
        return False
    try:
        r = subprocess.run([venv_python, "-m", "pip", "install", "pytest"],
                           capture_output=True, text=True, timeout=600,
                           env=_shim_setup_env())
        if r.returncode != 0:
            info["pytest_error"] = (((r.stderr or r.stdout or "").strip()
                                     or "pip rc=%d" % r.returncode))[-400:]
            return False
        return subprocess.run(probe, capture_output=True, text=True,
                              timeout=120,
                              env=_shim_setup_env()).returncode == 0
    except Exception as e:
        info["pytest_error"] = repr(e)
        return False


def setup_test_shim(run_dir: Path, env: dict) -> dict:
    """Build ``run_dir/testshim`` (``python`` + ``pytest`` exec shims) plus a
    per-cell venv (``run_dir/testenv``, --system-site-packages) and PREPEND
    both to ``env["PATH"]`` (mutated in place).

    The venv is the robust interpreter choice: the agent gets a real python it
    (and our pytest bootstrap) can pip install into without touching the
    shared study venv or ~/.local (PYTHONNOUSERSITE=1 stays in force). If venv
    creation or the pytest install fails (e.g. offline), fall back to plain
    python3 shims and record the failure -- never crash the cell. Returns the
    meta.json record: {"enabled", "venv_ok", "pytest_ok"} (+ *_error keys on
    failure)."""
    info: dict = {"enabled": True, "venv_ok": False, "pytest_ok": False}
    base_py = shutil.which("python3") or "/usr/bin/python3"
    target_py = base_py
    venv_dir = run_dir / "testenv"
    venv_python = venv_dir / "bin" / "python"
    try:
        r = subprocess.run([base_py, "-m", "venv", "--system-site-packages",
                            str(venv_dir)], capture_output=True, text=True,
                           timeout=300, env=_shim_setup_env())
        if r.returncode == 0 and venv_python.is_file():
            info["venv_ok"] = True
            target_py = str(venv_python)
        else:
            info["venv_error"] = (((r.stderr or r.stdout or "").strip()
                                   or "venv rc=%d" % r.returncode))[-400:]
    except Exception as e:
        info["venv_error"] = repr(e)
    if info["venv_ok"]:
        info["pytest_ok"] = _venv_pytest_ok(str(venv_python), info)

    shim_dir = run_dir / "testshim"
    shim_dir.mkdir(parents=True, exist_ok=True)
    for name, tail in (("python", ""), ("pytest", " -m pytest")):
        sh = shim_dir / name
        sh.write_text("#!/bin/sh\nexec %s%s \"$@\"\n"
                      % (shlex.quote(target_py), tail), encoding="utf-8")
        sh.chmod(0o755)
    prepend = [str(shim_dir)]
    if info["venv_ok"]:
        prepend.append(str(venv_dir / "bin"))
    old_path = env.get("PATH", "")
    env["PATH"] = os.pathsep.join(prepend + ([old_path] if old_path else []))
    return info


# The essential built-in toolset for a bug-fix agent. Restricting --tools to
# this small set (instead of claude's full default toolset incl. Cron*/Task*/
# Monitor/EnterWorktree/...) keeps the active-tool budget small enough that MCP
# rep/codegraph tools STAY ACTIVE (never demoted to the deferred bucket), so the
# agent calls them organically -- no ToolSearch round-trip needed. MCP tools are
# added on top of --tools automatically.
ESSENTIAL_BUILTIN_TOOLS = "Read,Edit,Write,Bash,Grep,Glob"


def build_command(claude_bin: str, prompt: str, model: str, effort: str,
                  max_turns: int, max_budget_usd: float,
                  mcp_config: Optional[Path], system_prompt_file: Optional[Path],
                  restrict_tools: bool = False,
                  settings_file: Optional[Path] = None,
                  plugin_dir: Optional[Path] = None,
                  extra_disallowed: Optional[str] = None) -> list[str]:
    """claude argv: -p, json output, EDIT-capable (bypassPermissions).

    restrict_tools=True caps the built-in toolset to ESSENTIAL_BUILTIN_TOOLS so
    MCP tools stay active (non-deferred) for rep/codegraph cells.
    settings_file (when set) supplies the explore-gate PreToolUse hook.
    plugin_dir (ContextSniper arm) loads the tool's own Claude Code plugin.
    extra_disallowed (SWE-Pruner Route B) disables native tools so every read
    and edit goes through the pruned wrappers.
    """
    cmd = [claude_bin, "-p", prompt, "--output-format", "json",
           "--strict-mcp-config"]
    if mcp_config is not None:
        cmd += ["--mcp-config", str(mcp_config)]
    cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]
    cmd += ["--permission-mode", "bypassPermissions",
            "--max-turns", str(max_turns),
            "--max-budget-usd", str(max_budget_usd),
            "--disallowedTools",
            "WebFetch,WebSearch" + ("," + extra_disallowed
                                    if extra_disallowed else "")]
    if restrict_tools:
        cmd += ["--tools", ESSENTIAL_BUILTIN_TOOLS]
    if system_prompt_file is not None:
        cmd += ["--append-system-prompt-file", str(system_prompt_file)]
    if settings_file is not None:
        cmd += ["--settings", str(settings_file)]
    if plugin_dir is not None:
        cmd += ["--plugin-dir", str(plugin_dir)]
    return cmd


def run_agent(inst: dict[str, Any], run_dir: Path, *, claude_bin: str,
              worktree: Optional[Path] = None,
              model: str = "claude-haiku-4-5", effort: str = "low",
              max_turns: int = 60, max_budget_usd: float = 2.0,
              timeout_sec: int = 1200, mcp_config: Optional[Path] = None,
              system_prompt_file: Optional[Path] = None,
              extra_env: Optional[dict] = None,
              restrict_tools: bool = False,
              explore_tool: Optional[str] = None,
              dyn_tool: Optional[str] = None,
              flow_tools: Optional[tuple] = None,
              flow_first_tools: Optional[tuple] = None,
              nl_plan_tool: Optional[str] = None,
              flow_only_tool: Optional[str] = None,
              prompt_override: Optional[str] = None,
              gate_settings_file: Optional[Path] = None,
              skill_md: Optional[str] = None,
              sentinel: Optional[str] = None,
              plugin_dir: Optional[Path] = None,
              cbmem_dirs: bool = False,
              cbmem_payload: bool = False,
              claudemd_root_file: Optional[Path] = None,
              extra_disallowed: Optional[str] = None,
              cmv_relay: bool = False) -> dict[str, Any]:
    """Run the agent in ``worktree`` (already prepared + maps built), extract the
    patch. If ``worktree`` is None, prepare a fresh one (standalone use).

    Writes meta.json / result.json / patch.diff into run_dir and returns a
    summary dict. The agent edits the worktree; ``patch.diff`` is its diff vs
    base_commit (test files excluded)."""
    # run_dir MUST be absolute: the agent's cwd is the worktree, so a relative
    # run_dir would put claude-home INSIDE the repo and pollute the patch diff.
    run_dir = Path(run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    wt = Path(worktree) if worktree is not None else checkout.prepare_worktree(inst)
    # prompt_override lets a different task (e.g. SWT-bench test generation)
    # supply its own user prompt while reusing all the agent-invocation
    # machinery. When None, build the default bug-fix prompt.
    prompt = prompt_override if prompt_override is not None else build_prompt(
        inst, explore_tool=explore_tool, dyn_tool=dyn_tool,
        flow_tools=flow_tools, flow_first_tools=flow_first_tools,
        nl_plan_tool=nl_plan_tool, flow_only_tool=flow_only_tool)
    (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")

    home = _seed_claude_home(run_dir)
    if skill_md:
        # flow3 skill delivery: SKILL.md goes into the per-run claude-home,
        # NEVER the worktree (a worktree file would leak into patch.diff).
        from experiments.common import flow_protocol
        flow_protocol.write_skill(home, skill_md)
    if cbmem_payload:
        # baseline_cbmem2: the tool's own Claude Code install payload (skill,
        # agents, hooks) in the per-cell claude-home; hook wiring via
        # --settings.
        cbm_settings = _lay_cbmem_payload(run_dir, home)
        if gate_settings_file is None:
            gate_settings_file = cbm_settings
    started_at = __import__("time").time()
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(home)
    env["PYTHONPATH"] = str(STUDY_ROOT)
    # PARITY with LinuxFLBench: ENABLE_TOOL_SEARCH=false disables claude's
    # deferred-tools / tool-search mechanism, so MCP tools stay DIRECTLY callable
    # (never demoted to a searchable bucket) -- this is why LinuxFLBench's rep
    # tools were always active without --tools or a ToolSearch step. Plus the
    # autoupdater + run-id/run-dir env vars LinuxFLBench set.
    env["ENABLE_TOOL_SEARCH"] = "false"
    env["DISABLE_AUTOUPDATER"] = "1"
    env["RUN_ID"] = run_dir.name
    env["RUN_DIR"] = str(run_dir)
    # ISOLATION (cross-worktree-import contamination, HARDENED): agents run
    # `pip install -e .` / `pip install --user`, which writes an editable .pth /
    # .egg-link into the SHARED ~/.local/site-packages pointing at THAT cell's
    # worktree. Concurrent cells then import the package (ansible/openlibrary/...)
    # from a SIBLING worktree -> the agent's edits "don't take effect", it
    # thrashes 70+ turns / millions of tokens. Three layers, all in the env the
    # agent's Bash subprocesses inherit:
    #   1. PYTHONNOUSERSITE=1  -> every python ignores ~/.local at import time.
    #   2. PYTHONUSERBASE = a PER-CELL dir -> any `pip install --user` / editable
    #      install the agent does lands HERE (inside run_dir), NOT shared
    #      ~/.local, so even concurrent same-repo cells cannot clobber each other.
    #   3. PIP_USER=0 -> default pip away from --user installs entirely.
    # PYTHONNOUSERSITE alone (the prior fix) was necessary but not sufficient:
    # ~/.local kept accumulating sibling-worktree .pth files across runs, and
    # subprocesses that didn't honor the flag still read them. (1)+(2)+(3) make
    # the shared location unreachable AND unwritable for cross-contamination.
    env["PYTHONNOUSERSITE"] = "1"
    userbase = run_dir / "pyuserbase"
    userbase.mkdir(parents=True, exist_ok=True)
    env["PYTHONUSERBASE"] = str(userbase)
    env["PIP_USER"] = "0"
    # -- related-work arms ---------------------------------------------------
    if cbmem_dirs:
        env.update(cbmem_env(run_dir, worktree))
    if plugin_dir:
        # ContextSniper: ripgrep must be on PATH for their candidate collector
        # (`rg -l -F`); ~/.local/bin is where a static rg usually lives.
        env["PATH"] = str(Path.home() / ".local" / "bin") + os.pathsep + env.get("PATH", "")
        env.update(csniper_env(run_dir, worktree))
    if cmv_relay:
        # the relay hook resolves node + the CMV entry from these
        env["CMV_NODE_BIN"] = str(NODE_BIN)
        env["CMV_ENTRY"] = str(CMV_ENTRY)
    # AGENTS.md replication arm (arXiv 2601.20404, baseline_agentsmd): the
    # per-repo file sits at the REPOSITORY ROOT as project memory, exactly
    # where the study's organic AGENTS.md sat. It is removed again before
    # patch extraction (see below) so it can never enter patch.diff.
    laid_root_claudemd = False
    if claudemd_root_file is not None:
        root_md = wt / "CLAUDE.md"
        if not root_md.exists():
            root_md.write_text(
                Path(claudemd_root_file).read_text(encoding="utf-8"),
                encoding="utf-8")
            laid_root_claudemd = True
    if extra_env:
        env.update(extra_env)

    # test-environment shim (DEFAULT OFF; see setup_test_shim above). The flag
    # is read from the MERGED env so the harness can enable it per-cell via
    # extra_env or ambiently via the orchestrator's environment. When unset,
    # the agent env (incl. PATH) is byte-identical to before and no shim dirs
    # are created -- comparability with every completed run.
    test_shim_info = None
    if env.get("REPR_TEST_SHIM") == "1":
        test_shim_info = setup_test_shim(run_dir, env)

    if cbmem_payload:
        # Warm per-cell CBM daemon (the tool's documented steady state: the
        # shipped hooks only produce context while one is running; in a cold
        # `-p` session the SessionStart hook fires BEFORE the MCP server
        # starts the daemon). Own UI port per cell so concurrent daemons do
        # not collide; stopped after the agent. Logged to cbmem_daemon.log.
        try:
            port = 20000 + int(hashlib.md5(str(run_dir).encode()).hexdigest()[:4],
                               16) % 20000
            d = subprocess.run([str(CBMEM_BIN), "daemon", "start",
                                "--port=%d" % port],
                               env=env, capture_output=True, text=True,
                               timeout=120)
            (run_dir / "cbmem_daemon.log").write_text(
                "start rc=%s\n%s\n%s\n" % (d.returncode, d.stdout, d.stderr),
                encoding="utf-8")
        except Exception as e:  # fail-open like the hooks themselves
            (run_dir / "cbmem_daemon.log").write_text(
                "start FAILED: %r\n" % (e,), encoding="utf-8")

    cmd = build_command(claude_bin, prompt, model, effort, max_turns,
                        max_budget_usd, mcp_config, system_prompt_file,
                        restrict_tools=restrict_tools,
                        settings_file=gate_settings_file,
                        plugin_dir=plugin_dir,
                        extra_disallowed=extra_disallowed)
    meta = {"instance_id": inst["instance_id"], "repo": inst["repo"],
            "base_commit": inst["base_commit"], "worktree": str(wt),
            "model": model, "effort": effort}
    if test_shim_info is not None:
        meta["test_shim"] = test_shim_info
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2), "utf-8")

    timed_out = False
    try:
        # start_new_session: give claude its own process group/session so a
        # SIGHUP to the orchestrator's group (e.g. shell teardown, WSL bounce)
        # can't kill the agent mid-run. Without this, early cells died with
        # rc=129 (128+SIGHUP) -> empty patch -> false "unresolved".
        proc = subprocess.run(cmd, cwd=str(wt), env=env, capture_output=True,
                              text=True, timeout=timeout_sec,
                              start_new_session=True)
        (run_dir / "result.json").write_text(proc.stdout or "", "utf-8")
        if proc.stderr:
            (run_dir / "stderr.log").write_text(proc.stderr, "utf-8")
        rc = proc.returncode
    except subprocess.TimeoutExpired as e:
        timed_out = True
        (run_dir / "result.json").write_text(e.stdout or "" if isinstance(
            e.stdout, str) else "", "utf-8")
        rc = -1

    if cmv_relay and not timed_out:
        # baseline_cmv2: stop-and-resume relay on every CMV trim, so the
        # trimmed transcript is what the model actually sees.
        rc, timed_out = _cmv_relay_stages(run_dir, cmd, env, wt, rc,
                                          max_turns, max_budget_usd,
                                          timeout_sec)

    # harvest the session transcript (for MCP/tool-usage telemetry) from the
    # per-run claude-home before extracting the patch.
    _harvest_transcript(home, run_dir, started_at)

    if cbmem_payload:
        try:
            d = subprocess.run([str(CBMEM_BIN), "daemon", "stop"], env=env,
                               capture_output=True, text=True, timeout=120)
            with open(run_dir / "cbmem_daemon.log", "a", encoding="utf-8") as fh:
                fh.write("stop rc=%s\n%s\n%s\n" % (d.returncode, d.stdout,
                                                   d.stderr))
        except Exception as e:
            with open(run_dir / "cbmem_daemon.log", "a", encoding="utf-8") as fh:
                fh.write("stop FAILED: %r\n" % (e,))

    if sentinel:
        # flow3 skill telemetry: did /flowfix actually expand? Recorded in
        # meta.json; measurement only, never fails the cell.
        from experiments.common import flow_protocol
        meta["skill_expanded"] = flow_protocol.skill_expanded(
            run_dir / "transcript.jsonl", sentinel)
        (run_dir / "meta.json").write_text(json.dumps(meta, indent=2), "utf-8")

    if laid_root_claudemd:
        # take the repo-root CLAUDE.md back out (and out of the index, in
        # case the agent staged it) before the diff is extracted.
        try:
            (wt / "CLAUDE.md").unlink()
        except OSError:
            pass
        subprocess.run(["git", "rm", "-q", "--cached", "--ignore-unmatch",
                        "--", "CLAUDE.md"], cwd=str(wt), capture_output=True)

    # extract the agent's patch (worktree diff vs base, test files excluded)
    patch = checkout.diff_excluding_tests(inst, wt)
    (run_dir / "patch.diff").write_text(patch, encoding="utf-8")

    summary = {"instance_id": inst["instance_id"], "returncode": rc,
               "timed_out": timed_out, "patch_bytes": len(patch),
               "empty_patch": len(patch.strip()) == 0}
    (run_dir / "agent_summary.json").write_text(
        json.dumps(summary, indent=2), "utf-8")
    return summary
