"""Tests for the cfg/dfg perrep map builders and their engine wiring."""

import sqlite3

import pytest

from perrep import build as perrep_build
from perrep.engine import Engine, config_for

from tests.conftest import FIXTURE_REPO as SAMPLEPROJ


@pytest.fixture(scope="module")
def cfg_db(tmp_path_factory, built_index):
    """Map built into tmp; index reused from the session built_index (the
    fixture repo itself must never be mutated -- conftest contract)."""
    out_dir = tmp_path_factory.mktemp("cfgmap")
    stats = perrep_build.build_rep(
        SAMPLEPROJ, "cfg", out_dir=out_dir, repr_index=built_index.index_dir
    )
    return out_dir / "cfg.db", stats


@pytest.fixture(scope="module")
def dfg_db(tmp_path_factory, built_index):
    out_dir = tmp_path_factory.mktemp("dfgmap")
    stats = perrep_build.build_rep(
        SAMPLEPROJ, "dfg", out_dir=out_dir, repr_index=built_index.index_dir
    )
    return out_dir / "dfg.db", stats


def test_build_reps_includes_flow_reps():
    assert "cfg" in perrep_build.REPS
    assert "dfg" in perrep_build.REPS


def test_cfg_map_shape(cfg_db):
    db, stats = cfg_db
    assert stats["rep"] == "cfg"
    assert stats["nodes"] > 0 and stats["edges"] > 0
    assert stats["flow_docs"] > 0, "no function got a flow doc"
    conn = sqlite3.connect(str(db))
    kinds = {k for (k,) in conn.execute("SELECT DISTINCT kind FROM nodes")}
    assert "file" in kinds
    assert kinds & {"function", "method"}
    edge_kinds = {k for (k,) in conn.execute("SELECT DISTINCT kind FROM edges")}
    assert edge_kinds == {"contains"}
    conn.close()


def test_cfg_flow_doc_carries_conditions(cfg_db):
    db, _stats = cfg_db
    conn = sqlite3.connect(str(db))
    (doc,) = conn.execute(
        "SELECT doc FROM nodes WHERE name = 'validate_input'"
    ).fetchone()
    conn.close()
    assert doc and doc.startswith("flow:")
    # validate_input has a loop and a compound if -> both must be visible
    assert "loops" in doc
    assert "conditions:" in doc
    assert "isinstance" in doc  # the branch condition text is FTS-searchable


def test_cfg_explore_ranks_by_condition_text(cfg_db):
    db, _stats = cfg_db
    engine = Engine(config_for("cfg", db, SAMPLEPROJ))
    result = engine.explore("isinstance strip record")
    text = str(result)
    assert "validate_input" in text


def test_dfg_map_shape(dfg_db):
    db, stats = dfg_db
    assert stats["rep"] == "dfg"
    assert stats["nodes"] > 0
    assert stats["flow_docs"] > 0, "no function got a def/use doc"
    conn = sqlite3.connect(str(db))
    edge_kinds = {k for (k,) in conn.execute("SELECT DISTINCT kind FROM edges")}
    assert "contains" in edge_kinds
    conn.close()


def test_dfg_doc_lists_variables(dfg_db):
    db, _stats = dfg_db
    conn = sqlite3.connect(str(db))
    (doc,) = conn.execute(
        "SELECT doc FROM nodes WHERE name = 'validate_input'"
    ).fetchone()
    conn.close()
    assert doc and "defines" in doc
    assert "validated" in doc  # its local accumulator variable


def test_dfg_explore_ranks_by_variable_name(dfg_db):
    db, _stats = dfg_db
    engine = Engine(config_for("dfg", db, SAMPLEPROJ))
    result = engine.explore("validated records")
    assert "validate_input" in str(result)


def test_executed_path_overlays_trace(built_index, tmp_path, monkeypatch):
    """executed_path marks the traced blocks executed and the rest not."""
    import json

    monkeypatch.setenv("REPO_ROOT", str(SAMPLEPROJ))
    monkeypatch.setenv("REPO_INDEX_DIR", str(built_index.index_dir))
    trace = {"ok": True, "test_id": "tests/test_x.py::test_bug",
             "files": {"sampleproj/core/engine.py": [7, 8, 9, 12]}}
    tf = tmp_path / "trace.json"
    tf.write_text(json.dumps(trace), encoding="utf-8")
    monkeypatch.setenv("REPR_TRACE_FILE", str(tf))

    from servers.cfg_server import server as cfg_srv

    items = cfg_srv.executed_path(function="validate_input")
    assert len(items) == 1
    it = items[0]
    assert it["test"] == "tests/test_x.py::test_bug"
    assert it["total_blocks"] > 0
    assert 0 < it["executed_blocks"] < it["total_blocks"]
    flags = {b["executed"] for b in it["blocks"]}
    assert flags == {True, False}

    cov = cfg_srv.covered_blocks(file="sampleproj/core/engine.py")
    by_fn = {c["function"].split(".")[-1]: c for c in cov if "function" in c}
    assert by_fn["validate_input"]["executed_lines"] == 4
    assert by_fn["run_pipeline"]["executed_lines"] == 0


def test_executed_path_without_trace_notes(built_index, tmp_path, monkeypatch):
    monkeypatch.setenv("REPO_ROOT", str(SAMPLEPROJ))
    monkeypatch.setenv("REPO_INDEX_DIR", str(built_index.index_dir))
    monkeypatch.setenv("REPR_TRACE_FILE", str(tmp_path / "absent.json"))

    from servers.cfg_server import server as cfg_srv

    items = cfg_srv.executed_path(function="validate_input")
    assert items and all("note" in it for it in items)
    cov = cfg_srv.covered_blocks(file="sampleproj/core/engine.py")
    assert cov and "note" in cov[0]


def test_perrep_server_gates_dynamic_tools_on_env(
    cfg_db, built_index, monkeypatch
):
    from servers.perrep import server as perrep_server

    db, _ = cfg_db
    monkeypatch.setenv("REPR_PERREP_REP", "cfg")
    monkeypatch.setenv("REPR_PERREP_DB", str(db))
    monkeypatch.setenv("REPO_ROOT", str(SAMPLEPROJ))
    monkeypatch.setenv("REPO_INDEX_DIR", str(built_index.index_dir))
    monkeypatch.delenv("REPR_TRACE", raising=False)
    static_names = set(perrep_server.create_server().tools)
    assert "executed_path" not in static_names
    monkeypatch.setenv("REPR_TRACE", "1")
    dyn_names = set(perrep_server.create_server().tools)
    assert {"executed_path", "covered_blocks"} <= dyn_names


def test_condition_parsing_dyn():
    from experiments.swebench_pro.run_experiment import (
        condition_dyn, condition_rep,
    )

    assert condition_rep("cfg_perrep") == "cfg"
    assert condition_rep("cfg_perrep_dyn") == "cfg"
    assert condition_rep("dfg_perrep_dyn") == "dfg"
    assert condition_rep("baseline") is None
    assert condition_rep("nonsense_perrep_dyn") is None
    assert condition_dyn("cfg_perrep_dyn") is True
    assert condition_dyn("cfg_perrep") is False
    assert condition_dyn("nonsense_perrep_dyn") is False


def test_dyn_steer_mentions_overlay_tools():
    from experiments.swebench_pro import steer

    static = steer.system_prompt_for("cfg_perrep", tools_active=True)
    dyn = steer.system_prompt_for("cfg_perrep_dyn", tools_active=True)
    assert static and "executed_path" not in static
    assert dyn and "mcp__cfg_perrep__executed_path" in dyn
    assert dyn.startswith(static)


def test_dyn_steer_dfg_names_only_served_tools():
    """The dfg dyn steer must name executed_flows, NEVER executed_path
    (which the dfg server does not register -- the cg_steer lesson)."""
    from experiments.swebench_pro import steer

    dyn = steer.system_prompt_for("dfg_perrep_dyn", tools_active=True)
    assert dyn and "mcp__dfg_perrep__executed_flows" in dyn
    assert "mcp__dfg_perrep__covered_blocks" in dyn
    assert "executed_path" not in dyn


def test_executed_flows_overlays_trace(built_index, tmp_path, monkeypatch):
    import json

    monkeypatch.setenv("REPO_ROOT", str(SAMPLEPROJ))
    monkeypatch.setenv("REPO_INDEX_DIR", str(built_index.index_dir))
    trace = {"ok": True, "test_id": "tests/test_x.py::test_bug",
             "files": {"sampleproj/core/engine.py": [7, 8, 9, 12]}}
    tf = tmp_path / "trace.json"
    tf.write_text(json.dumps(trace), encoding="utf-8")
    monkeypatch.setenv("REPR_TRACE_FILE", str(tf))

    from servers.dfg_server import server as dfg_srv

    items = dfg_srv.executed_flows(file="sampleproj/core/engine.py",
                                   function="validate_input")
    assert items and "note" not in items[0]
    flags = {it["on_failing_path"] for it in items}
    assert True in flags  # validated defined@7 used@9 -> both executed
    # pairs touching the never-executed append line (10) are off the path
    off = [it for it in items if not it["on_failing_path"]]
    assert off, "expected at least one pair off the failing path"
    # on-path pairs sort first
    assert items[0]["on_failing_path"] is True

    # no-trace behavior
    monkeypatch.setenv("REPR_TRACE_FILE", str(tmp_path / "absent.json"))
    noitems = dfg_srv.executed_flows(file="sampleproj/core/engine.py")
    assert noitems and "note" in noitems[0]


def test_perrep_server_gates_dfg_dynamic_tools(
    dfg_db, built_index, monkeypatch
):
    from servers.perrep import server as perrep_server

    db, _ = dfg_db
    monkeypatch.setenv("REPR_PERREP_REP", "dfg")
    monkeypatch.setenv("REPR_PERREP_DB", str(db))
    monkeypatch.setenv("REPO_ROOT", str(SAMPLEPROJ))
    monkeypatch.setenv("REPO_INDEX_DIR", str(built_index.index_dir))
    monkeypatch.delenv("REPR_TRACE", raising=False)
    static_names = set(perrep_server.create_server().tools)
    assert "executed_flows" not in static_names
    monkeypatch.setenv("REPR_TRACE", "1")
    dyn_names = set(perrep_server.create_server().tools)
    assert {"executed_flows", "covered_blocks"} <= dyn_names
    assert "executed_path" not in dyn_names  # cfg-only tool


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


def test_trace_sanitize_drops_oracle():
    from experiments.swebench_pro.trace import sanitize_for_run

    raw = {
        "ok": True,
        "test_id": "pkg/tests/test_x.py::test_bug",
        "files": {
            "pkg/core/logic.py": [1, 2, 3],
            "pkg/tests/test_x.py": [10, 11],       # gold test (explicit)
            "pkg/tests/sample_fixture.py": [5],     # gold fixture (explicit)
            "pkg/util/test_helpers.py": [7],        # test-looking, not listed
        },
    }
    clean = sanitize_for_run(raw, ["pkg/tests/test_x.py",
                                   "pkg/tests/sample_fixture.py"])
    assert set(clean["files"]) == {"pkg/core/logic.py"}
    assert clean["test_id"] == "(withheld)"
    assert clean["n_files"] == 1
    # original untouched
    assert len(raw["files"]) == 4


def test_build_prompt_dyn_nudge():
    from experiments.swebench_pro.agent import build_prompt

    inst = {"problem_statement": "the bug", "requirements": "",
            "interface": ""}
    plain = build_prompt(inst, explore_tool="mcp__cfg_perrep__cfg_explore")
    assert "Step 1" not in plain
    dyn = build_prompt(inst, explore_tool="mcp__cfg_perrep__cfg_explore",
                       dyn_tool="mcp__cfg_perrep__executed_path")
    assert "Step 0" in dyn and "Step 1" in dyn
    assert "mcp__cfg_perrep__executed_path" in dyn


def test_perrep_server_registers_flow_native_tools(
    cfg_db, dfg_db, built_index, monkeypatch
):
    """create_server() under REPR_PERREP_REP=cfg|dfg exposes the native tools."""
    from servers.perrep import server as perrep_server

    for rep, db_fixture, expected in (
        ("cfg", cfg_db, {"get_cfg", "cfg_paths", "dominators"}),
        ("dfg", dfg_db, {"def_use", "backward_slice", "forward_slice",
                         "data_flow_between"}),
    ):
        db, _ = db_fixture
        monkeypatch.setenv("REPR_PERREP_REP", rep)
        monkeypatch.setenv("REPR_PERREP_DB", str(db))
        monkeypatch.setenv("REPO_ROOT", str(SAMPLEPROJ))
        monkeypatch.setenv("REPO_INDEX_DIR", str(built_index.index_dir))
        server = perrep_server.create_server()
        names = set(server.tools)
        assert {"%s_explore" % rep, "%s_node" % rep} <= names, names
        assert expected <= names, (rep, names)


# -- dfg flow-first (dfg_perrep_flow) ------------------------------------------
#
# The DFG analog of cfg flow-first: under REPR_PERREP_REP=dfg +
# REPR_FLOW_FIRST=1 the server exposes ONLY flow_of/flow_plan/flow_check and
# flow_of renders the def-use (value-flow) view instead of the block CFG.


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


def test_swe_build_prompt_flow_first_dfg_wording():
    from experiments.swebench_pro import agent as swe_agent

    inst = {"problem_statement": "the bug", "requirements": "", "interface": ""}
    dfg_prompt = swe_agent.build_prompt(
        inst,
        flow_first_tools=("mcp__dfg_perrep__flow_of",
                          "mcp__dfg_perrep__flow_plan",
                          "mcp__dfg_perrep__flow_check"))
    assert "VALUE-FLOW" in dfg_prompt
    assert "mcp__dfg_perrep__flow_of" in dfg_prompt
    assert "add_def_use" in dfg_prompt
    assert "CONTROL-FLOW GRAPH" not in dfg_prompt

    cfg_prompt = swe_agent.build_prompt(
        inst,
        flow_first_tools=("mcp__cfg_perrep__flow_of",
                          "mcp__cfg_perrep__flow_plan",
                          "mcp__cfg_perrep__flow_check"))
    assert "CONTROL-FLOW GRAPH" in cfg_prompt
    assert "VALUE-FLOW" not in cfg_prompt


def test_swe_flow_first_prompt_fallback_and_final_verify():
    """The updated protocol: an explicit 'bug not in the flow -> Grep/Read'
    fallback step, and a REQUIRED-LAST flow_verify (final change vs whole
    plan) when the runner passes the 4th tool. 3-tuples (old callers) still
    build a prompt with the fallback but no final-verify step."""
    from experiments.swebench_pro import agent as swe_agent
    inst = {"problem_statement": "the bug", "requirements": "", "interface": ""}
    for rep in ("cfg", "dfg"):
        four = ("mcp__%s_perrep__flow_of" % rep,
                "mcp__%s_perrep__flow_plan" % rep,
                "mcp__%s_perrep__flow_check" % rep,
                "mcp__%s_perrep__flow_verify" % rep)
        p = swe_agent.build_prompt(inst, flow_first_tools=four)
        assert "IF THE BUG IS NOT IN THE FLOW" in p
        assert "Grep" in p and "Read" in p
        assert "mcp__%s_perrep__flow_verify" % rep in p
        assert "REQUIRED LAST" in p
        # backward compat: a 3-tuple must not mention flow_verify
        p3 = swe_agent.build_prompt(inst, flow_first_tools=four[:3])
        assert "IF THE BUG IS NOT IN THE FLOW" in p3
        assert "flow_verify" not in p3


def test_secbench_flow_first_prompt_fallback_and_final_verify():
    from experiments.secbench import run_experiment as sec_rex
    inst = {"sanitizer": "asan", "bug_report": "boom",
            "sanitizer_report": "READ of size 4"}
    for rep in ("cfg", "dfg"):
        four = ("mcp__%s_perrep__flow_of" % rep,
                "mcp__%s_perrep__flow_plan" % rep,
                "mcp__%s_perrep__flow_check" % rep,
                "mcp__%s_perrep__flow_verify" % rep)
        p = sec_rex.build_prompt(inst, flow_first_tools=four)
        assert "IF THE BUG IS NOT IN THE FLOW" in p
        assert "mcp__%s_perrep__flow_verify" % rep in p
        assert "REQUIRED LAST" in p
        p3 = sec_rex.build_prompt(inst, flow_first_tools=four[:3])
        assert "flow_verify" not in p3


def test_swt_runner_sets_testgen_mode():
    """The SWT runner must launch the server in TEST-GEN mode (semantics of
    plan/check/verify flip to source-untouched + test-only-diff) and pass the
    4-tool set to the prompt."""
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


def test_cfgdfg_prompts_combined_wording_all_benchmarks():
    """cfgdfg prompts must take the COMBINED branch (not the dfg branch --
    'dfg_perrep' is a substring of 'cfgdfg_perrep') on all three benchmarks."""
    from experiments.swebench_pro import agent as swe_agent
    from experiments.secbench import run_experiment as sec_rex
    from experiments.swtbench import prompt as swt_prompt
    inst = {"problem_statement": "the bug", "requirements": "", "interface": "",
            "sanitizer": "asan", "bug_report": "boom", "sanitizer_report": "x"}
    four = ("mcp__cfgdfg_perrep__flow_of", "mcp__cfgdfg_perrep__flow_plan",
            "mcp__cfgdfg_perrep__flow_check", "mcp__cfgdfg_perrep__flow_verify")
    for p in (swe_agent.build_prompt(inst, flow_first_tools=four),
              sec_rex.build_prompt(inst, flow_first_tools=four)):
        assert "TWO flow graphs" in p
        assert "CONTROL-FLOW GRAPH" in p and "VALUE-FLOW" in p
        assert "add_guard" in p and "add_def_use" in p
        assert "IF THE BUG IS NOT IN THE FLOW" in p
        assert "mcp__cfgdfg_perrep__flow_verify" in p and "REQUIRED LAST" in p
    swt = swt_prompt.build_prompt(inst, flow_first_tools=four)
    assert "TWO flow graphs" in swt
    assert "IF THE BUG IS NOT IN THE FLOW" in swt
    assert "Do NOT fix the bug" in swt
    assert "mcp__cfgdfg_perrep__flow_verify" in swt


def test_cfgdfg_condition_parsing_all_runners():
    from experiments.swebench_pro import run_experiment as swe_rex
    from experiments.secbench import run_experiment as sec_rex
    assert swe_rex.condition_rep("cfgdfg_perrep_flow") == "cfgdfg"
    assert swe_rex.condition_flow_first("cfgdfg_perrep_flow")
    assert not swe_rex.condition_nlplan("cfgdfg_perrep_flow")
    assert sec_rex.condition_rep("cfgdfg_perrep_flow") == "cfgdfg"
    assert sec_rex.condition_flow_first("cfgdfg_perrep_flow")
    assert "cfgdfg" in swe_rex._REP_BACKENDS
    assert swe_rex._REP_BACKENDS["cfgdfg"] == ("symbols",)


def test_swe_condition_flow_first_accepts_dfg():
    from experiments.swebench_pro import run_experiment as swe_rex

    assert swe_rex.condition_flow_first("cfg_perrep_flow")
    assert swe_rex.condition_flow_first("dfg_perrep_flow")
    assert not swe_rex.condition_flow_first("ast_perrep_flow")
    # the pre-fix A/B variant stays cfg-only
    assert swe_rex.condition_flow_first("cfg_perrep_flow_nofix")
    assert not swe_rex.condition_flow_first("dfg_perrep_flow_nofix")
    assert swe_rex.condition_rep("dfg_perrep_flow") == "dfg"


# -- SWT-bench dfg flow-first prompt wording -----------------------------------


def test_swt_build_prompt_flow_first_dfg_wording():
    from experiments.swtbench import prompt as swt_prompt

    inst = {"problem_statement": "the bug", "requirements": "", "interface": ""}
    dfg = swt_prompt.build_prompt(
        inst,
        flow_first_tools=("mcp__dfg_perrep__flow_of",
                          "mcp__dfg_perrep__flow_plan",
                          "mcp__dfg_perrep__flow_check",
                          "mcp__dfg_perrep__flow_verify"))
    assert "VALUE-FLOW" in dfg and "def-use" in dfg
    assert "mcp__dfg_perrep__flow_of" in dfg
    assert "CONTROL-FLOW GRAPH" not in dfg
    # deliverable is still a TEST, not a fix
    assert "Do NOT fix the bug" in dfg

    cfg = swt_prompt.build_prompt(
        inst,
        flow_first_tools=("mcp__cfg_perrep__flow_of",
                          "mcp__cfg_perrep__flow_plan",
                          "mcp__cfg_perrep__flow_check",
                          "mcp__cfg_perrep__flow_verify"))
    assert "CONTROL-FLOW GRAPH" in cfg and "VALUE-FLOW" not in cfg


def test_swt_prompt_requires_plan_and_final_verify():
    """v2 SWT fix: flow_plan and flow_verify are REQUIRED steps with TEST-GEN
    semantics (plan = the delta a FIX would make; verify = source untouched +
    test-only diff). The old prompt made them optional and agents skipped
    them."""
    from experiments.swtbench import prompt as swt_prompt
    inst = {"problem_statement": "the bug", "requirements": "", "interface": ""}
    for rep in ("cfg", "dfg", "cfgdfg"):
        four = ("mcp__%s_perrep__flow_of" % rep,
                "mcp__%s_perrep__flow_plan" % rep,
                "mcp__%s_perrep__flow_check" % rep,
                "mcp__%s_perrep__flow_verify" % rep)
        p = swt_prompt.build_prompt(inst, flow_first_tools=four)
        assert "(REQUIRED) " in p and "flow_plan" in p
        assert "delta a correct FIX would make" in p
        assert "REQUIRED LAST" in p
        assert "mcp__%s_perrep__flow_verify" % rep in p
        assert "ONLY test files" in p
        assert "IF THE BUG IS NOT IN THE FLOW" in p
        # a 3-tuple (no verify tool) must still build sanely
        p3 = swt_prompt.build_prompt(inst, flow_first_tools=four[:3])
        assert "flow_verify" not in p3


def test_swt_run_cell_rep_dispatch():
    # the runner derives rep from the condition prefix for both flow reps.
    for cond, rep in (("cfg_perrep_flow", "cfg"), ("dfg_perrep_flow", "dfg")):
        ff = cond in ("cfg_perrep_flow", "dfg_perrep_flow")
        assert ff and cond.split("_", 1)[0] == rep
    assert "baseline" not in ("cfg_perrep_flow", "dfg_perrep_flow")


# -- RQ2 NL-plan ablation arm (_nlplan) ----------------------------------------
#
# The ablation control for RQ2: same MAP-FREE flow_of surface and same forced
# "plan-before-edit, then revise" discipline as the flow-first arm, but the
# plan is NATURAL LANGUAGE in the prompt and flow_plan/flow_check are NOT
# registered (no machine verification). Comparing flow-first (checked graph
# plan) vs nlplan (unchecked NL plan) isolates the value of the graph contract.


def test_nlplan_condition_parsing():
    from experiments.swebench_pro import run_experiment as R
    assert R.condition_nlplan("cfg_perrep_nlplan")
    assert R.condition_nlplan("dfg_perrep_nlplan")
    assert not R.condition_nlplan("cfg_perrep_flow")
    assert not R.condition_flow_first("cfg_perrep_nlplan")
    assert R.condition_rep("cfg_perrep_nlplan") == "cfg"
    assert R.condition_rep("dfg_perrep_nlplan") == "dfg"


def test_nlplan_server_exposes_only_flow_of(built_index, monkeypatch):
    from servers.perrep import server as perrep_server
    monkeypatch.setenv("REPR_PERREP_REP", "cfg")
    monkeypatch.setenv("REPR_FLOW_OF_ONLY", "1")
    monkeypatch.delenv("REPR_FLOW_FIRST", raising=False)
    monkeypatch.setenv("REPO_ROOT", str(SAMPLEPROJ))
    monkeypatch.setenv("REPO_INDEX_DIR", str(built_index.index_dir))
    server = perrep_server.create_server()
    assert set(server.tools) == {"flow_of"}, set(server.tools)


def test_nlplan_prompt_forces_written_plan_no_flow_check():
    from experiments.swebench_pro import agent as A
    inst = {"problem_statement": "x", "requirements": "", "interface": ""}
    p = A.build_prompt(inst, nl_plan_tool="mcp__cfg_perrep__flow_of")
    assert "mcp__cfg_perrep__flow_of" in p
    assert "WRITE OUT YOUR PLAN" in p
    assert "plain English" in p
    # the ablation must NOT mention the verification tools
    assert "flow_check" not in p
    assert "flow_plan" not in p
    # map-free: no code-map Step 0, no cfg_explore, no Grep-gate rules
    assert "localize with the code map" not in p
    assert "cfg_explore" not in p
    assert "blocked on purpose" not in p


def test_flow_first_prompt_is_map_free_in_rules():
    # regression: the flow-first arm's Rules section must not tell the agent to
    # "localize using the code map" (there is no map) or that Grep is blocked.
    from experiments.swebench_pro import agent as A
    inst = {"problem_statement": "x", "requirements": "", "interface": ""}
    p = A.build_prompt(inst, flow_first_tools=("mcp__cfg_perrep__flow_of",
                                               "mcp__cfg_perrep__flow_plan",
                                               "mcp__cfg_perrep__flow_check"))
    assert "localize using the code map" not in p.lower()
    assert "blocked on purpose" not in p


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
    # the gated MAP arm still gets the map rules (the gate really blocks Grep)
    pm = A.build_prompt(inst, explore_tool="mcp__ast_perrep__ast_explore")
    assert "code map (explore/node)" in pm
    assert "blocked on purpose" in pm


# -- RQ2 flow_of-only lower-bound control (_flowonly) ---------------------------
#
# The cleanest control: map-free flow_of ONLY, NO forced plan and NO verify.
# Isolates whether the flow VIEW alone helps, separate from any plan/verify
# discipline. Same flow_of-only server as nlplan; only the prompt differs.


def test_flowonly_condition_parsing():
    from experiments.swebench_pro import run_experiment as R
    assert R.condition_flowonly("cfg_perrep_flowonly")
    assert R.condition_flowonly("dfg_perrep_flowonly")
    # must NOT be mistaken for the other arms
    assert not R.condition_flow_first("cfg_perrep_flowonly")
    assert not R.condition_nlplan("cfg_perrep_flowonly")
    assert not R.condition_flowonly("cfg_perrep_flow")
    assert not R.condition_flowonly("cfg_perrep_nlplan")
    assert R.condition_rep("cfg_perrep_flowonly") == "cfg"


def test_flowonly_prompt_has_flow_of_no_plan_no_verify():
    from experiments.swebench_pro import agent as A
    inst = {"problem_statement": "x", "requirements": "", "interface": ""}
    p = A.build_prompt(inst, flow_only_tool="mcp__cfg_perrep__flow_of")
    assert "mcp__cfg_perrep__flow_of" in p
    # no planning, no verification, no map/gate
    assert "WRITE OUT YOUR PLAN" not in p
    assert "flow_plan" not in p
    assert "flow_check" not in p
    assert "localize with the code map" not in p
    assert "blocked on purpose" not in p


# -- flow3 (v3) arm -------------------------------------------------------------
#
# v3 is a NEW ARM (user hard rule): the v2 conditions, their prompt text and
# their tool behavior stay byte-identical; everything flow3 is additive and
# gated (REPR_FLOW_V3=1 server-side, *_perrep_flow3 harness-side).


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
    # never confused with the v2/ablation arms or junk
    for cond in ("cfg_perrep_flow", "dfg_perrep_flow", "cfgdfg_perrep_flow",
                 "cfg_perrep_flowonly", "cfg_perrep_nlplan", "baseline",
                 "ast_perrep_flow3", "cfg_perrep_flow3x", "cfg_perrep_dyn"):
        assert not swe_rex.condition_flow_v3(cond), cond
        assert not sec_rex.condition_flow_v3(cond), cond
        assert not swt_rex.condition_flow_v3(cond), cond
    # the v2 parsers are unchanged by the _flow3 suffix addition
    assert swe_rex.condition_rep("cfg_perrep_flow") == "cfg"
    assert swe_rex.condition_flow_first("cfgdfg_perrep_flow")
    assert sec_rex.condition_rep("cfgdfg_perrep_flow") == "cfgdfg"
    assert sec_rex.condition_flow_first("dfg_perrep_flow")


def test_flow3_mcp_env_swe_and_sec(tmp_path):
    """flow3 cells: REPR_FLOW_FIRST=1 (v2 registration reused) +
    REPR_FLOW_V3=1, and the REPR_FLOW_LEGACY_VERIFY pin is NOT set; the v2
    arms keep their pin and never see REPR_FLOW_V3=1."""
    import json
    from experiments.swebench_pro import run_experiment as swe_rex
    from experiments.secbench import run_experiment as sec_rex
    p = swe_rex.write_mcp_config("cfg", tmp_path, tmp_path, flow_first=True,
                                 flow_v3=True)
    env = json.loads(p.read_text())["mcpServers"]["cfg_perrep"]["env"]
    assert env["REPR_FLOW_FIRST"] == "1"
    assert env["REPR_FLOW_V3"] == "1"
    assert env["REPR_FLOW_LEGACY_VERIFY"] != "1"  # no legacy pin on flow3
    p2 = swe_rex.write_mcp_config("cfg", tmp_path, tmp_path, flow_first=True)
    env2 = json.loads(p2.read_text())["mcpServers"]["cfg_perrep"]["env"]
    assert env2["REPR_FLOW_LEGACY_VERIFY"] == "1"  # v2 keeps its pin
    assert env2.get("REPR_FLOW_V3", "") != "1"
    # SWT flow3 launch: TESTGEN mode + v3, verify tool stays registered
    p3 = swe_rex.write_mcp_config("cfg", tmp_path, tmp_path, flow_first=True,
                                  flow_testgen=True, flow_v3=True)
    env3 = json.loads(p3.read_text())["mcpServers"]["cfg_perrep"]["env"]
    assert env3["REPR_FLOW_TESTGEN"] == "1"
    assert env3["REPR_FLOW_V3"] == "1"
    assert env3["REPR_FLOW_VERIFY"] == ""
    assert env3["REPR_FLOW_LEGACY_VERIFY"] != "1"
    # secbench: same contract (dyn/joern_port are positional there)
    p4 = sec_rex.write_mcp_config("cfg", tmp_path, tmp_path, False, None,
                                  flow_v3=True)
    env4 = json.loads(p4.read_text())["mcpServers"]["cfg_perrep"]["env"]
    assert env4["REPR_FLOW_FIRST"] == "1"  # flow3 reuses the registration
    assert env4["REPR_FLOW_V3"] == "1"
    assert env4["REPR_FLOW_LEGACY_VERIFY"] != "1"
    p5 = sec_rex.write_mcp_config("cfg", tmp_path, tmp_path, False, None,
                                  flow_first=True)
    env5 = json.loads(p5.read_text())["mcpServers"]["cfg_perrep"]["env"]
    assert env5["REPR_FLOW_LEGACY_VERIFY"] == "1"
    assert env5.get("REPR_FLOW_V3", "") != "1"


def test_flow3_server_flow_plan_gains_replace(built_index, monkeypatch):
    """Under REPR_FLOW_V3=1 the flow-first server registers the v3
    flow_plan callable (whose schema adds the optional 'replace' arg) PLUS
    the step-2 flow_locate tool -- exactly 5 tools; the default env keeps
    the exact v2 callable and exactly 4 tools (v2 arms byte-identical)."""
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
    """SWT/SecBench flow3 prompts wrap each benchmark's PLAIN v2 task body
    (no v2 flow steps) with the task-matched v3 protocol; testgen never
    carries repair wording."""
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


def test_v2_prompts_byte_stable_key_lines():
    """HARD-RULE guard: the v2 flow conditions' prompt text and the baseline
    prompt are untouched by the flow3 addition. Assert the exact key lines
    (each parts[] element is one output line) and the absence of any v3
    vocabulary."""
    from experiments.swebench_pro import agent as A
    from experiments.swtbench import prompt as swt_prompt
    from experiments.secbench import run_experiment as sec_rex
    inst = {"problem_statement": "the bug", "requirements": "",
            "interface": "", "sanitizer": "asan", "bug_report": "boom",
            "sanitizer_report": "x"}
    four = ("mcp__cfg_perrep__flow_of", "mcp__cfg_perrep__flow_plan",
            "mcp__cfg_perrep__flow_check", "mcp__cfg_perrep__flow_verify")
    v3_vocab = ("fault_site", "/flowfix", "add_function",
                "Do NOT call flow_plan", "re-check ONCE", "flow_locate")

    p = A.build_prompt(inst, flow_first_tools=four)
    lines = p.splitlines()
    assert ("1. (REQUIRED FIRST) Call `mcp__cfg_perrep__flow_of` with the "
            "function/symbol names the bug report mentions. It returns each "
            "function's real control-flow graph -- its basic blocks and the "
            "true/false/loop edges between them. There is no explore/map "
            "tool; this flow is your surface.") in lines
    assert ("5. Call `mcp__cfg_perrep__flow_check` to confirm your edit "
            "made the control-flow change you planned. If match=false, fix "
            "the edit and re-check once.") in lines
    for bad in v3_vocab:
        assert bad not in p, bad

    b = A.build_prompt(inst)
    assert ("- Localize the bug with your own tools (Grep/Glob to search, "
            "Read to open files), then Edit the source. You must end with "
            "a non-empty patch; never stop with the bug unfixed."
            ) in b.splitlines()
    for bad in v3_vocab:
        assert bad not in b, bad

    swt = swt_prompt.build_prompt(inst, flow_first_tools=four)
    assert ("5. (REQUIRED LAST) Before you stop, call "
            "`mcp__cfg_perrep__flow_verify` (no arguments). It verifies the "
            "SOURCE flow of every planned function is UNCHANGED and that "
            "your diff touches ONLY test files, and lists your test edits. "
            "If it reports a source change or a non-test edit, revert that "
            "and call it once more. Then make sure the test really fails on "
            "the current code, and stop.") in swt.splitlines()
    for bad in v3_vocab:
        assert bad not in swt, bad

    sec = sec_rex.build_prompt(inst, flow_first_tools=four)
    assert ("3. Read the flow to locate the fix: most C security fixes ADD "
            "A GUARD -- a bounds/null/overflow check (an `if (...) "
            "return/break/goto`) that dominates the crashing statement. "
            "Call `mcp__cfg_perrep__flow_plan` to record that one "
            "control-flow change (op + function + anchor + rationale). Plan "
            "EVERY function you intend to touch in this one call."
            ) in sec.splitlines()
    for bad in v3_vocab:
        assert bad not in sec, bad
