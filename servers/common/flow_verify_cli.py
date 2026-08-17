"""Harness-side final flow verification (post-run measurement record).

Runs the SAME plan-vs-final-code check the agent-facing flow_verify tool
performs, but from the RUNNER after the agent finished -- so every flow cell
gets a flow_verify_final.json in its run_dir whether or not the agent called
the tool. Env contract (identical to the perrep server's mcp.json env):
REPO_ROOT, REPO_INDEX_DIR, RUN_DIR, REPR_PERREP_REP, PYTHONPATH; SecBench dfg
cells additionally need JOERN_SERVER_URL/JOERN_USER/JOERN_PASSWORD (so the
runner must invoke this while the per-cell Joern server is still up).

Usage: <venv-python> -m servers.common.flow_verify_cli
Writes $RUN_DIR/flow_verify_final.json and prints the record to stdout.
"""

import json
import os
import sys
from pathlib import Path


def main() -> int:
    try:
        from servers.common import flowedit_tools
        rec = flowedit_tools.flow_verify()[0]
    except Exception as e:  # a measurement record must never crash the runner
        rec = {"match": None, "error": repr(e)}
    out = json.dumps(rec, indent=2, default=str)
    run_dir = os.environ.get("RUN_DIR", "").strip()
    if run_dir:
        try:
            p = Path(run_dir) / "flow_verify_final.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(out, encoding="utf-8")
        except OSError as e:
            rec["write_error"] = repr(e)
            out = json.dumps(rec, indent=2, default=str)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
