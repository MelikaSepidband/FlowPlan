"""Tests for the flow-first perrep surface and the 4-condition harnesses.

This export ships exactly four conditions -- baseline, cfg_perrep_flow3,
dfg_perrep_flow3 and cfgdfg_perrep_flow3 -- so these tests cover: condition
parsing (all 3 runners) + the whitelist, the flow-first server surface, the
flow tool semantics shared modules, the flow3 MCP env contract, the flow3
prompt assembly, and the gold-test-leak checkout hygiene shared by every
condition.  Removed-arm predicates are import-compatible stubs that must
return False/None for every input.
"""

import pytest

from tests.conftest import FIXTURE_REPO as SAMPLEPROJ


def test_condition_parsing_removed_arms_stubbed():
    """condition_rep recognizes ONLY '<rep>_perrep[_flow3]'; the removed-arm
    predicates always answer False (import-compatible stubs)."""
    from experiments.swebench_pro.run_experiment import (
        condition_dyn, condition_flow_first, condition_flow_nofix,
        condition_flowonly, condition_flowplan, condition_nlplan,
        condition_rep,
    )

    assert condition_rep("cfg_perrep") == "cfg"
    assert condition_rep("cfg_perrep_flow3") == "cfg"
    assert condition_rep("dfg_perrep_flow3") == "dfg"
    assert condition_rep("cfgdfg_perrep_flow3") == "cfgdfg"
    assert condition_rep("baseline") is None
    # retired arms no longer parse to a rep
    assert condition_rep("cfg_perrep_dyn") is None
    assert condition_rep("dfg_perrep_dyn") is None
    assert condition_rep("cfg_perrep_flow") is None
    assert condition_rep("cfg_perrep_nlplan") is None
    assert condition_rep("nonsense_perrep_dyn") is None
    for cond in ("cfg_perrep_dyn", "cfg_perrep_flow", "cfg_perrep_nlplan",
                 "cfg_perrep_flowonly", "cfg_perrep_flow_nofix",
                 "cfg_perrep_flow3", "baseline", "nonsense"):
        assert condition_dyn(cond) is False, cond
        assert condition_flow_first(cond) is False, cond
        assert condition_flow_nofix(cond) is False, cond
        assert condition_nlplan(cond) is False, cond
        assert condition_flowonly(cond) is False, cond
        assert condition_flowplan(cond) is False, cond


def test_allowed_conditions_whitelist_all_runners():
    """All three runners whitelist exactly the 4 shipped conditions."""
    from experiments.swebench_pro import run_experiment as swe_rex
    from experiments.secbench import run_experiment as sec_rex
    from experiments.swtbench import run_experiment as swt_rex

    want = {"baseline", "cfg_perrep_flow3", "dfg_perrep_flow3",
            "cfgdfg_perrep_flow3"}
    assert swe_rex.ALLOWED_CONDITIONS == want
    assert sec_rex.ALLOWED_CONDITIONS == want
    assert swt_rex.ALLOWED_CONDITIONS == want
    assert swe_rex.PERREP_REPS == ("cfg", "dfg", "cfgdfg")
    assert sec_rex.PERREP_REPS == ("cfg", "dfg", "cfgdfg")
    # the .repr_index backends each flow rep needs (symbols only: cfg/dfg
    # parse source on the fly)
    assert swe_rex._REP_BACKENDS == {"cfg": ("symbols",),
                                     "dfg": ("symbols",),
                                     "cfgdfg": ("symbols",)}


def test_strip_gold_tests_removes_both_new_and_modified(tmp_path):
    """strip_gold_tests DELETES gold tests whether or not they existed at
    base -- restoring a base version would still leak oracle test code."""
    import subprocess

    from experiments.swebench_pro.checkout import strip_gold_tests

    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*a):
        subprocess.run(["git", *a], cwd=repo, check=True,
                       capture_output=True)

    git("init", "-q")
    git("config", "user.email", "p@p")
    git("config", "user.name", "p")
    (repo / "src.py").write_text("x = 1\n")
    tests_dir = repo / "pkg" / "tests"
    tests_dir.mkdir(parents=True)
    # test_modified existed at base; test_new does not.
    (tests_dir / "test_modified.py").write_text("def test_a(): assert real()\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()
    # laydown: modify the existing test + add a new oracle test/fixture
    (tests_dir / "test_modified.py").write_text(
        "def test_a(): assert fixed()\n")
    (tests_dir / "test_new.py").write_text("def test_b(): assert fixed2()\n")

    inst = {
        "base_commit": base,
        "test_patch": (
            "diff --git a/pkg/tests/test_modified.py b/pkg/tests/test_modified.py\n"
            "diff --git a/pkg/tests/test_new.py b/pkg/tests/test_new.py\n"
        ),
        "before_repo_set_cmd": "",
    }
    stripped = strip_gold_tests(inst, repo)
    assert set(stripped) == {"pkg/tests/test_modified.py",
                             "pkg/tests/test_new.py"}
    assert not (tests_dir / "test_modified.py").exists()
    assert not (tests_dir / "test_new.py").exists()
    # a non-test source file is untouched
    assert (repo / "src.py").exists()
    # The EXTRACTED patch (what grading sees) must carry neither oracle test:
    # not their content, and not a spurious deletion of the base-tracked one.
    from experiments.swebench_pro.checkout import diff_excluding_tests
    inst2 = dict(inst, repo="x/y",
                 selected_test_files_to_run=["pkg/tests/test_modified.py",
                                             "pkg/tests/test_new.py"])
    (repo / "src.py").write_text("x = 2\n")  # an agent edit
    patch = diff_excluding_tests(inst2, repo)
    assert "src.py" in patch
    assert "test_modified.py" not in patch
    assert "test_new.py" not in patch


def test_prepare_worktree_default_skips_gold_tests(monkeypatch, tmp_path):
    """The default (apply_gold_tests=False) must NOT run before_repo_set_cmd,
    so the oracle test files are never laid into the solving tree."""
    from experiments.swebench_pro import checkout as co

    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        import types
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(co, "_run", fake_run)
    monkeypatch.setattr(co, "ensure_clone", lambda *a, **k: tmp_path / "clone")

    inst = {"repo": "x/y", "repo_url": "", "base_commit": "a" * 40,
            "instance_id": "iid",
            "before_repo_set_cmd":
                "git checkout %s -- pkg/tests/test_x.py" % ("b" * 40)}
    # default: gold-test checkout must NOT appear in any executed command
    co.prepare_worktree(inst, suffix="c")
    ran = " ".join(" ".join(c) for c in calls)
    assert ("b" * 40) not in ran, "before_repo_set_cmd ran during solving"
    # ablation: apply_gold_tests=True DOES run it (via bash -lc)
    calls.clear()
    co.prepare_worktree(inst, suffix="c", apply_gold_tests=True)
    ran2 = " ".join(" ".join(str(x) for x in c) for c in calls)
    assert "before_repo_set_cmd" in ran2 or "bash" in ran2


def test_verify_no_oracle(tmp_path):
    """verify_no_oracle flags a reachable fix commit / on-disk gold test."""
    import subprocess

    from experiments.swebench_pro.checkout import (
        gold_fix_sha, verify_no_oracle,
    )

    repo = tmp_path / "r"
    repo.mkdir()

    def git(*a):
        subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "p@p")
    git("config", "user.name", "p")
    (repo / "src.py").write_text("x=1\n")
    git("add", "-A")
    git("commit", "-qm", "base")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()
    (repo / "t.py").write_text("assert 1\n")
    git("add", "-A")
    git("commit", "-qm", "fix")
    fix = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                         capture_output=True, text=True).stdout.strip()

    inst = {"before_repo_set_cmd":
            "git checkout %s -- pkg/tests/test_x.py" % fix,
            "test_patch": "diff --git a/pkg/tests/test_x.py "
                          "b/pkg/tests/test_x.py\n"}
    assert gold_fix_sha(inst) == fix
    # fix reachable -> NOT clean
    bad = verify_no_oracle(inst, repo)
    assert bad["fix_commit_reachable"] is True
    assert bad["clean"] is False
    # reset to base only (drop the fix commit) -> clean
    git("reset", "--hard", base)
    git("reflog", "expire", "--expire=now", "--all")
    git("gc", "--prune=now", "--quiet")
    good = verify_no_oracle(inst, repo)
    assert good["fix_commit_reachable"] is False
    assert good["clean"] is True


def test_isolate_worktree_history_blocks_git_show(tmp_path):
    """After isolation, the fix commit's blobs are unreachable -- git show
    <fix-sha>:<path> cannot recover the oracle from a shared object store."""
    import subprocess

    from experiments.swebench_pro.checkout import isolate_worktree_history

    origin = tmp_path / "origin"
    origin.mkdir()

    def git(cwd, *a):
        return subprocess.run(["git", *a], cwd=cwd, check=True,
                              capture_output=True, text=True)

    git(origin, "init", "-q", "-b", "main")
    git(origin, "config", "user.email", "p@p")
    git(origin, "config", "user.name", "p")
    (origin / "src.py").write_text("x = 1\n")
    git(origin, "add", "-A")
    git(origin, "commit", "-qm", "base")
    base_sha = git(origin, "rev-parse", "HEAD").stdout.strip()
    # a later 'fix' commit adds the oracle test
    (origin / "test_oracle.py").write_text("def test(): assert SECRET\n")
    git(origin, "add", "-A")
    git(origin, "commit", "-qm", "fix")
    fix_sha = git(origin, "rev-parse", "HEAD").stdout.strip()

    # worktree shares origin's object store (like git worktree add)
    wt = tmp_path / "wt"
    git(origin, "worktree", "add", "--detach", str(wt), base_sha)
    # oracle recoverable BEFORE isolation
    pre = subprocess.run(["git", "show", "%s:test_oracle.py" % fix_sha],
                         cwd=wt, capture_output=True, text=True)
    assert pre.returncode == 0 and "SECRET" in pre.stdout

    isolate_worktree_history(wt)

    # oracle UNrecoverable after isolation (blob not in the new object store)
    post = subprocess.run(["git", "show", "%s:test_oracle.py" % fix_sha],
                          cwd=wt, capture_output=True, text=True)
    assert post.returncode != 0
    assert "SECRET" not in post.stdout
    # and the fix sha itself is gone
    assert subprocess.run(["git", "cat-file", "-e", fix_sha + "^{commit}"],
                          cwd=wt, capture_output=True).returncode != 0
    # working source is intact + history is a single commit
    assert (wt / "src.py").read_text() == "x = 1\n"
    n = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=wt,
                       capture_output=True, text=True).stdout.strip()
    assert n == "1"


def test_test_patch_paths_extraction():
    from experiments.swebench_pro.checkout import test_patch_paths

    inst = {
        "test_patch": (
            "diff --git a/pkg/tests/test_x.py b/pkg/tests/test_x.py\n"
            "new file mode 100644\n--- /dev/null\n+++ b/pkg/tests/test_x.py\n"
        ),
        "before_repo_set_cmd": (
            "git reset --hard abc\n"
            "git checkout DEADBEEF -- pkg/tests/sample.py pkg/tests/test_x.py\n"
        ),
    }
    paths = set(test_patch_paths(inst))
    assert paths == {"pkg/tests/test_x.py", "pkg/tests/sample.py"}


# -- flow-first server surface -------------------------------------------------


def test_perrep_server_flow_first_gate_cfg_and_dfg(built_index, monkeypatch):
    from servers.perrep import server as perrep_server

    for rep in ("cfg", "dfg"):
        monkeypatch.setenv("REPR_PERREP_REP", rep)
        monkeypatch.setenv("REPR_FLOW_FIRST", "1")
        monkeypatch.setenv("REPO_ROOT", str(SAMPLEPROJ))
        monkeypatch.setenv("REPO_INDEX_DIR", str(built_index.index_dir))
        server = perrep_server.create_server()
        names = set(server.tools)
        assert names == {"flow_of", "flow_plan", "flow_check",
                         "flow_verify"}, (rep, names)
    monkeypatch.delenv("REPR_FLOW_FIRST")


def test_perrep_server_raises_outside_flow_surface(built_index, monkeypatch):
    """The export ships ONLY the flow-first surface: any rep/env combo
    outside cfg/dfg/cfgdfg + REPR_FLOW_FIRST=1 raises RuntimeError."""
    from servers.perrep import server as perrep_server

    monkeypatch.setenv("REPO_ROOT", str(SAMPLEPROJ))
    monkeypatch.setenv("REPO_INDEX_DIR", str(built_index.index_dir))
    monkeypatch.delenv("REPR_FLOW_FIRST", raising=False)
    monkeypatch.delenv("REPR_FLOW_OF_ONLY", raising=False)
    # a flow rep WITHOUT the flow-first env: no map surface exists any more
    monkeypatch.setenv("REPR_PERREP_REP", "cfg")
    with pytest.raises(RuntimeError):
        perrep_server.create_server()
    # a non-flow (map) rep raises even WITH the env
    monkeypatch.setenv("REPR_FLOW_FIRST", "1")
    monkeypatch.setenv("REPR_PERREP_REP", "ast")
    with pytest.raises(RuntimeError):
        perrep_server.create_server()


def test_dfg_flow_of_description_is_value_flow_worded():
    from servers.common import flowedit_tools as fe

    assert "VALUE-FLOW" in fe.DFG_FLOW_OF_DESCRIPTION
    assert "def-use" in fe.DFG_FLOW_OF_DESCRIPTION
    assert "add_def_use" in fe.DFG_FLOW_OF_DESCRIPTION
    # the cfg description stays control-flow worded
    assert "CONTROL-FLOW" in fe.FLOW_OF_DESCRIPTION


def test_dfg_flow_text_renders_def_use(built_index, monkeypatch):
    from servers.common import flowedit

    monkeypatch.setenv("REPO_ROOT", str(SAMPLEPROJ))
    monkeypatch.setenv("REPO_INDEX_DIR", str(built_index.index_dir))
    text = flowedit.dfg_flow_text("validate_input")
    assert text is not None
    assert "value-flow (def-use) graph of validate_input()" in text
    # validated is defined then used twice (append + return)
    assert "validated" in text
    assert "def L" in text and "-> uses" in text


def test_flow_of_dispatches_by_rep(built_index, monkeypatch):
    from servers.common import flowedit_tools as fe

    monkeypatch.setenv("REPO_ROOT", str(SAMPLEPROJ))
    monkeypatch.setenv("REPO_INDEX_DIR", str(built_index.index_dir))

    monkeypatch.setenv("REPR_PERREP_REP", "dfg")
    items = fe.flow_of(["validate_input"])
    assert len(items) == 1 and items[0]["cfg"]
    assert "value-flow (def-use) graph" in items[0]["cfg"]

    monkeypatch.setenv("REPR_PERREP_REP", "cfg")
    items = fe.flow_of(["validate_input"])
    assert len(items) == 1 and items[0]["cfg"]
    assert "control-flow graph of" in items[0]["cfg"]


def test_swt_runner_sets_testgen_mode():
    """The SWT runner must launch the server in TEST-GEN mode (semantics of
    plan/check/verify flip to source-untouched + test-only-diff) and keep the
    verify tool registered."""
    from experiments.swebench_pro import run_experiment as swe_rex
    import json
    import tempfile
    from pathlib import Path
    tmp = Path(tempfile.mkdtemp())
    p = swe_rex.write_mcp_config("cfg", tmp, tmp, flow_first=True,
                                 flow_testgen=True)
    env = json.loads(p.read_text())["mcpServers"]["cfg_perrep"]["env"]
    assert env["REPR_FLOW_TESTGEN"] == "1"
    assert env["REPR_FLOW_FIRST"] == "1"
    assert env["REPR_FLOW_VERIFY"] == ""    # verify tool stays registered


def test_flow_of_descriptions_state_fallback():
    from servers.common import flowedit_tools as fe
    for desc in (fe.FLOW_OF_DESCRIPTION, fe.DFG_FLOW_OF_DESCRIPTION):
        assert "FALLBACK" in desc
        assert "Grep" in desc and "Read" in desc
    assert "FINAL STEP" in fe.FLOW_VERIFY_DESCRIPTION
    assert "OUTSIDE" in fe.FLOW_VERIFY_DESCRIPTION


def test_flow_first_server_surface_cfgdfg_and_verify_toggle(built_index,
                                                            monkeypatch):
    """The combined rep exposes the same 4-tool flow surface; REPR_FLOW_VERIFY=0
    (SWT test-gen / LFLB localization) withholds the final verificator."""
    from servers.perrep import server as perrep_server
    monkeypatch.setenv("REPO_ROOT", str(SAMPLEPROJ))
    monkeypatch.setenv("REPO_INDEX_DIR", str(built_index.index_dir))
    monkeypatch.setenv("REPR_FLOW_FIRST", "1")
    monkeypatch.setenv("REPR_PERREP_REP", "cfgdfg")
    monkeypatch.delenv("REPR_FLOW_VERIFY", raising=False)
    names = set(perrep_server.create_server().tools)
    assert names == {"flow_of", "flow_plan", "flow_check", "flow_verify"}
    monkeypatch.setenv("REPR_FLOW_VERIFY", "0")
    for rep in ("cfg", "dfg", "cfgdfg"):
        monkeypatch.setenv("REPR_PERREP_REP", rep)
        names = set(perrep_server.create_server().tools)
        assert names == {"flow_of", "flow_plan", "flow_check"}, (rep, names)
    monkeypatch.delenv("REPR_FLOW_VERIFY")
    monkeypatch.delenv("REPR_FLOW_FIRST")


def test_baseline_prompt_has_no_map_rules():
    # regression for the baseline-contamination bug: with NO tools at all
    # (baseline passes every tool arg as None), the Rules section must not
    # mention the code map, the explore/node tools, or a Grep gate -- baseline
    # cells have no MCP config and no hook, so that text described a different
    # experimental condition. It must still nudge a non-empty patch.
    from experiments.swebench_pro import agent as A
    inst = {"problem_statement": "x", "requirements": "", "interface": ""}
    p = A.build_prompt(inst)
    assert "code map" not in p.lower()
    assert "explore/node" not in p
    assert "blocked on purpose" not in p
    assert "flow points you to" not in p  # flow-arm wording, wrong for baseline
    assert "non-empty patch" in p


# -- flow3 (v3) arm -------------------------------------------------------------
#
# flow3 is THE flow arm of this export: *_perrep_flow3 harness-side,
# REPR_FLOW_FIRST=1 + REPR_FLOW_V3=1 server-side.


def test_flow3_condition_parsing_all_runners():
    from experiments.swebench_pro import run_experiment as swe_rex
    from experiments.secbench import run_experiment as sec_rex
    from experiments.swtbench import run_experiment as swt_rex
    for cond, rep in (("cfg_perrep_flow3", "cfg"),
                      ("dfg_perrep_flow3", "dfg"),
                      ("cfgdfg_perrep_flow3", "cfgdfg")):
        assert swe_rex.condition_flow_v3(cond), cond
        assert swe_rex.condition_rep(cond) == rep, cond
        assert not swe_rex.condition_flow_first(cond), cond
        assert not swe_rex.condition_flowonly(cond), cond
        assert not swe_rex.condition_nlplan(cond), cond
        assert not swe_rex.condition_dyn(cond), cond
        assert not swe_rex.condition_flowplan(cond), cond
        assert sec_rex.condition_flow_v3(cond), cond
        assert sec_rex.condition_rep(cond) == rep, cond
        assert not sec_rex.condition_flow_first(cond), cond
        assert not sec_rex.condition_flowonly(cond), cond
        assert not sec_rex.condition_dyn(cond), cond
        assert swt_rex.condition_flow_v3(cond), cond
    # never confused with the retired v2/ablation arms or junk
    for cond in ("cfg_perrep_flow", "dfg_perrep_flow", "cfgdfg_perrep_flow",
                 "cfg_perrep_flowonly", "cfg_perrep_nlplan", "baseline",
                 "ast_perrep_flow3", "cfg_perrep_flow3x", "cfg_perrep_dyn"):
        assert not swe_rex.condition_flow_v3(cond), cond
        assert not sec_rex.condition_flow_v3(cond), cond
        assert not swt_rex.condition_flow_v3(cond), cond
    # the retired v2 arms parse to NOTHING in this export
    assert swe_rex.condition_rep("cfg_perrep_flow") is None
    assert not swe_rex.condition_flow_first("cfgdfg_perrep_flow")
    assert sec_rex.condition_rep("cfgdfg_perrep_flow") is None
    assert not sec_rex.condition_flow_first("dfg_perrep_flow")


def test_flow3_mcp_env_swe_and_sec(tmp_path):
    """flow3 cells: REPR_FLOW_FIRST=1 (flow-first registration reused) +
    REPR_FLOW_V3=1, and the REPR_FLOW_LEGACY_VERIFY pin is NOT set."""
    import json
    from experiments.swebench_pro import run_experiment as swe_rex
    from experiments.secbench import run_experiment as sec_rex
    p = swe_rex.write_mcp_config("cfg", tmp_path, tmp_path, flow_first=True,
                                 flow_v3=True)
    env = json.loads(p.read_text())["mcpServers"]["cfg_perrep"]["env"]
    assert env["REPR_FLOW_FIRST"] == "1"
    assert env["REPR_FLOW_V3"] == "1"
    assert env["REPR_FLOW_LEGACY_VERIFY"] != "1"  # no legacy pin on flow3
    # SWT flow3 launch: TESTGEN mode + v3, verify tool stays registered
    p3 = swe_rex.write_mcp_config("cfg", tmp_path, tmp_path, flow_first=True,
                                  flow_testgen=True, flow_v3=True)
    env3 = json.loads(p3.read_text())["mcpServers"]["cfg_perrep"]["env"]
    assert env3["REPR_FLOW_TESTGEN"] == "1"
    assert env3["REPR_FLOW_V3"] == "1"
    assert env3["REPR_FLOW_VERIFY"] == ""
    assert env3["REPR_FLOW_LEGACY_VERIFY"] != "1"
    # secbench: flow3 is the only flow arm -- write_mcp_config always emits
    # the flow3 env (joern_port is positional there)
    p4 = sec_rex.write_mcp_config("cfg", tmp_path, tmp_path, None)
    env4 = json.loads(p4.read_text())["mcpServers"]["cfg_perrep"]["env"]
    assert env4["REPR_FLOW_FIRST"] == "1"
    assert env4["REPR_FLOW_V3"] == "1"
    assert env4["REPR_FLOW_LEGACY_VERIFY"] != "1"


def test_flow3_server_flow_plan_gains_replace(built_index, monkeypatch):
    """Under REPR_FLOW_V3=1 the flow-first server registers the v3
    flow_plan callable (whose schema adds the optional 'replace' arg) PLUS
    the step-2 flow_locate tool -- exactly 5 tools; the default env keeps
    the exact v2 callable and exactly 4 tools."""
    from servers.perrep import server as perrep_server
    from servers.common import flowedit_tools as fe
    monkeypatch.setenv("REPO_ROOT", str(SAMPLEPROJ))
    monkeypatch.setenv("REPO_INDEX_DIR", str(built_index.index_dir))
    monkeypatch.setenv("REPR_PERREP_REP", "cfg")
    monkeypatch.setenv("REPR_FLOW_FIRST", "1")
    monkeypatch.delenv("REPR_FLOW_V3", raising=False)
    monkeypatch.delenv("REPR_FLOW_VERIFY", raising=False)
    monkeypatch.delenv("REPR_FLOW_TESTGEN", raising=False)
    srv = perrep_server.create_server()
    assert set(srv.tools) == {"flow_of", "flow_plan", "flow_check",
                              "flow_verify"}
    assert srv.tools["flow_plan"].__wrapped__ is fe.flow_plan
    monkeypatch.setenv("REPR_FLOW_V3", "1")
    for rep in ("cfg", "dfg", "cfgdfg"):
        monkeypatch.setenv("REPR_PERREP_REP", rep)
        srv3 = perrep_server.create_server()
        assert set(srv3.tools) == {"flow_of", "flow_locate", "flow_plan",
                                   "flow_check", "flow_verify"}, \
            (rep, set(srv3.tools))
        assert srv3.tools["flow_plan"].__wrapped__ is fe.flow_plan_v3, rep
        assert srv3.tools["flow_locate"].__wrapped__ is fe.flow_locate, rep


def test_swe_build_prompt_map_free_rules_flag():
    """The flow3 task body: build_prompt(map_free_rules=True) swaps ONLY the
    Rules tail to the map-free variant (no protocol steps in the body); the
    flag defaults off and the default output is byte-identical."""
    from experiments.swebench_pro import agent as A
    inst = {"problem_statement": "x", "requirements": "", "interface": ""}
    base = A.build_prompt(inst)
    mf = A.build_prompt(inst, map_free_rules=True)
    assert "Localize the bug with your own tools" in base
    assert "Localize the bug with your own tools" not in mf
    assert "flow points you to" in mf  # the map-free rules tail
    # no protocol steps in the body (they live in flow_protocol / the skill)
    assert "Work IN THE FLOW" not in mf
    assert "flow_plan" not in mf and "fault_site" not in mf
    # flag off == the exact old default
    assert A.build_prompt(inst, map_free_rules=False) == base


def test_swe_flow3_prompt_skill_and_inline(monkeypatch):
    """The SWE flow3 user prompt: skill mode (default) starts with /flowfix
    + blank line + task body; REPR_FLOW_SKILL=0 inlines the protocol above
    the same body. The sentinel never appears in the user prompt."""
    from experiments.common import flow_protocol as fp
    from experiments.swebench_pro import agent as A
    inst = {"problem_statement": "the bug", "requirements": "",
            "interface": ""}
    body = A.build_prompt(inst, map_free_rules=True)
    tools = fp.tool_names("cfg")
    monkeypatch.delenv("REPR_FLOW_SKILL", raising=False)
    p = fp.build_user_prompt("repair", "cfg", tools, body)
    lines = p.splitlines()
    assert lines[0] == "/flowfix" and lines[1] == ""
    assert lines[2] == "# Bug-fix task"
    assert fp.SENTINEL not in p
    assert "Do NOT call flow_plan" not in p  # protocol lives in the skill
    monkeypatch.setenv("REPR_FLOW_SKILL", "0")
    q = fp.build_user_prompt("repair", "cfg", tools, body)
    assert "/flowfix" not in q
    assert "Do NOT call flow_plan until you can see the bug in a graph" in q
    assert q.endswith(body)
    assert fp.SENTINEL not in q


def test_flow3_swt_and_sec_prompt_assembly(monkeypatch):
    """SWT/SecBench flow3 prompts wrap each benchmark's PLAIN task body
    with the task-matched v3 protocol; testgen never carries repair
    wording."""
    from experiments.common import flow_protocol as fp
    from experiments.swtbench import prompt as swt_prompt
    from experiments.secbench import run_experiment as sec_rex
    inst = {"problem_statement": "the bug", "requirements": "",
            "interface": "", "sanitizer": "asan", "bug_report": "boom",
            "sanitizer_report": "READ of size 4"}
    body_t = swt_prompt.build_prompt(inst)
    body_s = sec_rex.build_prompt(inst)
    monkeypatch.delenv("REPR_FLOW_SKILL", raising=False)
    pt = fp.build_user_prompt("testgen", "cfg", fp.tool_names("cfg"), body_t)
    assert pt.splitlines()[0] == "/flowfix" and pt.splitlines()[1] == ""
    monkeypatch.setenv("REPR_FLOW_SKILL", "0")
    it = fp.build_user_prompt("testgen", "cfg", fp.tool_names("cfg"), body_t)
    assert ("Do NOT call flow_plan until the faulty source function's graph "
            "is in view") in it
    assert "You fix this bug" not in it  # no repair wording in testgen
    assert "Make the SMALL code edit" not in it
    assert it.endswith(body_t)
    isec = fp.build_user_prompt("security", "cfgdfg",
                                fp.tool_names("cfgdfg"), body_s)
    assert "Do NOT call flow_plan until you can see the bug in a graph" in isec
    assert "vulnerability" in isec and "fault_site" in isec
    assert isec.endswith(body_s)


def test_baseline_prompt_byte_stable_key_lines():
    """Guard: the baseline prompt is untouched by the flow3 machinery --
    assert its exact key Rules line and the absence of any v3 vocabulary."""
    from experiments.swebench_pro import agent as A
    inst = {"problem_statement": "the bug", "requirements": "",
            "interface": ""}
    v3_vocab = ("fault_site", "/flowfix", "add_function",
                "Do NOT call flow_plan", "re-check ONCE", "flow_locate")
    b = A.build_prompt(inst)
    assert ("- Localize the bug with your own tools (Grep/Glob to search, "
            "Read to open files), then Edit the source. You must end with "
            "a non-empty patch; never stop with the bug unfixed."
            ) in b.splitlines()
    for bad in v3_vocab:
        assert bad not in b, bad
