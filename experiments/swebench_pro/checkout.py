"""Host-side repo checkout for SWE-bench Pro.

Architecture: the agent runs on the HOST against a clean checkout of the repo at
``base_commit`` (Docker is only the test oracle later). To keep it fast across
253 instances over 3 repos, we keep ONE bare-ish cached clone per repo and, per
instance, create a fresh working tree at the instance's base_commit via
``git worktree`` (cheap, no re-download). ``before_repo_set_cmd`` then resets to
base_commit and lays the gold test files into the tree.

The agent edits files in the worktree; the produced patch = ``git diff`` of the
worktree against base_commit (EXCLUDING the test files, which were laid down by
the harness, not the agent -- SWE-bench grades the agent's NON-test changes).
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

CACHE_ROOT = Path(os.environ.get(
    "SWEBENCH_CACHE", str(Path.home() / "swebench_cache")))
CLONES = CACHE_ROOT / "clones"        # one cached clone per repo
WORKTREES = CACHE_ROOT / "worktrees"  # per-instance working trees


def _run(cmd: list[str], cwd: Path | None = None, check: bool = True,
         timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=check,
                          capture_output=True, text=True, timeout=timeout)


def _safe_name(repo: str) -> str:
    return repo.replace("/", "__")


def ensure_clone(repo: str, repo_url: str) -> Path:
    """Cached full clone of ``repo`` (fetched once, reused for all instances)."""
    dest = CLONES / _safe_name(repo)
    if (dest / ".git").is_dir():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "clone", "--filter=blob:none", repo_url, str(dest)],
         timeout=1800)
    return dest


def prepare_worktree(inst: dict[str, Any], suffix: str = "",
                     apply_gold_tests: bool = False) -> Path:
    """A fresh working tree at the instance's base_commit.

    ``suffix`` isolates per-condition worktrees (e.g. one per (instance,
    condition)) so parallel cells that EDIT the tree do not collide. Idempotent:
    an existing worktree at this key is wiped and rebuilt clean.

    ``apply_gold_tests=False`` (the default, and the CORRECT behavior): do NOT
    run ``before_repo_set_cmd``. Per the official SWE-bench Pro harness
    (scaleapi/SWE-bench_Pro-os, create_entryscript), that field is a
    GRADING-TIME command executed inside the evaluation container after the
    candidate patch -- it is what deposits the oracle test files for the test
    run. Executing it in the solving environment hands the agent the very
    tests it is graded against (this harness did exactly that until
    2026-07-27). ``apply_gold_tests=True`` reproduces the old leaky behavior
    for ablation only.
    """
    repo, url, commit = inst["repo"], inst["repo_url"], inst["base_commit"]
    clone = ensure_clone(repo, url)
    have = _run(["git", "cat-file", "-e", commit + "^{commit}"], cwd=clone,
                check=False)
    if have.returncode != 0:
        _run(["git", "fetch", "origin", commit], cwd=clone, check=False,
             timeout=1800)

    key = inst["instance_id"] + (("__" + suffix) if suffix else "")
    wt = WORKTREES / key
    if wt.exists():
        _run(["git", "worktree", "remove", "--force", str(wt)], cwd=clone,
             check=False)
        if wt.exists():
            shutil.rmtree(wt, ignore_errors=True)
    # Prune stale registry entries first: isolate_worktree_history() replaces a
    # worktree's .git with a standalone repo, which orphans the clone's
    # registry entry ("gitdir points to non-existent location"); a later
    # `git worktree add` at that path then fails with 128. Pruning clears
    # every such dangling entry before we add.
    _run(["git", "worktree", "prune"], cwd=clone, check=False)
    wt.parent.mkdir(parents=True, exist_ok=True)
    add = _run(["git", "worktree", "add", "--detach", str(wt), commit],
               cwd=clone, check=False)
    if add.returncode != 0:
        # last-ditch: force-remove any lingering registration for this exact
        # path, prune again, and retry once.
        _run(["git", "worktree", "remove", "--force", str(wt)], cwd=clone,
             check=False)
        _run(["git", "worktree", "prune"], cwd=clone, check=False)
        shutil.rmtree(wt, ignore_errors=True)
        _run(["git", "worktree", "add", "--detach", str(wt), commit],
             cwd=clone, check=True)

    if apply_gold_tests:
        setup = inst.get("before_repo_set_cmd") or ""
        if setup.strip():
            _run(["bash", "-lc", setup], cwd=wt, check=False)
    return wt


# .repr_index cache keyed by repo@base_commit -- the expensive symbols+callgraph
# index is identical for all conditions on one instance, so build it ONCE here
# and copy into each per-condition worktree.
REPR_CACHE = CACHE_ROOT / "repr_index"


def repr_index_cache_key(inst: dict[str, Any]) -> Path:
    # "-nt" (no-tests) suffix: caches built before 2026-07-27 were built from
    # worktrees that still contained the harness-laid GOLD test files (the
    # strip_gold_tests fix below), so their symbol/doc rows leak gold-test
    # content into the maps. The new key forces a clean rebuild.
    safe = inst["repo"].replace("/", "__") + "@" + inst["base_commit"][:12]
    return REPR_CACHE / (safe + "-nt")


def test_patch_paths(inst: dict[str, Any]) -> list[str]:
    """Repo-relative paths the benchmark's gold-test laydown touches.

    Union of the ``test_patch`` diff's target paths and any explicit paths in
    ``before_repo_set_cmd``'s ``git checkout <commit> -- <paths>`` lines (the
    two mechanisms that write FIX-side test files into the tree)."""
    import re as _re
    paths: list[str] = []
    for line in (inst.get("test_patch") or "").splitlines():
        if line.startswith("diff --git "):
            m = _re.search(r" b/(\S+)$", line)
            if m:
                paths.append(m.group(1))
    for line in (inst.get("before_repo_set_cmd") or "").splitlines():
        s = line.strip()
        if s.startswith("git checkout ") and " -- " in s:
            paths.extend(s.split(" -- ", 1)[1].split())
    out: list[str] = []
    for p in paths:
        p = p.strip().lstrip("./")
        if p and p not in out:
            out.append(p)
    return out


def isolate_worktree_history(wt: Path) -> str:
    """Replace the worktree's shared git with a single-snapshot standalone repo.

    A ``git worktree`` shares the cached clone's object database, so even
    though the fix commit is unreachable from HEAD the agent can still recover
    the oracle with ``git show <fix-sha>:<path>`` (the blob is in the shared
    store, and the sha is in the benchmark metadata). Observed live: an agent
    ran ``git show HEAD:...test_vendors.py`` 29× and recovered the 17 KB oracle.

    We make history-spelunking impossible: re-init ``<wt>/.git`` as a fresh
    repo and commit the CURRENT working tree (post strip_gold_tests) as the
    lone commit. No fix commit, no gold-test blobs, no remotes, no 20k-commit
    history remain reachable. The agent's patch is still extracted as a diff
    vs this base snapshot (``diff_excluding_tests`` uses ``inst['base_commit']``
    -> we return the NEW base sha for callers that need it). Grading is
    unaffected: the official evaluation container re-checks-out the repo and
    lays the fix tests down itself.

    Returns the new single-commit base sha.
    """
    wt = Path(wt)
    # Deregister from the shared clone BEFORE detaching, so we don't leave an
    # orphaned "gitdir points to non-existent location" entry that later
    # breaks `git worktree add`. The .git here is a FILE pointing at
    # <clone>/.git/worktrees/<name>; read the clone dir from it, then ask the
    # clone to forget this worktree (metadata only -- the tree stays on disk).
    gitlink = wt / ".git"
    clone_dir = None
    if gitlink.is_file():
        try:
            txt = gitlink.read_text(encoding="utf-8").strip()
            if txt.startswith("gitdir:"):
                gd = Path(txt.split(":", 1)[1].strip())
                # .../<clone>/.git/worktrees/<name> -> <clone>
                if "worktrees" in gd.parts:
                    i = gd.parts.index("worktrees")
                    clone_dir = Path(*gd.parts[:i - 1]) if i >= 1 else None
        except OSError:
            pass
    # remove the gitdir link/dir, re-init fresh.
    if gitlink.is_file() or gitlink.is_dir():
        if gitlink.is_dir():
            shutil.rmtree(gitlink, ignore_errors=True)
        else:
            gitlink.unlink()
    if clone_dir and (clone_dir / ".git").exists():
        _run(["git", "worktree", "prune"], cwd=clone_dir, check=False)
    _run(["git", "init", "-q", "-b", "main", str(wt)], check=False)
    _run(["git", "config", "user.email", "harness@study"], cwd=wt, check=False)
    _run(["git", "config", "user.name", "harness"], cwd=wt, check=False)
    _run(["git", "add", "-A", "--",
          ":(exclude).perrep", ":(exclude).repr_index", ":(exclude).codegraph"],
         cwd=wt, check=False)
    _run(["git", "commit", "-q", "--no-verify", "-m", "base snapshot"],
         cwd=wt, check=False)
    sha = _run(["git", "rev-parse", "HEAD"], cwd=wt, check=False).stdout.strip()
    return sha


def gold_fix_sha(inst: dict[str, Any]) -> str | None:
    """The FIX-side commit sha named in before_repo_set_cmd's test checkout."""
    import re as _re
    for line in (inst.get("before_repo_set_cmd") or "").splitlines():
        m = _re.search(r"git checkout ([0-9a-f]{40}) -- ", line.strip() + " ")
        if m:
            return m.group(1)
    return None


def verify_no_oracle(inst: dict[str, Any], wt: Path) -> dict:
    """Per-cell leakage guard (DeepSWE 'Sanitized' recipe): assert the oracle
    is unreachable before the agent starts.

    Checks, in the agent's worktree: (1) no gold-test file on disk, (2) the
    fix commit is NOT resolvable (git cat-file must fail -- blocks
    `git show <fix>:<path>` recovery, the vector 63% of Opus SWE-bench-Pro
    resolutions exploited per arXiv 2607.07946). Returns a record for
    run_dir/oracle_check.json; ``clean`` False means the cell must not run.
    """
    on_disk = [p for p in test_patch_paths(inst) if (Path(wt) / p).exists()]
    fix = gold_fix_sha(inst)
    fix_reachable = False
    if fix:
        fix_reachable = _run(
            ["git", "cat-file", "-e", fix + "^{commit}"], cwd=wt, check=False,
        ).returncode == 0
    return {
        "gold_tests_on_disk": on_disk,
        "fix_sha": fix,
        "fix_commit_reachable": fix_reachable,
        "clean": not on_disk and not fix_reachable,
    }


def strip_gold_tests(inst: dict[str, Any], wt: Path) -> list[str]:
    """Remove the gold-test laydown from the agent's worktree (leakage fix).

    ``before_repo_set_cmd`` checks the benchmark's FIX-side (oracle) test
    files out into the tree the agent works in, where an agent can simply
    ``cat`` them and reverse-engineer the fix (observed in the flowsmoke
    transcripts). We DELETE every such path outright -- not merely restore it
    to base_commit: for a test that already existed at base, the base version
    is still oracle-shaped test code that names the target API (and the smoke
    audit showed it still gets read), and the fix-side additions are exactly
    the assertions we must hide. The agent then sees the repo as a developer
    filing the bug would: source present, no test telling it the answer.

    Grading is unaffected -- the official evaluation container lays the FIX
    test files down itself before running them, independent of this worktree.
    Returns the paths actually removed. Also unstages any prior staging so a
    deleted-then-restored base file cannot sneak into the extracted patch.
    """
    stripped: list[str] = []
    for rel in test_patch_paths(inst):
        target = Path(wt) / rel
        removed = False
        if target.exists():
            try:
                target.unlink()
                removed = True
            except OSError:
                pass
        # drop it from the index too, so `git diff --cached base` (the patch
        # extractor) never resurrects a base-tracked version of the oracle.
        _run(["git", "rm", "-q", "--cached", "--ignore-unmatch", "--", rel],
             cwd=wt, check=False)
        if removed:
            stripped.append(rel)
    return stripped


# Tool artifacts we drop into the worktree (rep maps / indexes). They are NOT
# the agent's work and must never appear in the extracted patch.
TOOL_ARTIFACT_PREFIXES = (".perrep/", ".repr_index/", ".codegraph/")


def diff_excluding_tests(inst: dict[str, Any], wt: Path) -> str:
    """The agent's patch: git diff of the worktree vs its base snapshot, with
    the harness-applied test files AND our tool-artifact dirs (.perrep/,
    .repr_index/, .codegraph/) removed -- none are the agent's work.

    When the worktree history was isolated (isolate_worktree_history), the
    base snapshot is HEAD; otherwise it is the instance's base_commit. We
    detect isolation by checking whether base_commit resolves in this repo."""
    base = inst["base_commit"]
    if _run(["git", "cat-file", "-e", base + "^{commit}"], cwd=wt,
            check=False).returncode != 0:
        base = "HEAD"  # isolated single-snapshot repo
    # stage everything (so new files appear in the diff) EXCEPT the tool dirs,
    # then diff against base.
    # .cmind / .claude / .mcp.json: the CoderMind arm's own workspace files
    # (baseline_codermind), laid by `cmind init` -- tool artifacts, not the
    # agent's work; no other arm creates them.
    _run(["git", "add", "-A", "--",
          ":(exclude).perrep", ":(exclude).repr_index", ":(exclude).codegraph",
          ":(exclude).cmind", ":(exclude).claude", ":(exclude).mcp.json"],
         cwd=wt, check=False)
    res = _run(["git", "diff", "--cached", base, "--",
                ":(exclude).perrep", ":(exclude).repr_index",
                ":(exclude).codegraph", ":(exclude).cmind", ":(exclude).claude",
                ":(exclude).mcp.json"], cwd=wt, check=False)
    full = res.stdout
    # Strip BOTH the tests selected for grading AND every gold-test path the
    # benchmark lays down (test_patch + before_repo_set_cmd checkouts). The
    # latter matters after strip_gold_tests: a base-tracked oracle we deleted
    # would otherwise show up as a spurious deletion hunk in the agent's patch.
    test_files = set(inst.get("selected_test_files_to_run") or [])
    test_files.update(test_patch_paths(inst))
    return _strip_paths(full, test_files)


def _strip_paths(diff_text: str, paths: set[str]) -> str:
    """Remove the per-file sections of a unified diff whose path is in paths or
    under a tool-artifact dir (.perrep/, .repr_index/, .codegraph/)."""
    out, keep = [], True
    for line in diff_text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            # 'diff --git a/<p> b/<p>'
            parts = line.split()
            p = parts[2][2:] if len(parts) >= 3 and parts[2].startswith("a/") else ""
            keep = (p not in paths) and not any(
                p.startswith(pre) for pre in TOOL_ARTIFACT_PREFIXES)
        if keep:
            out.append(line)
    return "".join(out)


def cleanup_worktree(inst: dict[str, Any], suffix: str = "") -> None:
    """Remove the (per-condition) worktree (free disk after grading)."""
    repo = inst["repo"]
    clone = CLONES / _safe_name(repo)
    key = inst["instance_id"] + (("__" + suffix) if suffix else "")
    wt = WORKTREES / key
    if wt.exists():
        _run(["git", "worktree", "remove", "--force", str(wt)], cwd=clone,
             check=False)
        if wt.exists():
            shutil.rmtree(wt, ignore_errors=True)
