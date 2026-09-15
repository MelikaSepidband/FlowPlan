"""SHERLOC comparator (arm baseline_sherloc; arXiv 2606.24820, EMNLP'26): the
paper's four read-only repository tools, served to Claude Code over MCP.

The tools run THEIR code unmodified (tools/sherloc_src/recipes/sherloc/
inference/sherloc_utils: ToolExecutor + RepoManager) against the same kind of
in-memory repository snapshot their loop uses -- a pickled nested dict built
from the worktree at the instance's base commit (SHERLOC_SNAPSHOT), filtered
with their DEFAULT_EXCLUDE_DIRS / DEFAULT_FILE_EXTENSIONS and served with
their defaults (max_view_lines=1000, show_line_counts=False). Tool names,
arguments and rendered outputs are theirs; the descriptions are the tool
paragraphs of their system prompt (prompt/eval/sherloc/system.yaml).

Deviation (disclosed): their model emits <tool_call> JSON in text and their
loop parses it; here Claude Code calls these tools natively. Every call is
logged (SHERLOC_LOG, JSONL) with the tool, arguments and output size.
"""
import json
import os
import pickle
import time
from typing import Optional

from mcp.server.fastmcp import FastMCP

from servers.sherloc_server._their import their_modules

RepoManager, ToolExecutor, EXCLUDE_DIRS, FILE_EXTENSIONS = their_modules()

SNAPSHOT = os.environ["SHERLOC_SNAPSHOT"]
LOG_PATH = os.environ.get("SHERLOC_LOG")


class _Cfg:
    """The two ToolExecutor knobs, at their SherlocGenerationConfig defaults."""
    max_view_lines = int(os.environ.get("SHERLOC_MAX_VIEW_LINES", "1000"))
    show_line_counts = os.environ.get("SHERLOC_SHOW_LINE_COUNTS", "0") == "1"


with open(SNAPSHOT, "rb") as _fh:
    _repo = pickle.load(_fh)
REPO = RepoManager.filter_repo_dict(_repo, EXCLUDE_DIRS, FILE_EXTENSIONS)
del _repo
EXEC = ToolExecutor(_Cfg(), tokenizer=None)

mcp = FastMCP("sherloc")


def _run(block: dict) -> str:
    t0 = time.time()
    out, ntok = EXEC.execute_tool(block, REPO)
    if LOG_PATH:
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": t0, "call": block, "chars": len(out),
                                     "est_tokens": ntok,
                                     "ms": int((time.time() - t0) * 1000)}) + "\n")
        except Exception:
            pass
    return out


@mcp.tool()
def view_file(path: str, view_range: Optional[list[int]] = None) -> str:
    """Show a file with line numbers. Required argument: `path` (string).
    Optional argument: `view_range` (array of two integers): `[start, end]` shows
    inclusive lines start-end; `[start, -1]` shows from start to EOF; omit
    `view_range` to show the whole file."""
    block = {"tool": "view_file", "path": path}
    if view_range is not None:
        block["view_range"] = list(view_range)
    return _run(block)


@mcp.tool()
def codebase_search(query: str) -> str:
    """Searches the entire repository case-insensitively for the exact text in
    `query` (functions, classes, variables, or any text). Returns preformatted
    snippets with 20 lines before and after each match, capped at 3 snippets
    per file and the 5 files with the most matches."""
    return _run({"tool": "codebase_search", "query": query})


@mcp.tool()
def repo_tree() -> str:
    """Display the repository file structure."""
    return _run({"tool": "repo_tree"})


@mcp.tool()
def connected_tree(file: Optional[str] = None) -> str:
    """Shows import dependencies and relationships. If `file` is specified,
    shows the direct imports of that file and the files that import it. If no
    file is specified, shows a repository-wide import overview with import
    counts."""
    block = {"tool": "connected_tree"}
    if file:
        block["file"] = file
    return _run(block)


if __name__ == "__main__":
    mcp.run()
