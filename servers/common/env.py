"""Environment-variable resolution for repr-study servers and indexers.

All three variables are resolved at call time (never cached at import) so
test fixtures and per-run harness environments take effect immediately:

- REPO_ROOT       repository under analysis (default: current working dir)
- REPO_INDEX_DIR  index directory (default: $REPO_ROOT/.repr_index)
- RUN_DIR         per-run log directory (default: $REPO_INDEX_DIR/run)
"""

import os
from pathlib import Path

__all__ = ["repo_root", "index_dir", "run_dir"]


def repo_root() -> Path:
    """Return the repository under analysis as a resolved absolute path."""
    value = os.environ.get("REPO_ROOT")
    return Path(value).resolve() if value else Path.cwd().resolve()


def index_dir() -> Path:
    """Return the index directory as a resolved absolute path."""
    value = os.environ.get("REPO_INDEX_DIR")
    return Path(value).resolve() if value else (repo_root() / ".repr_index")


def run_dir() -> Path:
    """Return the per-run log directory as a resolved absolute path."""
    value = os.environ.get("RUN_DIR")
    return Path(value).resolve() if value else (index_dir() / "run")
