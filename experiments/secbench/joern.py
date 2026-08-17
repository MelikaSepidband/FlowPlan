"""Per-instance Joern CPG build + server for the SecBench C/C++ static CFG.

The static cfg tools (get_cfg/cfg_paths/dominators) and the block-level dynamic
overlay (executed_path/covered_blocks) need a control-flow graph. On C/C++ the
pure-Python engine cannot parse the source, so they route to Joern
(JOERN_SERVER_URL). This module builds a scoped ``c2cpg`` CPG for one checkout
and runs a ``joern --server`` preloaded with it, exposing the three
JOERN_SERVER_URL / JOERN_USER / JOERN_PASSWORD env vars the cfg/dfg servers
read.

Scope: rather than a whole-repo CPG (huge + slow on gpac/exiv2/libredwg), we
build the CPG over the DIRECTORIES named in the sanitizer crash stack -- exactly
the code the agent needs control-flow for, and nothing else. This is correct
(covers every crash-path file) and cheap (seconds-to-minutes, not hours).
"""

import os
import subprocess
import time
from pathlib import Path

from experiments.secbench import trace as sec_trace

# Tool locations, overridable via env (defaults match the study machine).
JOERN_CLI = Path(os.environ.get("JOERN_CLI_DIR",
                                str(Path.home() / "tools" / "joern-cli")))
JDK_HOME = Path(os.environ.get("JOERN_JAVA_HOME",
                               str(Path.home() / "tools" / "jdk-21.0.5+11")))
C2CPG = JOERN_CLI / "c2cpg.sh"


def _c2cpg_env(heap_gb: int) -> dict:
    env = dict(os.environ)
    env["JAVA_HOME"] = str(JDK_HOME)
    env["PATH"] = str(JDK_HOME / "bin") + os.pathsep + env.get("PATH", "")
    env["JAVA_OPTS"] = "-Xmx%dg" % int(heap_gb)
    return env


def crash_scope_dirs(inst: dict, wt: Path) -> list[Path]:
    """Absolute directories of the crash-stack files that exist in ``wt``.

    Falls back to the gold-changed file's dir set is NOT used (that would leak
    the fix); we scope purely on the crash stack, which is agent-visible.
    """
    t = sec_trace.build_trace(inst, repo_root=wt)
    dirs: list[Path] = []
    seen = set()
    for rel in (t.get("files") or {}):
        d = (Path(wt) / rel).parent
        if d.is_dir() and str(d) not in seen:
            seen.add(str(d))
            dirs.append(d)
    return dirs


def build_cpg(inst: dict, wt: Path, out_cpg: Path, *, heap_gb: int = 8,
              timeout: int = 1800) -> dict:
    """Build a c2cpg CPG scoped to the crash-stack directories of ``wt``.

    c2cpg takes ONE input path. When the crash stack spans multiple dirs we
    pick their nearest common ancestor within the repo (so all crash files are
    covered); when it is one dir we use it directly. Returns a record dict.
    """
    out_cpg.parent.mkdir(parents=True, exist_ok=True)
    dirs = crash_scope_dirs(inst, wt)
    rec = {"cpg": str(out_cpg), "ok": False, "error": None, "seconds": 0.0,
           "scope": None}
    if not dirs:
        rec["error"] = "no in-repo crash-stack dirs to scope the CPG"
        return rec
    if len(dirs) == 1:
        scope = dirs[0]
    else:
        # nearest common ancestor of all crash dirs, bounded to inside wt.
        parts = [d.resolve().parts for d in dirs]
        common = []
        for tup in zip(*parts):
            if len(set(tup)) == 1:
                common.append(tup[0])
            else:
                break
        scope = Path(*common) if common else Path(wt)
        # never scope above the checkout root
        wtr = Path(wt).resolve()
        if not str(scope).startswith(str(wtr)):
            scope = wtr
    rec["scope"] = str(scope)

    if out_cpg.is_file() and out_cpg.stat().st_size > 0:
        rec["ok"] = True
        rec["error"] = "exists"
        rec["bytes"] = out_cpg.stat().st_size
        return rec

    cmd = ["bash", str(C2CPG), str(scope), "--output", str(out_cpg)]
    start = time.monotonic()
    try:
        proc = subprocess.run(cmd, env=_c2cpg_env(heap_gb), cwd=str(wt),
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        rec["seconds"] = round(time.monotonic() - start, 1)
        rec["error"] = "c2cpg timeout after %ds" % timeout
        return rec
    rec["seconds"] = round(time.monotonic() - start, 1)
    if proc.returncode != 0:
        rec["error"] = "c2cpg exit %d: %s" % (
            proc.returncode, (proc.stderr or proc.stdout or "")[-500:])
        return rec
    if out_cpg.is_file() and out_cpg.stat().st_size > 0:
        rec["ok"] = True
        rec["bytes"] = out_cpg.stat().st_size
    else:
        rec["error"] = "c2cpg produced no CPG file"
    return rec
