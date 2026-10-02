"""Materialize a leak-free host checkout of a SecBench C repo at base_commit.

Mirrors the SecBench adapter's clone flow (mirror -> --shared clone -> checkout
base) but adds the git-history isolation the original adapter lacks: after
checking out base, the fix commit is still reachable through the shared mirror,
so an agent can ``git show <fix-sha>:<file>`` to recover the gold patch (the
documented SWE-bench cheating vector, arXiv 2607.07946). We rewrite the
checkout's .git into a single base-snapshot repo so no future commit / fix ref
is reachable. Grading is unaffected: the official ``secb patch`` re-applies the
model diff in a fresh vendor container.
"""

import shutil
import subprocess
from pathlib import Path
from typing import Any

from experiments.secbench import instances as sec_inst

CACHE_ROOT = Path.home() / "secbench_cache"
MIRRORS = CACHE_ROOT / "mirrors"
CHECKOUTS = CACHE_ROOT / "checkouts"
HOST_REPO_DIR = "secbench_repo"


def _run(cmd, cwd=None, check=False, timeout=1800):
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=True,
                          text=True, timeout=timeout)


# dataset `repo` -> current GitHub clone URL, for projects that moved (2026-09-19)
REPO_URL_OVERRIDES = {
    "matio/matio": "https://github.com/tbeu/matio.git",
}


def _safe(name: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9_.-]+", "__", (name or "").strip())[:120] or "x"


def ensure_mirror(inst: dict[str, Any]) -> Path:
    """A cached bare mirror of the instance's upstream repo.

    Concurrency-safe: two cells for the SAME repo (e.g. exiv2.cve-A and
    exiv2.cve-B under --concurrency>1) would otherwise both see the mirror
    absent and race a ``git clone --mirror`` into the same path (both fail
    rc=128). We serialize per-repo with an flock on a sibling lockfile, and
    clone into a temp dir then atomically rename so a partial clone is never
    observed as "exists".
    """
    import fcntl

    mirror = MIRRORS / _safe(inst.get("repo", ""))
    mirror.parent.mkdir(parents=True, exist_ok=True)
    lock_path = mirror.parent / (_safe(inst.get("repo", "")) + ".lock")
    with open(lock_path, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if mirror.exists():
            _run(["git", "-C", str(mirror), "fetch", "--prune"], timeout=900)
            return mirror
        tmp = mirror.parent / (mirror.name + ".tmp")
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        # Upstream repositories that moved since the dataset was built: the
        # dataset's `repo` field no longer resolves on GitHub (clone rc=128),
        # which is why the 3 matio instances were missing from the earlier
        # 166-id list. Same code, same base commits; only the clone URL differs.
        url = REPO_URL_OVERRIDES.get(inst.get("repo", ""), inst["repo_url"])
        _run(["git", "clone", "--mirror", url, str(tmp)],
             check=True, timeout=1800)
        tmp.rename(mirror)
        return mirror


def isolate_history(wt: Path) -> str:
    """Rewrite <wt>/.git into a fresh single-commit repo (base snapshot).

    Same recipe as experiments/swebench_pro/checkout.isolate_worktree_history,
    minus the shared-worktree registry handling: this is a plain clone dir, so
    we just remove .git and re-init + commit the working tree. Returns the new
    base sha.
    """
    wt = Path(wt)
    git = wt / ".git"
    if git.is_dir():
        shutil.rmtree(git, ignore_errors=True)
    elif git.is_file():
        git.unlink()
    _run(["git", "init", "-q", "-b", "main", str(wt)])
    _run(["git", "config", "user.email", "harness@study"], cwd=wt)
    _run(["git", "config", "user.name", "harness"], cwd=wt)
    _run(["git", "add", "-A", "--", ":(exclude).perrep", ":(exclude).repr_index"],
         cwd=wt)
    _run(["git", "commit", "-q", "--no-verify", "-m", "base snapshot"], cwd=wt)
    return _run(["git", "rev-parse", "HEAD"], cwd=wt).stdout.strip()


def verify_no_oracle(inst: dict[str, Any], wt: Path) -> dict:
    """Assert the gold fix is unreachable from the solving checkout.

    SecBench does not lay gold TEST files into the tree (the crash repro is
    in-container), so the only oracle is the fix commit / gold patch. After
    isolate_history the fix commit must not resolve. We do not know the fix
    SHA from the sample row directly, so we assert (a) HEAD is the lone commit
    and (b) no remotes/mirror alternates remain reachable.
    """
    log_count = _run(["git", "rev-list", "--count", "HEAD"], cwd=wt).stdout.strip()
    remotes = _run(["git", "remote"], cwd=wt).stdout.strip()
    alternates = (Path(wt) / ".git" / "objects" / "info" / "alternates")
    return {
        "commit_count": log_count,
        "remotes": remotes,
        "has_alternates": alternates.exists(),
        "clean": log_count == "1" and not remotes and not alternates.exists(),
    }


def prepare_checkout(inst: dict[str, Any], suffix: str = "") -> Path:
    """A leak-free host repo at base_commit under CHECKOUTS/<id>[__suffix].

    Returns the path to the host repo dir the agent edits (also the perrep
    REPO_ROOT). Idempotent.
    """
    base = inst.get("base_commit") or ""
    if not base:
        raise ValueError("instance has no base_commit")
    key = inst["instance_id"] + (("__" + suffix) if suffix else "")
    wt = CHECKOUTS / key
    if wt.exists():
        shutil.rmtree(wt, ignore_errors=True)
    wt.parent.mkdir(parents=True, exist_ok=True)

    mirror = ensure_mirror(inst)
    _run(["git", "clone", "--shared", str(mirror), str(wt)], check=True,
         timeout=900)
    _run(["git", "-C", str(wt), "checkout", "--force", base], check=True,
         timeout=300)
    _run(["git", "-C", str(wt), "submodule", "update", "--init", "--recursive"],
         timeout=900)
    # isolate BEFORE the agent (and before any index/CPG build reads git) so
    # `git show <fix>:path` cannot recover the gold patch.
    isolate_history(wt)
    return wt


def cleanup_checkout(inst: dict[str, Any], suffix: str = "") -> None:
    key = inst["instance_id"] + (("__" + suffix) if suffix else "")
    wt = CHECKOUTS / key
    if wt.exists():
        shutil.rmtree(wt, ignore_errors=True)
