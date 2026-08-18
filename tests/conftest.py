"""Shared session-scoped fixtures for all repr-study module tests.

See INTERFACES.md section 11 for the fixture contract. The fixture repo
(tests/fixtures/sampleproj) must NEVER be mutated in place; tests that need
to edit files copy it to a tmp dir first.
"""

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

_REPO_TOP = Path(__file__).resolve().parent.parent
if str(_REPO_TOP) not in sys.path:
    sys.path.insert(0, str(_REPO_TOP))

from indexer import build_index as build_index_mod  # noqa: E402

FIXTURE_REPO = Path(__file__).resolve().parent / "fixtures" / "sampleproj"

_ENV_KEYS = ("REPO_ROOT", "REPO_INDEX_DIR", "RUN_DIR")


@dataclass
class BuiltIndex:
    """A built index over the fixture repo plus a backend-build helper."""

    repo_root: Path
    index_dir: Path
    stats: dict = field(default_factory=dict)

    def run_backend(self, name: str) -> dict:
        """Build an additional indexer backend into the same index dir."""
        from indexer.backends import get_backend

        return get_backend(name).build(self.repo_root, self.index_dir)


@pytest.fixture(scope="session")
def fixture_repo() -> Path:
    """Path to the sampleproj fixture repo root (read-only; never mutate)."""
    assert FIXTURE_REPO.is_dir(), f"fixture repo missing: {FIXTURE_REPO}"
    return FIXTURE_REPO


@pytest.fixture(scope="session")
def built_index(
    tmp_path_factory: pytest.TempPathFactory, fixture_repo: Path
) -> BuiltIndex:
    """Manifest + symbols backend built over the fixture repo in a tmp dir."""
    index_dir = tmp_path_factory.mktemp("repr_index")
    stats = build_index_mod.build(repo=fixture_repo, index_dir=index_dir, reps=[])
    return BuiltIndex(repo_root=fixture_repo, index_dir=index_dir, stats=stats)


@pytest.fixture(scope="session")
def run_dir(
    tmp_path_factory: pytest.TempPathFactory,
    fixture_repo: Path,
    built_index: BuiltIndex,
):
    """Session run dir with REPO_ROOT/REPO_INDEX_DIR/RUN_DIR exported.

    Yields the run dir path; the prior environment is restored on teardown.
    Use monkeypatch.setenv to point elsewhere within a single test.
    """
    rd = tmp_path_factory.mktemp("run")
    saved = {key: os.environ.get(key) for key in _ENV_KEYS}
    os.environ["REPO_ROOT"] = str(fixture_repo)
    os.environ["REPO_INDEX_DIR"] = str(built_index.index_dir)
    os.environ["RUN_DIR"] = str(rd)
    yield rd
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
