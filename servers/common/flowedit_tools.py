"""The flow_plan / flow_check MCP tool implementations (graph-mediated editing).

Registered on the perrep server for the cfg/dfg reps when REPR_FLOWEDIT=1.
The rep (cfg|dfg) is read from REPR_PERREP_REP so the same two tools serve both
the control-flow and data-flow variants of the mechanism.
"""

import os

from servers.common import flowedit


def _rep() -> str:
    r = os.environ.get("REPR_PERREP_REP", "cfg").strip()
    return r if r in ("dfg", "cfgdfg") else "cfg"


def _testgen() -> bool:
    """TEST-GENERATION mode (SWT): the deliverable is a TEST, not a fix.
    flow_plan records the delta a correct FIX would make (the contract the
    test asserts the absence of); flow_check/flow_verify then verify the
    SOURCE flow is UNCHANGED (fixing the bug would defeat reproduction) and
    that the diff touches ONLY test files."""
    return os.environ.get("REPR_FLOW_TESTGEN", "").strip() == "1"


FLOW_PLAN_DESCRIPTION = (
    "Commit to the GRAPH-LEVEL change you intend BEFORE editing code. Given the "
    "bug report and the function you will fix, state the delta the fix makes to "
    "the control-/data-flow graph as a list of operations, each "
    "{op, function, anchor, rationale}. 'function' is the exact function name "
    "(or file:line); 'anchor' is the file:line the change centers on; "
    "'rationale' is one sentence tying it to the bug. Valid ops (control flow): "
    "add_guard (add an if/early-return/raise dominating a line -- the most "
    "common fix), remove_branch, change_condition, change_block_structure, "
    "rewire_edge. Data flow: add_def_use, remove_def_use, move_def_use. If the "
    "fix does NOT change flow (pure expression/constant/call-target/signature "
    "edit), use op 'no_flow_change' rather than inventing a delta. This tool "
    "RECORDS your plan and snapshots each named function's current graph so "
    "flow_check can later verify your edit realized this plan. Call it ONCE, "
    "before editing. Example: flow_plan(deltas=[{'op':'add_guard', "
    "'function':'wrap_var', 'anchor':'utils/vars.py:88', 'rationale':'guard "
    "None before merge'}])."
)

FLOW_CHECK_DESCRIPTION = (
    "After editing, verify your code change realized the graph plan you "
    "committed in flow_plan. Pass the function you edited (name or file:line). "
    "This recomputes that function's control-/data-flow graph from your EDITED "
    "source, diffs it against the pre-edit snapshot flow_plan took, classifies "
    "the ACTUAL delta, and reports whether it matches the op you planned "
    "(match=true/false), plus the observed delta. A mismatch means your edit "
    "did not make the graph change you intended -- reconsider the edit. "
    "'no_flow_change' planned AND observed also matches. Example: "
    "flow_check(function='wrap_var')."
)


# --- TEST-GENERATION (SWT) mode descriptions ---------------------------------
#
# Same three tools, test-gen semantics: the plan is the delta a correct FIX
# would make (what the test asserts is MISSING), and verification proves the
# agent did NOT alter the source flow and delivered only test files.

TG_FLOW_PLAN_DESCRIPTION = (
    "REQUIRED before writing your test. Record the flow delta a correct FIX "
    "would make to the buggy function, as a list of {op, function, anchor, "
    "rationale} (control flow: add_guard, remove_branch, change_condition, "
    "change_block_structure, rewire_edge; data flow: add_def_use, "
    "remove_def_use, move_def_use; or no_flow_change). This is the CONTRACT "
    "your test checks: the test must FAIL on the current code BECAUSE that "
    "delta is absent, and PASS once a fix realizes it. Design the test to "
    "drive execution through exactly that flow locus. This also snapshots "
    "the source function's current graph so flow_check/flow_verify can "
    "confirm you did not alter the source. Call it ONCE, before writing the "
    "test. Example: flow_plan(deltas=[{'op':'add_guard', "
    "'function':'wrap_var', 'anchor':'utils/vars.py:88', 'rationale':'a fix "
    "would guard None before merge; my test passes None and expects the "
    "guarded behavior'}])."
)

TG_FLOW_CHECK_DESCRIPTION = (
    "Confirm the SOURCE function's flow graph is UNCHANGED (pass the "
    "function name). You must NOT fix or alter the bug -- your test has to "
    "FAIL on the CURRENT code; a 'fixed' source would make it pass and "
    "defeat reproduction. match=true means the source flow is untouched, as "
    "required. match=false means your edits changed the source function's "
    "flow -- revert them; deliver ONLY a test. Example: "
    "flow_check(function='wrap_var')."
)

TG_FLOW_VERIFY_DESCRIPTION = (
    "FINAL STEP -- call ONCE, with no arguments, when your test is written "
    "and BEFORE you stop. Verifies the whole deliverable: (1) EVERY planned "
    "source function's flow graph is UNCHANGED (you did not fix/alter the "
    "bug -- the test must fail on the current code), (2) your diff touches "
    "ONLY test files (any non-test source edit is a violation -- revert "
    "it), and (3) lists the test files you added/changed. If it reports a "
    "source-flow change or a non-test edit, fix that and call flow_verify "
    "once more."
)


CFG_VIEW_DESCRIPTION = (
    "SEE the actual control-flow graph of a function before you plan or edit "
    "it. Pass the function name (or file:line). Returns the real block-level "
    "CFG as boxes-and-arrows: each basic block (with its first line of code) "
    "and the edges between them, where --T--> / --F--> are the true/false arms "
    "of a branch and =loop=> is a loop back-edge. Use this to find WHERE in the "
    "control flow the bug lives (a missing guard = a branch that should exist "
    "but doesn't; a wrong path = an edge going the wrong way) so your flow_plan "
    "targets the right block. Example: cfg_view(function='wrap_var')."
)


def cfg_view(function: str) -> list:
    """Render the real block-level CFG of a function (boxes-and-arrows)."""
    text = flowedit.block_cfg_text(str(function).strip())
    if text is None:
        return [{"function": function, "cfg": None,
                 "note": "function not found in the map; check the name/anchor"}]
    return [{"function": function, "cfg": text}]


FLOW_OF_DESCRIPTION = (
    "START HERE. Pass the function/symbol names the bug report mentions (a list) "
    "and get back the real CONTROL-FLOW GRAPH of each -- its basic blocks and "
    "the true/false/loop edges between them. This is your PRIMARY working "
    "surface: there is no code map to browse. Read the flow to find the ONE part "
    "(a branch that should exist but doesn't, a guard missing before a "
    "statement, an edge going the wrong way) that -- if changed -- fixes the "
    "bug, so you change THAT part of the flow rather than rewriting the whole "
    "function. Then plan it with flow_plan, edit that spot, and verify with "
    "flow_check. FALLBACK -- IF THE BUG IS NOT IN THE FLOW: when the report "
    "names no function, a name does not resolve, or the returned graph does "
    "not contain the faulty region, do NOT force a fix into this flow. "
    "Localize with your own tools first (Grep the error message / suspect "
    "identifiers, Read candidate files), then call flow_of again on the "
    "function you found and work in ITS flow. "
    "Example: flow_of(functions=['parse_config', 'validate'])."
)

DFG_FLOW_OF_DESCRIPTION = (
    "START HERE. Pass the function/symbol names the bug report mentions (a list) "
    "and get back the real VALUE-FLOW (def-use) GRAPH of each -- every "
    "definition of a variable and every place its value is used. This is your "
    "PRIMARY working surface: there is no code map to browse. Read the flow to "
    "find the ONE value relation that is wrong (a value defined but used "
    "unchecked, a def that should be updated/moved, a use reading a stale or "
    "wrong definition) so you change THAT def or use rather than rewriting the "
    "whole function. Then plan it with flow_plan (ops add_def_use / "
    "remove_def_use / move_def_use), edit that spot, and verify with "
    "flow_check. FALLBACK -- IF THE BUG IS NOT IN THE FLOW: when the report "
    "names no function, a name does not resolve, or the returned graph does "
    "not contain the faulty value relation, do NOT force a fix into this "
    "flow. Localize with your own tools first (Grep the error message / "
    "suspect identifiers, Read candidate files), then call flow_of again on "
    "the function you found and work in ITS flow. "
    "Example: flow_of(functions=['parse_config', 'validate'])."
)


CFGDFG_FLOW_OF_DESCRIPTION = (
    "START HERE. Pass the function/symbol names the bug report mentions (a "
    "list) and get back BOTH real flow graphs of each function: its "
    "CONTROL-FLOW GRAPH (basic blocks and the true/false/loop edges between "
    "them) AND its VALUE-FLOW (def-use) GRAPH (every definition of a variable "
    "and every place its value is used). This is your PRIMARY working "
    "surface: there is no code map to browse. Read the two graphs together "
    "to find the ONE wrong element -- a branch/guard that should exist but "
    "doesn't, a condition that's wrong, a value used unchecked, a use reading "
    "a stale definition -- and change THAT, rather than rewriting the whole "
    "function. Then plan it with flow_plan (control-flow ops add_guard / "
    "remove_branch / change_condition / change_block_structure / rewire_edge, "
    "or data-flow ops add_def_use / remove_def_use / move_def_use -- pick the "
    "op from the graph where the bug lives), edit that spot, and verify with "
    "flow_check. FALLBACK -- IF THE BUG IS NOT IN THE FLOW: when the report "
    "names no function, a name does not resolve, or neither returned graph "
    "contains the faulty region, do NOT force a fix into these flows. "
    "Localize with your own tools first (Grep the error message / suspect "
    "identifiers, Read candidate files), then call flow_of again on the "
    "function you found and work in ITS flows. "
    "Example: flow_of(functions=['parse_config', 'validate'])."
)


def _flow_text(spec: str):
    """Rep-dispatching renderer: cfg -> block CFG, dfg -> def-use view,
    cfgdfg -> BOTH graphs stacked (the combined arm's surface)."""
    rep = _rep()
    if rep == "dfg":
        return flowedit.dfg_flow_text(spec)
    if rep == "cfgdfg":
        cfg_t = flowedit.block_cfg_text(spec)
        dfg_t = flowedit.dfg_flow_text(spec)
        if cfg_t is None and dfg_t is None:
            return None
        return "\n\n".join([
            cfg_t if cfg_t is not None
            else "(no control-flow graph available for %r)" % spec,
            dfg_t if dfg_t is not None
            else "(no value-flow graph available for %r)" % spec,
        ])
    return flowedit.block_cfg_text(spec)


def flow_of(functions: list) -> list:
    """CFG of each named function -- the flow-first entry point (no repo map).

    Robust to: bare names, dotted qualnames, Class.method, file:line anchors,
    and a CLASS name (expands to its methods' CFGs). Never raises on an
    unresolved name -- returns a graceful miss note so the agent can retry.
    """
    if isinstance(functions, str):
        functions = [functions]
    if not isinstance(functions, list) or not functions:
        raise ValueError("functions must be a non-empty list of names "
                         "(or file:line anchors) from the bug report")

    # A/B toggle: REPR_FLOW_NOFIX=1 reproduces the PRE-FIX flow_of exactly --
    # no class expansion, and block_cfg_text is called WITHOUT catching the
    # resolver's ValueError, so a class name (or other unresolved spec) crashes
    # the tool call as it did in the first pilot. Used to isolate the fix's
    # effect from k=1 noise on a fresh instance sample.
    nofix = os.environ.get("REPR_FLOW_NOFIX", "").strip() == "1"
    if nofix:
        # PRE-FIX reproduction: no class expansion, and resolution errors are
        # NOT swallowed -- a class/unresolved name raises (as it did in the
        # first pilot, before resolve_func_file_ranged caught ValueError). We
        # call the raw resolver so the fix to the underlying resolver does not
        # mask the original behavior.
        from servers.cfg_server import server as cfg_srv
        out = []
        for fn in functions:
            spec = str(fn).strip()
            if not spec:
                continue
            cfg_srv._resolve_functions(spec)   # raises ValueError on a miss
            text = flowedit.block_cfg_text(spec)
            out.append({"function": spec, "cfg": text})
        return out

    out = []
    for fn in functions:
        spec = str(fn).strip()
        if not spec:
            continue
        try:
            text = _flow_text(spec)
        except Exception:
            text = None
        if text is not None:
            item = {"function": spec, "cfg": text}
            # v3 (fix 2b): a bare name matching definitions in SEVERAL files
            # still renders the first pick's graph (v2 parity), but now says
            # so and lists the other candidates as file:line so the agent
            # can disambiguate with Class.method or file:line instead of
            # silently working on the wrong function.
            if flowedit.flow_v3():
                try:
                    mnote = flowedit.multi_candidate_note(spec)
                except Exception:
                    mnote = None
                if mnote:
                    item["note"] = mnote
            out.append(item)
            continue
        # miss: if the spec names a CLASS, expand to its methods' flows.
        try:
            method_anchors = flowedit.class_method_specs(spec)
        except Exception:
            method_anchors = []
        if method_anchors:
            rendered = 0
            for anchor in method_anchors[:12]:
                try:
                    mtext = _flow_text(anchor)
                except Exception:
                    mtext = None
                if mtext is not None:
                    out.append({"function": "%s (method @ %s)" % (spec, anchor),
                                "cfg": mtext})
                    rendered += 1
            if rendered:
                continue
        out.append({"function": spec, "cfg": None,
                    "note": "could not resolve %r. Pass the exact FUNCTION or "
                            "METHOD name (not a class, unless you want all its "
                            "methods), a Class.method form, or a file:line "
                            "anchor. If you cannot name the faulty function "
                            "from the bug report, do NOT guess: localize it "
                            "with your own tools (Grep the error message / "
                            "identifiers, Read candidate files), then call "
                            "flow_of again on the function you found." % spec})
    return out


FLOW_LOCATE_DESCRIPTION = (
    "STEP 2 -- declare WHERE the flow is wrong before planning: "
    "flow_locate(site='file.py:LINE', reason='one sentence'). Validates the "
    "spot against the current graph and records it; flow_plan inherits it as "
    "each delta's fault_site unless you override."
)


def _dfg_var_present(spec: str, var: str):
    """Does ``var`` appear in the function's def-use graph? True/False, or
    None when the graph cannot be computed (C without Joern, parse failure,
    unexpected fingerprint shape). Best-effort -- never raises."""
    try:
        fp = flowedit.func_fingerprint_now("dfg", spec)
        if not isinstance(fp, tuple) or len(fp) < 2:
            return None
        pairs = fp[1]
        if not isinstance(pairs, frozenset):
            return None
        return var in {v for v, _d, _u in pairs}
    except Exception:
        return None


def flow_locate(site, reason: str) -> list:
    """flow3 step 2 (wrong-path localization): declare the faulty spot the
    graph shows, BEFORE planning. Normalizes the site, resolves its
    containing function (the site's own file is the anchor), validates it
    against the current graph(s), and APPENDS the record to
    RUN_DIR/flow_locate.json so flow_plan can inherit it as each delta's
    fault_site. Never raises on an unresolvable site -- returns the nearest
    candidates with a disambiguation hint instead."""
    rep = _rep()
    norm, err = flowedit.normalize_fault_site(site)
    reason = str(reason or "").strip()
    if norm is None:
        return [{"located": None, "valid": False,
                 "note": "%s. Give the faulty spot as 'path/to/file.py:LINE' "
                         "(or {file, line, var?}) -- the exact line the "
                         "graph shows as wrong."
                         % (err or "no site given")}]
    spec = "%s:%d" % (norm["file"], norm["line"])
    try:
        rows, _note = flowedit.resolve_rows_scoped(spec, spec)
    except Exception:
        rows = []
    if not rows:
        cands = flowedit.nearest_function_candidates(norm["file"],
                                                     norm["line"])
        if cands:
            note = ("%s does not fall inside any indexed function. Nearest "
                    "candidates in %s: %s. Call flow_locate again with a "
                    "line INSIDE the faulty function (a file:line its graph "
                    "actually contains)."
                    % (spec, norm["file"],
                       "; ".join("%s (L%s: %s)"
                                 % (c["function"], c["line"], c["code"])
                                 for c in cands)))
        else:
            note = ("could not resolve %s: no indexed functions in %r. "
                    "Check the path (repo-relative) and line, or localize "
                    "with your own tools (Grep/Read) first, then call "
                    "flow_locate again." % (spec, norm["file"]))
        flowedit.save_locate({"site": norm, "reason": reason,
                              "function": None, "valid": False})
        return [{"located": None, "valid": False, "candidates": cands,
                 "note": note}]
    row = rows[0]
    func = row.get("qualname") or row.get("name") or spec
    try:
        ok = flowedit.fault_site_extent_check(norm, spec, spec)
    except Exception:
        ok = None
    # resolution succeeded BY CONTAINMENT, so only an explicit False (e.g. a
    # missing/odd extent row) marks the site invalid.
    valid = ok is not False
    located = {"function": func, "file": row.get("file"),
               "line": norm["line"]}
    notes = []
    block = None
    if rep in ("cfg", "cfgdfg"):
        try:
            block = flowedit.fault_site_block(spec, norm["line"])
        except Exception:
            block = None
        if block is not None:
            located["block"] = block
            notes.append("the site sits in basic block %s of %s's "
                         "control-flow graph" % (block, func))
    if rep in ("dfg", "cfgdfg") and norm.get("var"):
        present = _dfg_var_present(spec, str(norm["var"]))
        if present is True:
            notes.append("var %r appears in %s's def-use graph"
                         % (norm["var"], func))
        elif present is False:
            notes.append("WARNING: var %r does NOT appear in %s's def-use "
                         "graph -- double-check the variable name"
                         % (norm["var"], func))
    record = {"site": norm, "reason": reason, "function": func,
              "valid": valid}
    if block is not None:
        record["block"] = block
    flowedit.save_locate(record)
    note = ("located in %s (%s:%s)%s. Recorded -- flow_plan will inherit "
            "this spot as each delta's fault_site unless a delta gives its "
            "own." % (func, row.get("file"), row.get("line"),
                      ("; " + "; ".join(notes)) if notes else ""))
    return [{"located": located, "valid": valid, "note": note}]


def flow_plan(deltas: list) -> list:
    """Record the intended CFG/DFG delta plan + snapshot pre-edit graphs."""
    return _flow_plan_impl(deltas, replace=False)


def flow_plan_v3(deltas: list, replace: bool = False) -> list:
    """The flow3 arm's flow_plan surface: identical behavior to flow_plan
    (whose v3 semantics -- merge-on-replan, HEAD snapshots, fault_site,
    add_function -- are gated on REPR_FLOW_V3=1) PLUS the optional
    ``replace`` argument: replace=true discards the previously recorded plan
    instead of merging into it. Kept as a SEPARATE callable so the v2 tool
    schema (derived from flow_plan's signature) never gains the extra
    argument; the server registers this one under the tool name 'flow_plan'
    for flow3 cells only."""
    return _flow_plan_impl(deltas, replace=bool(replace))


def _flow_plan_impl(deltas: list, replace: bool) -> list:
    rep = _rep()
    v3 = flowedit.flow_v3()
    ops = flowedit.valid_ops(rep)
    if not isinstance(deltas, list) or not deltas:
        raise ValueError("deltas must be a non-empty list of "
                         "{op, function, anchor, rationale}")
    recorded = []
    snapshots = {}
    problems = []
    fs_missing = []
    fs_invalid = []
    for d in deltas:
        if not isinstance(d, dict):
            problems.append("each delta must be an object")
            continue
        op = str(d.get("op", "")).strip()
        func = str(d.get("function") or d.get("anchor") or "").strip()
        if op not in ops:
            problems.append("unknown op %r; valid: %s" % (op, sorted(ops)))
            continue
        entry = {"op": op, "function": func,
                 "anchor": str(d.get("anchor", "")),
                 "rationale": str(d.get("rationale", ""))}
        if v3:
            _record_fault_site(rep, d, entry, fs_missing, fs_invalid)
        recorded.append(entry)
        if v3 and op == "add_function":
            # add_function: no pre-edit snapshot exists BY DESIGN (the
            # function must not exist at HEAD); verification is the index-
            # bypassing existence check, not a fingerprint diff.
            _plan_add_function(entry, problems)
            continue
        if v3:
            _snapshot_head_v3(rep, func, str(d.get("anchor", "")).strip(),
                              snapshots, problems)
            continue
        if rep == "cfgdfg" and func:
            # combined arm: snapshot BOTH graphs (each delta verifies against
            # ITS graph via rep_for_op; no_flow_change means NEITHER changed,
            # so it is snapshotted too and actually verified).
            anchor = str(d.get("anchor", "")).strip()
            snap = {}
            for sub in ("cfg", "dfg"):
                fp = flowedit.func_fingerprint_now(sub, func)
                if fp is None and anchor and anchor != func:
                    fp = flowedit.func_fingerprint_now(sub, anchor)
                if fp is not None:
                    snap[sub] = repr(fp)
            if snap:
                snapshots[func] = snap
            else:
                problems.append(
                    "could not resolve %r (nor its anchor) to snapshot its "
                    "graphs; flow_check cannot verify this function" % func)
        elif func:
            # resolve by the function NAME first; fall back to the anchor
            # (file:line) when the name is unresolvable -- e.g. a C++ method
            # the symbol index doesn't carry, or an ambiguous overload. The
            # snapshot is still keyed by ``func`` so flow_check finds it.
            # no_flow_change deltas are snapshotted too, so 'the graph did
            # not change' is actually VERIFIED rather than vacuously matched.
            fp = flowedit.func_fingerprint_now(rep, func)
            anchor = str(d.get("anchor", "")).strip()
            if fp is None and anchor and anchor != func:
                fp = flowedit.func_fingerprint_now(rep, anchor)
            if fp is not None:
                # store as JSON-safe: fingerprints contain sets -> reprs
                snapshots[func] = repr(fp)
            else:
                problems.append(
                    "could not resolve %r (nor its anchor) to snapshot its "
                    "graph; flow_check cannot verify this function" % func)
    if v3 and fs_missing:
        # ONE nudge per response (never per delta, never a rejection): the
        # fault_site sharpens verification but its absence must not wedge
        # the agent into a re-plan loop.
        problems.append(
            "fault_site missing on %d delta(s) (%s): include "
            "fault_site='file.py:LINE' -- the exact faulty spot the delta "
            "targets -- in each delta (or declare the spot first with "
            "flow_locate; deltas then inherit it). The plan is recorded "
            "anyway; add it if you re-plan."
            % (len(fs_missing), ", ".join(fs_missing[:5])))
    if v3 and fs_invalid:
        problems.append(
            "fault_site problem on %d delta(s) (%s) -- double-check the "
            "file:line; the plan is recorded anyway."
            % (len(fs_invalid), "; ".join(fs_invalid[:5])))
    if not recorded:
        raise ValueError("no valid deltas: %s" % "; ".join(problems))
    if v3:
        plan = _merge_plan_v3(rep, recorded, snapshots, replace)
    else:
        plan = {"rep": rep, "deltas": recorded, "snapshots": snapshots}
    flowedit.save_plan(plan)
    result = {
        "recorded": len(recorded),
        "deltas": recorded,
        "snapshotted_functions": sorted(snapshots),
        "warnings": problems,
        "next": (
            "Now write the TEST that drives execution through this planned "
            "flow locus so it FAILS on the current code (do NOT edit source "
            "files), then call flow_verify before you stop."
            if _testgen() else
            "Now make the code edit that realizes this plan, then call "
            "flow_check on each function you edited."),
    }
    if v3:
        # merged-plan bookkeeping: this call's deltas replaced their
        # functions' previous deltas; other functions' deltas persist.
        result["revision"] = plan.get("revision")
        result["planned_functions"] = sorted(
            {d.get("function") for d in plan.get("deltas", [])
             if d.get("function")})
    # Stronger arm (REPR_CFG_BLOCKS=1): fold the real block-level CFG of each
    # planned function INTO the flow_plan response. flow_plan is called 100% of
    # the time, so this GUARANTEES the agent sees the actual control-flow graph
    # (boxes-and-arrows) at plan time -- putting the CFG on the critical path
    # instead of relying on the agent choosing to call a separate cfg_view
    # (which it skipped). It can revise its plan by calling flow_plan again.
    if rep == "cfg" and os.environ.get("REPR_CFG_BLOCKS", "").strip() == "1":
        graphs = {}
        for d in recorded:
            fn = d["function"]
            if fn and fn not in graphs:
                txt = flowedit.block_cfg_text(fn)
                if txt:
                    graphs[fn] = txt
        if graphs:
            result["control_flow_graphs"] = graphs
            result["next"] = (
                "Above is the REAL control-flow graph of each function you are "
                "about to change. Confirm your planned delta matches where the "
                "bug actually is in that graph (a missing guard = a branch that "
                "should exist; a wrong path = a mis-routed edge). If your plan "
                "doesn't fit the graph, call flow_plan again with a corrected "
                "delta. Otherwise make the edit, then call flow_check.")
    return [result]


# --- v3 (flow3) helpers ------------------------------------------------------
#
# Everything below runs ONLY when REPR_FLOW_V3=1 (checked by the callers);
# v2 arms never reach these, so their behavior stays byte-identical.


def _v3_verdict(res: dict, verdict: str) -> dict:
    """Attach the graded verdict (v3 only; v2 results stay byte-identical)."""
    if flowedit.flow_v3():
        res["verdict"] = verdict
    return res


def _read_rel(rel: str):
    from servers.common import env
    try:
        return (env.repo_root() / rel).read_text(encoding="utf-8",
                                                 errors="replace")
    except OSError:
        return None


def _addfn_locate(planned: dict):
    """(rel_file, bare_name) for an add_function delta: the FILE from the
    first file[:line] spec among anchor/function, the NAME from the first
    non-file spec. Either piece is None when not determinable."""
    from pathlib import Path
    from servers.common import env
    anchor = str(planned.get("anchor", "")).strip()
    fn = str(planned.get("function", "")).strip()
    rel = None
    for spec in (anchor, fn):
        base = spec.split(":", 1)[0].strip().replace("\\", "/")
        if base and base.lower().endswith(flowedit._SRC_EXTS):
            rel = base
            break
    if rel is not None and Path(rel).is_absolute():
        try:
            rel = str(Path(rel).relative_to(env.repo_root())).replace(
                "\\", "/")
        except ValueError:
            rel = None
    if rel is not None and rel.startswith("./"):
        rel = rel[2:]
    name = None
    for spec in (fn, anchor):
        base = spec.split(":", 1)[0].strip()
        if base and not base.replace("\\", "/").lower().endswith(
                flowedit._SRC_EXTS):
            name = base.rsplit(".", 1)[-1]
            break
    return rel, name


def _record_fault_site(rep: str, d: dict, entry: dict, fs_missing: list,
                       fs_invalid: list) -> None:
    """v3: normalize/validate the OPTIONAL per-delta fault_site and store it
    on the delta (fault_site normalized-or-null; fault_site_valid
    true/false/null=not-given-or-not-checkable). A delta with NO fault_site
    field inherits the MOST RECENT flow_locate record (tagged
    fault_site_source='flow_locate'; an explicit field tags 'explicit') and
    goes through the same validation. Never rejects the delta."""
    site, err = flowedit.normalize_fault_site(d.get("fault_site"))
    source = "explicit" if site is not None else None
    if site is None and err is None:
        # flow_locate inheritance: the field was simply not given -- fall
        # back to the agent's step-2 declaration (a malformed explicit
        # field keeps its own invalid warning instead).
        rec = flowedit.latest_locate()
        if rec and isinstance(rec.get("site"), dict):
            site = dict(rec["site"])
            source = "flow_locate"
    entry["fault_site"] = site
    if site is None:
        entry["fault_site_valid"] = None
        if err is None:
            fs_missing.append(entry["function"] or entry["op"])
        else:
            fs_invalid.append("%s: %s" % (entry["function"] or entry["op"],
                                          err))
        return
    entry["fault_site_source"] = source
    if entry["op"] == "add_function":
        # a brand-new function has no pre-edit extent to validate against
        entry["fault_site_valid"] = None
        return
    anchor = str(d.get("anchor", "")).strip()
    try:
        ok = flowedit.fault_site_extent_check(site, entry["function"], anchor)
    except Exception:
        ok = None
    entry["fault_site_valid"] = ok
    if ok is False:
        fs_invalid.append("%s: line %s is not inside the planned function"
                          % (entry["function"], site.get("line")))
        return
    if ok and rep in ("cfg", "cfgdfg"):
        # note which basic block contains the fault site, when the engine
        # exposes block line ranges (cfg only; best-effort)
        try:
            block = flowedit.fault_site_block(entry["function"] or anchor,
                                              site["line"])
        except Exception:
            block = None
        if block is not None:
            entry["fault_site_block"] = block


def _plan_add_function(entry: dict, problems: list) -> None:
    """v3: record expected_new for an add_function delta. Absent at HEAD
    (whether the whole file or just the function) -> expected_new=True with
    NO warning; already existing at HEAD -> expected_new=False + warning
    (verification will report the mismatch)."""
    rel, name = _addfn_locate(entry)
    entry["expected_new"] = True
    if rel is None or name is None:
        problems.append(
            "add_function delta for %r: give 'function' as the NEW "
            "function's NAME and 'anchor' as file:line of where it will "
            "live, so flow_check can find it" % entry.get("function"))
        return
    text, _status = flowedit.git_head_text(rel)
    if text is None:
        return    # file absent at HEAD (or no git): brand-new file, no warning
    from servers.common import joern_cfg
    src = flowedit.func_source_by_name(text, name, joern_cfg.is_c_file(rel))
    if src is not None:
        entry["expected_new"] = False
        problems.append(
            "add_function planned for %r but a function with that name "
            "already exists at HEAD -- if you mean to CHANGE it, plan a "
            "modify op (add_guard/change_condition/...) instead" % name)


def _snapshot_head_v3(rep: str, func: str, anchor: str, snapshots: dict,
                      problems: list) -> None:
    """v3: snapshot the planned function AS OF GIT HEAD (both graphs for the
    combined arm), so planning after an edit cannot re-baseline the pre-edit
    fingerprint to the already-edited source. A non-git root falls back to
    the on-disk snapshot (v2 semantics)."""
    if not func:
        return
    # fix C (honest func_absent): a plan can name a function that does NOT
    # exist at HEAD (e.g. the fix will CREATE it) while its anchor points at
    # a line where some OTHER def sits; the anchor fallback below then
    # silently fingerprinted that NEIGHBOR and flow_check later graded the
    # agent against the wrong function's graph. A positively-absent name
    # gets NO snapshot (flow_check reports unknown) and an explicit warning.
    if flowedit.head_named_func_absent(func, anchor):
        problems.append(
            "function %r does not exist at HEAD -- if your fix ADDS it, "
            "plan op add_function" % func)
        return
    subs = ("cfg", "dfg") if rep == "cfgdfg" else (rep,)
    snap = {}
    statuses = []
    for sub in subs:
        # fix 2a: the anchor scopes resolution to ITS file first, so the
        # snapshot is of the SAME function flow_check will later fingerprint
        # (not a same-named function in another file).
        fp, status = flowedit.func_fingerprint_head(sub, func, anchor=anchor)
        if fp is None and anchor and anchor != func:
            fp2, status2 = flowedit.func_fingerprint_head(sub, anchor)
            if fp2 is not None:
                fp, status = fp2, status2
            elif status == "unresolved":
                status = status2
        if fp is None and status == "no_git":
            fp = flowedit.func_fingerprint_now(sub, func, anchor=anchor)
            if fp is None and anchor and anchor != func:
                fp = flowedit.func_fingerprint_now(sub, anchor)
        if fp is not None:
            snap[sub] = repr(fp)
        else:
            statuses.append(status)
    if snap:
        snapshots[func] = snap if rep == "cfgdfg" else snap[rep]
        return
    reason = statuses[0] if statuses else "unresolved"
    if reason in ("file_absent", "func_absent"):
        problems.append(
            "%r does not exist at git HEAD (new file/function?) -- no "
            "pre-edit snapshot, so flow_check cannot verify it. If you are "
            "ADDING a brand-new function, plan op 'add_function' instead."
            % func)
    else:
        problems.append(
            "could not resolve %r (nor its anchor) to snapshot its graph "
            "at git HEAD; flow_check cannot verify this function" % func)


def _merge_plan_v3(rep: str, recorded: list, snapshots: dict,
                   replace: bool) -> dict:
    """v3 re-plan semantics: MERGE by function name (this call's deltas
    replace that function's previous deltas; other functions' deltas are
    preserved), with an audit trail -- 'revision' (int, starts at 1) and
    'history' (list of the superseded delta-lists). replace=True discards
    the previous deltas/snapshots entirely (the trail is still kept)."""
    old = flowedit.load_plan()
    revision = (int(old.get("revision") or 0) + 1) if old else 1
    history = list(old.get("history") or []) if old else []
    old_deltas = [d for d in (old.get("deltas") or []) if isinstance(d, dict)] \
        if old else []
    if old_deltas:
        history.append(old_deltas)
    if replace or not old_deltas:
        kept, kept_snaps = [], {}
    else:
        new_funcs = {e.get("function") for e in recorded}
        kept = [d for d in old_deltas if d.get("function") not in new_funcs]
        kept_names = {d.get("function") for d in kept}
        kept_snaps = {k: v for k, v in (old.get("snapshots") or {}).items()
                      if k in kept_names}
    kept_snaps.update(snapshots)
    return {"rep": rep, "deltas": kept + recorded, "snapshots": kept_snaps,
            "revision": revision, "history": history}


def flow_check(function: str) -> list:
    """Verify the edited function's actual graph delta matches the plan."""
    rep = _rep()
    plan = flowedit.load_plan()
    if not plan:
        return [_v3_verdict(
            {"function": function, "match": None,
             "note": "no flow_plan on record; call flow_plan before "
                     "editing so the pre-edit graph can be snapshotted"},
            "unknown")]
    func = str(function).strip()
    planned = next((d for d in plan.get("deltas", [])
                    if d["function"] == func
                    or func.endswith(d["function"])
                    or d["function"].endswith(func)), None)
    if planned is None:
        return [_v3_verdict(
            {"function": func, "match": None,
             "note": "this function was not in the flow_plan; planned "
                     "functions: %s"
                     % [d["function"] for d in plan.get("deltas", [])]},
            "unknown")]
    if _testgen():
        if flowedit.flow_v3() and planned.get("op") == "add_function":
            return [_testgen_check_add_function(planned, func)]
        return [_check_source_untouched(rep, plan, planned, func)]
    return [_verify_delta(rep, plan, planned, func)]


def _check_source_untouched(rep: str, plan: dict, planned: dict,
                            func: str) -> dict:
    """TEST-GEN verification: the planned SOURCE function's graph(s) must be
    UNCHANGED -- the test must fail on the current code, so 'fixing' the bug
    defeats the task. Handles both single-rep (str snapshot) and combined
    (dict {'cfg':..,'dfg':..}) snapshot shapes."""
    snap = plan.get("snapshots", {}).get(planned["function"])
    anchor = str(planned.get("anchor", "")).strip()
    if isinstance(snap, dict):
        pairs = sorted(snap.items())
    elif isinstance(snap, str):
        pairs = [(plan.get("rep") or rep, snap)]
    else:
        pairs = []
    if not pairs:
        return _v3_verdict(
            {"function": func, "planned_op": planned["op"], "match": None,
             "note": "no pre-plan snapshot for this function (was the "
                     "name resolvable at flow_plan time?)"}, "unknown")
    changed = []
    for sub, before_repr in pairs:
        sub = "cfg" if sub not in ("cfg", "dfg") else sub
        # anchor scopes resolution under v3 (fix 2a); inert for v2 arms.
        after = flowedit.func_fingerprint_now(sub, func, anchor=anchor)
        if after is None and anchor and anchor != func:
            after = flowedit.func_fingerprint_now(sub, anchor)
        if after is None:
            continue
        if flowedit.flow_v3():
            # fix A, testgen side: structural (object) comparison. The SWT
            # flow3 harness re-runs this check CROSS-PROCESS (record_final_
            # verify -> flow_verify_cli), where a different hash seed
            # reorders the dfg frozenset repr -- a repr-string compare
            # false-reported source_flow_changed on an UNTOUCHED source
            # function; see _verify_delta. v2 keeps the repr-string compare
            # byte-identical (v2 SWT cells never run the harness-side CLI).
            before = _eval_fp(before_repr)
            if before is None:
                return _v3_verdict(
                    {"function": func, "planned_op": planned["op"],
                     "match": None,
                     "note": "corrupt pre-edit snapshot"}, "unknown")
            if flowedit.fp_shape_mismatch(before, after):
                return _mixed_fp_unknown(func, planned["op"], graph=sub)
            if before != after:
                changed.append(sub)
        elif repr(after) != before_repr:
            changed.append(sub)
    if changed:
        return _v3_verdict(
            {"function": func, "planned_op": planned["op"],
             "observed": "source_flow_changed", "match": False,
             "note": "the SOURCE function's %s graph CHANGED -- you must "
                     "NOT fix or alter the bug; your test must FAIL on "
                     "the CURRENT code. Revert your source edits and "
                     "deliver ONLY a test." % "+".join(changed)}, "mismatch")
    return _v3_verdict(
        {"function": func, "planned_op": planned["op"],
         "observed": "source_flow_unchanged", "match": True,
         "note": "source flow unchanged, as required. Your planned %s is "
                 "the delta a FIX would make -- your test should FAIL "
                 "now because that change is absent, and PASS once a fix "
                 "makes it." % planned["op"]}, "exact")


def _testgen_check_add_function(planned: dict, func: str) -> dict:
    """TEST-GEN + v3 add_function: the plan says a FIX would ADD this
    function; the agent must NOT add it -- verify it still does not exist
    in the current source (index-bypassing, by name)."""
    rel, name = _addfn_locate(planned)
    if rel is None or name is None:
        return _v3_verdict(
            {"function": func, "planned_op": "add_function", "match": None,
             "note": "cannot locate the planned new function (need a "
                     "file:line anchor plus the function NAME)"}, "unknown")
    text = _read_rel(rel)
    if text is None:
        return _v3_verdict(
            {"function": func, "planned_op": "add_function",
             "observed": "source_flow_unchanged", "match": True,
             "note": "the file a fix would create does not exist -- the "
                     "function a fix would ADD is still absent, as "
                     "required; your test should FAIL now for that "
                     "reason."}, "exact")
    from servers.common import joern_cfg
    src = flowedit.func_source_by_name(text, name, joern_cfg.is_c_file(rel))
    if src is not None:
        return _v3_verdict(
            {"function": func, "planned_op": "add_function",
             "observed": "source_flow_changed", "match": False,
             "note": "you ADDED function %r -- that is the FIX's job, not "
                     "the test's; your test must FAIL on the CURRENT code. "
                     "Revert the source edit and deliver ONLY a test."
                     % name}, "mismatch")
    return _v3_verdict(
        {"function": func, "planned_op": "add_function",
         "observed": "source_flow_unchanged", "match": True,
         "note": "the function a fix would ADD is still absent, as "
                 "required -- your test should FAIL now for that reason."},
        "exact")


def _verify_delta(rep: str, plan: dict, planned: dict, func: str) -> dict:
    """Verify ONE planned delta against the CURRENT source (shared by
    flow_check -- right after an edit -- and flow_verify -- over the final
    code). Recomputes the function's graph, diffs it against the pre-edit
    snapshot, and reports whether the observed delta matches the planned op."""
    if flowedit.flow_v3() and planned.get("op") == "add_function":
        # v3: existence-based verification that BYPASSES the symbol index
        # (it cannot know a function the agent just created).
        return _verify_add_function(plan, planned, func)
    if (plan.get("rep") or rep) == "cfgdfg":
        return _verify_delta_combined(plan, planned, func)
    planned_op = planned["op"]
    anchor = str(planned.get("anchor", "")).strip()
    before_repr = plan.get("snapshots", {}).get(planned["function"])
    # recompute the post-edit fingerprint; mirror flow_plan's anchor fallback
    # so C++ methods / unresolved names verify via their file:line anchor.
    # v3 (fix 2a): the anchor also SCOPES resolution to its file first, so a
    # same-named function in another file cannot hijack the fingerprint
    # (inert for v2 arms -- gated inside func_fingerprint_now).
    after = flowedit.func_fingerprint_now(rep, func, anchor=anchor)
    if after is None:
        if anchor and anchor != func:
            after = flowedit.func_fingerprint_now(rep, anchor)

    if planned_op == "no_flow_change":
        # match iff the graph is UNCHANGED (or was never snapshotted)
        if before_repr is None or after is None:
            if flowedit.flow_v3():
                # v3 is honest about unverifiable: unknown, not a free pass
                return {"function": func, "planned_op": planned_op,
                        "match": None, "verdict": "unknown",
                        "observed": "no_flow_change",
                        "note": "no graph to compare -- cannot verify "
                                "no_flow_change"}
            return {"function": func, "planned_op": planned_op, "match": True,
                    "observed": "no_flow_change",
                    "note": "no graph to compare; treated as no_flow_change"}
        if flowedit.flow_v3():
            # fix A: STRUCTURAL comparison of the rebuilt fingerprint
            # OBJECTS. frozenset repr ordering is PYTHONHASHSEED-dependent,
            # so comparing repr STRINGS across processes (the plan snapshot
            # written by the MCP server vs the flow_verify CLI's recompute)
            # false-mismatched genuinely unchanged dfg graphs. v2 keeps the
            # repr-string compare byte-identical (its frozen records carry
            # the same latent taint, but v2 is frozen -- do not change it).
            before = _eval_fp(before_repr)
            if before is None:
                return _v3_verdict(
                    {"function": func, "planned_op": planned_op,
                     "match": None,
                     "note": "corrupt pre-edit snapshot"}, "unknown")
            if flowedit.fp_shape_mismatch(before, after):
                return _mixed_fp_unknown(func, planned_op)
            unchanged = (before == after)
        else:
            unchanged = repr(after) == before_repr
        return _v3_verdict(
            {"function": func, "planned_op": planned_op,
             "observed": "no_flow_change" if unchanged else "flow_changed",
             "match": unchanged,
             "note": ("edit left flow unchanged as planned" if unchanged
                      else "you planned no_flow_change but the graph "
                           "CHANGED -- your edit altered flow")},
            "exact" if unchanged else "mismatch")

    if before_repr is None:
        return _v3_verdict(
            {"function": func, "planned_op": planned_op, "match": None,
             "note": "no pre-edit snapshot for this function (was the "
                     "name resolvable at flow_plan time?)"}, "unknown")
    if after is None:
        return _v3_verdict(
            {"function": func, "planned_op": planned_op, "match": None,
             "note": "cannot compute the edited function's graph "
                     "(unparseable / not found post-edit)"}, "unknown")

    before = _eval_fp(before_repr)
    if before is None:
        return _v3_verdict(
            {"function": func, "planned_op": planned_op, "match": None,
             "note": "corrupt pre-edit snapshot"}, "unknown")
    observed = flowedit.classify_delta(rep, before, after)
    if flowedit.flow_v3():
        res = _graded_single(func, planned_op, observed,
                             flowedit.op_verdict(planned_op, observed))
        rn = flowedit.scoped_resolution_note(func, anchor)
        if rn:
            res["resolution_note"] = rn
        return res
    match = (observed == planned_op)
    return {
        "function": func,
        "planned_op": planned_op,
        "observed_op": observed,
        "match": match,
        "note": (
            "the edit realized the planned %s change. NOTE: a matching graph "
            "change is NECESSARY BUT NOT SUFFICIENT -- it does not prove the "
            "bug/crash is fixed. Confirm this guard is on the right function "
            "(on the failing/crash path) and actually covers the bad input "
            "before you stop." % planned_op
            if match else
            "MISMATCH: you planned %s but the edit produced %s -- "
            "the code change did not make the graph change you intended"
            % (planned_op, observed)),
    }


def _mixed_fp_unknown(func: str, planned_op: str, graph: str = None) -> dict:
    """v3 (fix 3): the pre-edit snapshot and the recomputed fingerprint have
    DIFFERENT shapes (an old-format snapshot meeting the extended v3
    fingerprint, or vice versa) -- comparing them would be meaningless, so
    the verdict is unknown with a note; never a crash, never a fake
    mismatch."""
    res = {"function": func, "planned_op": planned_op, "match": None,
           "verdict": "unknown",
           "note": "cannot compare: the pre-edit snapshot and the recomputed "
                   "graph use different fingerprint formats (old/new mix) -- "
                   "call flow_plan again to refresh the snapshot, then "
                   "re-check"}
    if graph:
        res["graph"] = graph
    return res


def _graded_single(func: str, planned_op: str, observed: str, verdict: str,
                   graph: str = None) -> dict:
    """v3 graded result for a classified delta: verdict 'exact' |
    'same_family' | 'mismatch' | 'unknown' (mixed fingerprint shapes); match
    stays boolean-compatible (True for exact AND same_family, False for
    mismatch, None for unknown). ``graph`` names the combined arm's
    sub-graph in the note."""
    if verdict == "unknown":
        res = _mixed_fp_unknown(func, planned_op, graph=graph)
        res["observed_op"] = observed
        return res
    match = verdict in ("exact", "same_family")
    where = (" in the %s graph" % graph) if graph else ""
    if verdict == "same_family":
        note = ("family pass: planned %s realized as %s -- same family. "
                "The realized graph change is in the planned op's family, "
                "so this counts as a match; still confirm the edit does "
                "what the plan intended before you stop."
                % (planned_op, observed))
    elif verdict == "exact":
        note = ("the edit realized the planned %s change%s. NOTE: a "
                "matching graph change is NECESSARY BUT NOT SUFFICIENT -- "
                "it does not prove the bug/crash is fixed. Confirm the "
                "change is on the right function and actually covers the "
                "bad input before you stop." % (planned_op, where))
    else:
        note = ("MISMATCH: you planned %s but the edit produced %s%s -- "
                "the code change did not make the graph change you intended"
                % (planned_op, observed, where))
    res = {"function": func, "planned_op": planned_op,
           "observed_op": observed, "match": match, "verdict": verdict,
           "note": note}
    if graph:
        res["graph"] = graph
    return res


def _verify_add_function(plan: dict, planned: dict, func: str) -> dict:
    """v3: verify an add_function delta. Observed op is 'add_function' iff
    the named function exists in the CURRENT source of the anchor file but
    did not exist at git HEAD. Extraction bypasses the symbol index (Python:
    ast walk of the current file text by name; C: brace balancing from the
    first matching definition line). Exact-only -- no family partner."""
    from servers.common import joern_cfg
    rel, name = _addfn_locate(planned)
    if rel is None or name is None:
        return _v3_verdict(
            {"function": func, "planned_op": "add_function", "match": None,
             "note": "cannot locate the new function: give 'function' as "
                     "its NAME and 'anchor' as file:line of where it "
                     "lives"}, "unknown")
    now_text = _read_rel(rel)
    if now_text is None:
        return _v3_verdict(
            {"function": func, "planned_op": "add_function", "match": None,
             "note": "cannot read %s to look for the new function" % rel},
            "unknown")
    is_c = joern_cfg.is_c_file(rel)
    if not is_c:
        try:
            import ast as _ast
            _ast.parse(now_text)
        except (SyntaxError, ValueError):
            return _v3_verdict(
                {"function": func, "planned_op": "add_function",
                 "match": None,
                 "note": "%s does not parse -- fix the syntax error, then "
                         "re-check" % rel}, "unknown")
    now_src = flowedit.func_source_by_name(now_text, name, is_c)
    head_text, _status = flowedit.git_head_text(rel)
    head_src = (flowedit.func_source_by_name(head_text, name, is_c)
                if head_text is not None else None)
    if now_src is not None and head_src is None:
        return _v3_verdict(
            {"function": func, "planned_op": "add_function",
             "observed_op": "add_function", "match": True,
             "note": "the new function %r exists in %s and was not present "
                     "at HEAD -- add_function realized. NOTE: necessary "
                     "but not sufficient; confirm the new function is "
                     "actually wired into the failing path before you "
                     "stop." % (name, rel)}, "exact")
    if now_src is None:
        return _v3_verdict(
            {"function": func, "planned_op": "add_function",
             "observed_op": "function_missing", "match": False,
             "note": "you planned add_function for %r but no function with "
                     "that name exists in %s -- the addition was not "
                     "realized (or it lives under a different name/file; "
                     "re-plan if so)." % (name, rel)}, "mismatch")
    # exists now AND existed at HEAD: not an addition
    observed = "function_preexisting"
    if not is_c:
        rep = plan.get("rep") or _rep()
        sub = "dfg" if rep == "dfg" else "cfg"
        fb = flowedit.fingerprint(sub, head_src)
        fa = flowedit.fingerprint(sub, now_src)
        if fb is not None and fa is not None:
            observed = flowedit.classify_delta(sub, fb, fa)
    return _v3_verdict(
        {"function": func, "planned_op": "add_function",
         "observed_op": observed, "match": False,
         "note": "function %r already existed at HEAD -- add_function "
                 "requires a brand-new function (observed change vs HEAD: "
                 "%s). If you meant to CHANGE it, re-plan with a modify "
                 "op." % (name, observed)}, "mismatch")


def _flow_verify_testgen(rep: str, plan: dict, deltas: list) -> dict:
    """TEST-GEN final verification: (1) every planned SOURCE function's flow
    unchanged, (2) the diff touches ONLY test files, (3) a test edit exists."""
    results = [
        (_testgen_check_add_function(d, d["function"])
         if flowedit.flow_v3() and d.get("op") == "add_function"
         else _check_source_untouched(rep, plan, d, d["function"]))
        for d in deltas]
    try:
        # empty plan on purpose: in test-gen NOTHING outside test files may
        # change -- including the planned functions' own files (the plan
        # names what a FIX would touch, not what the AGENT may touch), so no
        # edit is filtered as "planned".
        edits = flowedit.unplanned_edits({"deltas": []})
    except Exception:
        edits = []
    violations = [e for e in edits if not flowedit.is_test_path(e["file"])]
    test_edits = [e for e in edits if flowedit.is_test_path(e["file"])]
    n_changed = sum(1 for r in results if r.get("match") is False)
    n_unknown = sum(1 for r in results if r.get("match") is None)
    ok = (n_changed == 0 and not violations and bool(test_edits))
    if ok:
        note = ("FINAL VERIFICATION PASSED: every planned source function's "
                "flow is unchanged and your diff touches only test files "
                "(%d test edit(s)). Make sure the test actually FAILS on "
                "the current code for the planned reason, then stop."
                % len(test_edits))
    else:
        probs = []
        if n_changed:
            probs.append("%d planned SOURCE function(s) have a CHANGED flow "
                         "graph -- you must not fix/alter the bug; revert "
                         "those source edits" % n_changed)
        if violations:
            probs.append("%d NON-TEST file edit(s) in the diff (%s) -- a "
                         "test-generation deliverable may only touch test "
                         "files; revert them"
                         % (len(violations),
                            ", ".join(sorted({v["file"]
                                              for v in violations})[:5])))
        if not test_edits:
            probs.append("no test-file edit found yet -- write your "
                         "reproducing test in the project's test suite")
        if n_unknown:
            probs.append("%d planned function(s) could not be checked (see "
                         "their notes)" % n_unknown)
        note = ("FINAL VERIFICATION ISSUES: " + "; ".join(probs)
                + ". Address these, then call flow_verify once more.")
    return {
        "planned_deltas": len(deltas),
        "results": results,
        "source_flow_untouched": n_changed == 0,
        "non_test_edits": violations,
        "test_edits": test_edits,
        # report-compat aliases: "unplanned" in test-gen = the violations
        "unplanned_edits": violations,
        "all_planned_match": ok,
        "note": note,
    }


def _verify_delta_combined(plan: dict, planned: dict, func: str) -> dict:
    """Combined-arm (cfgdfg) verification: each delta checks against ITS
    graph (rep_for_op); no_flow_change means NEITHER graph changed. Snapshots
    are per-function dicts {'cfg': repr, 'dfg': repr} taken by flow_plan."""
    planned_op = planned["op"]
    snap = plan.get("snapshots", {}).get(planned["function"])
    anchor = str(planned.get("anchor", "")).strip()

    def _now(sub):
        # anchor scopes resolution under v3 (fix 2a); inert for v2 arms.
        after = flowedit.func_fingerprint_now(sub, func, anchor=anchor)
        if after is None and anchor and anchor != func:
            after = flowedit.func_fingerprint_now(sub, anchor)
        return after

    if planned_op == "no_flow_change":
        if not isinstance(snap, dict) or not snap:
            if flowedit.flow_v3():
                # v3 is honest about unverifiable: unknown, not a free pass
                return {"function": func, "planned_op": planned_op,
                        "match": None, "verdict": "unknown",
                        "observed": "no_flow_change",
                        "note": "no graph to compare -- cannot verify "
                                "no_flow_change"}
            return {"function": func, "planned_op": planned_op, "match": True,
                    "observed": "no_flow_change",
                    "note": "no graph to compare; treated as no_flow_change"}
        changed = []
        for sub in sorted(snap):
            after = _now(sub)
            if after is None:
                continue
            if flowedit.flow_v3():
                # fix A: structural (object) comparison -- frozenset repr
                # order is PYTHONHASHSEED-dependent, so repr-string equality
                # false-mismatches across processes; see _verify_delta.
                before = _eval_fp(snap[sub])
                if before is None:
                    return _v3_verdict(
                        {"function": func, "planned_op": planned_op,
                         "match": None,
                         "note": "corrupt pre-edit snapshot"}, "unknown")
                if flowedit.fp_shape_mismatch(before, after):
                    return _mixed_fp_unknown(func, planned_op, graph=sub)
                if before != after:
                    changed.append(sub)
            elif repr(after) != snap[sub]:
                changed.append(sub)
        if changed:
            return _v3_verdict(
                {"function": func, "planned_op": planned_op,
                 "observed": "flow_changed", "match": False,
                 "note": "you planned no_flow_change but the %s graph "
                         "CHANGED -- your edit altered flow"
                         % "+".join(changed)}, "mismatch")
        return _v3_verdict(
            {"function": func, "planned_op": planned_op,
             "observed": "no_flow_change", "match": True,
             "note": "edit left both graphs unchanged as planned"}, "exact")

    sub = flowedit.rep_for_op(planned_op) or "cfg"
    before_repr = snap.get(sub) if isinstance(snap, dict) else None
    if before_repr is None:
        return _v3_verdict(
            {"function": func, "planned_op": planned_op, "match": None,
             "note": "no pre-edit %s-graph snapshot for this function "
                     "(was the name resolvable at flow_plan time?)" % sub},
            "unknown")
    after = _now(sub)
    if after is None:
        return _v3_verdict(
            {"function": func, "planned_op": planned_op, "match": None,
             "note": "cannot compute the edited function's %s graph "
                     "(unparseable / not found post-edit)" % sub}, "unknown")
    before = _eval_fp(before_repr)
    if before is None:
        return _v3_verdict(
            {"function": func, "planned_op": planned_op, "match": None,
             "note": "corrupt pre-edit snapshot"}, "unknown")
    observed = flowedit.classify_delta(sub, before, after)
    if flowedit.flow_v3():
        res = _graded_single(func, planned_op, observed,
                             flowedit.op_verdict(planned_op, observed),
                             graph=sub)
        rn = flowedit.scoped_resolution_note(func, anchor)
        if rn:
            res["resolution_note"] = rn
        return res
    match = (observed == planned_op)
    return {
        "function": func,
        "planned_op": planned_op,
        "observed_op": observed,
        "graph": sub,
        "match": match,
        "note": (
            "the edit realized the planned %s change in the %s graph. NOTE: "
            "a matching graph change is NECESSARY BUT NOT SUFFICIENT -- it "
            "does not prove the bug/crash is fixed. Confirm this change is "
            "on the right function and actually covers the bad input before "
            "you stop." % (planned_op, sub)
            if match else
            "MISMATCH: you planned %s but the edit produced %s in the %s "
            "graph -- the code change did not make the graph change you "
            "intended" % (planned_op, observed, sub)),
    }


FLOW_VERIFY_DESCRIPTION = (
    "FINAL STEP -- call ONCE, with no arguments, when you believe your fix is "
    "complete and BEFORE you stop. Verifies the FINAL state of the code "
    "against the WHOLE plan you committed in flow_plan: (1) re-checks EVERY "
    "planned delta (recomputes each planned function's control-/data-flow "
    "graph from the final source and compares to the plan -- a later edit can "
    "silently undo an earlier flow_check-verified change), and (2) lists "
    "source edits you made OUTSIDE the planned functions (scope the plan "
    "never committed to). If any delta reports match=false, or an unplanned "
    "edit was accidental, fix that and call flow_verify once more. A pass is "
    "necessary but not sufficient -- it does not prove the bug is fixed."
)


def flow_verify() -> list:
    """Final verification: the WHOLE final change vs the WHOLE recorded plan."""
    rep = _rep()
    plan = flowedit.load_plan()
    if not plan:
        return [_v3_verdict(
            {"match": None,
             "note": "no flow_plan on record -- nothing to verify against. "
                     "Commit your intended graph delta with flow_plan "
                     "BEFORE editing so the final change can be checked "
                     "against it."}, "unknown")]
    deltas = plan.get("deltas", [])
    if not deltas:
        return [_v3_verdict(
            {"match": None,
             "note": "the recorded flow_plan has no deltas -- re-plan "
                     "with flow_plan, then call flow_verify again."},
            "unknown")]
    if _testgen():
        return [_flow_verify_testgen(rep, plan, deltas)]
    results = [_verify_delta(rep, plan, d, d["function"]) for d in deltas]
    try:
        unplanned = flowedit.unplanned_edits(plan)
    except Exception:
        unplanned = []
    # function-level drift is a hard issue; module-level edits (imports,
    # globals, new files) often legitimately accompany a fix -- report them,
    # but do not fail the verdict on them alone.
    fn_unplanned = [u for u in unplanned if u.get("function")]
    mod_unplanned = [u for u in unplanned if not u.get("function")]
    n_mismatch = sum(1 for r in results if r.get("match") is False)
    n_unknown = sum(1 for r in results if r.get("match") is None)
    all_planned_match = (n_mismatch == 0 and n_unknown == 0
                         and len(results) > 0)
    if all_planned_match and not fn_unplanned:
        note = ("FINAL VERIFICATION PASSED: every planned graph delta is "
                "realized in the final code and no FUNCTION outside the plan "
                "was edited."
                + (" Note: %d module-level/new-file edit(s) accompany the "
                   "fix (see unplanned_edits) -- fine if the fix needs them "
                   "(e.g. an import); revert any that are accidental."
                   % len(mod_unplanned) if mod_unplanned else "")
                + " This is necessary but NOT sufficient -- confirm the "
                  "change actually addresses the reported bug, then stop.")
    else:
        probs = []
        if n_mismatch:
            probs.append("%d planned delta(s) do NOT match the final code -- "
                         "a later edit may have undone or altered them; "
                         "revise those edits (or re-plan if your intent "
                         "changed)" % n_mismatch)
        if n_unknown:
            probs.append("%d planned delta(s) could not be verified (see "
                         "their notes)" % n_unknown)
        if fn_unplanned:
            probs.append("%d edit(s) touch FUNCTIONS outside the plan -- "
                         "revert them if accidental, or keep them only if "
                         "the fix truly needs them" % len(fn_unplanned))
        if mod_unplanned:
            probs.append("%d module-level/new-file edit(s) outside the plan "
                         "(fine if the fix needs them, e.g. an import)"
                         % len(mod_unplanned))
        note = "FINAL VERIFICATION ISSUES: " + "; ".join(probs) + \
               ". Address these, then call flow_verify once more."
    return [{
        "planned_deltas": len(deltas),
        "results": results,
        "all_planned_match": all_planned_match,
        "unplanned_edits": unplanned,
        "note": note,
    }]


def _eval_fp(rep_str: str):
    """Rebuild a fingerprint tuple from its repr (contains frozenset)."""
    try:
        return eval(rep_str, {"frozenset": frozenset, "__builtins__": {}})
    except Exception:
        return None
