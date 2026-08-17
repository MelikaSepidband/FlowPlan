"""SWT-bench (test-generation) grader for our study.

Protocol (github.com/logic-star-ai/swt-bench, NeurIPS 2024): the agent produces
a TEST PATCH. We run the test suite TWICE in the instance's Docker image --
once on the buggy tree (base_commit + agent tests) and once on the fixed tree
(base_commit + agent tests + GOLD SOURCE patch) -- and diff the per-test
outcomes:

  FAIL_TO_PASS : fail@buggy  -> pass@fixed   (reproduces the bug -- the good one)
  PASS_TO_FAIL : pass@buggy  -> fail@fixed   (over-fits / breaks fixed code -- bad)
  FAIL_TO_FAIL : fail both                   (broken/irrelevant test -- ignored)
  PASS_TO_PASS : pass both

RESOLVED (S) iff >=1 FAIL_TO_PASS from an AGENT-INTRODUCED test (a test the
agent's patch creates -- in a new file, or added by name to an existing file)
AND no PASS_TO_FAIL regression. Attribution matters: official test files stay
at their base-commit state (they are the PASS_TO_PASS regression set), and a
pre-existing test flipping fail->pass says nothing about the agent's tests.
FAIL_TO_FAIL is NOT a regression (the test was never passing on either tree);
tests SKIPPED in either run are excluded from all categories.

Applicability (W): the generated test patch applies AND at least one of its
tests actually ran (appears in results) in the buggy run.

Reuses the SWE-Pro Docker plumbing (jefzda/sweap images + the official harness
run_script/parser) -- only the entryscript (two runs) and the scoring differ.
NOTE: unlike the SWE-Pro (repair) grader, the instance's fixed-commit TEST
checkout (before_repo_set_cmd's last line) must NOT run here -- see
_sanitize_before.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from experiments.swebench_pro.grade import (
    DOCKER_REGISTRY,
    OFFICIAL_HARNESS,
    _strip_binary_hunks,
)


def _env_block(inst: dict, harness: Path) -> str:
    iid = inst["instance_id"]
    base_df = (harness / "dockerfiles" / "base_dockerfile" / iid /
               "Dockerfile").read_text(encoding="utf-8", errors="replace")
    inst_df = (harness / "dockerfiles" / "instance_dockerfile" / iid /
               "Dockerfile").read_text(encoding="utf-8", errors="replace")
    env_cmds = []
    for content in (base_df, inst_df):
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("ENV"):
                env_cmds.append(line.replace("ENV", "export", 1))
    return "\n".join(env_cmds)


def _sanitize_before(before: str) -> str:
    """The instance's ``before_repo_set_cmd``, filtered for SWT grading.

    In the SWE-Pro (repair) grader the block's last line -- ``git checkout
    <fix-sha> -- <official test files>`` -- is REQUIRED: repair is graded by
    the benchmark's post-fix tests. In SWT grading that same line is a
    GOLD-TEST CLOBBER: it used to run AFTER the agent's test patch in BOTH
    suite runs, silently replacing agent edits to official test files with
    the benchmark's post-fix tests and crediting those gold tests' F2P
    transitions to the agent (vacuous resolves). In SWT the graded trees are
    exactly base+agent-tests and base+agent-tests+gold-SOURCE-patch, with
    official test files at their base-commit state (the P2P regression set),
    so ANY path-restricted checkout from another commit is dropped here.

    Also dropped: bare ``git reset/clean/checkout`` lines (the entryscript
    already hard-resets + cleans to base itself). Anything else -- e.g. a
    conftest fix or other environment setup -- is KEPT for parity with the
    official images. Survey of samples/swebench_pro_n253 (2026-08-11): all
    253 instances' blocks are exactly reset+clean+checkout-base+
    gold-test-checkout, so today NOTHING survives the filter; the keep path
    exists so a future instance's genuine env setup is not lost silently.
    """
    kept = []
    for line in (before or "").splitlines():
        s = line.strip()
        if not s:
            continue
        if re.match(r"^git\s+checkout\s+\S+\s+--(\s|$)", s):
            continue  # fixed-commit file lay-down (the gold-test clobber)
        if re.match(r"^git\s+(reset|clean|checkout)\b", s):
            continue  # base reset/clean: the entryscript does these itself
        kept.append(s)
    return "\n".join(kept)


def _entryscript(inst: dict, harness: Path) -> str:
    """Two runs in one container: buggy+testpatch, then fixed(=+gold)+testpatch.

    Each run: hard-reset to base, apply the agent's test patch, (run 2 also
    applies the gold code patch), run the selected tests, parse to a json.
    Writes /workspace/out_buggy.json and /workspace/out_fixed.json.
    The fixed-commit TEST checkout from before_repo_set_cmd is NOT run
    (gold-test clobber -- see _sanitize_before); official test files stay at
    their base-commit state in both runs.
    """
    env_block = _env_block(inst, harness)
    before_keep = _sanitize_before(inst.get("before_repo_set_cmd") or "")
    # SWT: the agent may add its test to ANY test file (or a new one), so we
    # must run the WHOLE selected test suite, not only the pre-selected files.
    # run_script.sh with NO args -> run_all_tests(); with a file list ->
    # run_selected_tests (would MISS the agent's test if placed elsewhere).
    # We pass the agent's touched test files (union with the selected set) so
    # its new test always runs, even in a file the benchmark didn't preselect.
    selected = list(inst.get("selected_test_files_to_run") or [])
    base = inst["base_commit"]
    selected_csv = ",".join(selected)
    return f"""#!/bin/bash
{env_block}
# test files to run = selected set UNION the files the agent's test patch
# touches (so the agent's new test runs wherever it placed it).
AGENT_TFILES=$(grep -oE '^\\+\\+\\+ b/\\S+' /workspace/test_patch.diff \
  | sed 's|^+++ b/||' | sort -u | tr '\\n' ',' )
RUN_FILES="{selected_csv},$AGENT_TFILES"
RUN_FILES=$(echo "$RUN_FILES" | tr ',' '\\n' | grep -v '^$' | sort -u | tr '\\n' ',')
run_suite() {{
  local out="$1"; shift
  local apply_gold="$1"; shift
  cd /app
  git reset --hard {base} >/dev/null 2>&1
  git checkout {base} >/dev/null 2>&1
  # remove UNTRACKED leftovers from the previous suite run -- reset --hard
  # does NOT delete them, so an agent test file CREATED by run 1 survived
  # into run 2 and made its own `git apply` fail with "already exists"
  # (auto-failing every agent that wrote a brand-new test file; present in
  # all runs before 2026-08-11).
  git clean -fd >/dev/null 2>&1
  # agent's generated test patch (must apply in BOTH states)
  git apply -v /workspace/test_patch.diff 2>/workspace/apply_test_$out.log || \
    echo "TEST_PATCH_APPLY_FAILED" > /workspace/apply_test_$out.err
  if [ "$apply_gold" = "1" ]; then
    git apply -v /workspace/gold_patch.diff 2>/workspace/apply_gold_$out.log || \
      echo "GOLD_APPLY_FAILED" > /workspace/apply_gold_$out.err
  fi
  {before_keep}
  # keep only RUN_FILES entries that EXIST in this run's tree. A selected
  # official test file may exist only at the FIX commit (the gold test patch
  # creates it; pre-fix it reached the tree via the dropped gold-test
  # checkout). Some instances' run_script.sh passes every file to a single
  # pytest invocation, and pytest ABORTS the whole run on one missing path
  # (exit 4, 0 collected) -- silently killing the agent's real tests too.
  RUN_EXIST=""
  for f in $(echo "$RUN_FILES" | tr ',' ' '); do
    [ -e "$f" ] && RUN_EXIST="$RUN_EXIST$f,"
  done
  bash /workspace/run_script.sh "$RUN_EXIST" \
    > /workspace/stdout_$out.log 2> /workspace/stderr_$out.log || true
  python /workspace/parser.py /workspace/stdout_$out.log \
    /workspace/stderr_$out.log /workspace/$out.json || true
}}
run_suite out_buggy 0
run_suite out_fixed 1
"""


def _statuses(out_json: Path) -> dict[str, str]:
    """{test_name: PASSED|FAILED|...} from a parsed run, or {} if missing."""
    if not out_json.is_file():
        return {}
    try:
        data = json.loads(out_json.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    return {t["name"]: t.get("status", "")
            for t in (data.get("tests") or []) if t.get("name")}


def _agent_test_files(test_patch: str) -> tuple[set[str], dict[str, list[str]]]:
    """Parse the agent's test patch for F2P attribution.

    Returns (new_files, added_lines): ``new_files`` are paths the patch
    CREATES (``new file mode`` / ``--- /dev/null``) -- every test collected
    from such a file is agent-authored. ``added_lines`` maps each
    pre-existing file the patch EDITS to the bodies of its added (+) lines --
    a test there is agent-authored only if its name appears in one of them,
    so pre-existing tests in the same file are never credited.
    """
    new_files: set[str] = set()
    added: dict[str, list[str]] = {}
    cur = None
    for line in (test_patch or "").splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            cur = None
            if len(parts) >= 4 and parts[3].startswith("b/"):
                cur = parts[3][2:]
                added.setdefault(cur, [])
            continue
        if cur is None:
            continue
        if line.startswith("new file mode") or line.rstrip() == "--- /dev/null":
            new_files.add(cur)
        elif line.startswith("+") and not line.startswith("+++"):
            added[cur].append(line[1:])
    for p in new_files:
        added.pop(p, None)
    return new_files, added


def _is_agent_test(name: str, new_files: set[str],
                   added: dict[str, list[str]]) -> bool:
    """Is this parsed test id (``path::[Class::]test[params]``) a test the
    agent's patch introduced? New file -> yes (path match); edited existing
    file -> only if the test's NAME appears as a whole identifier in an added
    (+) line (word-boundary match: a pre-existing ``test_foo`` must not be
    credited just because the agent added ``test_foo_extended``)."""
    path = name.split("::", 1)[0]
    if path.startswith("./"):
        path = path[2:]
    if path in new_files:
        return True
    lines = added.get(path)
    if not lines:
        return False
    # strip any [param] part FIRST (params may themselves contain '::'),
    # then take the last ::-component as the bare test-function name.
    leaf = name.split("[", 1)[0].rsplit("::", 1)[-1].strip()
    if not leaf:
        return False
    pat = re.compile(r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])" % re.escape(leaf))
    return any(pat.search(ln) for ln in lines)


def grade(inst: dict[str, Any], run_dir: Path, *,
          harness: Path = OFFICIAL_HARNESS,
          pull_timeout: int = 1800, run_timeout: int = 2400) -> dict[str, Any]:
    """Grade one test-generation run; writes grade.json, returns the record."""
    run_dir = Path(run_dir).resolve()
    iid = inst["instance_id"]
    tag = inst.get("dockerhub_tag")

    def _fail(reason, extra=None):
        rec = {"instance_id": iid, "resolved": False, "score": 0.0,
               "reason": reason, **(extra or {})}
        (run_dir / "grade.json").write_text(json.dumps(rec, indent=2), "utf-8")
        return rec

    if not tag:
        return _fail("no_dockerhub_tag")
    # the agent's output is a TEST patch (its diff vs base, restricted to tests)
    test_patch_path = run_dir / "test_patch.diff"
    if not test_patch_path.is_file():
        return _fail("no_test_patch_file")
    test_patch = _strip_binary_hunks(test_patch_path.read_text(encoding="utf-8"))
    if not test_patch.strip():
        return _fail("empty_test_patch")
    gold = _strip_binary_hunks(inst.get("patch") or "")
    if not gold.strip():
        return _fail("no_gold_patch")

    try:
        subprocess.run(["docker", "version"], check=True, capture_output=True,
                       timeout=15)
    except Exception:
        return _fail("docker_unavailable")

    rs_dir = harness / "run_scripts" / iid
    try:
        run_script = (rs_dir / "run_script.sh").read_text("utf-8", "replace")
        parser_py = (rs_dir / "parser.py").read_text("utf-8", "replace")
        entry = _entryscript(inst, harness)
    except FileNotFoundError as e:
        return _fail("harness_files_missing", {"error": str(e)})

    ws = run_dir / "_grader_ws"
    if ws.exists():
        shutil.rmtree(ws, ignore_errors=True)
    ws.mkdir(parents=True)
    (ws / "test_patch.diff").write_text(test_patch, encoding="utf-8", newline="\n")
    (ws / "gold_patch.diff").write_text(gold, encoding="utf-8", newline="\n")
    (ws / "run_script.sh").write_text(run_script, encoding="utf-8", newline="\n")
    (ws / "parser.py").write_text(parser_py, encoding="utf-8", newline="\n")
    (ws / "entryscript.sh").write_text(entry, encoding="utf-8", newline="\n")

    image = f"{DOCKER_REGISTRY}:{tag}"
    pull_err = None
    try:
        subprocess.run(["docker", "pull", image], check=True,
                       capture_output=True, text=True, timeout=pull_timeout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        pull_err = getattr(e, "stderr", None) or repr(e)
        local = subprocess.run(["docker", "image", "inspect", image],
                               capture_output=True, text=True)
        if local.returncode != 0:
            return _fail("docker_pull_failed",
                         {"image": image, "stderr_tail": str(pull_err)[-400:]})

    host_ws = str(ws.resolve())
    try:
        proc = subprocess.run(
            ["docker", "run", "--rm", "-v", f"{host_ws}:/workspace",
             "--entrypoint", "/bin/bash", image,
             "-c", "bash /workspace/entryscript.sh"],
            capture_output=True, text=True, timeout=run_timeout)
    except subprocess.TimeoutExpired:
        return _fail("docker_run_timeout", {"image": image})

    # test-patch must have APPLIED in both states (applicability W)
    if (ws / "apply_test_out_buggy.err").exists() or \
            (ws / "apply_test_out_fixed.err").exists():
        return _fail("test_patch_apply_failed",
                     {"image": image, "applicable": False,
                      "container_exit": proc.returncode})
    # the gold SOURCE patch must apply in run 2 -- otherwise the 'fixed' run
    # is just the buggy tree again and the verdict would silently come out
    # no_agent_f2p. Surface it as its own reason (resolved stays False either
    # way: a test patch that conflicts with the gold patch is unresolved under
    # official SWT semantics too, and a corrupt gold diff is an infra bug we
    # must see, not mis-bin).
    if (ws / "apply_gold_out_fixed.err").exists():
        return _fail("gold_patch_apply_failed",
                     {"image": image, "container_exit": proc.returncode})

    buggy = _statuses(ws / "out_buggy.json")
    fixed = _statuses(ws / "out_fixed.json")
    if not buggy and not fixed:
        return _fail("no_output_json",
                     {"image": image, "container_exit": proc.returncode,
                      "stderr_tail": (proc.stderr or "")[-400:]})

    # applicability W = the patch applied (checked above) AND >=1 of the
    # agent's own tests actually RAN in the buggy run (appears in its parsed
    # results with any status) -- a patch whose tests were never collected is
    # not an applicable test suite even though git accepted the diff.
    new_files, added = _agent_test_files(test_patch)
    agent_ran = any(_is_agent_test(n, new_files, added) for n in buggy)
    rec = {"instance_id": iid, "applicable": bool(agent_ran), "image": image,
           **_verdict(buggy, fixed, test_patch)}
    (run_dir / "grade.json").write_text(json.dumps(rec, indent=2), "utf-8")
    return rec


def _verdict(buggy: dict[str, str], fixed: dict[str, str],
             test_patch: str) -> dict[str, Any]:
    """Pure SWT verdict from the two parsed status maps + the agent's patch.

    reproduced iff >=1 AGENT-introduced FAIL_TO_PASS and NO PASS_TO_FAIL
    regression. Split out from grade() so it is unit-testable without Docker.
    """
    def is_pass(s):
        return s == "PASSED"

    # categorize every test seen in BOTH runs by its buggy->fixed transition.
    # Tests SKIPPED in either run are excluded outright: a skip is not a
    # verdict, so it can neither qualify as F2P nor count as a regression
    # (pre-fix, a pre-existing test skipped in both runs was binned F2F and
    # failed the cell as a 'regression').
    names = set(buggy) | set(fixed)
    f2p, p2f, f2f, p2p, skipped = [], [], [], [], []
    for n in names:
        b, f = buggy.get(n), fixed.get(n)
        if b is None or f is None:
            continue  # test only ran in one state -> can't categorize
        if b == "SKIPPED" or f == "SKIPPED":
            skipped.append(n)
            continue
        if not is_pass(b) and is_pass(f):
            f2p.append(n)
        elif is_pass(b) and not is_pass(f):
            p2f.append(n)
        elif not is_pass(b) and not is_pass(f):
            f2f.append(n)
        else:
            p2p.append(n)

    # F2P credit goes ONLY to tests the agent's patch introduced (new test
    # file, or a test NAME added to an existing file) -- a pre-existing test
    # flipping fail->pass must not resolve the cell.
    new_files, added = _agent_test_files(test_patch)
    agent_f2p = [n for n in f2p if _is_agent_test(n, new_files, added)]

    # The VERDICT is scoped to the AGENT'S OWN SUITE (official SWT-bench S):
    # >=1 agent test FAIL_TO_PASS and NO agent test PASS_TO_FAIL (an agent
    # test that passes on buggy and fails on fixed is an anti-reproduction
    # test). Repo-suite P2F is NOT the agent's fault and must not gate the
    # verdict: with official test files held at their base-commit state, the
    # gold SOURCE patch legitimately breaks stale base official tests -- a
    # no-agent-patch control run on 42355d18 showed the identical 10 P2F, and
    # 28/30 instances' gold test patches rewrite existing official files, so
    # a global no-P2F rule would make most instances unresolvable for every
    # agent. Repo transitions remain in the record as telemetry.
    agent_p2f = [n for n in p2f if _is_agent_test(n, new_files, added)]

    # F2F is NOT a regression -- the test was never passing on either tree
    # (the agent's own tests failing on both trees simply don't qualify as
    # F2P; they must not additionally fail the cell).
    # This is a REPRODUCTION metric (the agent wrote a test, it did NOT fix the
    # bug), so the primary field is `reproduced`. `resolved` is kept as an alias
    # only for code that reuses the SWE-Pro schema -- it does NOT mean the bug
    # was fixed here.
    reproduced = (len(agent_f2p) >= 1 and len(agent_p2f) == 0)
    return {
        "reproduced": reproduced,
        "resolved": reproduced,  # alias (schema compat) -- means 'reproduced'
        "score": 1.0 if reproduced else 0.0,
        "reason": ("agent_f2p_no_regression" if reproduced else
                   ("no_agent_f2p" if not agent_f2p else "agent_test_p2f")),
        "n_agent_p2f": len(agent_p2f),
        "pass_to_fail_agent": sorted(agent_p2f),
        "fail_to_pass": sorted(f2p),
        "agent_fail_to_pass": sorted(agent_f2p),
        "pass_to_fail": sorted(p2f),
        "fail_to_fail": sorted(f2f),
        "skipped_excluded": sorted(skipped),
        "n_f2p": len(f2p), "n_agent_f2p": len(agent_f2p), "n_p2f": len(p2f),
        "n_f2f": len(f2f), "n_p2p": len(p2p),
    }
