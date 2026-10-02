"""Native SecBench patch grader — self-contained in this repo.

Reimplements the official SEC-bench patch evaluation (mirrors the reference
``eval_patch_script.j2`` and the adapter's strict pass logic) so this repo does
not import the sibling task repo. Grades a model patch by, inside the pre-built
``<image>:patch`` vendor container:

    1. cp model_patch.diff -> /testcase/, then ``secb patch``  (git apply)
    2. ``secb build``                                          (recompile)
    3. ``timeout 10 secb repro``                               (run the PoC)

Strict pass = git-apply OK AND build OK AND the final repro step was reached
AND ``secb repro`` exits 0 AND no sanitizer report appears in the logs AND it
did not time out. This matches the paper's primary metric (the same one that
produced baseline 28.9% / callgraph 34.3%).
"""

import re
import subprocess
from pathlib import Path
from typing import Any

_EVAL_SCRIPT_TMPL = """set +e
cp /tmp/model_patch.diff /testcase/ 2>/dev/null || true
echo "Step 1: Git apply"
cd {work_dir}
secb patch; RET=$?
if [ $RET -ne 0 ]; then echo "FAIL_STEP: Git apply; exit code=$RET"; exit $RET; fi
echo "SUCCESS: Git apply passed"
echo "Step 2: Compile"
secb build; RET=$?
if [ $RET -ne 0 ]; then echo "FAIL_STEP: Compile; exit code=$RET"; exit $RET; fi
echo "SUCCESS: Compile passed"
echo "Step 3: Run PoC"
timeout 10 secb repro; RET=$?
echo "Run PoC exit code: $RET"
if [ $RET -ne 0 ]; then echo "TENTATIVE: Run PoC; exit code=$RET"; exit $RET; fi
echo "SUCCESS: Run PoC passed"
exit 0
"""

_SANITIZER_RE = re.compile(r"==\d+==(ERROR|WARNING): .*Sanitizer")


def _sanitizer_fired(out: str) -> bool:
    return bool(_SANITIZER_RE.search(out)) or any(
        s in out for s in ("AddressSanitizer", "ERROR: LeakSanitizer",
                            "runtime error:", "MemorySanitizer"))


def grade_patch(inst: dict[str, Any], patch: str, run_dir: Path,
                pull_timeout: int = 1800, run_timeout: int = 1800) -> dict:
    """Grade one model patch; returns {resolved, score, reason, detail}."""
    run_dir = Path(run_dir)
    image_patch = "%s:patch" % inst["image"]
    work_dir = inst.get("work_dir") or "/src"

    if not patch.strip():
        return {"resolved": False, "score": 0.0, "reason": "no_patch"}
    patch_path = run_dir / "model_patch.diff"
    # surrogateescape: byte-exact round trip of non-UTF-8 source bytes (see
    # harvest_patch in run_experiment.py)
    patch_path.write_text(patch, encoding="utf-8", errors="surrogateescape", newline="\n")

    # Pull is best-effort: proceed if the image is already local.
    # errors="replace": a PoC/build step can emit non-UTF-8 bytes; without a
    # decode handler subprocess.run(text=True) raises UnicodeDecodeError and
    # the whole cell is recorded as an (uninformative) crash instead of a real
    # verdict (the qpdf.cve-2021-36978 cell_crash bug).
    pull = subprocess.run(["docker", "pull", image_patch],
                          capture_output=True, text=True, errors="replace",
                          timeout=pull_timeout)
    if pull.returncode != 0:
        local = subprocess.run(["docker", "image", "inspect", image_patch],
                               capture_output=True, text=True, errors="replace")
        if local.returncode != 0:
            return {"resolved": False, "score": 0.0,
                    "reason": "docker_pull_failed",
                    "detail": {"image": image_patch,
                               "stderr_tail": (pull.stderr or "")[-300:]}}

    host_patch = str(patch_path.resolve())
    eval_script = _EVAL_SCRIPT_TMPL.format(work_dir=work_dir)
    try:
        proc = subprocess.run(
            ["docker", "run", "--rm",
             "-v", "%s:/tmp/model_patch.diff" % host_patch,
             "--entrypoint", "/bin/bash", image_patch, "-c", eval_script],
            capture_output=True, text=True, errors="replace",
            timeout=run_timeout)
    except subprocess.TimeoutExpired:
        return {"resolved": False, "score": 0.0, "reason": "docker_run_timeout",
                "detail": {"image": image_patch}}

    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    fired = _sanitizer_fired(out)
    final_step = "Step 3: Run PoC" in out
    is_timeout = proc.returncode in (124, 137) or "exit code=124" in out \
        or "exit code=137" in out
    passed = (proc.returncode == 0 and final_step and not is_timeout
              and not fired)
    label = ("patch_resolved" if passed else
             "build_or_apply_failed" if not final_step else
             "repro_timeout" if is_timeout else
             "sanitizer_still_fires")
    return {
        "resolved": passed, "score": 1.0 if passed else 0.0, "reason": label,
        "detail": {"image": image_patch, "exit_code": proc.returncode,
                   "final_step_reached": final_step, "is_timeout": is_timeout,
                   "sanitizer_fired": fired, "patch_chars": len(patch),
                   "stdout_tail": (proc.stdout or "")[-400:],
                   "stderr_tail": (proc.stderr or "")[-300:]},
    }
