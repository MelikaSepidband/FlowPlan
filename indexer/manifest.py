"""Build-time manifest: per-file sha256/mtime/size plus built_at and commit.

The manifest is the staleness baseline read by servers.common.staleness on
every tool call. It is written to ``<index_dir>/manifest.json`` (INTERFACES.md
section 5).
"""

import hashlib
import json
import subprocess
import time
from collections.abc import Iterable, Iterator
from pathlib import Path

from servers.common.navkit import EXCLUDED_DIRS

__all__ = ["MANIFEST_NAME", "DEFAULT_EXTS", "build_manifest", "git_commit", "iter_source_files"]

MANIFEST_NAME = "manifest.json"
DEFAULT_EXTS: tuple[str, ...] = (".py", ".c", ".h")


def git_commit(repo_root: Path) -> str | None:
    """Return ``git rev-parse HEAD`` for repo_root, or None when unavailable."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    sha = proc.stdout.strip()
    return sha or None


def iter_source_files(repo_root: Path, exts: Iterable[str]) -> Iterator[Path]:
    """Yield source files under repo_root with one of the given extensions.

    Walks deterministically (sorted) and honors the canonical exclusion list
    (servers.common.navkit.EXCLUDED_DIRS).
    """
    root = Path(repo_root)
    suffixes = {ext if ext.startswith(".") else f".{ext}" for ext in exts}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in suffixes:
            continue
        rel = path.relative_to(root)
        if any(part in EXCLUDED_DIRS for part in rel.parts[:-1]):
            continue
        yield path


def build_manifest(
    repo_root: Path, index_dir: Path, exts: list[str] | None = None
) -> dict:
    """Walk repo_root, hash every source file, and write the manifest.

    Records sha256, mtime, and size per file (keyed by repo-root-relative
    POSIX path), the build timestamp, and the git commit when repo_root is a
    git checkout (else null). Returns the manifest dict and writes it to
    ``<index_dir>/manifest.json``.
    """
    root = Path(repo_root).resolve()
    out_dir = Path(index_dir).resolve()
    chosen_exts = list(exts) if exts else list(DEFAULT_EXTS)
    files: dict[str, dict] = {}
    for path in iter_source_files(root, chosen_exts):
        st = path.stat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        files[path.relative_to(root).as_posix()] = {
            "sha256": digest,
            "mtime": st.st_mtime,
            "size": st.st_size,
        }
    manifest = {
        "built_at": time.time(),
        "commit": git_commit(root),
        "repo_root": str(root),
        "exts": chosen_exts,
        "files": files,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest
