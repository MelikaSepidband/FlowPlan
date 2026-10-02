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
# SecBench (C/C++): their DEFAULT_FILE_EXTENSIONS is ("py", "cfg") -- a config
# default for their Python benchmarks, not part of the method. The stage passes
# the C/C++ extensions through this env so their filter_repo_dict keeps the C
# sources; every other knob stays at their default. Disclosed in the arm notes.
if os.environ.get("SHERLOC_FILE_EXTENSIONS"):
    FILE_EXTENSIONS = tuple(
        x.strip() for x in os.environ["SHERLOC_FILE_EXTENSIONS"].split(",") if x.strip())

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


C_EXTS = (".c", ".h", ".cc", ".cpp", ".cxx", ".hh", ".hpp", ".hxx")


def _repo_files(node, prefix=""):
    """Flatten their nested snapshot dict into (relative path, lines)."""
    out = []
    for k, v in node.items():
        if not isinstance(v, dict):
            continue
        p = "%s/%s" % (prefix, k) if prefix else k
        if "text" in v:
            out.append((p, v["text"]))
        else:
            out.extend(_repo_files(v, p))
    return out


def _c_connected_tree(repo: dict, target: Optional[str]) -> str:
    """C/C++ analog of their connected_tree (which parses Python import
    statements only, so on a C repository it returns nothing): the internal
    `#include` graph. A quoted or angle include resolves to a repository file
    by path relative to the includer, then by path suffix, then by unique
    basename; system headers that resolve to nothing are dropped (their tool
    likewise shows only internal dependencies). Output mirrors their per-file
    layout (imports / imported-by, 10 per list) and their summary of the ten
    most connected files. Same skip list of test/docs/build directories."""
    import re
    # their snapshot nests the tree under "structure" and their tools address
    # files relative to it; the analog does the same
    if isinstance(repo.get("structure"), dict):
        repo = repo["structure"]
    skip = {"test", "tests", "testing", "docs", "documentation", "build", "dist",
            ".git", "third_party", "thirdparty", "external", "vendor"}
    files = [(p, t) for p, t in _repo_files(repo)
             if p.endswith(C_EXTS) and not (set(p.split("/")[:-1]) & skip)]
    paths = [p for p, _ in files]
    by_base = {}
    for p in paths:
        by_base.setdefault(p.rsplit("/", 1)[-1], []).append(p)
    inc_re = re.compile(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]')
    deps = {p: set() for p in paths}
    for p, lines in files:
        d = p.rsplit("/", 1)[0] if "/" in p else ""
        for line in lines:
            m = inc_re.match(line)
            if not m:
                continue
            inc = m.group(1).strip().replace("\\", "/")
            cand = ("%s/%s" % (d, inc)) if d else inc
            # normalise ../ segments
            parts = []
            for seg in cand.split("/"):
                if seg == "..":
                    if parts:
                        parts.pop()
                elif seg and seg != ".":
                    parts.append(seg)
            cand = "/".join(parts)
            hit = None
            if cand in deps:
                hit = cand
            else:
                suf = [q for q in paths if q.endswith("/" + inc) or q == inc]
                if len(suf) == 1:
                    hit = suf[0]
                elif not suf:
                    b = by_base.get(inc.rsplit("/", 1)[-1], [])
                    if len(b) == 1:
                        hit = b[0]
            if hit and hit != p:
                deps[p].add(hit)
    rdeps = {p: set() for p in paths}
    for p, ds in deps.items():
        for q in ds:
            rdeps[q].add(p)
    if target:
        t = target.strip().lstrip("./")
        if t not in deps:
            suf = [q for q in paths if q.endswith("/" + t)]
            if len(suf) == 1:
                t = suf[0]
            else:
                return "ERROR: File '%s' not found in repository." % target
        out = ["═══ %s ═══" % t]
        d = sorted(deps[t])
        out.append("→ INCLUDES (%d):" % len(d))
        out += ["   • %s" % q for q in d[:10]]
        if len(d) > 10:
            out.append("   ... and %d more" % (len(d) - 10))
        r = sorted(rdeps[t])
        out.append("← INCLUDED BY (%d):" % len(r))
        out += ["   • %s" % q for q in r[:10]]
        if len(r) > 10:
            out.append("   ... and %d more" % (len(r) - 10))
        return "\n".join(out)
    ranked = sorted(paths, key=lambda q: -(len(deps[q]) + len(rdeps[q])))[:10]
    out = ["CONNECTED FILES OVERVIEW (C/C++ #include graph; %d source files, top 10 most connected):" % len(paths)]
    for q in ranked:
        out.append("  %s  → includes %d, ← included by %d" % (q, len(deps[q]), len(rdeps[q])))
    return "\n".join(out)


def _repo_has_python() -> bool:
    root = REPO["structure"] if isinstance(REPO.get("structure"), dict) else REPO
    return any(p.endswith(".py") for p, _ in _repo_files(root))


@mcp.tool()
def connected_tree(file: Optional[str] = None) -> str:
    """Shows import dependencies and relationships. If `file` is specified,
    shows the direct imports of that file and the files that import it. If no
    file is specified, shows a repository-wide import overview with import
    counts."""
    block = {"tool": "connected_tree"}
    if file:
        block["file"] = file
    if not _repo_has_python():
        # C/C++ repository (SecBench): their Python-import analysis has nothing
        # to read; serve the #include analog, logged like every other call.
        t0 = time.time()
        out = _c_connected_tree(REPO, file)
        if LOG_PATH:
            try:
                with open(LOG_PATH, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"ts": t0, "call": dict(block, analog="c_include"),
                                         "chars": len(out), "est_tokens": len(out) // 4,
                                         "ms": int((time.time() - t0) * 1000)}) + "\n")
            except Exception:
                pass
        return out
    return _run(block)


if __name__ == "__main__":
    mcp.run()
