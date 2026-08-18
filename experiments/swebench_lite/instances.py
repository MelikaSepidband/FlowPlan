"""Load SWE-bench Lite instances (princeton-nlp/SWE-bench_Lite, TEST split).

Read straight from the HF parquet blob via pyarrow (no `datasets` dep),
mirroring experiments/swebench_verified/instances.py. Lite's hub cache holds
TWO parquet blobs (test = 300 rows, dev = 23 rows); we want the 300-row TEST
split only. Each instance is a code-FIXING task driven ONLY by a raw GitHub
issue (problem_statement) -- no interface/requirements field.

Fields used here: repo, instance_id, base_commit, problem_statement, patch,
test_patch, FAIL_TO_PASS, PASS_TO_PASS, version, environment_setup_commit.
We add repo_url to match the swebench_pro/swebench_verified instance shape so
the shared checkout/agent code can consume it.
"""
import glob
import os

import pyarrow.parquet as pq

_HUB_DIR = ("/home/tempadmin/.cache/huggingface/hub/"
            "datasets--princeton-nlp--SWE-bench_Lite")
_BLOB = (_HUB_DIR + "/blobs/"
         "7a21f37b8bc179c7db5beeb14e88ac538ba283455c776e6b2535bbfb6e3551b4")


def _resolve_blob():
    if os.path.isfile(_BLOB):
        return _BLOB
    # fall back 1: the snapshot symlink for the TEST split parquet
    hub = os.path.expanduser(
        "~/.cache/huggingface/hub/datasets--princeton-nlp--SWE-bench_Lite")
    for c in sorted(glob.glob(hub + "/snapshots/*/data/test-*.parquet")):
        try:
            pq.read_metadata(c)
            return c
        except Exception:
            continue
    # fall back 2: among readable parquet blobs, pick the TEST split (the
    # 300-row one) -- i.e. the one with the most rows, NOT the 23-row dev.
    best, best_rows = None, -1
    for c in glob.glob(hub + "/blobs/*"):
        try:
            n = pq.read_metadata(c).num_rows
        except Exception:
            continue
        if n > best_rows:
            best, best_rows = c, n
    if best is not None:
        return best
    raise FileNotFoundError("SWE-bench Lite test parquet blob not found")


_CACHE = None


def _all():
    global _CACHE
    if _CACHE is None:
        rows = pq.read_table(_resolve_blob()).to_pylist()
        for r in rows:
            r["repo_url"] = "https://github.com/%s.git" % r["repo"]
        _CACHE = rows
    return _CACHE


def load_all():
    return list(_all())


def load_subset(ids):
    idset = set(ids)
    by = {r["instance_id"]: r for r in _all()}
    return [by[i] for i in ids if i in by]
