"""SHERLOC comparator arm (baseline_sherloc): the paper's localization
pre-stage, run on Claude Code, ahead of the ordinary bug-fix session.

Paper: "SHERLOC: Structured Diagnostic Localization for Code Repair Agents"
(Tamoyan et al., arXiv 2606.24820, EMNLP 2026 -- author-reported acceptance,
Anthology entry pending). Code: NVIDIA-NeMo/Skills, recipes/sherloc, sparse
clone at tools/sherloc_src (commit c5b9e72).

What their method is: a separate, read-only localization agent (reasoning LLM
+ four compact repository tools + self-recovery) that answers with a
<findings> diagnosis and a <locations> list, which are then injected into the
repair agent; they report -36.7% localization tokens, -23.1% total tokens and
+5.95 pp resolve rate on SWE-bench Verified with open-weight models inside
SWE-agent / OpenHands.

What this arm does, per cell:
  1. Snapshot: the worktree at the base commit is pickled into their nested
     dict format (their README builder, verbatim) and filtered with their
     DEFAULT_EXCLUDE_DIRS / DEFAULT_FILE_EXTENSIONS at load time.
  2. Localization session: a headless Claude Code session (same model /
     effort as the arm) whose ONLY tools are their four tools served over
     MCP (servers/sherloc_server), whose system prompt is their
     system.yaml (tools/sherloc_system.md), and whose user message is their
     first turn verbatim: "### Problem Description" + "### Repository
     Structure" (their tree rendering). A PostToolUse hook replays their
     loop-detection intervention and final-turn instruction
     (sherloc_hook.py); --max-turns caps the session at total_steps + 2.
  3. The final message is parsed with their parser rules (tag extraction
     with unclosed-tag fallback; path:L<start>-L<end> and path:L<line>).
  4. Injection: on success the <findings> + <locations> blocks are appended
     to the baseline task prompt of the repair session under a short
     heading; on failure the repair session runs as plain baseline and the
     failure is recorded (their paper does not specify the injection
     template or the failure path -- disclosed).
  5. Accounting: the localization session's usage / cost / turns are merged
     into the cell's result.json by agent.run_agent (pre_stage_dir), so the
     arm's tokens include the pre-stage, as the paper's "total tokens" do.

Disclosed deviations from their setup: Claude Code's loop replaces theirs
(native tool calls instead of <tool_call> JSON in text, native reasoning
instead of <think> tags, Claude Code context management instead of their
first-and-recent truncation); Haiku 4.5 at low effort instead of their
open-weight reasoning models; the Problem Description carries the same
task text the repair agent sees (SWE-bench Pro problem statement,
requirements and interface, per build_prompt's field policy) rather than the
SWE-bench problem statement alone.
"""
import json
import os
import pickle
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from experiments.swebench_pro import agent
from servers.sherloc_server._their import their_modules

STUDY_ROOT = agent.STUDY_ROOT
SHERLOC_SYSTEM_PROMPT = STUDY_ROOT / "tools" / "sherloc_system.md"
SHERLOC_HOOK = STUDY_ROOT / "experiments" / "swebench_pro" / "sherloc_hook.py"
TOTAL_STEPS = int(os.environ.get("SHERLOC_TOTAL_STEPS", "20"))
# every native tool off: the session has exactly their four tools
DISALLOWED_BUILTINS = ("Bash,Read,Grep,Glob,Edit,Write,MultiEdit,NotebookEdit,"
                       "Task,TodoWrite,Skill,ToolSearch,KillShell,BashOutput")
STAGE_DIRNAME = "sherloc"


# --- their snapshot builder (README "Build a repository snapshot", verbatim)
def build_snapshot(root: Path, instance_id: str, out_path: Path) -> dict:
    structure: dict = {}
    for path in sorted(root.rglob("*")):
        if path.is_dir() or ".git" in path.parts:
            continue
        rel = path.relative_to(root)
        node = structure
        for part in rel.parts[:-1]:
            node = node.setdefault(part, {})
        try:
            text = path.read_text(encoding="utf-8").splitlines()
        except (UnicodeDecodeError, OSError):
            continue
        node[rel.parts[-1]] = {"classes": [], "functions": [], "text": text}
    snapshot = {"instance_id": instance_id, "structure": structure}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "wb") as f:
        pickle.dump(snapshot, f)
    return snapshot


def problem_description(inst: dict) -> str:
    """The task text the repair agent sees (build_prompt's fields and
    REPR_PROMPT_MODE policy), for their "### Problem Description" slot."""
    mode = os.environ.get("REPR_PROMPT_MODE", "").strip().lower()
    parts = [str(inst.get("problem_statement") or "").strip()]
    if mode != "problem_only":
        req = str(inst.get("requirements") or "").strip()
        if req:
            parts += ["", "#### Requirements", "", req]
        iface = str(inst.get("interface") or "").strip()
        if iface and mode != "no_interface":
            parts += ["", "#### Interface / hints", "", iface]
    return "\n".join(parts)


def first_turn(problem: str, tree: str) -> str:
    """Their sherloc.py `inputs` f-string, verbatim."""
    return f"""
### Problem Description
{problem}

### Repository Structure
{tree}
"""


# --- their final-answer parser (dialog_processor.py rules) ------------------
def _tag_content(text: str, tag: str) -> str:
    m = re.search("<%s>(.*?)</%s>" % (tag, tag), text, re.DOTALL)
    if m:
        return m.group(1).strip()
    m = re.search("<%s>\\s*(.*?)$" % tag, text, re.DOTALL)   # unclosed tag
    return m.group(1).strip() if m else ""


def parse_final(text: str) -> dict:
    out: dict[str, Any] = {"findings": _tag_content(text, "findings") or None,
                           "locations": []}
    body = _tag_content(text, "locations")
    for line in body.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"([^:]+):L(\d+)-L(\d+)", line)
        if m:
            fp, a, b = m.groups()
            out["locations"].append({"file_path": fp, "start_line": int(a),
                                     "end_line": int(b), "raw": line})
            continue
        m = re.match(r"([^:]+):L(\d+)", line)
        if m:
            fp, a = m.groups()
            out["locations"].append({"file_path": fp, "start_line": int(a),
                                     "end_line": int(a), "raw": line})
        else:
            out["locations"].append({"raw": line})
    return out


def injection_block(loc: dict) -> str:
    """The hand-off to the repair agent: their two blocks, verbatim, under a
    short heading (their paper does not print the template it used)."""
    # Neutral hand-off only: the paper gives no template, so nothing is added
    # beyond naming what the blocks are. (The smoke's first wording also told
    # the agent to "verify them against the code before editing" -- a steer of
    # ours that could push the repair agent back into exploration; removed.)
    lines = [
        "## Localization from a prior read-only analysis (SHERLOC)",
        "A separate localization pass inspected this repository for the "
        "problem above. Its diagnosis and the edit locations it identified:",
        "",
        "<findings>",
        (loc.get("findings") or "").strip() or "(no findings block)",
        "</findings>",
        "",
        "<locations>",
    ]
    lines += [l["raw"] for l in loc.get("locations") or []]
    lines += ["</locations>"]
    return "\n".join(lines)


# --- per-cell config ---------------------------------------------------------
def write_mcp_config(stage: Path, snapshot: Path) -> Path:
    cfg = {"mcpServers": {"sherloc": {
        "command": str(agent.VENV_PYTHON),
        "args": ["-m", "servers.sherloc_server.server"],
        "env": {"PYTHONPATH": str(STUDY_ROOT),
                "SHERLOC_SNAPSHOT": str(snapshot),
                "SHERLOC_SRC": str(STUDY_ROOT / "tools" / "sherloc_src"),
                "SHERLOC_LOG": str(stage / "sherloc_tools.jsonl")}}}}
    p = stage / "mcp.json"
    p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return p


def write_hook_settings(stage: Path) -> Path:
    cmd = agent._hook_cmd({"RUN_DIR": str(stage),
                           "SHERLOC_TOTAL_STEPS": str(TOTAL_STEPS)},
                          SHERLOC_HOOK)
    settings = {"hooks": {"PostToolUse": [
        {"matcher": "mcp__sherloc__.*",
         "hooks": [{"type": "command", "command": cmd, "timeout": 30}]}]}}
    p = stage / "settings.json"
    p.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    return p


def _count_calls(stage: Path) -> int:
    try:
        return sum(1 for l in (stage / "sherloc_calls.jsonl").read_text(
            encoding="utf-8").splitlines() if l.strip())
    except OSError:
        return 0


# --- the stage -----------------------------------------------------------------
def run_stage(inst: dict, run_dir: Path, wt: Path, *, claude_bin: str,
              model: str, effort: str, max_budget_usd: float,
              timeout_sec: int) -> tuple[Optional[str], Path]:
    """Run the localization session for one cell. Returns (injection block
    or None on failure, stage dir). Never raises: a failure is recorded in
    stage/localization.json and the repair session runs as baseline."""
    run_dir = Path(run_dir).resolve()
    stage = run_dir / STAGE_DIRNAME
    stage.mkdir(parents=True, exist_ok=True)
    loc: dict[str, Any] = {"status": "failed", "reason": None, "findings": None,
                           "locations": [], "raw_final": "", "total_steps": TOTAL_STEPS}
    t0 = time.time()
    try:
        RepoManager, _Exec, exclude_dirs, file_exts = their_modules()
        snap_path = stage / "snapshot.pkl"
        snap = build_snapshot(Path(wt), inst["instance_id"], snap_path)
        filtered = RepoManager.filter_repo_dict(snap, exclude_dirs, file_exts)
        tree = RepoManager.tree_repo_dict(filtered, False)
        del snap, filtered
        prompt = first_turn(problem_description(inst), tree)
        (stage / "prompt.md").write_text(prompt, encoding="utf-8")
        loc["tree_lines"] = tree.count("\n") + 1
        loc["prompt_chars"] = len(prompt)

        mcp = write_mcp_config(stage, snap_path)
        settings = write_hook_settings(stage)
        home = agent._seed_claude_home(stage)
        env = dict(os.environ)
        env["CLAUDE_CONFIG_DIR"] = str(home)
        env["PYTHONPATH"] = str(STUDY_ROOT)
        env["ENABLE_TOOL_SEARCH"] = "false"
        env["DISABLE_AUTOUPDATER"] = "1"
        env["RUN_ID"] = run_dir.name + "-sherloc"
        env["RUN_DIR"] = str(stage)
        cmd = agent.build_command(
            claude_bin, prompt, model, effort, TOTAL_STEPS + 2, max_budget_usd,
            mcp, SHERLOC_SYSTEM_PROMPT, settings_file=settings,
            tools_override="", extra_disallowed=DISALLOWED_BUILTINS)
        # Their first turn embeds the whole filtered repository tree, which on
        # the ansible instances is ~197 KB -- past the kernel's per-argument
        # limit (MAX_ARG_STRLEN, 128 KB), so `-p <text>` dies with OSError(7)
        # "Argument list too long" before the model is ever called. `-p` with
        # no text reads the prompt from stdin instead; identical session,
        # verified on CLI 2.1.220.
        i = cmd.index("-p")
        cmd = cmd[:i + 1] + cmd[i + 2:]
        (stage / "cmd.txt").write_text(
            " ".join(shlex.quote(c) for c in cmd) + "\n# prompt on stdin\n",
            encoding="utf-8")
        started = time.time()
        timed_out = False
        try:
            proc = subprocess.run(cmd, cwd=str(wt), env=env, input=prompt,
                                  capture_output=True,
                                  text=True, timeout=timeout_sec,
                                  start_new_session=True)
            out, rc = proc.stdout or "", proc.returncode
            if proc.stderr:
                (stage / "stderr.log").write_text(proc.stderr, encoding="utf-8")
        except subprocess.TimeoutExpired as e:
            out = (e.stdout if isinstance(e.stdout, str) else "") or ""
            rc, timed_out = -1, True
        (stage / "result.json").write_text(out, encoding="utf-8")
        agent._harvest_transcript(home, stage, started)
        try:
            r = json.loads(out or "{}")
        except Exception:
            r = {}
        final = r.get("result") or ""
        parsed = parse_final(final)
        loc.update({"raw_final": final, "findings": parsed["findings"],
                    "locations": parsed["locations"], "rc": rc,
                    "timed_out": timed_out, "subtype": r.get("subtype"),
                    "is_error": r.get("is_error"), "num_turns": r.get("num_turns"),
                    "n_tool_calls": _count_calls(stage),
                    "total_cost_usd": r.get("total_cost_usd"),
                    "usage": r.get("usage"), "duration_ms": r.get("duration_ms"),
                    "session_id": r.get("session_id")})
        if timed_out:
            loc["reason"] = "timeout"
        elif r.get("is_error") or not r:
            loc["reason"] = "session_error: %s" % (r.get("subtype") or rc)
        elif not parsed["locations"]:
            loc["reason"] = ("max_steps_exceeded" if r.get("subtype") == "error_max_turns"
                             else "no_tool_or_location_generated")
        else:
            loc["status"], loc["reason"] = "success", None
        try:
            snap_path.unlink()   # tens of MB per cell; prompt.md keeps the tree
        except OSError:
            pass
    except Exception as e:  # never let the pre-stage kill the cell
        loc["reason"] = "stage_exception: %r" % (e,)
    loc["stage_seconds"] = round(time.time() - t0, 1)
    (stage / "localization.json").write_text(json.dumps(loc, indent=2),
                                             encoding="utf-8")
    block = injection_block(loc) if loc["status"] == "success" else None
    (stage / "injected.txt").write_text(
        block or "(stage %s: %s -- repair session ran as baseline)"
        % (loc["status"], loc["reason"]), encoding="utf-8")
    return block, stage
