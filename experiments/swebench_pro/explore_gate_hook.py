"""Explore-gate hook for SWE-bench Pro rep conditions (PreToolUse + PostToolUse).

GOAL: hold the closed-gate token economics -- the agent localizes via the MAP,
not by browsing with Read/Grep/Glob, so Read stays LOW / MCP HIGH / tokens
~-21% vs baseline -- WITHOUT the empty-patch failures.

THE BUG WE ARE FIXING (measured 2026-06-22): a pure "deny all Read" gate makes a
weak Haiku/low cell QUIT when its understanding-Read is denied -> it never edits
-> EMPTY PATCH. And Claude Code's Edit tool refuses to edit a file not opened
with Read first, so the agent genuinely NEEDS to Read the file it is about to
edit. An earlier "allow Read only of files already EDITED" gate was backwards
(the agent Reads BEFORE editing, so the file is not yet an edit target -> Read
denied -> still empty) and produced MORE empty patches with Read=0.

THE FIX (map-surfaced allow): allow a Read of any file the MAP HAS ALREADY
SURFACED (an explore/node/etc. result that included that file's source/path).
That is exactly the read-then-edit the agent needs: it localizes with the map,
the map shows it the file, it Reads that file to edit it -> allowed. But a Read
of a file the map did NOT surface (pure browsing) is still denied, and so are
Grep/Glob. So Read rises only a LITTLE (to ~the files the map pointed at, ~2-3),
tokens stay near -21%, and the read-to-edit never dead-ends -> no empty patches.

Mechanism:
  PostToolUse on the rep MCP tools: parse the result text, extract the full
    repo-relative source-file paths it mentions, append them to
    RUN_DIR/.surfaced_files (the allowlist).
  PreToolUse:
    - MCP map call / Edit / Write -> allow (Edit also pre-adds its target to the
      allowlist, so even a file the agent decides to edit without a prior map hit
      can still be Read to satisfy the editor's read-first rule);
    - Read of a file whose path ends with a surfaced relative path -> ALLOW;
    - Read of any other file -> DENY (browsing; use the map);
    - Grep / Glob -> DENY (browsing; use the map).
  Bash is never gated (the cat/sed block backfired -- see memory
  swebench-bash-not-waste).

Config (env): REPR_GATE_REP, REPR_GATE_TOOL, RUN_DIR.
Fails OPEN: any internal error exits 0 with no output, so a broken hook can
never wedge a run. (Note: failing open on the PostToolUse means a missed path
just yields a stricter gate, never a crash; failing open on a Read decision
allows the Read -- both safe.)
"""

import json
import os
import re
import sys
from pathlib import Path

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
# repo-relative source-file paths: at least one "dir/....ext" with a code ext.
_PATH_RE = re.compile(
    r"[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)+\.(?:py|c|h|cc|cpp|hpp|js|ts|go|rs|java|rb)")


def resolve_run_dir() -> Path:
    run_dir = os.environ.get("RUN_DIR")
    if run_dir:
        return Path(run_dir).resolve()
    index_dir = os.environ.get("REPO_INDEX_DIR")
    if index_dir:
        return Path(index_dir).resolve() / "run"
    repo_root = os.environ.get("REPO_ROOT")
    base = Path(repo_root).resolve() if repo_root else Path.cwd().resolve()
    return base / ".repr_index" / "run"


def _surfaced_file() -> Path:
    return resolve_run_dir() / ".surfaced_files"


def _load_surfaced() -> set:
    try:
        return {ln.strip() for ln in _surfaced_file().read_text(
            encoding="utf-8").splitlines() if ln.strip()}
    except Exception:
        return set()


def _add_surfaced(paths) -> None:
    paths = {p for p in paths if p}
    if not paths:
        return
    try:
        f = _surfaced_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("a", encoding="utf-8") as fh:
            for p in sorted(paths):
                fh.write(p + "\n")
    except Exception:
        pass


def _response_text(tool_response) -> str:
    """Best-effort flatten of any PostToolUse tool_response shape to text."""
    if isinstance(tool_response, str):
        return tool_response
    if isinstance(tool_response, dict):
        if isinstance(tool_response.get("text"), str):
            return tool_response["text"]
        # content: [ {type, text}, ... ]
        c = tool_response.get("content")
        if isinstance(c, list):
            return " ".join(b.get("text", "") for b in c
                            if isinstance(b, dict) and isinstance(b.get("text"), str))
        return json.dumps(tool_response)
    if isinstance(tool_response, list):
        return " ".join(_response_text(x) for x in tool_response)
    return str(tool_response or "")


def _allowed_read(fp: str, surfaced: set) -> bool:
    """A Read is allowed iff the requested path ends with a surfaced relative
    path (map output uses repo-relative paths; the Read uses an absolute worktree
    path -- so suffix-match is the robust join)."""
    if not fp:
        return False
    norm = fp.replace("\\", "/")
    for rel in surfaced:
        rel = rel.replace("\\", "/")
        if norm == rel or norm.endswith("/" + rel):
            return True
    return False


def deny_read(rep: str, explore_tool: str) -> dict:
    reason = (
        "Localize with the precomputed code map, not by browsing. Use `%s` / "
        "`%s_node` instead of Read/Grep/Glob -- the map returns the relevant "
        "source inline (treat it as already read). Read is allowed for a file the "
        "MAP HAS ALREADY SHOWN you (so you can open it to edit); this file was not "
        "surfaced by the map yet -- run the map on it first (e.g. `%s_node` on the "
        "symbol/file, or `%s` with its name), then Read and Edit it. You MUST "
        "finish with a non-empty patch -- do not stop with the bug unfixed."
        % (explore_tool or ("%s_explore" % rep), rep, rep,
           explore_tool or ("%s_explore" % rep))
    )
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason}}


def handle_post(event) -> int:
    """Record map-surfaced file paths from an MCP tool result."""
    tool = event.get("tool_name") or ""
    if not (tool.startswith("mcp__") and "_perrep__" in tool):
        return 0
    text = _response_text(event.get("tool_response"))
    if text:
        _add_surfaced(set(_PATH_RE.findall(text)))
    return 0


def handle_pre(event) -> int:
    tool = event.get("tool_name") or ""
    tin = event.get("tool_input") or {}
    rep = os.environ.get("REPR_GATE_REP", "").strip()
    explore_tool = os.environ.get("REPR_GATE_TOOL", "").strip()

    # MCP map calls: allowed (navigation).
    if tool.startswith("mcp__") and "_perrep__" in tool:
        return 0

    # Edit/Write: allowed; also pre-add the target so its Read is permitted even
    # if the agent chose it without an explicit map hit on that exact path.
    if tool in EDIT_TOOLS:
        fp = tin.get("file_path") or tin.get("path")
        if fp:
            _add_surfaced({fp.replace("\\", "/")})
        return 0

    # Read: allow iff the file was surfaced by the map (or is an edit target).
    if tool == "Read":
        fp = tin.get("file_path") or tin.get("path")
        if _allowed_read((fp or "").strip(), _load_surfaced()):
            return 0
        sys.stdout.write(json.dumps(deny_read(rep, explore_tool),
                                    ensure_ascii=False) + "\n")
        return 0

    # Grep / Glob: browsing -> denied.
    if tool in ("Grep", "Glob"):
        sys.stdout.write(json.dumps(deny_read(rep, explore_tool),
                                    ensure_ascii=False) + "\n")
        return 0
    return 0


def main() -> int:
    try:
        event = json.loads(sys.stdin.read())
        if not isinstance(event, dict):
            return 0
        ev_name = event.get("hook_event_name") or ""
        if ev_name == "PostToolUse":
            return handle_post(event)
        return handle_pre(event)
    except Exception:
        return 0


if __name__ == "__main__":
    sys.exit(main())
