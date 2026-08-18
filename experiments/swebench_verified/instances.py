"""Load SWE-bench Verified instances (princeton-nlp/SWE-bench_Verified).

Read straight from the HF parquet blob via pyarrow (no `datasets` dep). Each
instance is a code-FIXING task driven ONLY by a raw GitHub issue
(problem_statement) -- NO interface/requirements field, so unlike SWE-bench Pro
the fault LOCATION is NOT handed to the agent. That makes it the benchmark where
a localization representation should actually have something to do.

Fields used here: repo, instance_id, base_commit, problem_statement,
FAIL_TO_PASS, PASS_TO_PASS, test_patch, environment_setup_commit. We add
repo_url + _path-style accessors to match the swebench_pro instance shape so the
shared checkout/agent code can consume it.
"""
import glob
import os

import pyarrow.parquet as pq

_BLOB = ("/home/tempadmin/.cache/huggingface/hub/"
         "datasets--princeton-nlp--SWE-bench_Verified/blobs/"
         "a45b1fe4e2f0c8390b2b2938ac83e92ed5979000856808f3679c07812e9e6dcd")


def _resolve_blob():
    if os.path.isfile(_BLOB):
        return _BLOB
    # fall back: any parquet under the hub cache for this dataset
    cands = glob.glob(os.path.expanduser(
        "~/.cache/huggingface/hub/datasets--princeton-nlp--SWE-bench_Verified/"
        "blobs/*"))
    for c in cands:
        try:
            pq.read_metadata(c)
            return c
        except Exception:
            continue
    raise FileNotFoundError("SWE-bench Verified parquet blob not found")


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
