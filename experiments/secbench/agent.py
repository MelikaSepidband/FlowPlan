"""SecBench agent runner with stream-json capture.

Unlike the SWE-Pro runner (``--output-format json``, whose per-turn transcript
is harvested from CLAUDE_CONFIG_DIR/projects), this uses
``--output-format stream-json``: every message the agent emits -- including
each ``tool_use`` -- is streamed to stdout as one JSON object per line, which
we save verbatim to ``run_dir/stream.jsonl`` and mine for the tool-call
sequence (``tools.jsonl``). This gives ground-truth adoption data (which MCP
tools were actually called) that the aggregate ``json`` output hides, and it
does not depend on claude writing a project transcript.
"""

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

from experiments.swebench_pro import agent as swe_agent

STUDY_ROOT = Path(__file__).resolve().parents[2]
ESSENTIAL_TOOLS = "Read,Edit,Write,Bash,Grep,Glob"


def _build_cmd(claude_bin, prompt, model, effort, max_turns, max_budget,
               mcp_config, restrict_tools, settings_file) -> list[str]:
    cmd = [claude_bin, "-p", prompt, "--output-format", "stream-json",
           "--verbose", "--strict-mcp-config"]
    if mcp_config is not None:
        cmd += ["--mcp-config", str(mcp_config)]
    cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]
    cmd += ["--permission-mode", "bypassPermissions",
            "--max-turns", str(max_turns),
            "--max-budget-usd", str(max_budget),
            "--disallowedTools", "WebFetch,WebSearch"]
    if restrict_tools:
        cmd += ["--tools", ESSENTIAL_TOOLS]
    if settings_file is not None:
        cmd += ["--settings", str(settings_file)]
    return cmd


def _mine_tools(stream_path: Path, run_dir: Path) -> dict:
    """Extract the tool-call sequence + result summary from stream.jsonl."""
    tools = []
    result = None
    denials = 0
    for line in stream_path.read_text(encoding="utf-8",
                                      errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        t = ev.get("type")
        if t == "assistant":
            for c in (ev.get("message", {}) or {}).get("content", []) or []:
                if isinstance(c, dict) and c.get("type") == "tool_use":
                    tools.append({"name": c.get("name"),
                                  "input": c.get("input")})
        elif t == "result":
            result = ev
            denials = len(ev.get("permission_denials", []) or [])
        elif t == "user":
            # tool_result carrying a permission-deny message counts a denial
            for c in (ev.get("message", {}) or {}).get("content", []) or []:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    txt = json.dumps(c)
                    if "permissionDecision" in txt and "deny" in txt:
                        denials += 1
    (run_dir / "tools.jsonl").write_text(
        "\n".join(json.dumps(t) for t in tools), encoding="utf-8")
    return {"tools": tools, "result": result, "denials": denials}


def run_agent(inst, run_dir: Path, *, claude_bin, worktree: Path, model,
              effort, max_turns, max_budget_usd, timeout_sec, prompt,
              mcp_config: Optional[Path], restrict_tools: bool,
              settings_file: Optional[Path],
              skill_md: Optional[str] = None,
              sentinel: Optional[str] = None,
              agentdiet_proxy: bool = False,
              pre_stage_dir: Optional[Path] = None) -> dict:
    """agentdiet_proxy / pre_stage_dir: the related-work arms ported from the
    SWE-bench Pro runner (baseline_agentdiet: their trajectory reduction as a
    per-cell proxy; baseline_sherloc: fold the localization pre-stage's usage
    into result.json). Both reuse the SWE-Pro helpers unchanged so the
    accounting is identical across benchmarks; the CLI invocation stays the
    SecBench baseline's."""
    run_dir = Path(run_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")

    home = swe_agent._seed_claude_home(run_dir)
    if skill_md:
        # flow3 skill delivery: SKILL.md goes into the per-run claude-home,
        # NEVER the worktree (a worktree file would leak into the harvested
        # model_patch.diff).
        from experiments.common import flow_protocol
        flow_protocol.write_skill(home, skill_md)
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(home)
    env["PYTHONPATH"] = str(STUDY_ROOT)
    env["ENABLE_TOOL_SEARCH"] = "false"
    env["DISABLE_AUTOUPDATER"] = "1"
    env["RUN_ID"] = run_dir.name
    env["RUN_DIR"] = str(run_dir)
    env["PYTHONNOUSERSITE"] = "1"
    ub = run_dir / "pyuserbase"
    ub.mkdir(parents=True, exist_ok=True)
    env["PYTHONUSERBASE"] = str(ub)
    env["PIP_USER"] = "0"
    agentdiet = None
    if agentdiet_proxy:
        agentdiet = swe_agent._start_agentdiet_proxy(run_dir, claude_bin, model, effort)
        env["ANTHROPIC_BASE_URL"] = agentdiet[1]

    cmd = _build_cmd(claude_bin, prompt, model, effort, max_turns,
                     max_budget_usd, mcp_config, restrict_tools, settings_file)
    meta = {"instance_id": inst["instance_id"], "cmd": cmd,
            "worktree": str(worktree)}
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2),
                                       encoding="utf-8")

    stream_path = run_dir / "stream.jsonl"
    timed_out = False
    started = time.time()
    try:
        with open(stream_path, "w", encoding="utf-8") as out:
            proc = subprocess.run(cmd, cwd=str(worktree), env=env, stdout=out,
                                  stderr=subprocess.PIPE, text=True,
                                  timeout=timeout_sec, start_new_session=True)
        if proc.stderr:
            (run_dir / "stderr.log").write_text(proc.stderr, encoding="utf-8")
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        timed_out = True
        rc = -1

    mined = _mine_tools(stream_path, run_dir) if stream_path.exists() else \
        {"tools": [], "result": None, "denials": 0}
    # Harvest the project transcript (like SWE-Pro): the CLI writes the
    # EXPANDED skill body only into config_dir/projects/**, NEVER into the
    # stdout stream-json -- a live probe confirmed the flowfix sentinel is
    # absent from stream.jsonl but present in the project transcript. So skill
    # telemetry must read the harvested transcript, not the stream.
    swe_agent._harvest_transcript(home, run_dir, started)
    if sentinel:
        # flow3 skill telemetry: did /flowfix actually expand? (the sentinel
        # lives only in SKILL.md; the harvested transcript carries it iff it
        # expanded.) Measurement only, never fails the cell.
        from experiments.common import flow_protocol
        meta["skill_expanded"] = flow_protocol.skill_expanded(
            run_dir / "transcript.jsonl", sentinel)
        (run_dir / "meta.json").write_text(json.dumps(meta, indent=2),
                                           encoding="utf-8")
    if mined["result"] is not None:
        (run_dir / "result.json").write_text(
            json.dumps(mined["result"]), encoding="utf-8")
    if pre_stage_dir is not None:
        # baseline_sherloc: the localization session is part of the cell
        swe_agent._merge_pre_stage(run_dir, Path(pre_stage_dir),
                                   Path(pre_stage_dir).name)
    if agentdiet is not None:
        # baseline_agentdiet: stop the per-cell proxy (writes
        # agentdiet_summary.json) and fold its reflection calls' usage in
        swe_agent._stop_agentdiet_proxy(*agentdiet)
        swe_agent._merge_side_calls(run_dir, run_dir / "agentdiet_reflections.jsonl",
                                    "agentdiet")
    summary = {"instance_id": inst["instance_id"], "returncode": rc,
               "timed_out": timed_out, "wall_s": round(time.time() - started, 1),
               "n_tool_calls": len(mined["tools"]),
               "denials": mined["denials"],
               "tool_names": sorted({t["name"] for t in mined["tools"]})}
    (run_dir / "agent_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8")
    return summary
