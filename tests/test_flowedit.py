"""Tests for graph-mediated editing: flow_plan + flow_check."""

import os

import pytest

from servers.common import flowedit


FUNC_NO_GUARD = '''\
def wrap_var(v):
    result = process(v)
    return result
'''

FUNC_WITH_GUARD = '''\
def wrap_var(v):
    if v is None:
        return None
    result = process(v)
    return result
'''


def test_cfg_fingerprint_detects_added_guard():
    fp_before = flowedit.cfg_fingerprint(FUNC_NO_GUARD)
    fp_after = flowedit.cfg_fingerprint(FUNC_WITH_GUARD)
    assert fp_before is not None and fp_after is not None
    assert fp_before != fp_after
    assert flowedit.classify_delta("cfg", fp_before, fp_after) == "add_guard"


def test_cfg_no_flow_change_on_expression_edit():
    a = 'def f(x):\n    return x + 1\n'
    b = 'def f(x):\n    return x + 2\n'   # pure constant change
    fa, fb = flowedit.cfg_fingerprint(a), flowedit.cfg_fingerprint(b)
    assert flowedit.classify_delta("cfg", fa, fb) == "no_flow_change"


def test_dfg_delta_add_def_use():
    a = 'def f(x):\n    return g(x)\n'
    b = 'def f(x):\n    y = h(x)\n    return g(y)\n'  # new def-use y
    fa, fb = flowedit.dfg_fingerprint(a), flowedit.dfg_fingerprint(b)
    assert fa is not None and fb is not None
    assert flowedit.classify_delta("dfg", fa, fb) in ("add_def_use",
                                                      "move_def_use")


def test_legacy_verify_env_restores_prefix_behavior(tmp_path, monkeypatch,
                                                    built_index):
    """REPR_FLOW_LEGACY_VERIFY=1 (set by the runners for agent-facing tools)
    must reproduce the PRE-FIX behavior: a drifted function is NOT found by
    name (flow_check says 'cannot compute'), and the dfg classifier uses the
    ordinal set-diff. Offline analysis (env unset) keeps the fixed paths."""
    repo = _make_git_two_fn_proj(tmp_path, monkeypatch, "legacy")
    monkeypatch.setenv("REPR_FLOW_LEGACY_VERIFY", "1")
    from servers.common import flowedit_tools as fe
    fe.flow_plan(deltas=[{"op": "add_guard", "function": "fn_b",
                          "anchor": "m.py:5", "rationale": "guard None"}])
    m = repo / "m.py"
    m.write_text(("# padding\n" * 30) + m.read_text().replace(
        "def fn_b(z):\n",
        "def fn_b(z):\n    if z is None:\n        return None\n"))
    chk = fe.flow_check(function="fn_b")[0]
    assert chk["match"] is None, chk          # pre-fix: cannot compute
    assert "cannot compute" in chk["note"]
    # dfg legacy classifier: insertion-shift reads as move_def_use again
    a = flowedit.dfg_fingerprint("def f(x):\n    y = g(x)\n    return h(y)\n")
    b = flowedit.dfg_fingerprint(
        "def f(x):\n    w = k(x)\n    y = g(w)\n    return h(y)\n")
    assert flowedit.classify_delta("dfg", a, b) == "move_def_use"
    monkeypatch.delenv("REPR_FLOW_LEGACY_VERIFY")


def test_dfg_delta_insertion_is_add_not_move():
    """Regression for the ordinal-shift false mismatch: inserting a NEW
    def-use pair above existing ones shifts every later pair's ordinals; the
    classifier must still call it add_def_use, not move_def_use."""
    a = ("def f(x):\n"
         "    y = g(x)\n"
         "    return h(y)\n")
    b = ("def f(x):\n"
         "    w = k(x)\n"          # new pair inserted ABOVE the old ones
         "    y = g(w)\n"
         "    return h(y)\n")
    fa, fb = flowedit.dfg_fingerprint(a), flowedit.dfg_fingerprint(b)
    assert flowedit.classify_delta("dfg", fa, fb) == "add_def_use"
    # a genuine relocation (same pairs, moved) must still classify as move
    c = ("def f(x):\n"
         "    y = g(x)\n"
         "    z = 1\n"
         "    return h(y) + z\n")
    d = ("def f(x):\n"
         "    z = 1\n"
         "    y = g(x)\n"
         "    return h(y) + z\n")
    fc, fd = flowedit.dfg_fingerprint(c), flowedit.dfg_fingerprint(d)
    if fc != fd:   # ordinals differ, counts identical
        assert flowedit.classify_delta("dfg", fc, fd) == "move_def_use"


def test_valid_ops_per_rep():
    cfg_ops = flowedit.valid_ops("cfg")
    dfg_ops = flowedit.valid_ops("dfg")
    assert "add_guard" in cfg_ops and "no_flow_change" in cfg_ops
    assert "add_def_use" in dfg_ops and "add_guard" not in dfg_ops


def test_flow_plan_then_check_match(tmp_path, monkeypatch, built_index):
    """End-to-end: plan add_guard on a real repo function, edit it to add the
    guard, flow_check confirms the realized delta matches the plan."""
    from tests.conftest import FIXTURE_REPO
    import shutil

    # copy the fixture so we can edit it
    repo = tmp_path / "proj"
    shutil.copytree(FIXTURE_REPO, repo)
    # rebuild the index over the copy so symbol resolution points at it
    from indexer.backends import get_backend
    idx = repo / ".repr_index"
    idx.mkdir(exist_ok=True)
    get_backend("symbols").build(repo, idx)

    monkeypatch.setenv("REPO_ROOT", str(repo))
    monkeypatch.setenv("REPO_INDEX_DIR", str(idx))
    monkeypatch.setenv("RUN_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("REPR_PERREP_REP", "cfg")

    from servers.common import flowedit_tools as fe

    # pick a real function in the fixture: validate_input has a loop+if
    target = "validate_input"
    # 1) plan an add_guard on it (snapshots its current graph)
    res = fe.flow_plan(deltas=[{"op": "add_guard", "function": target,
                                "anchor": "sampleproj/core/engine.py:5",
                                "rationale": "guard empty input"}])
    assert res[0]["recorded"] == 1
    assert target in res[0]["snapshotted_functions"]

    # 2) edit the function to add an early-return guard
    eng = repo / "sampleproj" / "core" / "engine.py"
    src = eng.read_text()
    src = src.replace(
        "def validate_input(records: list[str]) -> list[str]:",
        "def validate_input(records: list[str]) -> list[str]:\n"
        "    if not records:\n        return []")
    eng.write_text(src)

    # 3) flow_check: the realized delta should be add_guard -> match
    chk = fe.flow_check(function=target)[0]
    assert chk["planned_op"] == "add_guard"
    assert chk["match"] is True, chk


def test_render_block_cfg_shows_branches_and_loops():
    """render_block_cfg emits blocks + typed edges (true/false/loop) so the
    agent can SEE the control-flow structure, not just a summary."""
    item = {
        "function": "f", "engine": "python-ast", "entry": 0, "exit": 1,
        "blocks": [
            {"id": 0, "line_range": None, "first_line_code": "<entry>"},
            {"id": 1, "line_range": None, "first_line_code": "<exit>"},
            {"id": 2, "line_range": [2, 2], "first_line_code": "if x is None:"},
            {"id": 3, "line_range": [3, 3], "first_line_code": "return None"},
        ],
        "edges": [
            {"src": 0, "dst": 2, "kind": "next"},
            {"src": 2, "dst": 3, "kind": "true"},
            {"src": 2, "dst": 1, "kind": "false"},
            {"src": 3, "dst": 1, "kind": "next"},
        ],
    }
    text = flowedit.render_block_cfg(item)
    assert "control-flow graph of f()" in text
    assert "if x is None:" in text
    assert "--T-->" in text and "--F-->" in text   # branch arms explicit
    assert "[entry]" in text and "[exit]" in text


def test_render_block_cfg_empty_graph_notes():
    item = {"function": "g", "blocks": [], "edges": [],
            "note": "outside CPG scope"}
    text = flowedit.render_block_cfg(item)
    assert "no control-flow graph" in text and "outside CPG scope" in text


def test_cfg_view_renders_real_function(tmp_path, monkeypatch, built_index):
    """cfg_view resolves a real repo function and renders its block CFG."""
    from tests.conftest import FIXTURE_REPO
    import shutil
    repo = tmp_path / "vproj"
    shutil.copytree(FIXTURE_REPO, repo)
    from indexer.backends import get_backend
    idx = repo / ".repr_index"
    idx.mkdir(exist_ok=True)
    get_backend("symbols").build(repo, idx)
    monkeypatch.setenv("REPO_ROOT", str(repo))
    monkeypatch.setenv("REPO_INDEX_DIR", str(idx))
    monkeypatch.setenv("REPR_PERREP_REP", "cfg")
    from servers.common import flowedit_tools as fe
    out = fe.cfg_view(function="validate_input")[0]
    assert out["cfg"] is not None
    assert "control-flow graph of validate_input()" in out["cfg"]
    # the fixture function has a loop over records -> a loop edge must appear
    assert "=loop=>" in out["cfg"] or "for record" in out["cfg"]


def test_flow_plan_injects_cfg_when_blocks_enabled(tmp_path, monkeypatch,
                                                   built_index):
    """With REPR_CFG_BLOCKS=1, flow_plan folds the real block-CFG of each
    planned function into its response (critical-path adoption of the graph)."""
    from tests.conftest import FIXTURE_REPO
    import shutil
    repo = tmp_path / "bproj"
    shutil.copytree(FIXTURE_REPO, repo)
    from indexer.backends import get_backend
    idx = repo / ".repr_index"
    idx.mkdir(exist_ok=True)
    get_backend("symbols").build(repo, idx)
    monkeypatch.setenv("REPO_ROOT", str(repo))
    monkeypatch.setenv("REPO_INDEX_DIR", str(idx))
    monkeypatch.setenv("RUN_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("REPR_PERREP_REP", "cfg")
    from servers.common import flowedit_tools as fe

    # blocks OFF -> no CFG in the response
    monkeypatch.delenv("REPR_CFG_BLOCKS", raising=False)
    off = fe.flow_plan(deltas=[{"op": "add_guard",
                                "function": "validate_input",
                                "anchor": "x", "rationale": "y"}])[0]
    assert "control_flow_graphs" not in off

    # blocks ON -> the real CFG of validate_input is injected
    monkeypatch.setenv("REPR_CFG_BLOCKS", "1")
    on = fe.flow_plan(deltas=[{"op": "add_guard",
                               "function": "validate_input",
                               "anchor": "x", "rationale": "y"}])[0]
    assert "control_flow_graphs" in on
    assert "validate_input" in on["control_flow_graphs"]
    assert "control-flow graph of validate_input()" in \
        on["control_flow_graphs"]["validate_input"]


def test_c_cfg_fingerprint_detects_added_guard():
    """C path (Joern-backed): adding a null-guard grows the CFG fingerprint and
    classifies as add_guard. Skipped where Joern/JDK are absent (e.g. CI)."""
    from servers.common import joern_cfg
    if not joern_cfg.available():
        pytest.skip("Joern CLI / JDK not installed")
    no_guard = ("int handle(char *p, int n){int t=0;"
                "for(int i=0;i<n;i++){t+=p[i];}return t;}\n")
    with_guard = ("int handle(char *p, int n){int t=0;if(p==0){return -1;}"
                  "for(int i=0;i<n;i++){t+=p[i];}return t;}\n")
    fb = flowedit.c_cfg_fingerprint(no_guard)
    fa = flowedit.c_cfg_fingerprint(with_guard)
    assert fb is not None and fa is not None
    assert fa[0] > fb[0]                     # more blocks after the guard
    assert flowedit.classify_delta("cfg", ("c", fb), ("c", fa)) == "add_guard"
    assert flowedit.classify_delta("cfg", ("c", fb), ("c", fb)) == \
        "no_flow_change"


def test_flow_of_expands_class_to_methods(tmp_path, monkeypatch, built_index):
    """flow_of on a CLASS name returns its methods' CFGs (not a crash/miss).
    Regression for the 5 flow-first misses where the agent named a class."""
    repo = tmp_path / "clsproj"
    repo.mkdir()
    (repo / "m.py").write_text(
        "class WikidataEntity:\n"
        "    def get_profiles(self):\n"
        "        if self.x:\n            return 1\n        return 2\n"
        "    def _link(self):\n        return None\n",
        encoding="utf-8")
    from indexer.backends import get_backend
    idx = repo / ".repr_index"
    idx.mkdir()
    get_backend("symbols").build(repo, idx)
    monkeypatch.setenv("REPO_ROOT", str(repo))
    monkeypatch.setenv("REPO_INDEX_DIR", str(idx))
    monkeypatch.setenv("REPR_PERREP_REP", "cfg")
    from servers.common import flowedit_tools as fe
    out = fe.flow_of(functions=["WikidataEntity"])
    rendered = [o for o in out if o.get("cfg")]
    assert len(rendered) >= 2, out          # both methods' CFGs
    assert all("WikidataEntity" in o["function"] for o in rendered)


def test_flow_of_never_crashes_on_unresolvable(tmp_path, monkeypatch,
                                                built_index):
    """A mix of resolvable + unresolvable names returns misses, not an error."""
    repo = tmp_path / "mixproj"
    repo.mkdir()
    (repo / "m.py").write_text(
        "def real_fn(x):\n    if x:\n        return 1\n    return 0\n",
        encoding="utf-8")
    from indexer.backends import get_backend
    idx = repo / ".repr_index"
    idx.mkdir()
    get_backend("symbols").build(repo, idx)
    monkeypatch.setenv("REPO_ROOT", str(repo))
    monkeypatch.setenv("REPO_INDEX_DIR", str(idx))
    monkeypatch.setenv("REPR_PERREP_REP", "cfg")
    from servers.common import flowedit_tools as fe
    out = fe.flow_of(functions=["real_fn", "NoSuchSymbol", "STAGED_CONST"])
    by = {o["function"]: o for o in out}
    assert by["real_fn"]["cfg"] is not None
    assert by["NoSuchSymbol"]["cfg"] is None      # graceful miss
    assert "could not resolve" in by["NoSuchSymbol"]["note"]


# -- flow_verify: the FINAL change vs the WHOLE plan ---------------------------
#
# flow_check verifies one function right after one edit; flow_verify re-checks
# EVERY planned delta against the FINAL source and lists edits made OUTSIDE
# the plan (via git diff vs HEAD -- all benchmark worktrees are git checkouts
# whose HEAD is the pre-edit base snapshot).


def _git(repo, *args):
    import subprocess
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True)


def _make_git_two_fn_proj(tmp_path, monkeypatch, name):
    """A tiny git repo with two indexed functions, env pointed at it."""
    repo = tmp_path / name
    repo.mkdir()
    (repo / "m.py").write_text(
        "def fn_a(x):\n"
        "    y = compute(x)\n"
        "    return y\n"
        "\n"
        "def fn_b(z):\n"
        "    w = z * 2\n"
        "    return w\n",
        encoding="utf-8")
    from indexer.backends import get_backend
    idx = repo / ".repr_index"
    idx.mkdir()
    get_backend("symbols").build(repo, idx)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "base")
    monkeypatch.setenv("REPO_ROOT", str(repo))
    monkeypatch.setenv("REPO_INDEX_DIR", str(idx))
    monkeypatch.setenv("RUN_DIR", str(tmp_path / (name + "_run")))
    monkeypatch.setenv("REPR_PERREP_REP", "cfg")
    return repo


def test_flow_verify_no_plan(tmp_path, monkeypatch):
    monkeypatch.setenv("RUN_DIR", str(tmp_path / "emptyrun"))
    monkeypatch.setenv("REPR_PERREP_REP", "cfg")
    from servers.common import flowedit_tools as fe
    out = fe.flow_verify()[0]
    assert out["match"] is None
    assert "no flow_plan" in out["note"]


def test_flow_verify_final_pass(tmp_path, monkeypatch, built_index):
    """Plan add_guard on fn_a, realize it, touch nothing else -> PASS."""
    repo = _make_git_two_fn_proj(tmp_path, monkeypatch, "fvpass")
    from servers.common import flowedit_tools as fe
    fe.flow_plan(deltas=[{"op": "add_guard", "function": "fn_a",
                          "anchor": "m.py:1", "rationale": "guard None"}])
    m = repo / "m.py"
    m.write_text(m.read_text().replace(
        "def fn_a(x):\n",
        "def fn_a(x):\n    if x is None:\n        return None\n"))
    out = fe.flow_verify()[0]
    assert out["planned_deltas"] == 1
    assert out["results"][0]["match"] is True, out
    assert out["all_planned_match"] is True
    assert out["unplanned_edits"] == [], out
    assert "PASSED" in out["note"]


def test_flow_verify_flags_unplanned_edit(tmp_path, monkeypatch, built_index):
    """An edit in a function the plan never mentioned is reported."""
    repo = _make_git_two_fn_proj(tmp_path, monkeypatch, "fvunplanned")
    from servers.common import flowedit_tools as fe
    fe.flow_plan(deltas=[{"op": "add_guard", "function": "fn_a",
                          "anchor": "m.py:1", "rationale": "guard None"}])
    m = repo / "m.py"
    src = m.read_text()
    src = src.replace("def fn_a(x):\n",
                      "def fn_a(x):\n    if x is None:\n        return None\n")
    # unplanned: also add a guard to fn_b
    src = src.replace("def fn_b(z):\n",
                      "def fn_b(z):\n    if z is None:\n        return None\n")
    m.write_text(src)
    out = fe.flow_verify()[0]
    assert out["results"][0]["match"] is True
    assert out["all_planned_match"] is True
    names = {u["function"] for u in out["unplanned_edits"]}
    assert names == {"fn_b"}, out["unplanned_edits"]
    assert "outside the plan" in out["note"]

    # git-level detection saw the file
    from servers.common import flowedit
    ranges = flowedit.changed_line_ranges()
    assert "m.py" in ranges and ranges["m.py"]


def test_flow_verify_detects_unrealized_plan(tmp_path, monkeypatch,
                                             built_index):
    """A planned delta the final code never realized -> match False (the
    'later edit undid the change' / 'never made the change' final signal)."""
    _make_git_two_fn_proj(tmp_path, monkeypatch, "fvunreal")
    from servers.common import flowedit_tools as fe
    fe.flow_plan(deltas=[{"op": "add_guard", "function": "fn_a",
                          "anchor": "m.py:1", "rationale": "guard None"}])
    # no edit at all
    out = fe.flow_verify()[0]
    assert out["results"][0]["match"] is False
    assert out["results"][0]["observed_op"] == "no_flow_change"
    assert out["all_planned_match"] is False
    assert "ISSUES" in out["note"]


def test_flow_verify_works_without_git(tmp_path, monkeypatch, built_index):
    """A non-git repo still verifies planned deltas; unplanned detection just
    degrades to empty (measurement never crashes the tool)."""
    repo = tmp_path / "nogit"
    repo.mkdir()
    (repo / "m.py").write_text(
        "def real_fn(x):\n    if x:\n        return 1\n    return 0\n",
        encoding="utf-8")
    from indexer.backends import get_backend
    idx = repo / ".repr_index"
    idx.mkdir()
    get_backend("symbols").build(repo, idx)
    monkeypatch.setenv("REPO_ROOT", str(repo))
    monkeypatch.setenv("REPO_INDEX_DIR", str(idx))
    monkeypatch.setenv("RUN_DIR", str(tmp_path / "nogit_run"))
    monkeypatch.setenv("REPR_PERREP_REP", "cfg")
    from servers.common import flowedit_tools as fe
    fe.flow_plan(deltas=[{"op": "no_flow_change", "function": "real_fn",
                          "anchor": "m.py:1", "rationale": "const"}])
    out = fe.flow_verify()[0]
    assert out["results"][0]["match"] is True
    assert out["unplanned_edits"] == []
    assert out["all_planned_match"] is True


def test_flow_verify_deleted_function_attributed_presides(tmp_path,
                                                          monkeypatch,
                                                          built_index):
    """Deleting a WHOLE function is attributed to THAT function via the
    PRE-edit (HEAD) side -- not to the surviving neighbor (review finding:
    pure deletions used to anchor to the preceding function and vanish)."""
    repo = _make_git_two_fn_proj(tmp_path, monkeypatch, "fvdel")
    from servers.common import flowedit_tools as fe
    fe.flow_plan(deltas=[{"op": "add_guard", "function": "fn_a",
                          "anchor": "m.py:1", "rationale": "guard None"}])
    m = repo / "m.py"
    src = m.read_text()
    src = src.replace("def fn_a(x):\n",
                      "def fn_a(x):\n    if x is None:\n        return None\n")
    # unplanned: delete fn_b ENTIRELY
    src = src[: src.index("def fn_b")].rstrip() + "\n"
    m.write_text(src)
    out = fe.flow_verify()[0]
    assert out["results"][0]["match"] is True
    dels = [u for u in out["unplanned_edits"] if u["function"] == "fn_b"]
    assert dels and "deleted" in dels[0]["lines"], out["unplanned_edits"]


def test_flow_verify_module_level_edit_reported_not_fatal(tmp_path,
                                                          monkeypatch,
                                                          built_index):
    """A module-level edit (e.g. an added import) in the SAME file as the
    planned function edit is REPORTED (review finding: it used to be silently
    dropped) but does not fail the verdict -- support edits are common."""
    repo = _make_git_two_fn_proj(tmp_path, monkeypatch, "fvmod")
    from servers.common import flowedit_tools as fe
    fe.flow_plan(deltas=[{"op": "add_guard", "function": "fn_a",
                          "anchor": "m.py:1", "rationale": "guard None"}])
    m = repo / "m.py"
    src = m.read_text()
    src = src.replace("def fn_a(x):\n",
                      "import os\n\n\ndef fn_a(x):\n"
                      "    if x is None:\n        return None\n")
    m.write_text(src)
    out = fe.flow_verify()[0]
    assert out["results"][0]["match"] is True
    mod = [u for u in out["unplanned_edits"] if u["function"] is None]
    assert mod, out["unplanned_edits"]           # the import IS reported
    assert "PASSED" in out["note"]               # ...but not verdict-fatal
    assert "module-level" in out["note"]


def test_unplanned_edits_range_spanning_two_functions(tmp_path, monkeypatch,
                                                      built_index):
    """One contiguous hunk touching the tail of fn_a and the head of fn_b
    reports BOTH functions (review finding: only one winner per range)."""
    repo = _make_git_two_fn_proj(tmp_path, monkeypatch, "fvspan")
    m = repo / "m.py"
    src = m.read_text()
    src = src.replace("    return y\n\ndef fn_b(z):\n",
                      "    return y + 1\n\ndef fn_b(z, w=0):\n")
    # the blank separator line is unchanged, producing two hunks -- rewrite
    # it too so the changes are one contiguous block spanning both defs.
    m.write_text(src.replace("    return y + 1\n\n",
                             "    return y + 1\n# bridge\n"))
    from servers.common import flowedit
    names = {u["function"]
             for u in flowedit.unplanned_edits({"deltas": []})}
    assert {"fn_a", "fn_b"} <= names, names


def test_delta_anchor_nonnumeric_is_not_wildcard(tmp_path, monkeypatch,
                                                 built_index):
    """A malformed anchor like 'm.py:helper' must not mask every edit in that
    file as planned (review finding: ValueError used to return True)."""
    repo = _make_git_two_fn_proj(tmp_path, monkeypatch, "fvwild")
    from servers.common import flowedit_tools as fe
    fe.flow_plan(deltas=[{"op": "add_guard", "function": "fn_a",
                          "anchor": "m.py:not_a_line", "rationale": "x"}])
    m = repo / "m.py"
    m.write_text(m.read_text().replace(
        "def fn_b(z):\n",
        "def fn_b(z):\n    if z is None:\n        return None\n"))
    out = fe.flow_verify()[0]
    names = {u["function"] for u in out["unplanned_edits"]}
    assert "fn_b" in names, out["unplanned_edits"]


def test_flow_check_no_flow_change_actually_verified(tmp_path, monkeypatch,
                                                     built_index):
    """no_flow_change deltas are snapshotted (review finding: they never
    were, so their PASS was vacuous): an edit that DOES change flow on a
    no_flow_change plan now reports match=False."""
    repo = _make_git_two_fn_proj(tmp_path, monkeypatch, "fvnfc")
    from servers.common import flowedit_tools as fe
    res = fe.flow_plan(deltas=[{"op": "no_flow_change", "function": "fn_a",
                                "anchor": "m.py:1", "rationale": "const"}])
    assert "fn_a" in res[0]["snapshotted_functions"], res
    m = repo / "m.py"
    m.write_text(m.read_text().replace(
        "def fn_a(x):\n",
        "def fn_a(x):\n    if x is None:\n        return None\n"))
    chk = fe.flow_check(function="fn_a")[0]
    assert chk["match"] is False, chk
    assert chk["observed"] == "flow_changed"


def test_flow_check_survives_line_drift(tmp_path, monkeypatch, built_index):
    """Regression for the qutebrowser-0aa57e4f verifier artifact: support
    edits ABOVE the planned function (imports/docstrings/tables) shift its
    def past the +-3 line tolerance of the stale symbol-index address, and
    flow_check used to give up with 'cannot compute the edited function's
    graph'. The by-NAME fallback must find the drifted function and return a
    real verdict."""
    repo = _make_git_two_fn_proj(tmp_path, monkeypatch, "drift")
    from servers.common import flowedit_tools as fe
    fe.flow_plan(deltas=[{"op": "add_guard", "function": "fn_b",
                          "anchor": "m.py:5", "rationale": "guard None"}])
    m = repo / "m.py"
    src = m.read_text()
    # 30 lines of module-level drift above everything + the planned guard
    src = ("# padding\n" * 30) + src.replace(
        "def fn_b(z):\n",
        "def fn_b(z):\n    if z is None:\n        return None\n")
    m.write_text(src)
    chk = fe.flow_check(function="fn_b")[0]
    assert chk["match"] is True, chk       # was: match None, "cannot compute"
    assert chk.get("observed_op") == "add_guard"
    out = fe.flow_verify()[0]
    assert out["results"][0]["match"] is True, out


def test_flow_check_line_drift_reports_real_verdict_on_miss(tmp_path,
                                                            monkeypatch,
                                                            built_index):
    """Drift + the planned change NOT made: the verdict must be a real
    mismatch (planned add_guard, observed no_flow_change), not an
    unverifiable 'cannot compute'."""
    repo = _make_git_two_fn_proj(tmp_path, monkeypatch, "driftmiss")
    from servers.common import flowedit_tools as fe
    fe.flow_plan(deltas=[{"op": "add_guard", "function": "fn_b",
                          "anchor": "m.py:5", "rationale": "guard None"}])
    m = repo / "m.py"
    m.write_text(("# padding\n" * 30) + m.read_text())   # drift, no guard
    chk = fe.flow_check(function="fn_b")[0]
    assert chk["match"] is False, chk
    assert chk.get("observed_op") == "no_flow_change"


# -- combined cfg+dfg arm (cfgdfg): both graphs, dual op vocab ----------------


def test_valid_ops_cfgdfg_has_both_vocabularies():
    ops = flowedit.valid_ops("cfgdfg")
    assert "add_guard" in ops and "add_def_use" in ops
    assert "no_flow_change" in ops
    assert flowedit.rep_for_op("add_guard") == "cfg"
    assert flowedit.rep_for_op("move_def_use") == "dfg"
    assert flowedit.rep_for_op("no_flow_change") is None


def test_cfgdfg_flow_of_renders_both_graphs(tmp_path, monkeypatch,
                                            built_index):
    repo = _make_git_two_fn_proj(tmp_path, monkeypatch, "bothview")
    monkeypatch.setenv("REPR_PERREP_REP", "cfgdfg")
    from servers.common import flowedit_tools as fe
    out = fe.flow_of(functions=["fn_a"])
    assert len(out) == 1 and out[0]["cfg"]
    assert "control-flow graph of fn_a()" in out[0]["cfg"]
    assert "value-flow (def-use) graph of fn_a()" in out[0]["cfg"]


def test_cfgdfg_plan_check_verify_roundtrip(tmp_path, monkeypatch,
                                            built_index):
    """Combined arm end-to-end: a cfg op and a dfg op in ONE plan, each
    verified against ITS graph; flow_verify passes on the final code."""
    repo = _make_git_two_fn_proj(tmp_path, monkeypatch, "bothrt")
    monkeypatch.setenv("REPR_PERREP_REP", "cfgdfg")
    from servers.common import flowedit_tools as fe
    res = fe.flow_plan(deltas=[
        {"op": "add_guard", "function": "fn_a", "anchor": "m.py:1",
         "rationale": "guard None"},
        {"op": "add_def_use", "function": "fn_b", "anchor": "m.py:5",
         "rationale": "derive checked value"},
    ])[0]
    assert res["recorded"] == 2
    assert set(res["snapshotted_functions"]) == {"fn_a", "fn_b"}
    m = repo / "m.py"
    src = m.read_text()
    src = src.replace("def fn_a(x):\n",
                      "def fn_a(x):\n    if x is None:\n        return None\n")
    # additive dfg edit: a NEW def-and-use of w inserted BEFORE the return
    # (removing/reordering existing pairs would classify as move_def_use).
    src = src.replace("def fn_b(z):\n    w = z * 2\n    return w\n",
                      "def fn_b(z):\n    w = z * 2\n    v = w + 1\n"
                      "    return w\n")
    m.write_text(src)
    cfg_chk = fe.flow_check(function="fn_a")[0]
    assert cfg_chk["match"] is True, cfg_chk
    assert cfg_chk.get("graph") == "cfg"
    dfg_chk = fe.flow_check(function="fn_b")[0]
    assert dfg_chk["match"] is True, dfg_chk
    assert dfg_chk.get("graph") == "dfg"
    out = fe.flow_verify()[0]
    assert out["all_planned_match"] is True, out
    assert [u for u in out["unplanned_edits"] if u["function"]] == []


def test_cfgdfg_no_flow_change_checks_both_graphs(tmp_path, monkeypatch,
                                                  built_index):
    """Combined no_flow_change: an edit that changes EITHER graph fails."""
    repo = _make_git_two_fn_proj(tmp_path, monkeypatch, "bothnfc")
    monkeypatch.setenv("REPR_PERREP_REP", "cfgdfg")
    from servers.common import flowedit_tools as fe
    fe.flow_plan(deltas=[{"op": "no_flow_change", "function": "fn_b",
                          "anchor": "m.py:5", "rationale": "const"}])
    ok = fe.flow_check(function="fn_b")[0]
    assert ok["match"] is True                       # untouched -> unchanged
    m = repo / "m.py"
    m.write_text(m.read_text().replace(
        "def fn_b(z):\n    w = z * 2\n",
        "def fn_b(z):\n    zz = abs(z)\n    w = zz * 2\n"))
    bad = fe.flow_check(function="fn_b")[0]
    assert bad["match"] is False, bad
    assert "graph CHANGED" in bad["note"]


# -- flow3 (v3 arm, REPR_FLOW_V3=1): add_function, fault_site, HEAD ----------
# -- snapshots, merge-on-replan, graded verdicts ------------------------------


def test_v3_valid_ops_add_function(monkeypatch):
    """add_function is a v3-only UNIVERSAL op (cfg, dfg AND cfgdfg)."""
    monkeypatch.delenv("REPR_FLOW_V3", raising=False)
    for rep in ("cfg", "dfg", "cfgdfg"):
        assert "add_function" not in flowedit.valid_ops(rep)
    monkeypatch.setenv("REPR_FLOW_V3", "1")
    for rep in ("cfg", "dfg", "cfgdfg"):
        assert "add_function" in flowedit.valid_ops(rep)


def test_v3_op_verdict_table():
    """The ONE graded-verdict function: exact / symmetric same-family /
    mismatch / unknown."""
    ov = flowedit.op_verdict
    assert ov("add_guard", "add_guard") == "exact"
    assert ov("no_flow_change", "no_flow_change") == "exact"
    assert ov("add_function", "add_function") == "exact"
    for a, b in [("add_guard", "change_block_structure"),
                 ("remove_branch", "change_block_structure"),
                 ("rewire_edge", "change_block_structure"),
                 ("change_condition", "rewire_edge"),
                 ("change_condition", "no_flow_change"),
                 ("add_def_use", "move_def_use"),
                 ("remove_def_use", "move_def_use")]:
        assert ov(a, b) == "same_family", (a, b)
        assert ov(b, a) == "same_family", (b, a)     # symmetric
    assert ov("add_guard", "remove_branch") == "mismatch"
    assert ov("add_guard", "move_def_use") == "mismatch"
    # add_function is exact-only: no family partner in either direction
    assert ov("add_function", "no_flow_change") == "mismatch"
    assert ov("add_guard", "add_function") == "mismatch"
    assert ov("add_guard", None) == "unknown"
    assert ov(None, "add_guard") == "unknown"
    assert ov("", "add_guard") == "unknown"


def test_v3_fault_site_normalize():
    ok, err = flowedit.normalize_fault_site("pkg/mod.py:88")
    assert err is None and ok == {"file": "pkg/mod.py", "line": 88}
    ok, err = flowedit.normalize_fault_site(
        {"file": "a.c", "line": "12", "kind": "guard", "var": "n"})
    assert err is None
    assert ok == {"file": "a.c", "line": 12, "kind": "guard", "var": "n"}
    # missing (None / blank) is NOT an error -- just not given
    assert flowedit.normalize_fault_site(None) == (None, None)
    assert flowedit.normalize_fault_site("  ") == (None, None)
    for bad in ("no_line_here", "x.py:notaline", ":12", 42, ["x.py", 3],
                {"line": 3}, {"file": "x.py"}, {"file": "x.py", "line": "z"}):
        norm, err = flowedit.normalize_fault_site(bad)
        assert norm is None and err, bad


def test_v3_fault_site_recorded_validated_and_warned(tmp_path, monkeypatch,
                                                     built_index):
    """fault_site is normalized + stored per delta with fault_site_valid
    True/False/None; missing/invalid NEVER rejects the delta -- exactly ONE
    nudge warning per response."""
    _make_git_two_fn_proj(tmp_path, monkeypatch, "v3fs")
    monkeypatch.setenv("REPR_FLOW_V3", "1")
    from servers.common import flowedit_tools as fe
    res = fe.flow_plan(deltas=[
        {"op": "add_guard", "function": "fn_a", "anchor": "m.py:1",
         "rationale": "g", "fault_site": "m.py:2"},
        {"op": "no_flow_change", "function": "fn_b", "anchor": "m.py:5",
         "rationale": "c", "fault_site": "m.py:999"},
    ])[0]
    assert res["recorded"] == 2          # invalid fault_site never rejects
    plan = flowedit.load_plan()
    d_a, d_b = plan["deltas"]
    assert d_a["fault_site"] == {"file": "m.py", "line": 2}
    assert d_a["fault_site_valid"] is True
    assert d_b["fault_site"] == {"file": "m.py", "line": 999}
    assert d_b["fault_site_valid"] is False
    assert any("fault_site" in w for w in res["warnings"])
    # missing on BOTH deltas -> exactly ONE nudge warning, deltas recorded
    res2 = fe.flow_plan(deltas=[
        {"op": "add_guard", "function": "fn_a", "anchor": "m.py:1",
         "rationale": "g"},
        {"op": "no_flow_change", "function": "fn_b", "anchor": "m.py:5",
         "rationale": "c"},
    ])[0]
    assert res2["recorded"] == 2
    assert sum("fault_site missing" in w for w in res2["warnings"]) == 1
    for d in flowedit.load_plan()["deltas"]:
        assert d["fault_site"] is None
        assert d["fault_site_valid"] is None
    # malformed fault_site -> stored as null + warned, still recorded
    res3 = fe.flow_plan(deltas=[
        {"op": "add_guard", "function": "fn_a", "anchor": "m.py:1",
         "rationale": "g", "fault_site": "no_line_here"}])[0]
    assert res3["recorded"] == 1
    assert any("fault_site problem" in w for w in res3["warnings"])
    d = next(x for x in flowedit.load_plan()["deltas"]
             if x["function"] == "fn_a")           # merge keeps fn_b first
    assert d["fault_site"] is None
    # wrong FILE -> invalid
    fe.flow_plan(deltas=[
        {"op": "add_guard", "function": "fn_a", "anchor": "m.py:1",
         "rationale": "g", "fault_site": "other.py:2"}])
    d = next(x for x in flowedit.load_plan()["deltas"]
             if x["function"] == "fn_a")
    assert d["fault_site_valid"] is False, d


def test_v3_head_snapshot_kills_rebaseline_dodge(tmp_path, monkeypatch,
                                                 built_index):
    """v3 snapshots fingerprint the function AS OF GIT HEAD: planning AFTER
    the edit can no longer re-baseline the snapshot to the edited source
    (v2's dodge: plan-after-edit -> observed no_flow_change -> free pass)."""
    repo = _make_git_two_fn_proj(tmp_path, monkeypatch, "v3head")
    monkeypatch.setenv("REPR_FLOW_V3", "1")
    from servers.common import flowedit_tools as fe
    # edit FIRST: add the guard before any plan exists
    m = repo / "m.py"
    m.write_text(m.read_text().replace(
        "def fn_a(x):\n",
        "def fn_a(x):\n    if x is None:\n        return None\n"))
    # plan AFTER the edit -- the snapshot must be the HEAD (pre-edit) graph
    fe.flow_plan(deltas=[{"op": "add_guard", "function": "fn_a",
                          "anchor": "m.py:1", "rationale": "guard None",
                          "fault_site": "m.py:2"}])
    chk = fe.flow_check(function="fn_a")[0]
    assert chk["match"] is True, chk
    assert chk["observed_op"] == "add_guard"
    assert chk["verdict"] == "exact"
    out = fe.flow_verify()[0]
    assert out["all_planned_match"] is True, out


def test_v3_replan_merges_by_function_with_history(tmp_path, monkeypatch,
                                                   built_index):
    """A second flow_plan MERGES by function name (replaced function's deltas
    swapped out, others preserved); revision increments and history keeps the
    superseded delta-lists. replace=True clears the previous deltas."""
    _make_git_two_fn_proj(tmp_path, monkeypatch, "v3merge")
    monkeypatch.setenv("REPR_FLOW_V3", "1")
    from servers.common import flowedit_tools as fe
    fe.flow_plan(deltas=[
        {"op": "add_guard", "function": "fn_a", "anchor": "m.py:1",
         "rationale": "g", "fault_site": "m.py:2"},
        {"op": "no_flow_change", "function": "fn_b", "anchor": "m.py:5",
         "rationale": "c", "fault_site": "m.py:6"},
    ])
    plan = flowedit.load_plan()
    assert plan["revision"] == 1 and plan["history"] == []
    # re-plan fn_b only: fn_a's delta is PRESERVED, fn_b's replaced
    res = fe.flow_plan(deltas=[
        {"op": "add_guard", "function": "fn_b", "anchor": "m.py:5",
         "rationale": "guard after all", "fault_site": "m.py:6"}])[0]
    assert res["revision"] == 2
    assert res["planned_functions"] == ["fn_a", "fn_b"]
    plan = flowedit.load_plan()
    assert plan["revision"] == 2
    assert [(d["function"], d["op"]) for d in plan["deltas"]] == \
        [("fn_a", "add_guard"), ("fn_b", "add_guard")]
    assert len(plan["history"]) == 1
    assert [d["function"] for d in plan["history"][0]] == ["fn_a", "fn_b"]
    assert set(plan["snapshots"]) == {"fn_a", "fn_b"}
    # replace=True: previous deltas discarded, trail kept
    fe.flow_plan_v3(deltas=[
        {"op": "no_flow_change", "function": "fn_a", "anchor": "m.py:1",
         "rationale": "const", "fault_site": "m.py:2"}], replace=True)
    plan = flowedit.load_plan()
    assert plan["revision"] == 3
    assert [(d["function"], d["op"]) for d in plan["deltas"]] == \
        [("fn_a", "no_flow_change")]
    assert set(plan["snapshots"]) == {"fn_a"}
    assert len(plan["history"]) == 2


def test_v3_add_function_plan_edit_check_roundtrip(tmp_path, monkeypatch,
                                                   built_index):
    """add_function: expected_new recorded at plan time (no warning), check
    is mismatch while the function is absent, exact once it exists in the
    CURRENT source (index-bypassing extraction) but not at HEAD."""
    repo = _make_git_two_fn_proj(tmp_path, monkeypatch, "v3addfn")
    monkeypatch.setenv("REPR_FLOW_V3", "1")
    from servers.common import flowedit_tools as fe
    res = fe.flow_plan(deltas=[
        {"op": "add_function", "function": "fn_new", "anchor": "m.py:8",
         "rationale": "extract the guard helper",
         "fault_site": "m.py:8"}])[0]
    assert res["recorded"] == 1
    plan = flowedit.load_plan()
    assert plan["deltas"][0]["expected_new"] is True
    assert plan["snapshots"] == {}               # null snapshot by design
    assert not any("HEAD" in w for w in res["warnings"])
    # not written yet -> honest mismatch, not a vacuous None
    chk = fe.flow_check(function="fn_new")[0]
    assert chk["match"] is False, chk
    assert chk["verdict"] == "mismatch"
    assert chk["observed_op"] == "function_missing"
    # write the new function (the symbol index does NOT know it)
    m = repo / "m.py"
    m.write_text(m.read_text() + "\ndef fn_new(a):\n"
                                 "    if a is None:\n        return 0\n"
                                 "    return a * 3\n")
    chk = fe.flow_check(function="fn_new")[0]
    assert chk["match"] is True, chk
    assert chk["verdict"] == "exact"
    assert chk["observed_op"] == "add_function"
    out = fe.flow_verify()[0]
    assert out["all_planned_match"] is True, out
    assert [u for u in out["unplanned_edits"] if u["function"]] == []


def test_v3_add_function_new_file_and_preexisting(tmp_path, monkeypatch,
                                                  built_index):
    """File absent at HEAD: expected_new=True, NO warning; verification
    passes once the new file carries the function. A function that already
    exists at HEAD is warned at plan time and mismatches at check time."""
    repo = _make_git_two_fn_proj(tmp_path, monkeypatch, "v3addf2")
    monkeypatch.setenv("REPR_FLOW_V3", "1")
    from servers.common import flowedit_tools as fe
    res = fe.flow_plan(deltas=[
        {"op": "add_function", "function": "brand_new",
         "anchor": "newmod.py:1", "rationale": "new helper module",
         "fault_site": "newmod.py:1"}])[0]
    plan = flowedit.load_plan()
    assert plan["deltas"][0]["expected_new"] is True
    assert res["warnings"] == []                 # file absent in HEAD: silent
    (repo / "newmod.py").write_text(
        "def brand_new(x):\n    if x:\n        return 1\n    return 0\n",
        encoding="utf-8")
    chk = fe.flow_check(function="brand_new")[0]
    assert chk["match"] is True and chk["verdict"] == "exact", chk
    # add_function on a function that ALREADY exists at HEAD
    res = fe.flow_plan(deltas=[
        {"op": "add_function", "function": "fn_b", "anchor": "m.py:5",
         "rationale": "oops", "fault_site": "m.py:5"}])[0]
    assert flowedit.load_plan()["deltas"][-1]["expected_new"] is False
    assert any("already exists at HEAD" in w for w in res["warnings"])
    chk = fe.flow_check(function="fn_b")[0]
    assert chk["match"] is False and chk["verdict"] == "mismatch", chk
    assert "already existed at HEAD" in chk["note"]


def test_v3_c_func_source_by_name_extraction():
    """The index-bypassing C extraction: finds a definition (not the
    prototype, not a call site), brace-balanced; returns None on a miss."""
    text = (
        "#include <stdio.h>\n"
        "int helper(char *p, int n);\n"          # prototype -- must skip
        "static int other(void) {\n"
        "    return helper(0, 0);\n"             # call site -- must skip
        "}\n"
        "int helper(char *p, int n) {\n"
        "    if (p == 0) {\n"
        "        return -1;\n"
        "    }\n"
        "    return n;\n"
        "}\n")
    src = flowedit.c_func_source_by_name(text, "helper")
    assert src is not None
    assert src.startswith("int helper(char *p, int n) {")
    assert src.rstrip().endswith("}")
    assert "if (p == 0)" in src
    assert flowedit.c_func_source_by_name(text, "nonexistent") is None


def test_v3_same_family_verdict_change_condition(tmp_path, monkeypatch,
                                                 built_index):
    """A predicate-only edit leaves the fingerprint unchanged (conditions
    carry no text), so planned change_condition observes no_flow_change --
    v2 called that a MISMATCH; v3 grades it same_family and passes."""
    repo = tmp_path / "v3fam"
    repo.mkdir()
    (repo / "b.py").write_text(
        "def picky(x):\n"
        "    if x > 0:\n"
        "        return 1\n"
        "    return 0\n",
        encoding="utf-8")
    from indexer.backends import get_backend
    idx = repo / ".repr_index"
    idx.mkdir()
    get_backend("symbols").build(repo, idx)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "base")
    monkeypatch.setenv("REPO_ROOT", str(repo))
    monkeypatch.setenv("REPO_INDEX_DIR", str(idx))
    monkeypatch.setenv("RUN_DIR", str(tmp_path / "v3fam_run"))
    monkeypatch.setenv("REPR_PERREP_REP", "cfg")
    monkeypatch.setenv("REPR_FLOW_V3", "1")
    from servers.common import flowedit_tools as fe
    fe.flow_plan(deltas=[{"op": "change_condition", "function": "picky",
                          "anchor": "b.py:2", "rationale": "off-by-one",
                          "fault_site": "b.py:2"}])
    d = flowedit.load_plan()["deltas"][0]
    assert d["fault_site_valid"] is True
    # cfg: the containing basic block is noted (engine exposes line ranges)
    assert d.get("fault_site_block") is not None
    b = repo / "b.py"
    b.write_text(b.read_text().replace("if x > 0:", "if x >= 0:"))
    chk = fe.flow_check(function="picky")[0]
    assert chk["verdict"] == "same_family", chk
    assert chk["match"] is True
    assert chk["observed_op"] == "no_flow_change"
    assert "family pass" in chk["note"]
    out = fe.flow_verify()[0]
    assert out["all_planned_match"] is True, out
    assert out["results"][0]["verdict"] == "same_family"


def test_v3_cfgdfg_graded_verdicts(tmp_path, monkeypatch, built_index):
    """Combined arm under v3: per-graph verification keeps working and the
    results carry graded verdicts (exact on a realized cfg delta)."""
    repo = _make_git_two_fn_proj(tmp_path, monkeypatch, "v3both")
    monkeypatch.setenv("REPR_PERREP_REP", "cfgdfg")
    monkeypatch.setenv("REPR_FLOW_V3", "1")
    from servers.common import flowedit_tools as fe
    fe.flow_plan(deltas=[
        {"op": "add_guard", "function": "fn_a", "anchor": "m.py:1",
         "rationale": "guard None", "fault_site": "m.py:2"}])
    m = repo / "m.py"
    m.write_text(m.read_text().replace(
        "def fn_a(x):\n",
        "def fn_a(x):\n    if x is None:\n        return None\n"))
    chk = fe.flow_check(function="fn_a")[0]
    assert chk["match"] is True and chk["verdict"] == "exact", chk
    assert chk.get("graph") == "cfg"


# -- flow3 flow_locate: the SEPARATE step-2 tool (wrong-path localization) ----
#
# flow_locate(site, reason) makes step 2 a first-class, measurable call:
# normalize the site, resolve its containing function (the site's own file
# IS the anchor), validate line-in-extent + containing cfg block / dfg var,
# APPEND to RUN_DIR/flow_locate.json; flow_plan inherits the most recent
# record as a delta's fault_site (fault_site_source='flow_locate').


def test_v3_flow_locate_roundtrip_and_append(tmp_path, monkeypatch,
                                             built_index):
    """flow_locate resolves the containing function, validates the line,
    notes the cfg block, and APPENDS records to RUN_DIR/flow_locate.json."""
    import json as _json
    _make_git_two_fn_proj(tmp_path, monkeypatch, "v3loc")
    monkeypatch.setenv("REPR_FLOW_V3", "1")
    from servers.common import flowedit_tools as fe
    out = fe.flow_locate(site="m.py:2", reason="y returned unguarded")[0]
    assert out["valid"] is True
    assert out["located"]["function"].endswith("fn_a")
    assert out["located"]["file"] == "m.py"
    assert out["located"]["line"] == 2
    assert "block" in out["located"]      # cfg rep: containing basic block
    assert "inherit" in out["note"]
    # dict-form site + append semantics
    out2 = fe.flow_locate(site={"file": "m.py", "line": 6},
                          reason="w doubled too early")[0]
    assert out2["valid"] is True
    assert out2["located"]["function"].endswith("fn_b")
    recs = _json.loads((tmp_path / "v3loc_run" / "flow_locate.json")
                       .read_text(encoding="utf-8"))
    assert len(recs) == 2
    assert recs[0]["function"].endswith("fn_a")
    assert recs[0]["site"] == {"file": "m.py", "line": 2}
    assert recs[0]["reason"] == "y returned unguarded"
    assert recs[0]["valid"] is True
    assert recs[1]["function"].endswith("fn_b")
    assert flowedit.latest_locate()["function"].endswith("fn_b")


def test_v3_flow_locate_unresolvable_site_candidates(tmp_path, monkeypatch,
                                                     built_index):
    """A site outside every function returns the nearest candidates
    (function + line + first code line) with a disambiguation hint,
    valid=false; unknown files and malformed sites note gracefully.
    Never raises."""
    _make_git_two_fn_proj(tmp_path, monkeypatch, "v3locmiss")
    monkeypatch.setenv("REPR_FLOW_V3", "1")
    from servers.common import flowedit_tools as fe
    out = fe.flow_locate(site="m.py:999", reason="somewhere")[0]
    assert out["valid"] is False and out["located"] is None
    cands = out["candidates"]
    assert 1 <= len(cands) <= 3
    assert cands[0]["function"].endswith("fn_b")   # nearest to 999
    assert cands[0]["line"] == 5
    assert "def fn_b" in cands[0]["code"]
    assert "INSIDE" in out["note"]
    # the miss is still recorded (function null, valid false)
    rec = flowedit.latest_locate()
    assert rec["function"] is None and rec["valid"] is False
    # unknown file -> no candidates, graceful note
    out2 = fe.flow_locate(site="nope/void.py:3", reason="x")[0]
    assert out2["valid"] is False and out2["candidates"] == []
    assert "no indexed functions" in out2["note"]
    # malformed site -> error note, nothing persisted for it
    n = len(flowedit.load_locates())
    out3 = fe.flow_locate(site="no_line_here", reason="x")[0]
    assert out3["valid"] is False and out3["located"] is None
    assert "file.py:LINE" in out3["note"]
    assert len(flowedit.load_locates()) == n


def test_v3_flow_locate_dfg_var_note(tmp_path, monkeypatch, built_index):
    """dfg rep: a site carrying ``var`` is checked against the function's
    def-use graph (present = confirmation, absent = warning); no cfg block
    on the dfg record."""
    _make_git_two_fn_proj(tmp_path, monkeypatch, "v3locdfg")
    monkeypatch.setenv("REPR_PERREP_REP", "dfg")
    monkeypatch.setenv("REPR_FLOW_V3", "1")
    from servers.common import flowedit_tools as fe
    ok = fe.flow_locate(site={"file": "m.py", "line": 2, "var": "y"},
                        reason="y unguarded")[0]
    assert ok["valid"] is True
    assert "'y'" in ok["note"] and "def-use" in ok["note"]
    assert "block" not in ok["located"]
    bad = fe.flow_locate(site={"file": "m.py", "line": 2, "var": "zz"},
                         reason="wrong var")[0]
    assert "does NOT appear" in bad["note"]


def test_v3_flow_plan_inherits_flow_locate(tmp_path, monkeypatch,
                                           built_index):
    """A delta with NO fault_site inherits the MOST RECENT flow_locate
    record (fault_site_source='flow_locate') and is validated like an
    explicit one; an explicit fault_site overrides ('explicit'); with
    neither, the missing-fault_site nudge fires as before."""
    _make_git_two_fn_proj(tmp_path, monkeypatch, "v3inh")
    monkeypatch.setenv("REPR_FLOW_V3", "1")
    from servers.common import flowedit_tools as fe
    # no locate + no field -> the nudge (pre-locate behavior unchanged)
    res0 = fe.flow_plan(deltas=[{"op": "add_guard", "function": "fn_a",
                                 "anchor": "m.py:1", "rationale": "g"}])[0]
    assert sum("fault_site missing" in w for w in res0["warnings"]) == 1
    d = flowedit.load_plan()["deltas"][0]
    assert d["fault_site"] is None and "fault_site_source" not in d
    # locate, then plan WITHOUT fault_site -> inherited + source tag
    fe.flow_locate(site="m.py:2", reason="y returned unguarded")
    res = fe.flow_plan(deltas=[{"op": "add_guard", "function": "fn_a",
                                "anchor": "m.py:1", "rationale": "g"}])[0]
    assert not any("fault_site missing" in w for w in res["warnings"])
    d = next(x for x in flowedit.load_plan()["deltas"]
             if x["function"] == "fn_a")
    assert d["fault_site"] == {"file": "m.py", "line": 2}
    assert d["fault_site_source"] == "flow_locate"
    assert d["fault_site_valid"] is True
    # explicit fault_site OVERRIDES the locate record
    fe.flow_plan(deltas=[{"op": "no_flow_change", "function": "fn_b",
                          "anchor": "m.py:5", "rationale": "c",
                          "fault_site": "m.py:6"}])
    d = next(x for x in flowedit.load_plan()["deltas"]
             if x["function"] == "fn_b")
    assert d["fault_site"] == {"file": "m.py", "line": 6}
    assert d["fault_site_source"] == "explicit"
    # an INHERITED site still validates: fn_a's spot inherited onto fn_b
    # falls outside fn_b's extent -> invalid + the usual problem warning
    res2 = fe.flow_plan(deltas=[{"op": "add_guard", "function": "fn_b",
                                 "anchor": "m.py:5", "rationale": "g"}])[0]
    d = next(x for x in flowedit.load_plan()["deltas"]
             if x["function"] == "fn_b")
    assert d["fault_site_source"] == "flow_locate"
    assert d["fault_site_valid"] is False
    assert any("fault_site problem" in w for w in res2["warnings"])
    # a MALFORMED explicit fault_site keeps its own invalid warning --
    # it must NOT silently inherit the locate record
    res3 = fe.flow_plan(deltas=[{"op": "add_guard", "function": "fn_a",
                                 "anchor": "m.py:1", "rationale": "g",
                                 "fault_site": "no_line_here"}])[0]
    d = next(x for x in flowedit.load_plan()["deltas"]
             if x["function"] == "fn_a")
    assert d["fault_site"] is None and "fault_site_source" not in d
    assert any("fault_site problem" in w for w in res3["warnings"])


def test_v2_parity_no_v3_fields_and_overwrite_replan(tmp_path, monkeypatch,
                                                     built_index):
    """PARITY GUARD: with REPR_FLOW_V3 unset, plan/check results and the
    stored plan carry NO v3 fields (verdict/fault_site/revision/history/
    expected_new), replan OVERWRITES, add_function is rejected, and the
    plan-after-edit rebaseline dodge behaves exactly as v2 did."""
    repo = _make_git_two_fn_proj(tmp_path, monkeypatch, "v2parity")
    monkeypatch.delenv("REPR_FLOW_V3", raising=False)
    from servers.common import flowedit_tools as fe
    res = fe.flow_plan(deltas=[{"op": "add_guard", "function": "fn_a",
                                "anchor": "m.py:1", "rationale": "g",
                                "fault_site": "m.py:2"}])[0]
    assert "revision" not in res and "planned_functions" not in res
    plan = flowedit.load_plan()
    assert set(plan) == {"rep", "deltas", "snapshots"}
    assert set(plan["deltas"][0]) == {"op", "function", "anchor", "rationale"}
    # v2 replan = full OVERWRITE (no merge, no history)
    fe.flow_plan(deltas=[{"op": "no_flow_change", "function": "fn_b",
                          "anchor": "m.py:5", "rationale": "c"}])
    plan = flowedit.load_plan()
    assert [d["function"] for d in plan["deltas"]] == ["fn_b"]
    assert "revision" not in plan and "history" not in plan
    chk = fe.flow_check(function="fn_b")[0]
    assert "verdict" not in chk
    # add_function is NOT a v2 op
    with pytest.raises(ValueError):
        fe.flow_plan(deltas=[{"op": "add_function", "function": "fn_new",
                              "anchor": "m.py:8", "rationale": "x"}])
    # v2 rebaseline dodge UNCHANGED: edit then plan snapshots the edited
    # disk, so the realized guard reads as no_flow_change (mismatch)
    m = repo / "m.py"
    m.write_text(m.read_text().replace(
        "def fn_a(x):\n",
        "def fn_a(x):\n    if x is None:\n        return None\n"))
    fe.flow_plan(deltas=[{"op": "add_guard", "function": "fn_a",
                          "anchor": "m.py:1", "rationale": "g"}])
    chk = fe.flow_check(function="fn_a")[0]
    assert chk["match"] is False and chk["observed_op"] == "no_flow_change"
    assert "verdict" not in chk


# -- flow3 fix 2 (file-scoped resolution) + fix 3 (comprehension signature) ---
#
# Fix 2: bare-name resolution used the GLOBAL first match, so flow_check
# fingerprinted a same-named function in an unrelated file (qutebrowser:
# '_on_finished' resolved to notification.py's instead of guiprocess.py's)
# -> false "planned add_guard / observed no_flow_change" mismatches that
# taught agents to re-plan as no_flow_change. v3 scopes resolution to the
# delta's anchor FILE first; flow_of lists multi-file candidates in a note.
# Fix 3: a comprehension-internal rewrite (openlibrary read_publisher) kept
# the block CFG identical -> v2 fingerprinted no_flow_change and an honest
# change_block_structure plan read as MISMATCH; v3 appends a comprehension
# signature to the CFG fingerprint and classifies it change_block_structure.


def _make_git_dupname_proj(tmp_path, monkeypatch, name):
    """Two SAME-NAMED functions in DIFFERENT files (the qutebrowser
    '_on_finished' shape): the global resolver's first pick is a.py's
    (branchy); the cell's actual work is on b.py's (flat)."""
    repo = tmp_path / name
    repo.mkdir()
    (repo / "a.py").write_text(
        "def _on_finished(x):\n"
        "    if x:\n"
        "        return 1\n"
        "    return 0\n"
        "\n"
        "def solo_fn(y):\n"
        "    if y:\n"
        "        return y\n"
        "    return None\n",
        encoding="utf-8")
    (repo / "b.py").write_text(
        "def _on_finished(z):\n"
        "    w = z * 2\n"
        "    return w\n",
        encoding="utf-8")
    from indexer.backends import get_backend
    idx = repo / ".repr_index"
    idx.mkdir()
    get_backend("symbols").build(repo, idx)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "base")
    monkeypatch.setenv("REPO_ROOT", str(repo))
    monkeypatch.setenv("REPO_INDEX_DIR", str(idx))
    monkeypatch.setenv("RUN_DIR", str(tmp_path / (name + "_run")))
    monkeypatch.setenv("REPR_PERREP_REP", "cfg")
    return repo


def test_v3_flow_check_scopes_resolution_to_anchor_file(tmp_path, monkeypatch,
                                                        built_index):
    """Fix 2a: '_on_finished' anchored in b.py must snapshot AND re-fingerprint
    b.py's function, so the realized guard verifies exact (the diagnosed
    false 'planned add_guard / observed no_flow_change' goes green)."""
    repo = _make_git_dupname_proj(tmp_path, monkeypatch, "v3scope")
    monkeypatch.setenv("REPR_FLOW_V3", "1")
    from servers.common import flowedit_tools as fe
    fe.flow_plan(deltas=[{"op": "add_guard", "function": "_on_finished",
                          "anchor": "b.py:1", "rationale": "guard None",
                          "fault_site": "b.py:2"}])
    d = flowedit.load_plan()["deltas"][0]
    assert d["fault_site_valid"] is True, d      # extent check is scoped too
    b = repo / "b.py"
    b.write_text(b.read_text().replace(
        "def _on_finished(z):\n",
        "def _on_finished(z):\n    if z is None:\n        return None\n"))
    chk = fe.flow_check(function="_on_finished")[0]
    assert chk["match"] is True, chk
    assert chk["observed_op"] == "add_guard"
    assert chk["verdict"] == "exact"
    assert "resolution_note" not in chk          # resolved INSIDE b.py
    out = fe.flow_verify()[0]
    assert out["all_planned_match"] is True, out


def test_v3_flow_check_global_fallback_carries_note(tmp_path, monkeypatch,
                                                    built_index):
    """Fix 2a fallback: the planned function does NOT exist in the anchor's
    file -> resolution falls back to the global pick and the graded result
    says so in resolution_note."""
    repo = _make_git_dupname_proj(tmp_path, monkeypatch, "v3fall")
    monkeypatch.setenv("REPR_FLOW_V3", "1")
    from servers.common import flowedit_tools as fe
    fe.flow_plan(deltas=[{"op": "add_guard", "function": "solo_fn",
                          "anchor": "b.py:1", "rationale": "g",
                          "fault_site": "a.py:7"}])
    a = repo / "a.py"
    a.write_text(a.read_text().replace(
        "def solo_fn(y):\n",
        "def solo_fn(y):\n    if y == 0:\n        return 0\n"))
    chk = fe.flow_check(function="solo_fn")[0]
    assert chk["match"] is True, chk
    assert chk["observed_op"] == "add_guard"
    assert "resolution_note" in chk, chk
    assert "b.py" in chk["resolution_note"]      # names the anchor file
    assert "a.py" in chk["resolution_note"]      # ...and the global pick


def test_v2_dupname_false_mismatch_unchanged(tmp_path, monkeypatch,
                                             built_index):
    """PARITY GUARD: with REPR_FLOW_V3 unset the global first-pick resolution
    (and its false mismatch on the dup-name shape) stays byte-identical."""
    repo = _make_git_dupname_proj(tmp_path, monkeypatch, "v2scope")
    monkeypatch.delenv("REPR_FLOW_V3", raising=False)
    from servers.common import flowedit_tools as fe
    fe.flow_plan(deltas=[{"op": "add_guard", "function": "_on_finished",
                          "anchor": "b.py:1", "rationale": "guard None"}])
    b = repo / "b.py"
    b.write_text(b.read_text().replace(
        "def _on_finished(z):\n",
        "def _on_finished(z):\n    if z is None:\n        return None\n"))
    chk = fe.flow_check(function="_on_finished")[0]
    assert chk["match"] is False, chk            # v2 fingerprints a.py's fn
    assert chk["observed_op"] == "no_flow_change"
    assert "verdict" not in chk and "resolution_note" not in chk


def test_v3_flow_of_multimatch_note_and_v2_silent(tmp_path, monkeypatch,
                                                  built_index):
    """Fix 2b: v3 flow_of on a multi-file bare name renders the SAME first
    pick as v2 but adds a note listing the other candidates as file:line;
    v2 stays silent; an unambiguous name gets no note."""
    _make_git_dupname_proj(tmp_path, monkeypatch, "v3multi")
    from servers.common import flowedit_tools as fe
    monkeypatch.delenv("REPR_FLOW_V3", raising=False)
    out = fe.flow_of(functions=["_on_finished"])
    assert len(out) == 1 and out[0]["cfg"]
    assert "note" not in out[0]                  # v2: silent first pick
    monkeypatch.setenv("REPR_FLOW_V3", "1")
    out3 = fe.flow_of(functions=["_on_finished", "solo_fn"])
    by = {o["function"]: o for o in out3}
    assert by["_on_finished"]["cfg"] == out[0]["cfg"]   # same graph shown
    note = by["_on_finished"]["note"]
    assert "a.py" in note                        # the shown pick is named
    assert "b.py:1" in note                      # other candidate as file:line
    assert "Class.method" in note
    assert "note" not in by["solo_fn"]           # unambiguous: no note


COMP_READ_PUBLISHER = (
    "def read_publisher(fields):\n"
    "    names = [f.strip() for f in fields if f]\n"
    "    return names\n")

IFELSE_READ_PUBLISHER = (
    "def read_publisher(fields):\n"
    "    names = [f.strip() if f else '' for f in fields]\n"
    "    return names\n")


def test_v3_comprehension_rewrite_is_block_structure_change(monkeypatch):
    """Fix 3 (openlibrary read_publisher): a comprehension for+if ->
    for+if/else rewrite keeps the BLOCK structure identical, so v2
    fingerprints it no_flow_change (and the honest change_block_structure
    plan read as MISMATCH). v3's comprehension signature sees it, both
    directions; the no_flow_change dodge now reads as the mismatch."""
    monkeypatch.delenv("REPR_FLOW_V3", raising=False)
    fa2 = flowedit.cfg_fingerprint(COMP_READ_PUBLISHER)
    fb2 = flowedit.cfg_fingerprint(IFELSE_READ_PUBLISHER)
    assert len(fa2) == 3 and fa2 == fb2          # v2: bit-identical
    assert flowedit.classify_delta("cfg", fa2, fb2) == "no_flow_change"
    monkeypatch.setenv("REPR_FLOW_V3", "1")
    fa3 = flowedit.cfg_fingerprint(COMP_READ_PUBLISHER)
    fb3 = flowedit.cfg_fingerprint(IFELSE_READ_PUBLISHER)
    assert len(fa3) == 4 and fa3[:3] == fa2      # v2 prefix preserved
    assert fa3 != fb3
    assert flowedit.classify_delta("cfg", fa3, fb3) == "change_block_structure"
    # reverse direction (for+if/else -> comprehension filter) detected too
    assert flowedit.classify_delta("cfg", fb3, fa3) == "change_block_structure"
    # an honest change_block_structure plan now grades exact...
    assert flowedit.op_verdict(
        "change_block_structure",
        flowedit.classify_delta("cfg", fa3, fb3)) == "exact"
    # ...and the learned no_flow_change dodge reads as the mismatch it is
    assert flowedit.op_verdict(
        "no_flow_change",
        flowedit.classify_delta("cfg", fa3, fb3)) == "mismatch"


def test_v3_comp_to_statement_loop_still_branch_classified(monkeypatch):
    """The branch/block rules keep precedence: rewriting the comprehension
    into a REAL statement-level for+if changes branch counts and still
    classifies on those (add_guard), not on the comprehension signature."""
    monkeypatch.setenv("REPR_FLOW_V3", "1")
    loop_src = (
        "def read_publisher(fields):\n"
        "    names = []\n"
        "    for f in fields:\n"
        "        if f:\n"
        "            names.append(f.strip())\n"
        "    return names\n")
    fa = flowedit.cfg_fingerprint(COMP_READ_PUBLISHER)
    fb = flowedit.cfg_fingerprint(loop_src)
    assert flowedit.classify_delta("cfg", fa, fb) == "add_guard"


def test_v3_mixed_fingerprint_shapes_yield_unknown_not_crash(monkeypatch):
    """An old 3-element fingerprint against the extended v3 4-element one
    (either direction) classifies 'unknown' -- never a crash, never a fake
    mismatch -- and op_verdict grades it unknown."""
    monkeypatch.delenv("REPR_FLOW_V3", raising=False)
    old = flowedit.cfg_fingerprint(COMP_READ_PUBLISHER)
    monkeypatch.setenv("REPR_FLOW_V3", "1")
    new = flowedit.cfg_fingerprint(COMP_READ_PUBLISHER)
    assert len(old) == 3 and len(new) == 4
    assert flowedit.classify_delta("cfg", old, new) == "unknown"
    assert flowedit.classify_delta("cfg", new, old) == "unknown"
    assert flowedit.op_verdict("add_guard", "unknown") == "unknown"
    assert flowedit.fp_shape_mismatch(old, new) is True
    assert flowedit.fp_shape_mismatch(new, new) is False


def test_v3_flow_check_on_v2_snapshot_is_unknown(tmp_path, monkeypatch,
                                                 built_index):
    """End-to-end mixed-format guard: a plan snapshotted with v2 fingerprints
    checked under v3 yields verdict unknown with a note (both the classified
    path and the no_flow_change repr-compare path), not a crash or a fake
    mismatch."""
    repo = _make_git_two_fn_proj(tmp_path, monkeypatch, "v3mixed")
    monkeypatch.delenv("REPR_FLOW_V3", raising=False)
    from servers.common import flowedit_tools as fe
    fe.flow_plan(deltas=[
        {"op": "add_guard", "function": "fn_a", "anchor": "m.py:1",
         "rationale": "g"},
        {"op": "no_flow_change", "function": "fn_b", "anchor": "m.py:5",
         "rationale": "c"},
    ])
    m = repo / "m.py"
    m.write_text(m.read_text().replace(
        "def fn_a(x):\n",
        "def fn_a(x):\n    if x is None:\n        return None\n"))
    monkeypatch.setenv("REPR_FLOW_V3", "1")
    chk = fe.flow_check(function="fn_a")[0]
    assert chk["match"] is None, chk
    assert chk["verdict"] == "unknown"
    assert "fingerprint formats" in chk["note"]
    chk_b = fe.flow_check(function="fn_b")[0]
    assert chk_b["match"] is None, chk_b
    assert chk_b["verdict"] == "unknown"
    assert "fingerprint formats" in chk_b["note"]


def test_flow_check_detects_mismatch(tmp_path, monkeypatch, built_index):
    """Plan add_guard but make NO flow change -> flow_check reports mismatch."""
    from tests.conftest import FIXTURE_REPO
    import shutil
    repo = tmp_path / "proj2"
    shutil.copytree(FIXTURE_REPO, repo)
    from indexer.backends import get_backend
    idx = repo / ".repr_index"
    idx.mkdir(exist_ok=True)
    get_backend("symbols").build(repo, idx)
    monkeypatch.setenv("REPO_ROOT", str(repo))
    monkeypatch.setenv("REPO_INDEX_DIR", str(idx))
    monkeypatch.setenv("RUN_DIR", str(tmp_path / "run2"))
    monkeypatch.setenv("REPR_PERREP_REP", "cfg")

    from servers.common import flowedit_tools as fe
    fe.flow_plan(deltas=[{"op": "add_guard", "function": "validate_input",
                          "anchor": "x", "rationale": "y"}])
    # edit that does NOT add a guard (rename a local var -> no flow change)
    eng = repo / "sampleproj" / "core" / "engine.py"
    s = eng.read_text().replace("validated", "kept")
    eng.write_text(s)
    chk = fe.flow_check(function="validate_input")[0]
    assert chk["match"] is False
    assert chk["observed_op"] == "no_flow_change"


# -- flow3 fix A (structural fingerprint compare), fix B (engine stdout ------
# -- hygiene) and fix C (name-first extraction + honest func_absent) ----------
#
# Fix A: no_flow_change verification compared fingerprint REPR STRINGS; the
# frozenset in the dfg fingerprint reprs in hash-seed order, so a snapshot
# written by one process could false-mismatch against another process's
# recompute of the IDENTICAL graph. v3 compares the rebuilt objects.
# Fix B: beniget reports unbound identifiers by PRINTING to stdout -- the MCP
# stdio channel -- which corrupted the protocol and hung the tool call on
# functions full of module-level names (ansible construct_rule shape).
# Fix C: the indexed def line goes stale as the agent inserts lines above a
# function; a NEIGHBOR's def drifting into the +-3 window got fingerprinted
# in place of the named function (false flow_changed on unedited code), and
# a planned name absent at HEAD silently snapshotted the anchor's neighbor.


def test_v3_no_flow_change_object_compare_survives_repr_order(
        tmp_path, monkeypatch, built_index):
    """Fix A: a stored snapshot whose frozenset repr uses a DIFFERENT element
    order than this process would print must still verify no_flow_change
    (object equality), and a corrupt snapshot yields unknown, not mismatch."""
    _make_git_two_fn_proj(tmp_path, monkeypatch, "v3repr")
    monkeypatch.setenv("REPR_FLOW_V3", "1")
    monkeypatch.setenv("REPR_PERREP_REP", "dfg")
    from servers.common import flowedit_tools as fe
    from servers.common.flowedit_tools import _eval_fp
    fe.flow_plan(deltas=[{"op": "no_flow_change", "function": "fn_b",
                          "anchor": "m.py:5", "rationale": "const",
                          "fault_site": "m.py:6"}])
    plan = flowedit.load_plan()
    fp = _eval_fp(plan["snapshots"]["fn_b"])
    assert isinstance(fp[1], frozenset) and len(fp[1]) >= 2
    # rewrite the stored repr with the frozenset elements REVERSED -- the
    # string another hash seed would have produced; object equality must
    # not care while a string compare would false-mismatch.
    scrambled = "(%d, frozenset({%s}))" % (
        fp[0], ", ".join(repr(e) for e in
                         reversed(sorted(fp[1]))))
    assert _eval_fp(scrambled) == fp
    plan["snapshots"]["fn_b"] = scrambled
    flowedit.save_plan(plan)
    chk = fe.flow_check(function="fn_b")[0]
    assert chk["match"] is True, chk
    assert chk["verdict"] == "exact"
    assert chk["observed"] == "no_flow_change"
    # corrupt snapshot -> honest unknown (never a fake mismatch)
    plan["snapshots"]["fn_b"] = "not a fingerprint("
    flowedit.save_plan(plan)
    chk = fe.flow_check(function="fn_b")[0]
    assert chk["match"] is None, chk
    assert chk["verdict"] == "unknown"
    assert "corrupt" in chk["note"]


def test_v3_cfgdfg_no_flow_change_object_compare(tmp_path, monkeypatch,
                                                 built_index):
    """Fix A, combined arm: the per-graph snapshot dict is compared as
    OBJECTS too -- a reordered dfg frozenset repr stays no_flow_change."""
    _make_git_two_fn_proj(tmp_path, monkeypatch, "v3reprc")
    monkeypatch.setenv("REPR_FLOW_V3", "1")
    monkeypatch.setenv("REPR_PERREP_REP", "cfgdfg")
    from servers.common import flowedit_tools as fe
    from servers.common.flowedit_tools import _eval_fp
    fe.flow_plan(deltas=[{"op": "no_flow_change", "function": "fn_b",
                          "anchor": "m.py:5", "rationale": "const",
                          "fault_site": "m.py:6"}])
    plan = flowedit.load_plan()
    fp = _eval_fp(plan["snapshots"]["fn_b"]["dfg"])
    scrambled = "(%d, frozenset({%s}))" % (
        fp[0], ", ".join(repr(e) for e in reversed(sorted(fp[1]))))
    assert _eval_fp(scrambled) == fp
    plan["snapshots"]["fn_b"]["dfg"] = scrambled
    flowedit.save_plan(plan)
    chk = fe.flow_check(function="fn_b")[0]
    assert chk["match"] is True, chk
    assert chk["verdict"] == "exact"
    assert chk["observed"] == "no_flow_change"


def _reordered_fp_repr(stored: str) -> str:
    """A repr string that rebuilds to the SAME fingerprint object but is
    guaranteed textually different from ``stored`` (the string another hash
    seed -- or another process -- could have written)."""
    from servers.common.flowedit_tools import _eval_fp
    fp = _eval_fp(stored)
    assert isinstance(fp[1], frozenset) and len(fp[1]) >= 2
    out = "(%d, frozenset({%s}))" % (
        fp[0], ", ".join(repr(e) for e in reversed(sorted(fp[1]))))
    if out == stored:   # reversed order happened to match this process's
        out = out.replace("frozenset({", "frozenset({ ", 1)
    assert _eval_fp(out) == fp and out != stored
    return out


def test_v3_beniget_unbound_identifiers_zero_stdout(tmp_path, monkeypatch,
                                                    built_index, capfd):
    """Fix B: a v3 dfg plan/edit/check round over a function whose body is
    full of unbound module-level identifiers (the ansible construct_rule
    shape) completes with ZERO bytes on stdout -- beniget's 'W: unbound
    identifier' prints would corrupt the MCP stdio channel."""
    repo = tmp_path / "v3quiet"
    repo.mkdir()
    (repo / "m.py").write_text(
        "def construct_rule(a, **kw):\n"
        "    style = kw.pop('style', None)\n"
        "    out = yaml.dump(a, Dumper=AnsibleDumper, style=style)\n"
        "    return to_text(out)\n",
        encoding="utf-8")
    from indexer.backends import get_backend
    idx = repo / ".repr_index"
    idx.mkdir()
    get_backend("symbols").build(repo, idx)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "base")
    monkeypatch.setenv("REPO_ROOT", str(repo))
    monkeypatch.setenv("REPO_INDEX_DIR", str(idx))
    monkeypatch.setenv("RUN_DIR", str(tmp_path / "v3quiet_run"))
    monkeypatch.setenv("REPR_PERREP_REP", "dfg")
    monkeypatch.setenv("REPR_FLOW_V3", "1")
    from servers.common import flowedit_tools as fe
    capfd.readouterr()                        # drain setup output
    fe.flow_plan(deltas=[{"op": "add_def_use", "function": "construct_rule",
                          "anchor": "m.py:1", "rationale": "stage the text",
                          "fault_site": "m.py:3"}])
    m = repo / "m.py"
    m.write_text(m.read_text().replace(
        "    return to_text(out)\n",
        "    txt = to_text(out)\n    return txt\n"))
    chk = fe.flow_check(function="construct_rule")[0]
    out, _err = capfd.readouterr()
    assert out == "", repr(out)               # NOTHING on the stdio channel
    assert chk["match"] is True, chk
    assert chk["observed_op"] == "add_def_use"


def test_dfg_engine_quiet_on_unbound_identifiers(capfd):
    """Fix B at the engine: ModuleDFG over unbound-name-heavy source builds
    the same pairs as before but writes nothing to stdout."""
    from servers.dfg_server import engine
    src = ("def construct_rule(a, **kw):\n"
           "    style = kw.pop('style', None)\n"
           "    out = yaml.dump(a, Dumper=AnsibleDumper, style=style)\n"
           "    return to_text(out)\n")
    capfd.readouterr()
    dfg = engine.ModuleDFG("_t.py", src)
    out, _err = capfd.readouterr()
    assert out == "", repr(out)
    assert ("out", 3, 4) in dfg.pairs         # analysis itself intact


def _make_git_drift_proj(tmp_path, monkeypatch, name):
    """fn_x (branchy) sits 20 lines above fn_y (flat); prepending 20 module-
    level lines drops fn_x's NEW def line exactly onto fn_y's STALE index
    line (25), so the +-3 proximity pick fingerprints the wrong function."""
    repo = tmp_path / name
    repo.mkdir()
    lines = ["# module prelude %d" % i for i in range(1, 5)]      # 1-4
    lines += ["def fn_x(a):",                                     # 5
              "    if a:",
              "        return 1",
              "    return 0",
              ""]                                                 # 6-9
    lines += ["# filler %d" % i for i in range(10, 25)]           # 10-24
    lines += ["def fn_y(z):",                                     # 25
              "    w = z * 2",
              "    return w"]
    (repo / "m.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    from indexer.backends import get_backend
    idx = repo / ".repr_index"
    idx.mkdir()
    get_backend("symbols").build(repo, idx)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "base")
    monkeypatch.setenv("REPO_ROOT", str(repo))
    monkeypatch.setenv("REPO_INDEX_DIR", str(idx))
    monkeypatch.setenv("RUN_DIR", str(tmp_path / (name + "_run")))
    monkeypatch.setenv("REPR_PERREP_REP", "cfg")
    return repo


def test_v3_name_first_survives_neighbor_drift(tmp_path, monkeypatch,
                                               built_index):
    """Fix C (i): after 20 inserted module-level lines put the NEIGHBOR's
    def within +-3 of the stale index line, v3 flow_check on the UNEDITED
    function still fingerprints it BY NAME and reports no_flow_change."""
    repo = _make_git_drift_proj(tmp_path, monkeypatch, "v3drift")
    monkeypatch.setenv("REPR_FLOW_V3", "1")
    from servers.common import flowedit_tools as fe
    fe.flow_plan(deltas=[{"op": "no_flow_change", "function": "fn_y",
                          "anchor": "m.py:25", "rationale": "const only",
                          "fault_site": "m.py:26"}])
    m = repo / "m.py"
    m.write_text(("# inserted module line\n" * 20) + m.read_text())
    chk = fe.flow_check(function="fn_y")[0]
    assert chk["match"] is True, chk
    assert chk["verdict"] == "exact"
    assert chk["observed"] == "no_flow_change"


def test_v2_neighbor_drift_false_mismatch_unchanged(tmp_path, monkeypatch,
                                                    built_index):
    """PARITY GUARD for fix C (i): with REPR_FLOW_V3 unset the proximity
    window still picks the drifted NEIGHBOR (branchy fn_x) and the unedited
    fn_y false-mismatches exactly as v2 always did."""
    repo = _make_git_drift_proj(tmp_path, monkeypatch, "v2drift")
    monkeypatch.delenv("REPR_FLOW_V3", raising=False)
    from servers.common import flowedit_tools as fe
    fe.flow_plan(deltas=[{"op": "no_flow_change", "function": "fn_y",
                          "anchor": "m.py:25", "rationale": "const only"}])
    m = repo / "m.py"
    m.write_text(("# inserted module line\n" * 20) + m.read_text())
    chk = fe.flow_check(function="fn_y")[0]
    assert chk["match"] is False, chk         # v2 fingerprints fn_x
    assert chk["observed"] == "flow_changed"
    assert "verdict" not in chk


def test_v3_absent_name_warns_and_checks_unknown(tmp_path, monkeypatch,
                                                 built_index):
    """Fix C (ii): planning a non-add_function op on a name that does not
    exist at HEAD records NO snapshot (never the anchor neighbor's
    fingerprint), warns to use add_function, and checks unknown."""
    _make_git_two_fn_proj(tmp_path, monkeypatch, "v3ghost")
    monkeypatch.setenv("REPR_FLOW_V3", "1")
    from servers.common import flowedit_tools as fe
    res = fe.flow_plan(deltas=[{"op": "no_flow_change",
                                "function": "fn_ghost",
                                "anchor": "m.py:5", "rationale": "tweak",
                                "fault_site": "m.py:6"}])[0]
    assert any("does not exist at HEAD" in w and "add_function" in w
               for w in res["warnings"]), res["warnings"]
    assert res["snapshotted_functions"] == []
    assert flowedit.load_plan()["snapshots"] == {}   # no neighbor snapshot
    chk = fe.flow_check(function="fn_ghost")[0]
    assert chk["match"] is None, chk
    assert chk["verdict"] == "unknown"


# --- name-collision fix (2026-08-13): class-scoped qualname resolution --------
#
# Root cause of the manual study's one WRONG graph (flow_of rendering other
# classes' __init__ bodies) and both verifier errors in 322d7a (false mismatch
# "__init__ already exists at HEAD" for a brand-new class -> agent waters the
# plan down -> false pass): lookups stripped the class qualifier and matched
# ANY same-named def in the file.

_COLLISION_TEXT = '''\
class CallbackSend:
    def __init__(self, method_name):
        self.method_name = method_name


class DisplaySend:
    def __init__(self, method):
        self.method = method


def helper():
    return 1
'''


def test_func_by_name_bare_first_match_unchanged():
    src = flowedit.py_func_source_by_name(_COLLISION_TEXT, "__init__")
    assert src is not None and "method_name" in src   # first __init__ wins


def test_func_by_name_class_scoped_picks_right_class():
    src = flowedit.py_func_source_by_name(_COLLISION_TEXT,
                                          "DisplaySend.__init__")
    assert src is not None
    assert "self.method = method" in src
    assert "method_name" not in src


def test_func_by_name_new_class_scope_returns_none():
    # THE 322d7a regression: a brand-new class's __init__ must NOT match the
    # legacy classes' __init__ at HEAD.
    assert flowedit.py_func_source_by_name(
        _COLLISION_TEXT, "SolrUpdateState.__init__") is None


def test_func_by_name_module_qualifier_falls_back():
    src = flowedit.py_func_source_by_name(_COLLISION_TEXT,
                                          "pkg.module.helper")
    assert src is not None and "return 1" in src


def test_addfn_locate_keeps_class_qualifier():
    from servers.common import flowedit_tools as fe
    rel, name = fe._addfn_locate({
        "op": "add_function", "function": "SolrUpdateState.__init__",
        "anchor": "openlibrary/solr/update_work.py:100"})
    assert rel == "openlibrary/solr/update_work.py"
    assert name == "SolrUpdateState.__init__"


def test_plan_add_function_new_class_init_no_false_preexisting(monkeypatch):
    from servers.common import flowedit_tools as fe
    monkeypatch.setattr(flowedit, "git_head_text",
                        lambda rel: (_COLLISION_TEXT, "head"))
    entry = {"op": "add_function", "function": "SolrUpdateState.__init__",
             "anchor": "openlibrary/solr/update_work.py:100"}
    problems = []
    fe._plan_add_function(entry, problems)
    assert entry["expected_new"] is True          # was False (the false alarm)
    assert problems == []


def test_plan_add_function_bare_name_still_warns(monkeypatch):
    from servers.common import flowedit_tools as fe
    monkeypatch.setattr(flowedit, "git_head_text",
                        lambda rel: (_COLLISION_TEXT, "head"))
    entry = {"op": "add_function", "function": "helper",
             "anchor": "openlibrary/solr/update_work.py:100"}
    problems = []
    fe._plan_add_function(entry, problems)
    assert entry["expected_new"] is False         # genuinely exists at HEAD
    assert problems and "already exists" in problems[0]


# --- 2026-08-14 tools-v2 additions: renderer honesty + module expansion -------


def test_render_def_use_header_counts_full_set():
    """Header counts must come from the FULL pair set: counting the truncated
    grouping undercounted defs whenever max_pairs elided rows."""
    pairs = [("v%d" % i, i + 1, i + 2) for i in range(10)]
    text = flowedit.render_def_use("f", pairs, lambda _l: "", "beniget",
                                   max_pairs=3)
    assert "[10 vars, 10 defs, 10 def->use pairs" in text
    assert "(7 more pairs)" in text


def test_module_function_specs_expands_module_paths(monkeypatch):
    from servers.cfg_server import server as cfg_srv
    rows = [
        {"file": "lib/ansible/galaxy/login.py", "qualname": "GalaxyLogin.create_github_token", "name": "create_github_token", "line": 30},
        {"file": "lib/ansible/galaxy/login.py", "qualname": "top_helper", "name": "top_helper", "line": 10},
        {"file": "lib/ansible/galaxy/api.py", "qualname": "other", "name": "other", "line": 5},
    ]
    monkeypatch.setattr(cfg_srv, "_load_function_rows", lambda: rows)
    specs = flowedit.module_function_specs("galaxy.login")
    assert specs == ["lib/ansible/galaxy/login.py:10"]   # top-level preferred
    assert flowedit.module_function_specs("galaxy.nosuch") == []


def test_resolve_functions_file_name_form(monkeypatch):
    """'pkg/mod.py:func_name' resolves the name WITHIN the file (used to fall
    through to global matching and miss)."""
    from servers.cfg_server import server as cfg_srv
    rows = [
        {"file": "pkg/hardware/linux.py", "qualname": "LinuxHardware.get_mount_facts", "name": "get_mount_facts", "line": 567, "end_line": 640},
        {"file": "pkg/hardware/aix.py", "qualname": "AixHardware.get_mount_facts", "name": "get_mount_facts", "line": 188, "end_line": 230},
    ]
    monkeypatch.setattr(cfg_srv, "_load_function_rows", lambda: rows)
    got = cfg_srv._resolve_functions("pkg/hardware/linux.py:get_mount_facts")
    assert len(got) == 1 and got[0]["file"] == "pkg/hardware/linux.py"


def test_c_func_by_name_qualified_class_scoping():
    """C++ analog of the name-collision fix: 'B::run' must extract B's run,
    not A's, even though A::run appears first in the file."""
    text = (
        "void A::run(int x) {\n"
        "    a_stuff();\n"
        "}\n"
        "void B::run(int x) {\n"
        "    b_stuff();\n"
        "}\n"
        "static int run(int y) {\n"
        "    plain_stuff();\n"
        "}\n"
    )
    src = flowedit.c_func_source_by_name(text, "B::run")
    assert src is not None and "b_stuff" in src and "a_stuff" not in src
    # bare form: unchanged behavior -- matches the plain-C definition
    bare = flowedit.c_func_source_by_name(text, "run")
    assert bare is not None and "plain_stuff" in bare
