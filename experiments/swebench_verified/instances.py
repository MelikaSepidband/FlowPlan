"""Load SWE-bench Verified instances (princeton-nlp/SWE-bench_Verified).

Read straight from the HF parquet blob via pyarrow (no `datasets` dep). Each
instance is a code-FIXING task driven ONLY by a raw GitHub issue
(problem_statement) -- NO interface/requirements field, so unlike SWE-bench Pro
the fault LOCATION is NOT handed to the agent.

Fields used here: repo, instance_id, base_commit, problem_statement,
FAIL_TO_PASS, PASS_TO_PASS, test_patch, environment_setup_commit. We add
repo_url to match the swebench_pro instance shape so the shared checkout/agent
code can consume it.

The parquet is taken from SWEBENCH_VERIFIED_PARQUET if set, otherwise from the
Hugging Face hub cache (download once with
`huggingface-cli download princeton-nlp/SWE-bench_Verified --repo-type dataset`).
The evaluation sample of the paper is data/ids/swebench_verified_218.json.
"""
import glob
import os

_HUB_BLOBS = ("~/.cache/huggingface/hub/"
              "datasets--princeton-nlp--SWE-bench_Verified/blobs/*")


def _resolve_blob():
    import pyarrow.parquet as pq
    explicit = os.environ.get("SWEBENCH_VERIFIED_PARQUET", "").strip()
    if explicit:
        if not os.path.isfile(explicit):
            raise FileNotFoundError(
                "SWEBENCH_VERIFIED_PARQUET does not exist: %s" % explicit)
        return explicit
    # any readable parquet under the hub cache for this dataset
    for c in sorted(glob.glob(os.path.expanduser(_HUB_BLOBS))):
        try:
            pq.read_metadata(c)
            return c
        except Exception:
            continue
    raise FileNotFoundError(
        "SWE-bench Verified parquet not found. Download the dataset "
        "(huggingface-cli download princeton-nlp/SWE-bench_Verified "
        "--repo-type dataset) or point SWEBENCH_VERIFIED_PARQUET at the "
        "test-split parquet file.")


_CACHE = None


def _load_rows():
    import pyarrow.parquet as pq
    return pq.read_table(_resolve_blob()).to_pylist()


def _all():
    global _CACHE
    if _CACHE is None:
        rows = _load_rows()
        for r in rows:
            r["repo_url"] = "https://github.com/%s.git" % r["repo"]
        _CACHE = rows
    return _CACHE


def load_all():
    return list(_all())


def load_subset(ids):
    by = {r["instance_id"]: r for r in _all()}
    return [by[i] for i in ids if i in by]
