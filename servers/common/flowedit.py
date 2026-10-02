"""Graph-mediated editing: flow_plan + flow_check shared implementation.

The central hypothesis of the study (repair phase): instead of editing raw code
directly, the agent first commits to an explicit CFG/DFG DELTA plan derived from
the bug report ("add a guard dominating line L", "break the def-use pair
x@Ldef->x@Luse", "reroute the false arm of the branch at L"), and only then
projects that plan into a code edit. flow_check then RECOMPUTES the function's
graph from the edited source and verifies the realized change matches the plan
-- a machine-checkable consistency signal a natural-language plan cannot offer.

The op vocabulary is data-grounded in Probe A's taxonomy over 60 real gold
patches (add_guard 35, remove_branch 43, block_structure_change 13,
rewire_edge 2; add/remove/move_def_use 20/21/41), plus a no_flow_change escape
hatch for the ~9% flow-invisible fixes (pure expression/constant edits).

State lives in $RUN_DIR: flow_plan.json (the committed plan) and per-function
pre-edit graph fingerprints, so flow_check can diff current-vs-planned without
re-reading the pre-edit source.
"""

import ast
import contextlib
import io
import json
import os
from pathlib import Path
from typing import Optional

from servers.common import env


def _quiet_stdout():
    """Silence engine computations' stdout (I/O hygiene, un-gated).

    The perrep server speaks MCP over stdio, so a stray print from an engine
    (e.g. beniget's "W: unbound identifier ..." warnings while fingerprinting
    a function snippet full of module-level names) corrupts the protocol
    channel and hangs the tool call. That output never legitimately reached
    any protocol, so every fingerprint/compute path wraps itself in this."""
    return contextlib.redirect_stdout(io.StringIO())

# --- op vocabulary (data-grounded) -------------------------------------------

CFG_OPS = {
    "add_guard": "add a branch/guard (if/early-return/raise) that dominates a "
                 "line -- the most common fix shape",
    "remove_branch": "remove or relax a branch/guard so a previously blocked "
                     "path is taken",
    "change_condition": "change a branch condition (same branch, different "
                        "predicate)",
    "change_block_structure": "restructure blocks (add/remove a loop, "
                              "try/except, split a block)",
    "rewire_edge": "reroute an edge between existing blocks (which arm goes "
                   "where) without changing the block set",
}
DFG_OPS = {
    "add_def_use": "introduce a new definition-and-use (compute/assign a value "
                   "and use it)",
    "remove_def_use": "remove a definition or its use (drop a variable/flow)",
    "move_def_use": "move where a value is defined or used (reorder/relocate "
                    "the assignment or its consumer)",
}
UNIVERSAL_OPS = {
    "no_flow_change": "the fix does NOT change control/data flow (pure "
                      "expression, constant, call-target, or signature edit) "
                      "-- valid; use this rather than inventing a fake delta",
}

# v3-only universal op: 8/30 SWE-Pro gold fixes ADD a whole function, which
# the v2 vocabulary cannot express. Verified by an index-bypassing existence
# check (exists in CURRENT source, absent at git HEAD), not a fingerprint
# diff -- see the add_function verification path in flowedit_tools.
V3_OPS = {
    "add_function": "add a brand-new function/definition that did not exist "
                    "before; give 'function' as the NEW function's name and "
                    "'anchor' as file:line of where it will live",
}


def flow_v3() -> bool:
    """The flow3 arm gate: REPR_FLOW_V3=1, read at CALL time (so tests and
    per-cell harness envs can toggle it). Default off = exact v2 behavior;
    every v3 behavior (add_function, fault_site, HEAD snapshots, merge-on-
    replan, graded verdicts) hangs off this."""
    return os.environ.get("REPR_FLOW_V3", "").strip() == "1"


def valid_ops(rep: str) -> dict:
    base = dict(UNIVERSAL_OPS)
    if rep == "cfgdfg":
        # combined arm: the agent sees BOTH graphs and may plan either kind
        # of delta; each delta is verified against ITS graph (rep_for_op).
        base.update(CFG_OPS)
        base.update(DFG_OPS)
    else:
        base.update(CFG_OPS if rep == "cfg" else DFG_OPS)
    if flow_v3():
        base.update(V3_OPS)
    return base


def rep_for_op(op: str):
    """Which graph a planned op is verified against: 'cfg', 'dfg', or None
    for no_flow_change (which in the combined arm means NEITHER changed)."""
    if op in CFG_OPS:
        return "cfg"
    if op in DFG_OPS:
        return "dfg"
    return None


# --- v3 graded verdicts ------------------------------------------------------
#
# The v2 verifier's exact-op match is too strict (Opus n=30: 9/30 FAILs score
# >=0.8 against the gold delta; change_condition is structurally unobservable
# because fingerprints carry no predicate text). v3 grades planned-vs-observed
# with SYMMETRIC "same family" pairs: a realized delta in the planned op's
# family passes with a note instead of firing the retry loop.

_FAMILY_PAIRS = frozenset({
    frozenset({"add_guard", "change_block_structure"}),
    frozenset({"remove_branch", "change_block_structure"}),
    frozenset({"rewire_edge", "change_block_structure"}),
    frozenset({"change_condition", "rewire_edge"}),
    frozenset({"change_condition", "no_flow_change"}),
    frozenset({"add_def_use", "move_def_use"}),
    frozenset({"remove_def_use", "move_def_use"}),
})


def op_verdict(planned, observed) -> str:
    """Grade a planned op against the observed one: 'exact' | 'same_family'
    | 'mismatch' | 'unknown'. THE single verdict function -- flow_check,
    flow_verify and the offline flow_verify CLI all call this so their
    verdicts agree. match-compatibility: exact/same_family -> match True,
    mismatch -> False, unknown -> None. add_function is exact-only (no
    family partner)."""
    if planned is None or observed is None:
        return "unknown"
    p, o = str(planned).strip(), str(observed).strip()
    if not p or not o:
        return "unknown"
    if "unknown" in (p, o):
        # v3 classifiers report 'unknown' when two fingerprints cannot be
        # compared (mixed old/new shapes) -- that is not a mismatch, it is
        # unverifiable. 'unknown' is not in any op vocabulary, so this
        # branch is unreachable from real planned/observed ops.
        return "unknown"
    if p == o:
        return "exact"
    if frozenset((p, o)) in _FAMILY_PAIRS:
        return "same_family"
    return "mismatch"


# --- fingerprints (position-independent, reused from Probe A's method) --------


def _dedent(src: str) -> str:
    lines = src.splitlines()
    pad = min((len(l) - len(l.lstrip()) for l in lines if l.strip()),
              default=0)
    return "\n".join(l[pad:] if len(l) >= pad else l for l in lines) + "\n"


def _func_source(file_abs: Path, func_line: int,
                 name: Optional[str] = None) -> Optional[str]:
    """The source text of the function whose def is at (or near) func_line.

    Python: parse with ``ast`` and return the exact def segment. C/C++ (and any
    non-Python source): fall back to a line-range extraction (``ast`` cannot
    parse C), which flowedit's Joern path fingerprints. ``end_line`` from the
    symbol index is threaded in by ``_func_source_ranged`` for the C case.

    ``name`` enables the LINE-DRIFT fallback: ``func_line`` comes from the
    symbol index, which was built BEFORE the agent's edits. Edits above the
    function (imports, docstrings, new tables) shift its def past the +-3
    tolerance and the line search finds nothing -- the exact failure that made
    flow_verify report "cannot compute the edited function's graph" on
    perfectly parseable code. The NAME does not move when lines shift, so when
    the line search misses we look the def up by name; if several defs share
    the name (methods of different classes) the one closest to the old line
    wins.
    """
    from servers.common import joern_cfg
    if joern_cfg.is_c_file(str(file_abs)):
        return None  # C uses _func_source_ranged (needs end_line)
    try:
        text = file_abs.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(text)
    except (OSError, SyntaxError, ValueError):
        return None
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if abs(node.lineno - func_line) <= 3:
                if best is None or abs(node.lineno - func_line) < abs(
                        best.lineno - func_line):
                    best = node
    # REPR_FLOW_LEGACY_VERIFY=1 (user decision 2026-08-05): the AGENT-FACING
    # tools run the PRE-FIX behavior -- no by-name fallback, so a drifted
    # function yields "cannot compute" exactly as in the Haiku-v2 and first
    # Opus runs. Offline analysis (env unset) keeps the fixed resolution.
    legacy = os.environ.get("REPR_FLOW_LEGACY_VERIFY", "").strip() == "1"
    if name and not legacy and flow_v3():
        # v3 (fix C): NAME-FIRST. The indexed line goes stale as the agent
        # edits above the function, so a NEIGHBOR's def can drift INTO the
        # +-3 window and get fingerprinted in place of the named function
        # (false 'flow_changed' on the unedited neighbor). When any def in
        # the text carries the planned name, the same-named def nearest to
        # func_line wins; the proximity pick is only trusted when NO def
        # carries the name. v2 keeps proximity-first byte-identical.
        short = str(name).rsplit(".", 1)[-1]
        cands = [n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name == short]
        if cands and (best is None or best.name != short):
            best = min(cands, key=lambda n: abs(n.lineno - func_line))
    elif best is None and name and not legacy:
        short = str(name).rsplit(".", 1)[-1]
        cands = [n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name == short]
        if cands:
            best = min(cands, key=lambda n: abs(n.lineno - func_line))
    if best is None:
        return None
    seg = ast.get_source_segment(text, best)
    return _dedent(seg) if seg else None


def _func_source_ranged(file_abs: Path, start_line: int,
                        end_line: Optional[int]) -> Optional[str]:
    """Function source by 1-based [start_line, end_line] range (C/C++ path).

    c2cpg parses a standalone function fine, so a plain line-slice is enough;
    if end_line is missing we take a bounded window and let brace-balancing
    trim it to the function body.
    """
    try:
        lines = file_abs.read_text(encoding="utf-8",
                                   errors="replace").splitlines()
    except OSError:
        return None
    if start_line < 1 or start_line > len(lines):
        return None
    if end_line and end_line >= start_line:
        seg = lines[start_line - 1:end_line]
    else:
        # no end_line: grab a window and brace-balance from the first '{'.
        window = lines[start_line - 1:start_line - 1 + 400]
        seg, depth, started = [], 0, False
        for ln in window:
            seg.append(ln)
            depth += ln.count("{") - ln.count("}")
            if "{" in ln:
                started = True
            if started and depth <= 0:
                break
    src = "\n".join(seg).strip()
    return (src + "\n") if src else None


def comprehension_signature(func_src: str) -> tuple:
    """v3 CFG-fingerprint extension (fix 3): counts of the INLINE control
    flow the block CFG cannot see -- ListComp/SetComp/DictComp/GeneratorExp
    nodes with their for clauses and filter ifs, plus IfExp conditional
    expressions.

    Motivation (openlibrary read_publisher): a comprehension rewrite that
    keeps the statement/block structure identical (for+if filter clause ->
    for + if/else expression) fingerprinted as no_flow_change under v2, so
    the agent's honest change_block_structure plan read as a MISMATCH and it
    learned to downgrade the plan to no_flow_change to satisfy the checker.
    The signature makes such rewrites observable. Sorted (name, count)
    tuples; () when the source does not parse."""
    from collections import Counter
    try:
        tree = ast.parse(func_src)
    except (SyntaxError, ValueError):
        return ()
    counts: Counter = Counter()
    for node in ast.walk(tree):
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp,
                             ast.GeneratorExp)):
            counts[type(node).__name__] += 1
            for gen in node.generators:
                counts["comp_for"] += 1
                if gen.ifs:
                    counts["comp_if"] += len(gen.ifs)
        elif isinstance(node, ast.IfExp):
            counts["IfExp"] += 1
    return tuple(sorted(counts.items()))


def cfg_fingerprint(func_src: str) -> Optional[tuple]:
    """(block_count, sorted edge multiset by rank, branch_edge_count).

    Under REPR_FLOW_V3=1 the comprehension signature is appended as a 4th
    element (see comprehension_signature); with the flag off the tuple is
    bit-identical to v2. Snapshot compatibility: a v3 flow_plan snapshot is
    only ever compared by a v3 flow_check in the same cell; mixed old/new
    shapes are caught by the v3 classifier ('unknown', never a crash)."""
    from collections import Counter
    from servers.cfg_server import engine as cfg_engine
    import tempfile
    tmp = Path(tempfile.gettempdir()) / "_flowedit_fn.py"
    tmp.write_text(func_src, encoding="utf-8")
    cfg_engine.clear_cache([tmp])
    try:
        with _quiet_stdout():
            cfgs = cfg_engine.file_cfgs(tmp, "_flowedit_fn.py")
    except (SyntaxError, ValueError, UnicodeDecodeError):
        return None
    if not cfgs:
        return None
    cfg = cfgs[0]

    def first_line(b):
        spans = b.get("spans") or []
        return spans[0][0] if spans else 10 ** 9
    order = sorted(cfg.blocks, key=lambda b: (first_line(b), b["id"]))
    rank = {b["id"]: i for i, b in enumerate(order)}
    edge_ms = Counter((e["kind"], rank.get(e["src"], -1), rank.get(e["dst"], -1))
                      for e in cfg.edges)
    branch = sum(1 for e in cfg.edges if e["kind"] in ("true", "false", "loop"))
    base = (len(cfg.blocks), tuple(sorted(edge_ms.items())), branch)
    if flow_v3():
        return base + (comprehension_signature(func_src),)
    return base


def dfg_fingerprint(func_src: str) -> Optional[tuple]:
    """(pair_count, frozenset of (var, def_ordinal, use_ordinal))."""
    from servers.dfg_server import engine as dfg_engine
    try:
        # _quiet_stdout: a function snippet references its module's names
        # unbound, and beniget REPORTS those by printing to stdout -- which
        # is the MCP stdio channel here (see _quiet_stdout).
        with _quiet_stdout():
            dfg = dfg_engine.ModuleDFG("_flowedit_fn.py", func_src)
    except (SyntaxError, ValueError):
        return None
    lines = sorted({ln for _v, d, u in dfg.pairs for ln in (d, u)})
    ordinal = {ln: i for i, ln in enumerate(lines)}
    skel = frozenset((var, ordinal.get(d, -1), ordinal.get(u, -1))
                     for var, d, u in dfg.pairs)
    return (len(skel), skel)


def fingerprint(rep: str, func_src: str) -> Optional[tuple]:
    return cfg_fingerprint(func_src) if rep == "cfg" else dfg_fingerprint(func_src)


# --- delta classification (matches Probe A's subtypes) ------------------------


def classify_cfg_delta(before: tuple, after: tuple) -> str:
    if flow_v3():
        return _classify_cfg_delta_v3(before, after)
    nb, _eb, brb = before
    na, _ea, bra = after
    if bra > brb:
        return "add_guard"          # a branch/guard was added
    if bra < brb:
        return "remove_branch"
    if na != nb:
        return "change_block_structure"
    if before != after:
        return "rewire_edge"        # same blocks/branches, edges moved
    return "no_flow_change"


def _classify_cfg_delta_v3(before: tuple, after: tuple) -> str:
    """v3 CFG delta classification (fix 3): the v2 block/branch/edge rules
    first; when those see NO change but the comprehension signature (4th
    fingerprint element) differs, the rewrite moved flow into/out of inline
    constructs the block CFG cannot see -> change_block_structure, not
    no_flow_change (which taught agents to downgrade honest plans).

    Mixed old/new shapes (a bare 3-element v2 fingerprint against the
    extended v3 4-element one, or malformed tuples) cannot be compared
    honestly: 'unknown' -- op_verdict maps it to verdict unknown with a
    note; never a crash, never a fake mismatch."""
    if not (isinstance(before, tuple) and isinstance(after, tuple)) \
            or len(before) != len(after) or len(before) < 3:
        return "unknown"
    nb, _eb, brb = before[:3]
    na, _ea, bra = after[:3]
    if bra > brb:
        return "add_guard"          # a branch/guard was added
    if bra < brb:
        return "remove_branch"
    if na != nb:
        return "change_block_structure"
    if before[:3] != after[:3]:
        return "rewire_edge"        # same blocks/branches, edges moved
    if len(before) > 3 and before[3:] != after[3:]:
        # block structure identical but the INLINE flow (comprehension /
        # conditional-expression) changed -- observable only via the v3
        # comprehension signature.
        return "change_block_structure"
    return "no_flow_change"


def fp_shape_mismatch(before, after) -> bool:
    """v3 guard (fix 3): True when two fingerprints cannot be compared
    because one is an old-shape tuple (e.g. a 3-element pre-v3 CFG
    fingerprint) and the other the extended v3 shape -- a repr comparison
    of mixed shapes would read as a spurious 'flow_changed'."""
    return (isinstance(before, tuple) and isinstance(after, tuple)
            and len(before) != len(after))


def classify_dfg_delta(before: tuple, after: tuple) -> str:
    """Classify a def-use delta ROBUSTLY to line drift.

    The fingerprint reduces line numbers to ordinals, so INSERTING one new
    pair shifts the ordinals of every pair below it -- a naive set diff then
    sees old pairs 'removed' and re-added and mislabels a plain addition as
    move_def_use (the false-mismatch source behind the 1/30 dfg plan-match
    telemetry). First compare per-variable PAIR COUNTS, which do not shift:
    counts only gained -> add; only lost -> remove. Only when the counts are
    identical but the ordinal structure differs is it a genuine relocation.
    """
    from collections import Counter
    _nb, sb = before
    _na, sa = after
    if sb == sa:
        return "no_flow_change"
    # REPR_FLOW_LEGACY_VERIFY=1: pre-fix ordinal set-diff (Haiku-v2 parity for
    # agent-facing tools); env unset = the drift-proof classifier (analysis).
    if os.environ.get("REPR_FLOW_LEGACY_VERIFY", "").strip() == "1":
        added, removed = sa - sb, sb - sa
        if added and not removed:
            return "add_def_use"
        if removed and not added:
            return "remove_def_use"
        return "move_def_use"
    cb = Counter(v for v, _d, _u in sb)
    ca = Counter(v for v, _d, _u in sa)
    gained = sum((ca - cb).values())
    lost = sum((cb - ca).values())
    if gained and not lost:
        return "add_def_use"
    if lost and not gained:
        return "remove_def_use"
    return "move_def_use"


def classify_c_cfg_delta(before: tuple, after: tuple) -> str:
    """Delta for the Joern C fingerprint ('c', (nblocks, edge_multiset)).

    Joern is statement-level (no basic-block branch tag), so we classify on the
    block/edge counts: more blocks => a branch/guard was added (the dominant
    C security-fix shape: an added bounds/null check); fewer => a branch was
    removed; same block count but different edges => a rewire.
    """
    _, (nb, eb) = before
    _, (na, ea) = after
    if na > nb:
        return "add_guard"
    if na < nb:
        return "remove_branch"
    if len(ea) != len(eb) or set(ea) != set(eb):
        return "rewire_edge"
    return "no_flow_change"


def classify_delta(rep: str, before: tuple, after: tuple) -> str:
    if before == after:
        return "no_flow_change"
    # C fingerprints are tagged ('c', (nb, edges)); dispatch to the C classifier.
    if (isinstance(before, tuple) and len(before) == 2 and before[0] == "c"
            and isinstance(after, tuple) and len(after) == 2
            and after[0] == "c"):
        return classify_c_cfg_delta(before, after)
    return (classify_cfg_delta if rep == "cfg" else classify_dfg_delta)(
        before, after)


# --- persisted plan state -----------------------------------------------------


def _plan_path() -> Path:
    run = os.environ.get("RUN_DIR", "").strip()
    base = Path(run) if run else env.repo_root() / ".perrep"
    return Path(base) / "flow_plan.json"


def load_plan() -> dict:
    try:
        return json.loads(_plan_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_plan(plan: dict) -> None:
    p = _plan_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(plan, indent=2), encoding="utf-8")


# --- v3 flow_locate state (RUN_DIR/flow_locate.json) --------------------------
#
# flow_locate is the flow3 protocol's SEPARATE step 2 (wrong-path
# localization): the agent declares WHERE the graph is wrong before planning.
# Records are an APPEND list; flow_plan inherits the MOST RECENT record as a
# delta's fault_site when the delta carries none. Same RUN_DIR-with-.perrep
# fallback as the plan state so both live next to each other.


def _locate_path() -> Path:
    run = os.environ.get("RUN_DIR", "").strip()
    base = Path(run) if run else env.repo_root() / ".perrep"
    return Path(base) / "flow_locate.json"


def load_locates() -> list:
    try:
        data = json.loads(_locate_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def save_locate(record: dict) -> None:
    records = load_locates()
    records.append(record)
    p = _locate_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(records, indent=2), encoding="utf-8")


def latest_locate():
    """The MOST RECENT flow_locate record (None when none recorded) -- the
    one flow_plan inherits a missing fault_site from."""
    records = load_locates()
    return records[-1] if records else None


def nearest_function_candidates(rel_file: str, line: int, k: int = 3) -> list:
    """The k indexed functions nearest to (rel_file, line) -- flow_locate's
    miss-path disambiguation aid. Each item: {function, line, code} where
    code is the first source line of the candidate's definition. [] when the
    file carries no indexed functions (or the index is unavailable)."""
    from servers.cfg_server import server as cfg_srv
    try:
        rows = cfg_srv._load_function_rows()
    except Exception:
        return []
    in_file = _rows_in_file(rows, rel_file)
    if not in_file:
        return []

    def dist(r):
        lo = int(r.get("line") or 0)
        hi = int(r.get("end_line") or lo)
        if lo <= int(line) <= hi:
            return 0
        return min(abs(int(line) - lo), abs(int(line) - hi))

    out = []
    for r in sorted(in_file, key=dist)[:k]:
        code = ""
        try:
            text = (env.repo_root() / r["file"]).read_text(
                encoding="utf-8", errors="replace").splitlines()
            ln = int(r.get("line") or 0)
            if 1 <= ln <= len(text):
                code = text[ln - 1].strip()
        except (OSError, ValueError, KeyError):
            code = ""
        out.append({"function": r.get("qualname") or r.get("name"),
                    "line": r.get("line"), "code": code})
    return out


def resolve_func_file(spec: str):
    """(abs_path, func_line) from 'file.py:line' or a function row lookup."""
    abs_path, line, _end = resolve_func_file_ranged(spec)
    return abs_path, line


def resolve_func_file_ranged(spec: str):
    """(abs_path, func_line, end_line) -- end_line drives the C line-extract.

    Returns (None, None, None) rather than raising when the spec does not
    resolve (``_resolve_functions`` raises ValueError on a miss)."""
    abs_path, line, end, _name = _resolve_row(spec)
    return abs_path, line, end


def _resolve_row(spec: str):
    """(abs_path, func_line, end_line, name) from the symbol index.

    ``name`` feeds _func_source's line-drift fallback: the indexed line is
    PRE-edit and goes stale as soon as the agent adds lines above the
    function; the name stays valid. (None, None, None, None) on a miss."""
    from servers.cfg_server import server as cfg_srv
    try:
        rows = cfg_srv._resolve_functions(spec)
    except (ValueError, KeyError):
        return None, None, None, None
    if not rows:
        return None, None, None, None
    row = rows[0]
    return (env.repo_root() / row["file"], row["line"], row.get("end_line"),
            row.get("name"))


# --- v3 file-scoped resolution (fix 2) ---------------------------------------
#
# The v2 resolver returns the GLOBAL first match, so a bare name like
# '_on_finished' can resolve to a same-named function in an unrelated file
# (notification.py instead of guiprocess.py) and flow_check then fingerprints
# the WRONG function -> false "planned add_guard / observed no_flow_change"
# MISMATCHes, which taught agents to re-plan the delta as no_flow_change for a
# vacuous green. v3 restricts resolution to the planned delta's ANCHOR file
# first, porting the map-tool _resolve_symbol discipline (file-scoped +
# Class.method + qualname-suffix + last-component).


def _anchor_file_line(anchor: str):
    """(rel_file, line) from a delta's 'file.py:LINE' anchor; a bare
    'file.py' still scopes the file (line None); (None, None) when the
    string is not file-shaped at all (e.g. a function name)."""
    a = str(anchor or "").strip().replace("\\", "/")
    if not a:
        return None, None
    f, sep, l = a.rpartition(":")
    if sep and f.strip() and l.strip().isdigit():
        return f.strip(), int(l.strip())
    if a.lower().endswith(_SRC_EXTS):
        return a, None
    return None, None


def _rows_in_file(rows: list, rel: str) -> list:
    """Rows whose indexed file matches ``rel`` (suffix-tolerant both ways --
    the plan's anchor path and the index path may differ in depth)."""
    rel = str(rel).replace("\\", "/")
    if rel.startswith("./"):
        rel = rel[2:]
    out = []
    for r in rows:
        rf = str(r.get("file") or "").replace("\\", "/")
        if rf and (rf == rel or rf.endswith("/" + rel)
                   or rel.endswith("/" + rf)):
            out.append(r)
    return out


def resolve_rows_scoped(spec: str, anchor: str = ""):
    """v3 function resolution (fix 2a): (candidate_rows, note).

    Candidates for the full spec (exact name/qualname + qualname-suffix, so
    Class.method works) plus a last-dotted-component retry, restricted to
    the delta's ANCHOR file FIRST; only when NOTHING matches inside that
    file does resolution fall back to the global pick -- and then the
    returned note says so. file:line specs keep the v2 resolution (already
    file-scoped). ([], None) on a total miss."""
    from servers.cfg_server import server as cfg_srv
    text = str(spec or "").strip()
    if not text:
        return [], None

    def _direct(q):
        try:
            return list(cfg_srv._resolve_functions(q) or [])
        except (ValueError, KeyError):
            return []

    if cfg_srv._parse_anchor(text) is not None:
        return _direct(text), None
    cands = _direct(text)
    parent = text.rsplit(".", 1)[0] if "." in text else ""
    last = text.rsplit(".", 1)[-1]
    lastc = _direct(last) if ("." in text and last and last != text) else []

    def _rank(rows, line=None):
        def key(r):
            q = str(r.get("qualname") or "")
            in_parent = 0 if (parent and parent in q) else 1
            if line is None:
                return (0, in_parent, 0)
            lo = int(r.get("line") or 0)
            hi = int(r.get("end_line") or lo)
            inside = 0 if lo <= line <= hi else 1
            return (inside, in_parent, abs(lo - line))
        return sorted(rows, key=key)

    anchor_rel, anchor_line = _anchor_file_line(anchor)
    if anchor_rel:
        for pool in (cands, lastc):
            in_file = _rows_in_file(pool, anchor_rel)
            if in_file:
                return _rank(in_file, anchor_line), None
        pool = cands or lastc
        if pool:
            ranked = _rank(pool)
            top = ranked[0]
            return ranked, (
                "no definition matching %r found in %s (the plan's anchor "
                "file); resolved globally to %s:%s -- if that is the wrong "
                "function, re-plan with a file:line or Class.method spec"
                % (text, anchor_rel, top.get("file"), top.get("line")))
        return [], None
    return (cands if cands else _rank(lastc)), None


def _resolve_row_scoped(spec: str, anchor: str):
    """v3 _resolve_row: the same (abs_path, line, end_line, name) tuple,
    resolved file-scoped by the delta's anchor first (resolve_rows_scoped);
    (None, None, None, None) on a miss."""
    rows, _note = resolve_rows_scoped(spec, anchor)
    if not rows:
        return None, None, None, None
    row = rows[0]
    return (env.repo_root() / row["file"], row["line"], row.get("end_line"),
            row.get("name"))


def scoped_resolution_note(spec: str, anchor: str):
    """v3: the global-fallback note for resolving ``spec`` under ``anchor``
    (None when resolution stayed inside the anchor's file, on any
    resolution error, or when v3 is off)."""
    if not flow_v3() or not str(anchor or "").strip():
        return None
    try:
        _rows, note = resolve_rows_scoped(spec, anchor)
    except Exception:
        return None
    return note


def multi_candidate_note(spec: str):
    """v3 flow_of disambiguation (fix 2b): when a name matches definitions
    in MORE THAN ONE file, name the pick being shown and list the other
    candidates as file:line so the agent can disambiguate with Class.method
    or file:line (v2 behavior: silent first pick). None when unambiguous,
    anchor-shaped, or unresolved."""
    from servers.cfg_server import server as cfg_srv
    text = str(spec or "").strip()
    if not text:
        return None
    try:
        if cfg_srv._parse_anchor(text) is not None:
            return None
        rows = cfg_srv._resolve_functions(text)
    except Exception:
        return None
    if not rows or len(rows) < 2:
        return None
    files = {str(r.get("file") or "") for r in rows}
    if len(files) < 2:
        return None
    shown = rows[0]
    others = ["%s:%s" % (r.get("file"), r.get("line")) for r in rows[1:9]]
    more = " (+%d more)" % (len(rows) - 9) if len(rows) > 9 else ""
    return ("%r matches definitions in multiple files; showing %s:%s. Other "
            "candidates: %s%s. If this is not the function you mean, call "
            "flow_of again with Class.method or a file:line anchor."
            % (text, shown.get("file"), shown.get("line"),
               ", ".join(others), more))


def class_method_specs(spec: str) -> list:
    """If ``spec`` names a CLASS, return its methods' 'file:line' anchors so the
    caller can render each method's CFG. Empty list if ``spec`` is not a class.

    The agent often names the CLASS it wants to fix (e.g. 'WikidataEntity');
    a class has no CFG of its own, but its methods do -- so flow_of expands a
    class into the flow of its methods rather than returning nothing.
    """
    from servers.cfg_server import server as cfg_srv
    name = str(spec).strip()
    if not name or ":" in name:
        return []
    try:
        rows = cfg_srv._load_function_rows()
    except Exception:
        return []
    # a method belongs to class <name> when its qualname ends with
    # '.<name>.<method>' (or is exactly '<name>.<method>').
    needle_mid = ".%s." % name
    needle_pre = "%s." % name
    out = []
    for r in rows:
        q = r.get("qualname", "")
        if needle_mid in q or q.startswith(needle_pre):
            # the segment right after the class name is the method
            after = q.split(needle_mid, 1)[-1] if needle_mid in q else \
                q[len(needle_pre):]
            # only direct methods (no further nested class/def in the path)
            if after and "." not in after:
                out.append("%s:%d" % (r["file"], r["line"]))
    return out


def module_function_specs(spec: str) -> list:
    """If ``spec`` names a MODULE (dotted path like 'galaxy.login' or a bare
    module name), return its functions' 'file:line' anchors. Empty list when
    no indexed file matches. Mirrors class_method_specs: agents routinely
    name the MODULE the fix lives in (2026-08-14 audit: 'galaxy.login' meant
    lib/ansible/galaxy/login.py, which existed -- the request just was not
    expandable). Top-level functions preferred; methods included only when a
    module has no top-level functions."""
    from servers.cfg_server import server as cfg_srv
    name = str(spec).strip()
    if not name or ":" in name or name.endswith(_SRC_EXTS):
        return []
    rel_suffix = "/" + name.replace(".", "/") + ".py"
    alt_suffix = name.replace(".", "/") + ".py"
    try:
        rows = cfg_srv._load_function_rows()
    except Exception:
        return []
    in_mod = [r for r in rows
              if str(r.get("file", "")).replace("\\", "/").endswith(rel_suffix)
              or str(r.get("file", "")).replace("\\", "/") == alt_suffix]
    if not in_mod:
        return []
    top = [r for r in in_mod if "." not in str(r.get("qualname", ""))]
    return ["%s:%d" % (r["file"], r["line"]) for r in (top or in_mod)]


def c_cfg_fingerprint(func_src: str, suffix: str = ".c") -> Optional[tuple]:
    """Joern-backed CFG fingerprint for a C/C++ function (see joern_cfg)."""
    from servers.common import joern_cfg
    with _quiet_stdout():
        return joern_cfg.c_cfg_fingerprint(func_src, suffix=suffix)


def func_fingerprint_now(rep: str, spec: str,
                         anchor: str = "") -> Optional[tuple]:
    from servers.common import joern_cfg
    # v3 (fix 2a): the planned delta's anchor scopes resolution to ITS file
    # first, so a same-named function elsewhere cannot hijack the
    # fingerprint. v2 callers pass no anchor (and the flow_v3 gate makes a
    # passed-through anchor inert) -> byte-identical v2 behavior.
    if anchor and flow_v3():
        abs_path, line, end, name = _resolve_row_scoped(spec, anchor)
    else:
        abs_path, line, end, name = _resolve_row(spec)
    if abs_path is None:
        return None
    # C/C++: BOTH reps fingerprint a snippet CPG built from the function's
    # CURRENT source (cfg: statement CFG; dfg: ossdataflow def-use pairs).
    # The per-instance ``joern --server`` CPG is built once PRE-edit and never
    # refreshed, so reading the post-edit fingerprint from it always returned
    # the pre-edit graph -- C dfg verification was vacuously "no_flow_change".
    # Snippet builds see whatever is on disk NOW, so before/after fingerprints
    # are honest on both sides.
    if joern_cfg.is_c_file(str(abs_path)):
        src = _func_source_ranged(Path(abs_path), line, end)
        if src is None:
            return None
        # preserve the file's extension so c2cpg uses the right (C vs C++)
        # frontend -- a C++ method mis-parsed as C yields a degenerate CFG.
        suffix = Path(str(abs_path)).suffix.lower() or ".c"
        if rep == "cfg":
            fp = c_cfg_fingerprint(src, suffix=suffix)
            # tag the fingerprint language so classify_delta dispatches right
            return ("c", fp) if fp is not None else None
        with _quiet_stdout():
            return joern_cfg.c_dfg_fingerprint(src, suffix=suffix)
    # thread the indexed NAME through so the extractor can survive line drift
    # (the spec itself is the last-resort name when the index row lacks one).
    fallback_name = name or spec.split(":", 1)[0]
    src = _func_source(Path(abs_path), line, name=fallback_name)
    if src is None:
        return None
    return fingerprint(rep, src)


# --- v3 (flow3) helpers: fault_site, HEAD snapshots, add_function ------------
#
# Everything below is used ONLY by the REPR_FLOW_V3=1 paths in flowedit_tools;
# nothing here is reachable from a v2 arm, so v2 behavior stays byte-identical.


def normalize_fault_site(raw):
    """Normalize a delta's OPTIONAL v3 ``fault_site`` field.

    Accepted forms: the string 'path/to/file.py:123' or an object
    {file, line, kind?, var?}. Returns (normalized_dict_or_None,
    error_or_None); (None, None) means the field was simply not given.
    A bad fault_site NEVER rejects the delta -- the caller only warns."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, None
    if isinstance(raw, str):
        f, sep, l = raw.strip().rpartition(":")
        if not sep or not f.strip():
            return None, "fault_site string must be 'path/to/file.py:LINE'"
        try:
            line = int(l)
        except ValueError:
            return None, "fault_site line %r is not an integer" % l
        return {"file": f.strip(), "line": line}, None
    if isinstance(raw, dict):
        f = str(raw.get("file", "") or "").strip()
        if not f:
            return None, "fault_site object needs a 'file'"
        try:
            line = int(raw.get("line"))
        except (TypeError, ValueError):
            return None, "fault_site object needs an integer 'line'"
        out = {"file": f, "line": line}
        for key in ("kind", "var"):
            if raw.get(key):
                out[key] = str(raw[key])
        return out, None
    return None, ("fault_site must be a 'file.py:LINE' string or an object "
                  "{file, line, kind?, var?}")


def fault_site_extent_check(site: dict, spec: str, anchor: str = ""):
    """Does the fault_site's line fall inside the planned function's extent?

    True/False, or None when the function (or its extent) cannot be resolved
    -- 'not checkable' never rejects the delta. The extent comes from the
    symbol index (end_line), with a Python ast fallback when the index row
    lacks one. v3 (fix 2a): resolution is scoped to the anchor's file first
    so the extent checked is the PLANNED function's, not a same-named one in
    another file (this helper is only called from v3 paths)."""
    if flow_v3() and anchor:
        abs_path, line, end, name = _resolve_row_scoped(spec, anchor)
    else:
        abs_path, line, end, name = _resolve_row(spec)
    if abs_path is None and anchor and anchor != spec:
        abs_path, line, end, name = _resolve_row(anchor)
    if abs_path is None or line is None:
        return None
    try:
        rel = str(Path(abs_path).relative_to(env.repo_root()))
    except (ValueError, OSError):
        rel = str(abs_path)
    rel = rel.replace("\\", "/")
    sf = str(site.get("file", "")).replace("\\", "/")
    if not sf or not (rel.endswith(sf) or sf.endswith(rel)):
        return False
    lo, hi = int(line), end
    if hi is None:
        from servers.common import joern_cfg
        if not joern_cfg.is_c_file(str(abs_path)):
            try:
                tree = ast.parse(Path(abs_path).read_text(
                    encoding="utf-8", errors="replace"))
            except (OSError, SyntaxError, ValueError):
                tree = None
            if tree is not None:
                short = str(name or spec).split(":", 1)[0].rsplit(".", 1)[-1]
                cands = [n for n in ast.walk(tree)
                         if isinstance(n, (ast.FunctionDef,
                                           ast.AsyncFunctionDef))
                         and (n.name == short or abs(n.lineno - lo) <= 3)]
                if cands:
                    best = min(cands, key=lambda n: abs(n.lineno - lo))
                    lo = best.lineno
                    hi = getattr(best, "end_lineno", None)
    if hi is None:
        return None
    return lo <= int(site["line"]) <= int(hi)


def fault_site_block(spec: str, line: int):
    """cfg note: the id of the basic block whose line range contains
    ``line``, when the engine exposes block line ranges. Best-effort -- a
    miss returns None and is never an error."""
    from servers.cfg_server import server as cfg_srv
    try:
        abs_path, _fline = resolve_func_file(spec)
        if abs_path is None:
            return None
        try:
            rel = str(Path(abs_path).relative_to(env.repo_root()))
        except (ValueError, OSError):
            rel = None
        fname = spec.split(":", 1)[0]
        if ":" in spec:
            rows = cfg_srv._resolve_functions(spec)
            if rows:
                fname = rows[0].get("name", fname)
        items = cfg_srv.get_cfg(fname, file=rel)
    except Exception:
        return None
    for it in items or []:
        for b in (it.get("blocks") or []):
            lr = b.get("line_range")
            try:
                if lr and lr[0] is not None and lr[0] <= int(line) <= lr[1]:
                    return b.get("id")
            except (TypeError, ValueError, IndexError):
                continue
    return None


def git_head_text(rel: str):
    """(text, 'ok') for HEAD:<rel> via ``git show`` in the repo root the
    tools already use; (None, 'file_absent') when the path does not exist at
    HEAD; (None, 'no_git') when the root is not a git checkout (v3 callers
    fall back to on-disk snapshots there)."""
    import subprocess
    root = env.repo_root()
    rel = str(rel).replace("\\", "/")
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--verify", "HEAD"],
            capture_output=True, text=True, timeout=60)
        if head.returncode != 0:
            return None, "no_git"
        show = subprocess.run(
            ["git", "-C", str(root), "show", "HEAD:%s" % rel],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None, "no_git"
    if show.returncode != 0:
        return None, "file_absent"
    return show.stdout, "ok"


def head_named_func_absent(func: str, anchor: str) -> bool:
    """v3 (fix C): POSITIVE determination that the planned function NAME
    does not exist at git HEAD.

    True only when ``func`` is a NAME (not a file[:line] spec), the symbol
    index cannot resolve it anywhere, AND the anchor file's HEAD text parses
    but carries no same-named definition. Every not-determinable case
    (file-shaped spec, no anchor file, file absent at HEAD, no git,
    unparseable HEAD text) returns False so the caller keeps its normal
    snapshot/report path. Used by _snapshot_head_v3 to refuse the anchor
    fallback that would silently fingerprint whatever NEIGHBOR def sits at
    the anchor line when the planned function does not exist yet."""
    from servers.common import joern_cfg
    spec = str(func or "").strip()
    if not spec:
        return False
    base = spec.split(":", 1)[0].replace("\\", "/")
    if base.lower().endswith(_SRC_EXTS):
        return False    # file-shaped spec: no NAME to check
    try:
        rows, _note = resolve_rows_scoped(spec, anchor)
    except Exception:
        rows = []
    if rows:
        # the index (built at cell start == HEAD) knows the name somewhere:
        # the normal snapshot path resolves it honestly.
        return False
    rel, _line = _anchor_file_line(anchor)
    if rel is None:
        return False
    text, _status = git_head_text(rel)
    if text is None:
        return False    # file absent at HEAD / no git: existing paths report
    is_c = joern_cfg.is_c_file(rel)
    if not is_c:
        try:
            ast.parse(text)
        except (SyntaxError, ValueError):
            return False    # cannot inspect HEAD text: not determinable
    return func_source_by_name(text, spec, is_c) is None


def _func_source_from_text(text: str, func_line: int,
                           name: Optional[str] = None) -> Optional[str]:
    """_func_source's Python extraction over ALREADY-READ text (the v3
    HEAD-snapshot path: text comes from ``git show HEAD:file``, not the
    working tree). Same +-3 line match with the by-name drift fallback; no
    legacy pin -- the v3 arm never sets REPR_FLOW_LEGACY_VERIFY."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if abs(node.lineno - func_line) <= 3:
                if best is None or abs(node.lineno - func_line) < abs(
                        best.lineno - func_line):
                    best = node
    if name and flow_v3():
        # v3 (fix C): NAME-FIRST -- see _func_source. A same-named def
        # anywhere in the text beats a differently-named proximity pick;
        # the +-3 window is only trusted when no def carries the name.
        short = str(name).rsplit(".", 1)[-1]
        cands = [n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name == short]
        if cands and (best is None or best.name != short):
            best = min(cands, key=lambda n: abs(n.lineno - func_line))
    elif best is None and name:
        short = str(name).rsplit(".", 1)[-1]
        cands = [n for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and n.name == short]
        if cands:
            best = min(cands, key=lambda n: abs(n.lineno - func_line))
    if best is None:
        return None
    seg = ast.get_source_segment(text, best)
    return _dedent(seg) if seg else None


def _func_source_ranged_from_text(text: str, start_line: int,
                                  end_line: Optional[int]) -> Optional[str]:
    """_func_source_ranged's C/C++ line-slice + brace-balance extraction over
    already-read text (the v3 HEAD-snapshot path)."""
    lines = text.splitlines()
    if start_line < 1 or start_line > len(lines):
        return None
    if end_line and end_line >= start_line:
        seg = lines[start_line - 1:end_line]
    else:
        window = lines[start_line - 1:start_line - 1 + 400]
        seg, depth, started = [], 0, False
        for ln in window:
            seg.append(ln)
            depth += ln.count("{") - ln.count("}")
            if "{" in ln:
                started = True
            if started and depth <= 0:
                break
    src = "\n".join(seg).strip()
    return (src + "\n") if src else None


def func_fingerprint_head(rep: str, spec: str, anchor: str = ""):
    """(fingerprint, status) of the function AS OF GIT HEAD.

    v3 snapshots use this instead of func_fingerprint_now so 'pre-edit' is
    well-defined under EVERY ordering: planning after an edit (or re-planning
    after partial edits) can no longer re-baseline the snapshot to the
    already-edited source. status: 'ok' | 'file_absent' | 'func_absent' |
    'unresolved' | 'no_git'. ``anchor`` scopes resolution to the anchor's
    file first (fix 2a) -- this is a v3-only entry point, but the gate is
    kept anyway for symmetry with func_fingerprint_now."""
    from servers.common import joern_cfg
    if anchor and flow_v3():
        abs_path, line, end, name = _resolve_row_scoped(spec, anchor)
    else:
        abs_path, line, end, name = _resolve_row(spec)
    if abs_path is None:
        return None, "unresolved"
    try:
        rel = str(Path(abs_path).relative_to(env.repo_root()))
    except (ValueError, OSError):
        return None, "unresolved"
    text, status = git_head_text(rel)
    if text is None:
        return None, status
    if joern_cfg.is_c_file(str(abs_path)):
        src = _func_source_ranged_from_text(text, line, end)
        if src is None:
            return None, "func_absent"
        suffix = Path(str(abs_path)).suffix.lower() or ".c"
        if rep == "cfg":
            fp = c_cfg_fingerprint(src, suffix=suffix)
            return (("c", fp), "ok") if fp is not None else (None,
                                                             "func_absent")
        with _quiet_stdout():
            fp = joern_cfg.c_dfg_fingerprint(src, suffix=suffix)
        return (fp, "ok") if fp is not None else (None, "func_absent")
    fallback_name = name or spec.split(":", 1)[0]
    src = _func_source_from_text(text, line, name=fallback_name)
    if src is None:
        return None, "func_absent"
    fp = fingerprint(rep, src)
    return (fp, "ok") if fp is not None else (None, "func_absent")


def py_func_source_by_name(text: str, name: str) -> Optional[str]:
    """Index-BYPASSING Python extraction (add_function verification): the
    symbol index cannot know a function the agent just created, so we walk
    the given text by NAME.

    Name-collision fix (2026-08-13): a QUALIFIED name is honored instead of
    being stripped to its last component. The bare-name walk made every
    ``__init__`` in a file interchangeable, which produced the manual study's
    only verifier false-mismatch ("__init__ already exists at HEAD" for a
    brand-new class's __init__, because LEGACY classes' __init__ matched) and
    its knock-on false pass. Rules:
      - bare name: first ``ast.walk`` match (byte-identical to the old
        behavior);
      - 'Class.method' where the class chain exists in this text: the def
        whose enclosing class chain ends with the qualifier;
      - qualifier looks like a CLASS (CapWords first letter) but no such
        enclosure exists in this text: None -- the scoped function genuinely
        is not here (e.g. the class is brand new at verify time / absent at
        HEAD at plan time);
      - qualifier looks like a MODULE path (lowercase): fall back to the
        bare-name walk (module paths cannot be checked against file text).
    None when the text does not parse or nothing matches."""
    parts = [p for p in str(name).strip().split(".") if p.strip()]
    if not parts:
        return None
    short, qual = parts[-1], parts[:-1]
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None

    def _seg(node):
        seg = ast.get_source_segment(text, node)
        return _dedent(seg) if seg else None

    def _bare():
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name == short:
                return _seg(node)
        return None

    if not qual:
        return _bare()
    cands = []          # (class_chain, node) in source order

    def _walk(node, chain):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if ch.name == short:
                    cands.append((chain, ch))
                _walk(ch, chain)      # nested defs keep the class chain
            elif isinstance(ch, ast.ClassDef):
                _walk(ch, chain + [ch.name])
            else:
                _walk(ch, chain)

    _walk(tree, [])
    for chain, node in cands:
        if len(chain) >= len(qual) and chain[-len(qual):] == qual:
            return _seg(node)
    if qual[-1][:1].isupper():
        return None      # class-scoped, and that class scope is not here
    return _bare()       # module-path qualifier: bare-name behavior


def c_func_source_by_name(text: str, name: str) -> Optional[str]:
    """Index-BYPASSING C/C++ extraction (add_function verification): brace-
    balance from the first line that LOOKS like a definition of ``name``
    (optional type tokens, then the name, then '(' -- with a '{' before any
    ';', since a ';' first means declaration or call statement). Heuristic
    by design; returns None rather than guessing when nothing matches."""
    import re
    raw = str(name).rsplit(".", 1)[-1].strip()
    short = raw.split("::")[-1].strip()
    if not short:
        return None
    # C++ analog of the Python name-collision fix (2026-08-14): when the name
    # carries a Class:: qualifier, try the QUALIFIED out-of-class definition
    # form first ('void Class::method(...)') so a same-named method of another
    # class cannot shadow the target; fall back to the bare short name.
    pats = []
    if "::" in raw:
        cls = raw.rsplit("::", 1)[0].split("::")[-1].strip()
        if cls:
            pats.append(re.compile(
                r"^\s*(?:[A-Za-z_][\w\s\*&:<>,]*[\s\*&])?%s\s*::\s*%s\s*\("
                % (re.escape(cls), re.escape(short))))
    pats.append(re.compile(
        r"^\s*(?:[A-Za-z_][\w\s\*&:<>,]*[\s\*&])?%s\s*\(" % re.escape(short)))
    lines = text.splitlines()
    for pat in pats:
        hit = _c_extract_at(lines, pat)
        if hit is not None:
            return hit
    return None


def _c_extract_at(lines: list, pat) -> Optional[str]:
    """Brace-balance extraction from the first line matching ``pat``."""
    for i, ln in enumerate(lines):
        stripped = ln.lstrip()
        if stripped.startswith(("//", "/*", "*", "#")):
            continue
        if not pat.match(ln):
            continue
        window = lines[i:i + 400]
        blob = "\n".join(window)
        brace, semi = blob.find("{"), blob.find(";")
        if brace == -1 or (semi != -1 and semi < brace):
            continue    # declaration or a statement calling the name
        seg, depth, started = [], 0, False
        for w in window:
            seg.append(w)
            depth += w.count("{") - w.count("}")
            if "{" in w:
                started = True
            if started and depth <= 0:
                break
        src = "\n".join(seg).strip()
        if src:
            return src + "\n"
    return None


def func_source_by_name(text: str, name: str, is_c: bool) -> Optional[str]:
    """Language-dispatching index-bypassing extraction (add_function)."""
    return (c_func_source_by_name if is_c else py_func_source_by_name)(
        text, name)


# --- final verification: the FINAL diff vs the committed plan ----------------
#
# flow_check verifies ONE function right after ONE edit; nothing re-checks the
# FINAL state of the code against the WHOLE plan (a later edit can undo an
# earlier verified change, or add flow the plan never mentioned). flow_verify
# closes that gap: it re-verifies EVERY planned delta against the current
# (final) source, and lists source edits that fall OUTSIDE the plan -- found by
# diffing the worktree against git HEAD (all three benchmarks run the agent in
# a git checkout whose HEAD is the pre-edit base).

_SRC_EXTS = (".py", ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp")
_TOOL_DIRS = (".perrep/", ".repr_index/", ".codegraph/")


def _is_source_rel(rel: str) -> bool:
    r = rel.replace("\\", "/")
    if any(r.startswith(d) or ("/" + d) in ("/" + r) for d in _TOOL_DIRS):
        return False
    return r.lower().endswith(_SRC_EXTS)


def _parse_git_diff() -> tuple:
    """(post, deleted) line ranges of the agent's edits vs git HEAD.

    post    = {rel: [(start, end), ...]}  POST-edit lines that were added or
              modified (plus untracked new source files, whole-file).
    deleted = {rel: [(start, end), ...]}  PRE-edit lines of PURE deletions
              (post-side span 0, incl. whole-file deletions) -- these lines
              no longer exist in the working tree, so they must be attributed
              against the HEAD version of the file.
    ({}, {}) when the repo root is not a git checkout. File headers are only
    honored OUTSIDE hunks so an added content line like '+++ x' cannot be
    misparsed as a header; renames are disabled so a moved file shows as
    delete+add rather than vanishing from the diff.
    """
    import subprocess
    root = env.repo_root()
    try:
        diff = subprocess.run(
            ["git", "-C", str(root), "-c", "diff.mnemonicPrefix=false",
             "-c", "diff.noprefix=false", "-c", "core.quotePath=false",
             "diff", "HEAD", "--unified=0", "--no-color", "--no-ext-diff",
             "--no-renames"],
            capture_output=True, text=True, timeout=60)
        untracked = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--others",
             "--exclude-standard"],
            capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return {}, {}
    if diff.returncode != 0:
        return {}, {}
    post: dict = {}
    deleted: dict = {}
    old = new = None
    in_hunk = False
    for line in (diff.stdout or "").splitlines():
        if line.startswith("diff --git "):
            old = new = None
            in_hunk = False
        elif line.startswith("--- ") and not in_hunk:
            p = line[4:].strip()
            p = p[2:] if p.startswith("a/") else (None if p == "/dev/null"
                                                  else p)
            old = p if (p and _is_source_rel(p)) else None
        elif line.startswith("+++ ") and not in_hunk:
            p = line[4:].strip()
            p = p[2:] if p.startswith("b/") else (None if p == "/dev/null"
                                                  else p)
            new = p if (p and _is_source_rel(p)) else None
        elif line.startswith("@@ "):
            in_hunk = True
            try:
                mid = line.split("@@", 2)[1].strip()      # "-a,b +c,d"
                minus, plus = mid.split(" ", 1)
                a, _, b = minus.lstrip("-").partition(",")
                c, _, d = plus.lstrip("+").partition(",")
                pre_start = max(int(a), 1)
                pre_span = int(b) if b else 1
                post_start = max(int(c), 1)
                post_span = int(d) if d else 1
            except (IndexError, ValueError):
                continue
            if post_span > 0 and new is not None:
                post.setdefault(new, []).append(
                    (post_start, post_start + post_span - 1))
            elif post_span == 0 and pre_span > 0 and old is not None:
                deleted.setdefault(old, []).append(
                    (pre_start, pre_start + pre_span - 1))
    for rel in (untracked.stdout or "").splitlines():
        rel = rel.strip()
        if rel and _is_source_rel(rel):
            try:
                n = len((Path(root) / rel).read_text(
                    encoding="utf-8", errors="replace").splitlines())
            except OSError:
                n = 1
            post.setdefault(rel, []).append((1, max(n, 1)))
    return post, deleted


def changed_line_ranges() -> dict:
    """Back-compat view: POST-edit changed source ranges only (see
    _parse_git_diff for the full post+deleted picture)."""
    return _parse_git_diff()[0]


def _py_defs(text: str) -> list:
    """[(name, start_line, end_line)] for every def in ``text`` ([] when the
    source does not parse -- the edit is still reported, just without a
    function attribution)."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    return [(n.name, n.lineno, getattr(n, "end_lineno", n.lineno))
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _index_defs(rel: str) -> list:
    """Symbol-index [(name, line, end_line)] for one file (C/C++ path; index
    lines are PRE-edit, exact for deletions, approximate post-edit)."""
    from servers.cfg_server import server as cfg_srv
    try:
        rows = cfg_srv._load_function_rows()
    except Exception:
        return []
    out = []
    for r in rows:
        if r.get("file") != rel:
            continue
        lo = r.get("line") or 0
        out.append((r.get("name", "?"), lo, r.get("end_line") or lo))
    return out


def _defs_overlapping(defs: list, a: int, b: int) -> list:
    """Defs overlapping [a, b] -- ALL overlapping siblings (a range spanning
    two functions reports both), but innermost-only among nested defs (an
    edit inside a method is not also attributed to its enclosing def)."""
    hit = [(n, lo, hi) for (n, lo, hi) in defs if a <= hi and lo <= b]
    return [(n, lo, hi) for (n, lo, hi) in hit
            if not any(lo2 >= lo and hi2 <= hi and (lo2, hi2) != (lo, hi)
                       for (_n2, lo2, hi2) in hit)]


def _delta_matches_function(delta: dict, name: str, rel: str,
                            lo: int, hi: int) -> bool:
    """Does a planned delta cover the edited function (name, rel:[lo,hi])?

    Matches by function name (same suffix rule flow_check uses), or by the
    delta's function/anchor given as file:line with the line falling INSIDE
    the edited function's span (small slack for the edit's line drift)."""
    fn = str(delta.get("function", "")).strip()
    anchor = str(delta.get("anchor", "")).strip()
    if fn:
        base = fn.split(":", 1)[0]
        last = base.rsplit(".", 1)[-1]
        if name == base or name == last or base.endswith("." + name):
            return True
    for spec in (fn, anchor):
        if ":" not in spec:
            continue
        f, _, l = spec.partition(":")
        if f.replace("\\", "/").endswith(rel.replace("\\", "/")) or \
                rel.replace("\\", "/").endswith(f.replace("\\", "/")):
            try:
                if lo - 3 <= int(l.split(":")[0]) <= hi + 3:
                    return True
            except ValueError:
                continue    # malformed line spec: never a whole-file wildcard
    return False


def unplanned_edits(plan: dict, max_items: int = 20) -> list:
    """Source edits (vs git HEAD) in functions the plan never mentioned.

    Each item: {file, lines, function} -- function is None for module-level /
    new-file / unattributable edits (every changed range is accounted for:
    a range no def covers is reported at file level, per range, not dropped).
    PURE deletions are attributed against the PRE-edit (HEAD) side -- the
    deleted lines no longer exist in the working tree, so deleting a whole
    function is attributed to THAT function, not its surviving neighbor.
    Planned functions (by name or anchor) are filtered out; everything else
    is scope the agent changed without a committed delta."""
    import subprocess
    from servers.common import joern_cfg
    deltas = [d for d in (plan or {}).get("deltas", []) if isinstance(d, dict)]
    root = env.repo_root()
    post, deleted = _parse_git_diff()
    out = []

    def emit(rel, label, funcs):
        if not funcs:
            out.append({"file": rel, "lines": label, "function": None})
            return
        for name, lo, hi in funcs:
            if any(_delta_matches_function(d, name, rel, lo, hi)
                   for d in deltas):
                continue
            out.append({"file": rel, "lines": label, "function": name})

    for rel, ranges in sorted(post.items()):
        abs_path = Path(root) / rel
        is_c = joern_cfg.is_c_file(str(abs_path))
        if is_c:
            defs = _index_defs(rel)
        else:
            try:
                defs = _py_defs(abs_path.read_text(encoding="utf-8",
                                                   errors="replace"))
            except OSError:
                defs = []
        slack = 3 if is_c else 0   # index lines are pre-edit -> small drift
        for (a, b) in ranges:
            emit(rel, "L%d-L%d" % (a, b),
                 _defs_overlapping(defs, a - slack, b + slack))

    for rel, ranges in sorted(deleted.items()):
        if joern_cfg.is_c_file(rel):
            defs = _index_defs(rel)          # pre-edit lines: exact here
        else:
            try:
                show = subprocess.run(
                    ["git", "-C", str(root), "show", "HEAD:%s" % rel],
                    capture_output=True, text=True, timeout=60)
                defs = _py_defs(show.stdout or "") \
                    if show.returncode == 0 else []
            except (OSError, subprocess.SubprocessError):
                defs = []
        for (a, b) in ranges:
            emit(rel, "deleted L%d-L%d" % (a, b),
                 _defs_overlapping(defs, a, b))

    seen = set()
    dedup = []
    for it in out:
        key = (it["file"], it["function"], it["lines"])
        if key not in seen:
            seen.add(key)
            dedup.append(it)
    return dedup[:max_items]


# --- block-level CFG rendering (the "stronger" arm) --------------------------
#
# The perrep cfg MAP is a FUNCTION-level map (nodes=defs, edges=contains) whose
# node docs carry a flow SUMMARY for search ranking -- the agent never SEES the
# real block-level control-flow graph. render_block_cfg turns get_cfg's actual
# blocks+edges into a compact boxes-and-arrows text so the agent can read the
# control-flow STRUCTURE of the function it is about to edit, not just its
# source. Surfaced by cfg_view() and appended to cfg_node when REPR_CFG_BLOCKS=1.

_EDGE_GLYPH = {"true": "--T-->", "false": "--F-->", "loop": "=loop=>",
               "next": "----->"}


def render_block_cfg(item: dict, max_blocks: int = 40) -> str:
    """A compact, readable control-flow graph for ONE get_cfg() item.

    Lists each block (id: first line of code, with its source line no.) then the
    edges as ``src --T--> dst`` so branch/loop structure is explicit. Kept small
    (block count capped) so it is cheap to embed next to the function source.
    """
    blocks = item.get("blocks") or []
    edges = item.get("edges") or []
    if not blocks:
        note = item.get("note")
        return "(no control-flow graph available%s)" % (
            ": " + note if note else "")
    fn = item.get("function", "?")
    entry, exit_ = item.get("entry"), item.get("exit")
    lines = ["control-flow graph of %s()  [%d blocks, %d edges, engine=%s]"
             % (fn, len(blocks), len(edges), item.get("engine", "?"))]
    # block table
    lines.append("blocks:")
    for b in blocks[:max_blocks]:
        bid = b.get("id")
        code = (b.get("first_line_code") or "").strip()
        lr = b.get("line_range")
        loc = ("L%d" % lr[0]) if lr else ""
        tag = ""
        if bid == entry:
            tag = " [entry]"
        elif bid == exit_:
            tag = " [exit]"
        lines.append("  %2s %-5s %s%s" % (bid, loc, code[:70], tag))
    if len(blocks) > max_blocks:
        lines.append("  ... (%d more blocks)" % (len(blocks) - max_blocks))
    # edges, grouped so branches stand out
    lines.append("edges:")
    for e in edges:
        g = _EDGE_GLYPH.get(e.get("kind"), "--%s-->" % e.get("kind"))
        lines.append("  %s %s %s" % (e.get("src"), g, e.get("dst")))
    return "\n".join(lines)


def render_def_use(func_name: str, pairs: list, code_at, engine_name: str,
                   max_pairs: int = 60) -> str:
    """A compact, readable value-flow (def-use) view for ONE function.

    ``pairs`` are (var, def_line, use_line) in REAL file line numbers;
    ``code_at(line)`` returns the (trimmed) source line. Pairs are grouped by
    (var, def_line) so each definition reads as one row with all the places
    its value flows to -- the data-flow analog of block_cfg_text's
    boxes-and-arrows.
    """
    if not pairs:
        return ("(no def-use pairs found in %s() -- the function may not "
                "define/use local values, or parsing failed)" % func_name)
    by_def: dict = {}
    for var, def_line, use_line in sorted(pairs)[:max_pairs]:
        by_def.setdefault((var, def_line), []).append(use_line)
    # header counts come from the FULL pair set, not the displayed slice --
    # counting the truncated grouping undercounted defs whenever the
    # max_pairs cap elided rows (2026-08-14 audit: "13 defs" vs 14 actual)
    n_defs_full = len({(v, d) for v, d, _u in pairs})
    n_vars = len({v for v, _d, _u in pairs})
    lines = [
        "value-flow (def-use) graph of %s()  [%d vars, %d defs, %d def->use "
        "pairs, engine=%s]" % (func_name, n_vars, n_defs_full, len(pairs),
                               engine_name),
        "each row: a definition and every place its value is used:",
    ]
    for (var, def_line), use_lines in sorted(by_def.items(),
                                             key=lambda kv: kv[0][1]):
        code = (code_at(def_line) or "").strip()
        uses = ", ".join("L%d" % u for u in sorted(set(use_lines)))
        lines.append("  %-14s def L%-5d %-48s -> uses %s"
                     % (var, def_line, code[:48], uses))
    if len(pairs) > max_pairs:
        lines.append("  ... (%d more pairs)" % (len(pairs) - max_pairs))
    return "\n".join(lines)


def _c_dfg_flow_text(abs_path: Path, line: int, spec: str,
                     max_pairs: int) -> Optional[str]:
    """C/C++ value-flow view via the live Joern CPG's data-dependence graph.

    Reuses servers.dfg_server's def-use query (ddgIn over the server-side CPG,
    the same edges its def_use tool serves) -- so a SecBench flow-first cell,
    which already boots a per-instance ``joern --server``, gets a real
    function-scoped def-use graph with NO extra CPG build. Returns None when
    no Joern server is reachable (caller falls back to a note); an empty
    result renders a clear note.
    """
    from servers.common.joern import JoernClient, JoernUnavailable
    from servers.dfg_server import server as dfg_srv
    client = JoernClient()
    if not client.available():
        return None
    try:
        rel = str(Path(abs_path).relative_to(env.repo_root()))
    except (ValueError, OSError):
        rel = str(abs_path)
    # resolve the function name at the spec's line (spec may be file:line)
    fname = spec.split(":", 1)[0].rsplit(".", 1)[-1]
    if ":" in spec:
        from servers.cfg_server import server as cfg_srv
        rows = cfg_srv._resolve_functions(spec)
        if rows:
            fname = rows[0].get("name", fname)
    try:
        items = dfg_srv._joern_def_use_items(client, rel, fname, None)
    except JoernUnavailable:
        return None
    # collapse the def-use / use-def item pairs to (var, def_line, use_line)
    pairs = []
    for it in items:
        if it.get("kind") != "def-use":
            continue
        var = it.get("var", "")
        try:
            def_line = int(str(it["from"]).split(":")[1])
            use_line = int(str(it["to"]).split(":")[1])
        except (KeyError, IndexError, ValueError):
            continue
        pairs.append((var, def_line, use_line))
    src_lines = None
    try:
        src_lines = Path(abs_path).read_text(
            encoding="utf-8", errors="replace").splitlines()
    except OSError:
        src_lines = []

    def code_at(real_line: int) -> str:
        if src_lines and 1 <= real_line <= len(src_lines):
            return src_lines[real_line - 1]
        return ""

    return render_def_use(fname, pairs, code_at, "joern",
                          max_pairs=max_pairs)


def dfg_flow_text(spec: str, max_pairs: int = 60) -> Optional[str]:
    """render_def_use for the function named by ``spec`` -- the dfg analog of
    block_cfg_text (flow_of's rendering path when REPR_PERREP_REP=dfg).

    Python: beniget over the extracted function source. C/C++: the live Joern
    CPG's data-dependence graph (SecBench boots a per-instance joern server).
    Returns None only when the function cannot be resolved (or C with no
    reachable Joern); parse/no-pairs cases render an explanatory note so the
    agent is never left guessing.
    """
    from servers.common import joern_cfg
    abs_path, line = resolve_func_file(spec)
    if abs_path is None:
        return None
    if joern_cfg.is_c_file(str(abs_path)):
        text = _c_dfg_flow_text(Path(abs_path), line, spec, max_pairs)
        if text is not None:
            return text
        return ("(no value-flow graph for %r: this is C/C++ and no Joern "
                "data-dependence graph is available for it)" % spec)
    src = _func_source(Path(abs_path), line)
    if src is None:
        return ("(could not extract %r as a Python function for a value-flow "
                "graph)" % spec)
    from servers.dfg_server import engine as dfg_engine
    try:
        dfg = dfg_engine.ModuleDFG("_flowedit_fn.py", src)
    except Exception:
        return ("(value-flow analysis failed to parse %r -- is it a plain "
                "Python function?)" % spec)
    offset = int(line) - 1
    pairs = [(var, d + offset, u + offset) for var, d, u in dfg.pairs]
    # display name: for anchor specs ('path/file.py:123') the old
    # basename-split produced "graph of py()" -- use the resolved row's real
    # function name instead (2026-08-14 audit, cosmetic header mislabel)
    name = spec.split(":", 1)[0].rsplit(".", 1)[-1]
    if ":" in spec:
        _abs, _l, _e, row_name = _resolve_row(spec)
        if row_name:
            name = str(row_name).rsplit(".", 1)[-1]

    src_lines = src.splitlines()

    def code_at(real_line: int) -> str:
        idx = real_line - offset - 1
        if 0 <= idx < len(src_lines):
            return src_lines[idx]
        return ""

    return render_def_use(name, pairs, code_at, "beniget",
                          max_pairs=max_pairs)


def block_cfg_text(spec: str, max_blocks: int = 40) -> Optional[str]:
    """render_block_cfg for the function named by ``spec`` (name or file:line).

    Returns None only when the function cannot be resolved at all; an empty
    graph still renders a clear note so the agent is never left guessing.
    """
    from servers.cfg_server import server as cfg_srv
    abs_path, line = resolve_func_file(spec)
    if abs_path is None:
        return None
    # get_cfg matches by name (+optional file); pass the resolved file so an
    # overloaded name in another file does not shadow the target.
    rel = None
    try:
        rel = str(Path(abs_path).relative_to(env.repo_root()))
    except (ValueError, OSError):
        rel = None
    fname = spec.split(":", 1)[0]
    # if spec is a file:line, resolve the actual function name at that line
    if ":" in spec:
        rows = cfg_srv._resolve_functions(spec)
        if rows:
            if flow_v3():
                # v3 name-collision fix (2026-08-13): re-query by QUALNAME,
                # not bare name. 'file.py:131' resolving to
                # TaskQueueManager.__init__ used to re-query get_cfg with
                # '__init__', which matched every __init__ in the file and
                # the [:3] render cap then dropped the real target entirely
                # (the manual study's one WRONG graph). v2 keeps the old
                # behavior byte-identical.
                fname = (rows[0].get("qualname")
                         or rows[0].get("name", fname))
            else:
                fname = rows[0].get("name", fname)
    items = cfg_srv.get_cfg(fname, file=rel)
    if not items:
        return "(function %r not found for a control-flow graph)" % fname
    return "\n\n".join(render_block_cfg(it, max_blocks=max_blocks)
                       for it in items[:3])
