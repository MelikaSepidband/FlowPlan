"""Shared access to the failing-test execution trace for the *dyn* rep arms.

The harness's trace collector (experiments/swebench_pro/trace.py) records the
lines each repo-relative file executed during the bug's failing-test run and
writes the result to ``<repo>/.perrep/trace.json`` (overridable via
``REPR_TRACE_FILE``). The cfg server's ``executed_path``/``covered_blocks``
and the dfg server's ``executed_flows`` read it through these helpers.
"""

import json
import os
from pathlib import Path
from typing import Optional

from servers.common import env

__all__ = ["load_trace", "lines_for", "NO_TRACE_NOTE"]

NO_TRACE_NOTE = (
    "no execution trace is available in this run (the failing test was not "
    "traced); reason from the collector, if any, is in 'reason'"
)


def load_trace() -> Optional[dict]:
    """The failing-test trace dict, or None when absent/unreadable."""
    p = os.environ.get("REPR_TRACE_FILE", "").strip()
    path = Path(p) if p else env.repo_root() / ".perrep" / "trace.json"
    try:
        trace = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return trace if isinstance(trace, dict) else None


def lines_for(trace: dict, rel_file: str) -> Optional[set[int]]:
    """Executed-line set for one repo-relative file, or None if untraced."""
    files = trace.get("files") or {}
    if rel_file in files:
        return set(files[rel_file])
    norm = rel_file.replace("\\", "/")
    for key, lines in files.items():
        if key == norm or key.endswith("/" + norm) or norm.endswith("/" + key):
            return set(lines)
    return None
