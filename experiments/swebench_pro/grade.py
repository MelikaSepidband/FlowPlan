"""Docker test-grader for SWE-bench Pro (official scaleapi flow).

Given an agent run dir (with patch.diff) + its instance, this:
  1. materializes the per-instance grading workspace (patch.diff + the official
     run_script.sh + parser.py + a generated entryscript.sh),
  2. pulls jefzda/sweap-images:<dockerhub_tag>,
  3. docker-runs the image with the workspace mounted; entryscript resets to
     base_commit, applies the patch, runs the selected tests, parses results,
  4. scores 1.0 iff (fail_to_pass | pass_to_pass) ⊆ passed tests, else 0.0.

The per-instance run_script/parser/dockerfiles come from the official harness,
already cloned at OFFICIAL_HARNESS (a shared cache). Adapted from the working
swebench_pro adapter; kept self-contained so it runs from this repo.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

DOCKER_REGISTRY = "jefzda/sweap-images"
# Checkout of the official SWE-bench Pro harness (run_scripts/ + dockerfiles/),
# overridable via env.
OFFICIAL_HARNESS = Path(os.environ.get(
    "SWEBENCH_PRO_HARNESS",
    str(Path.home() / "code_rep_data" / "swebench_pro" / "official_harness")))


def _strip_binary_hunks(diff: str) -> str:
    """Drop binary-file sections git apply can't handle (official harness does)."""
    if not diff:
        return diff
    kept = []
    for sec in re.split(r"(?m)^(?=diff --git )", diff):
        if not sec:
            continue
        if ("Binary files " in sec and " differ" in sec) or "GIT binary patch" in sec:
            continue
        kept.append(sec)
    return "".join(kept)


def _create_entryscript(inst: dict[str, Any], harness: Path) -> str:
    """The in-container script: env, reset, apply patch, run tests, parse."""
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
    env_block = "\n".join(env_cmds)

    before = (inst.get("before_repo_set_cmd") or "").strip()
    before_last = before.split("\n")[-1] if before else ""
    selected = inst.get("selected_test_files_to_run") or []
    selected_arg = ",".join(selected)
    base = inst["base_commit"]
    return f"""#!/bin/bash
{env_block}
cd /app
git reset --hard {base}
git checkout {base}
git apply -v /workspace/patch.diff || true
{before_last}
bash /workspace/run_script.sh {selected_arg} > /workspace/stdout.log 2> /workspace/stderr.log || true
python /workspace/parser.py /workspace/stdout.log /workspace/stderr.log /workspace/output.json || true
"""


def grade(inst: dict[str, Any], run_dir: Path, *, harness: Path = OFFICIAL_HARNESS,
          pull_timeout: int = 1800, run_timeout: int = 1800) -> dict[str, Any]:
    """Grade one agent run; writes grade.json into run_dir, returns the record."""
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
    patch_path = run_dir / "patch.diff"
    if not patch_path.is_file():
        return _fail("no_patch_file")
    patch = _strip_binary_hunks(patch_path.read_text(encoding="utf-8"))
    if not patch.strip():
        return _fail("empty_patch")

    try:
        subprocess.run(["docker", "version"], check=True, capture_output=True,
                       timeout=15)
    except Exception:
        return _fail("docker_unavailable")

    rs_dir = harness / "run_scripts" / iid
    try:
        run_script = (rs_dir / "run_script.sh").read_text("utf-8", errors="replace")
        parser_py = (rs_dir / "parser.py").read_text("utf-8", errors="replace")
        entry = _create_entryscript(inst, harness)
    except FileNotFoundError as e:
        return _fail("harness_files_missing", {"error": str(e)})

    ws = run_dir / "_grader_ws"
    if ws.exists():
        shutil.rmtree(ws, ignore_errors=True)
    ws.mkdir(parents=True)
    (ws / "patch.diff").write_text(patch, encoding="utf-8", newline="\n")
    (ws / "run_script.sh").write_text(run_script, encoding="utf-8", newline="\n")
    (ws / "parser.py").write_text(parser_py, encoding="utf-8", newline="\n")
    (ws / "entryscript.sh").write_text(entry, encoding="utf-8", newline="\n")

    image = f"{DOCKER_REGISTRY}:{tag}"
    # Pull is best-effort: a transient registry hiccup must NOT fail the grade
    # if the image is already cached locally (this caused a false
    # 'docker_pull_failed' miss). Only fail if the pull errors AND the image is
    # absent from the local daemon.
    pull_err = None
    try:
        subprocess.run(["docker", "pull", image], check=True,
                       capture_output=True, text=True, timeout=pull_timeout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        pull_err = getattr(e, "stderr", None) or repr(e)
        local = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True, text=True)
        if local.returncode != 0:
            return _fail("docker_pull_failed",
                         {"image": image, "stderr_tail": str(pull_err)[-400:]})
        # image present locally -> proceed with the cached copy

    host_ws = str(ws.resolve())
    try:
        proc = subprocess.run(
            ["docker", "run", "--rm", "-v", f"{host_ws}:/workspace",
             "--entrypoint", "/bin/bash", image,
             "-c", "bash /workspace/entryscript.sh"],
            capture_output=True, text=True, timeout=run_timeout)
    except subprocess.TimeoutExpired:
        return _fail("docker_run_timeout", {"image": image})

    out_path = ws / "output.json"
    if not out_path.exists():
        return _fail("no_output_json",
                     {"image": image, "container_exit": proc.returncode,
                      "stderr_tail": (proc.stderr or "")[-400:]})
    try:
        output = json.loads(out_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return _fail("invalid_output_json", {"error": str(e)})

    passed = {t["name"] for t in (output.get("tests") or [])
              if t.get("status") == "PASSED"}
    f2p = set(inst.get("fail_to_pass") or [])
    p2p = set(inst.get("pass_to_pass") or [])
    required = f2p | p2p
    missing = sorted(required - passed)
    ok = required.issubset(passed) and len(required) > 0
    rec = {"instance_id": iid, "resolved": bool(ok),
           "score": 1.0 if ok else 0.0, "reason": "f2p+p2p_subset",
           "image": image, "n_passed": len(passed), "n_required": len(required),
           "n_f2p": len(f2p), "n_p2p": len(p2p), "n_missing": len(missing),
           "missing_preview": missing[:5]}
    (run_dir / "grade.json").write_text(json.dumps(rec, indent=2), "utf-8")
    return rec
