"""Runner-side hook: record the final plan-vs-code verdict for a flow cell.

Spawns servers.common.flow_verify_cli with the SAME env the cell's perrep
server ran under, so after every flow-arm agent run the run_dir carries a
flow_verify_final.json -- the FINAL diff checked against the committed
flow_plan -- independent of whether the agent called the flow_verify tool
itself. Measurement only: failures are recorded, never raised.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


def record_final_verify(venv_python, study_root, rep: str, worktree,
                        run_dir, extra_env: dict | None = None,
                        timeout: int = 300) -> None:
    env = dict(os.environ)
    # never inherit a stale Joern connection from the orchestrator's shell --
    # only extra_env (the cell's own Joern server, if any) may set these.
    for k in ("JOERN_SERVER_URL", "JOERN_USER", "JOERN_PASSWORD"):
        env.pop(k, None)
    env.update({
        "REPO_ROOT": str(worktree),
        "REPO_INDEX_DIR": str(Path(worktree) / ".repr_index"),
        "RUN_DIR": str(run_dir),
        "PYTHONPATH": str(study_root),
        "REPR_PERREP_REP": rep,
        # frozenset repr order is PYTHONHASHSEED-dependent, so fingerprint
        # reprs written by one process could never be string-compared by
        # another; object comparison (fix A in flowedit_tools) is the real
        # fix -- this pins the CLI's seed for reproducibility of the
        # recorded reprs themselves.
        "PYTHONHASHSEED": "0",
    })
    if extra_env:
        env.update({k: str(v) for k, v in extra_env.items()
                    if v is not None and str(v) != ""})
    record = Path(run_dir) / "flow_verify_final.json"
    err = None
    try:
        res = subprocess.run(
            [str(venv_python), "-m", "servers.common.flow_verify_cli"],
            env=env, cwd=str(study_root), capture_output=True, text=True,
            timeout=timeout)
        if res.returncode != 0:
            err = "cli exit %d: %s" % (res.returncode,
                                       (res.stderr or "")[-2000:])
    except (OSError, subprocess.SubprocessError) as e:
        err = repr(e)
    if err is not None and not record.is_file():
        try:
            record.write_text(json.dumps({"match": None, "error": err}),
                              "utf-8")
        except OSError:
            pass
