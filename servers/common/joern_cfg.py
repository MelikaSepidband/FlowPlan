"""Joern-backed CFG fingerprint for a single C/C++ function (flowedit's C path).

The Python flow-edit fingerprint parses the function with stdlib ``ast`` and
runs the pure-Python CFG engine -- neither works on C. This module gives the
same *kind* of position-independent fingerprint for C by building a c2cpg CPG
of the ONE function (fast: ~1.5s build, ~5s query, proven in the round-trip
probe) and reading its statement-level CFG (nodes + cfgNext edges) out of the
CPG with a batch joern script.

The granularity differs from the Python engine (Joern is statement-level, the
python-ast engine is basic-block-level) but that is irrelevant: a fingerprint
is only ever compared BEFORE-vs-AFTER for the same function in the same
language, so the delta classification stays correct. C and Python fingerprints
are never compared to each other.
"""

import hashlib
import json
import os
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import Optional

JOERN_CLI = Path.home() / "tools" / "joern-cli"
C2CPG = JOERN_CLI / "c2cpg.sh"
JOERN = JOERN_CLI / "joern"
JDK_HOME = Path.home() / "tools" / "jdk-21.0.5+11"

C_EXTS = {".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".hh", ".hxx"}

# batch script: dump each internal method's statement-level CFG as JSON.
_CFG_SCRIPT = r'''
@main def exec(cpgFile: String, outFile: String) = {
  importCpg(cpgFile)
  val methods = cpg.method.isExternal(false).l
  val out = methods.map { m =>
    val nodes = m.cfgNode.l
    val id = nodes.zipWithIndex.map { case (n, i) => (n.id, i) }.toMap
    val edges = nodes.flatMap { n =>
      n.cfgNext.l.map { s => Map("src" -> id(n.id), "dst" -> id.getOrElse(s.id, -1)) }
    }
    // count control edges (branch/loop) via CONTROL_STRUCTURE cfgNext fan-out
    Map("method" -> m.name, "nblocks" -> nodes.size, "edges" -> edges)
  }
  java.nio.file.Files.writeString(java.nio.file.Paths.get(outFile), out.toJson)
}
'''


# batch script: dump each internal method's CFG WITH per-node line/code -- the
# record shape servers.cfg_server.server._joern_cfg_item consumes -- so a
# snippet CPG of ONE function can answer get_cfg/flow_of without any
# whole-repo CPG or running ``joern --server``. Node lines are relative to
# the snippet file (1 = first line of the function source); the caller
# re-offsets them to the real file.
_BLOCKS_SCRIPT = r'''
@main def exec(cpgFile: String, outFile: String) = {
  importCpg(cpgFile)
  val methods = cpg.method.isExternal(false).l
  val out = methods.map { m =>
    val nodes = m.cfgNode.l
    Map(
      "name" -> m.name,
      "fullName" -> m.fullName,
      "line" -> m.lineNumber.getOrElse(-1),
      "entry" -> m.id,
      "exit" -> m.methodReturn.id,
      "nodes" -> nodes.map { n =>
        Map("id" -> n.id, "code" -> n.code,
            "line" -> n.lineNumber.getOrElse(-1))
      },
      "edges" -> nodes.flatMap { n =>
        n.cfgNext.l.map { s => Map("src" -> n.id, "dst" -> s.id) }
      }
    )
  }
  java.nio.file.Files.writeString(java.nio.file.Paths.get(outFile), out.toJson)
}
'''


# batch script: dump (var, def_line, use_line) rows from the snippet CPG's
# data-dependence graph. ``run.ossdataflow`` computes reaching definitions on
# the freshly imported CPG (a snippet CPG has no overlays yet). Mirrors the
# dfg server's live-CPG def_use query (identifier.ddgIn) so the pair shape
# matches, but sourced from the CURRENT function source -- the live server's
# CPG is built PRE-edit and never refreshed, so post-edit fingerprints read
# from it are stale (they always equal the pre-edit graph).
_DDG_SCRIPT = r'''
@main def exec(cpgFile: String, outFile: String) = {
  importCpg(cpgFile)
  run.ossdataflow
  val rows = cpg.identifier.flatMap { u =>
    u.ddgIn.map { d =>
      Map("var" -> u.name,
          "use_line" -> u.lineNumber.getOrElse(-1),
          "def_line" -> d.lineNumber.getOrElse(-1))
    }
  }.dedup.l
  java.nio.file.Files.writeString(java.nio.file.Paths.get(outFile), rows.toJson)
}
'''


def is_c_file(path: str) -> bool:
    return Path(str(path)).suffix.lower() in C_EXTS


def _env() -> dict:
    env = dict(os.environ)
    env["JAVA_HOME"] = str(JDK_HOME)
    env["PATH"] = str(JDK_HOME / "bin") + os.pathsep + env.get("PATH", "")
    env["JAVA_OPTS"] = "-Xmx6g"
    return env


def available() -> bool:
    """True iff the c2cpg + joern CLIs and a JDK are present."""
    return C2CPG.is_file() and JOERN.is_file() and JDK_HOME.is_dir()


def _dump_cfg(c_src: str, work: Path, suffix: str = ".c",
              timeout: int = 300, script: str = _CFG_SCRIPT) -> Optional[list]:
    """Build a CPG of the single-function source and return per-method CFG.

    ``suffix`` controls which c2cpg frontend parses the file: ``.c`` for C,
    ``.cpp`` (or another C++ ext) so C++-only syntax (``Class::method``,
    templates, references) parses correctly instead of degrading to a
    mis-parsed C CFG. ``script`` selects the dump: the default fingerprint
    script (node/edge counts) or ``_BLOCKS_SCRIPT`` (nodes with line/code).
    """
    work.mkdir(parents=True, exist_ok=True)
    fname = "f" + (suffix if suffix.startswith(".") else "." + suffix)
    (work / fname).write_text(c_src, encoding="utf-8")
    cpg = work / "out.cpg"
    scriptf = work / "cfg.sc"
    scriptf.write_text(script, encoding="utf-8")
    outj = work / "cfg.json"
    env = _env()
    try:
        subprocess.run(["bash", str(C2CPG), str(work), "--output", str(cpg)],
                       capture_output=True, text=True, timeout=timeout, env=env)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if not cpg.is_file() or cpg.stat().st_size == 0:
        return None
    try:
        subprocess.run(
            ["bash", str(JOERN), "--script", str(scriptf),
             "--param", "cpgFile=%s" % cpg, "--param", "outFile=%s" % outj],
            capture_output=True, text=True, timeout=timeout, env=env)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if not outj.is_file():
        return None
    try:
        return json.loads(outj.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


#: Per-process cache of snippet method records: flow_of is typically called
#: several times on the same function within one agent run, and each snippet
#: build costs ~1.5s (c2cpg) + ~5s (joern script JVM).
_METHOD_RECORDS_CACHE: dict = {}


def c_cfg_method_records(func_src: str, suffix: str = ".c") -> Optional[list]:
    """Method records with per-node line/code from a snippet CPG of ONE
    function's source -- the record shape ``_joern_cfg_item`` consumes.

    LEAK-FIX path for LinuxFLBench flow-first: the per-version kernel CPGs
    under ``~/kernels`` were built scoped to the GROUND-TRUTH subsystem, so
    serving flow_of from them leaks answer locality (and their miss note
    named the scoping). A snippet CPG is built from the function source
    alone, so coverage is uniform across the repo: any function the
    (unscoped) symbol index resolves gets a CFG, none gets a scope tell.

    Node ``line`` values are relative to the snippet (1 = first line of
    ``func_src``); the caller re-offsets to the real file. Returns None when
    the Joern CLIs/JDK are absent or the build fails.
    """
    if not available():
        return None
    key = (hashlib.sha1(func_src.encode("utf-8", "replace")).hexdigest(),
           suffix)
    if key in _METHOD_RECORDS_CACHE:
        return _METHOD_RECORDS_CACHE[key]
    work = Path(tempfile.mkdtemp(prefix="flowedit_cblocks_"))
    records = _dump_cfg(func_src, work, suffix=suffix, script=_BLOCKS_SCRIPT)
    if records is not None:
        _METHOD_RECORDS_CACHE[key] = records
    return records


def c_def_use_pairs(func_src: str, suffix: str = ".c") -> Optional[list]:
    """[(var, def_line, use_line)] from a snippet CPG of ONE function's
    CURRENT source (lines are snippet-relative, 1 = first line of func_src).

    This is the C data-flow analog of c_cfg_fingerprint: because it is built
    from the source text passed in, a post-edit call sees the EDITED function
    -- unlike the per-instance ``joern --server`` CPG, which is built once
    pre-edit. Returns None when the Joern CLIs/JDK are absent or the build
    fails; [] when the function genuinely has no def-use pairs.
    """
    if not available():
        return None
    work = Path(tempfile.mkdtemp(prefix="flowedit_cddg_"))
    rows = _dump_cfg(func_src, work, suffix=suffix, script=_DDG_SCRIPT)
    if rows is None:
        return None
    pairs = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        var = str(r.get("var") or "")
        d, u = r.get("def_line"), r.get("use_line")
        if (var and isinstance(d, int) and d >= 1
                and isinstance(u, int) and u >= 1 and d != u):
            pairs.append((var, d, u))
    return sorted(set(pairs))


def c_dfg_fingerprint(func_src: str, suffix: str = ".c") -> Optional[tuple]:
    """(pair_count, frozenset of (var, def_ordinal, use_ordinal)) for a C/C++
    function -- position-independent (line numbers reduced to ordinals), same
    SHAPE as flowedit.dfg_fingerprint so classify_dfg_delta works unchanged."""
    pairs = c_def_use_pairs(func_src, suffix=suffix)
    if pairs is None:
        return None
    lines = sorted({ln for _v, d, u in pairs for ln in (d, u)})
    ordinal = {ln: i for i, ln in enumerate(lines)}
    skel = frozenset((var, ordinal.get(d, -1), ordinal.get(u, -1))
                     for var, d, u in pairs)
    return (len(skel), skel)


def c_cfg_fingerprint(func_src: str, suffix: str = ".c") -> Optional[tuple]:
    """(total_blocks, sorted (src,dst) edge multiset) for a C/C++ function.

    Same shape idea as flowedit.cfg_fingerprint (a position-independent block
    count + edge multiset) but sourced from Joern so it works on C/C++.
    ``suffix`` selects the c2cpg frontend (.c vs a C++ ext). Returns None if
    Joern/JDK are unavailable or the build fails.
    """
    if not available():
        return None
    work = Path(tempfile.mkdtemp(prefix="flowedit_c_"))
    methods = _dump_cfg(func_src, work, suffix=suffix)
    if methods is None:
        return None
    nb = sum(m.get("nblocks", 0) for m in methods)
    edges = Counter()
    for m in methods:
        for e in (m.get("edges") or []):
            edges[(e.get("src"), e.get("dst"))] += 1
    if nb == 0:
        return None
    return (nb, tuple(sorted(edges.items())))
