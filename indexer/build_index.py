"""Index build CLI: manifest + symbols backend + requested backends.

Usage:
    python -m indexer.build_index --repo R --index-dir I --reps callgraph,docs

Always builds the manifest (staleness baseline) and the symbols backend
(navigation kit), then each requested backend via importlib from
indexer.backends (INTERFACES.md section 7.2).
"""

import argparse
import json
import os
from pathlib import Path

from indexer.backends import get_backend
from indexer.manifest import build_manifest

__all__ = ["build", "main"]


def build(
    repo: Path | str,
    index_dir: Path | str,
    reps: list[str] | None = None,
    exts: list[str] | None = None,
) -> dict:
    """Build the manifest, the symbols backend, then each backend in reps.

    Backend names are deduplicated and 'symbols' is never built twice.
    Returns {"repo", "index_dir", "manifest": {...}, "backends": {name: stats}}.
    """
    repo_path = Path(repo).resolve()
    index_path = Path(index_dir).resolve()
    manifest = build_manifest(repo_path, index_path, exts=exts)
    stats: dict = {
        "repo": str(repo_path),
        "index_dir": str(index_path),
        "manifest": {
            "files": len(manifest["files"]),
            "built_at": manifest["built_at"],
            "commit": manifest["commit"],
        },
        "backends": {},
    }
    names = ["symbols"]
    for rep in reps or []:
        rep = rep.strip()
        if rep and rep not in names:
            names.append(rep)
    for name in names:
        stats["backends"][name] = get_backend(name).build(repo_path, index_path)
    return stats


def main(argv: list[str] | None = None) -> dict:
    """Parse CLI args, run the build, print stats JSON, and return stats."""
    parser = argparse.ArgumentParser(
        prog="python -m indexer.build_index",
        description="Build the repr-study indexes for a repository.",
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("REPO_ROOT") or ".",
        help="repository to index (default: $REPO_ROOT else cwd)",
    )
    parser.add_argument(
        "--index-dir",
        default=None,
        help="index output directory (default: $REPO_INDEX_DIR else <repo>/.repr_index)",
    )
    parser.add_argument(
        "--reps",
        default="",
        help="comma-separated extra backend names from indexer/backends "
        "(manifest and symbols are always built)",
    )
    args = parser.parse_args(argv)
    repo = Path(args.repo).resolve()
    if args.index_dir:
        index_dir = Path(args.index_dir)
    elif os.environ.get("REPO_INDEX_DIR"):
        index_dir = Path(os.environ["REPO_INDEX_DIR"])
    else:
        index_dir = repo / ".repr_index"
    reps = [rep for rep in args.reps.split(",") if rep.strip()]
    stats = build(repo, index_dir, reps=reps)
    print(json.dumps(stats, indent=2, default=str))
    return stats


if __name__ == "__main__":
    main()
