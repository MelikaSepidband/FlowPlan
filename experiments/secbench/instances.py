"""Load SecBench (SEC-bench patch task) instances.

Each ``<instance_id>.json`` describes one C/C++ OSS-Fuzz vulnerability: a repo
at ``base_commit``, a ``bug_report``/``bug_description``, the original
``sanitizer_report`` (the crash dump — also the dynamic signal, see trace.py),
the gold ``patch`` (NEVER shown to the agent — grading only), and the
``<project>:patch`` Docker image that is the test oracle (``secb patch|build|
repro``). ``work_dir`` is the in-container source root (e.g. ``/src/jq``).

Samples live in the sibling task repo by default; override with
``SECBENCH_SAMPLES_DIR``.
"""

import glob
import json
import os
from pathlib import Path
from typing import Any

STUDY_ROOT = Path(__file__).resolve().parents[2]
# Self-contained: samples live in THIS repo. (A copy exists in the sibling
# task repo, but we never depend on it.)
DEFAULT_SAMPLES = STUDY_ROOT / "samples" / "secbench_patch_n169"
SECB_IMAGE_PREFIX = "hwiwonlee/secb.eval.x86_64"


def samples_dir() -> Path:
    env = os.environ.get("SECBENCH_SAMPLES_DIR", "").strip()
    return Path(env) if env else DEFAULT_SAMPLES


def image_for(inst: dict[str, Any]) -> str:
    return "%s.%s" % (SECB_IMAGE_PREFIX, inst["instance_id"])


def repo_url(repo: str) -> str:
    repo = (repo or "").strip()
    if repo.startswith(("http://", "https://", "git@")):
        return repo
    if "/" in repo:
        return "https://github.com/%s.git" % repo
    return repo


def load_instance(path: Path) -> dict[str, Any]:
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    d["_path"] = str(path)
    d["image"] = image_for(d)
    d["repo_url"] = repo_url(d.get("repo", ""))
    d["work_dir"] = d.get("work_dir") or "/src"
    return d


def load_all(sdir: Path | None = None) -> list[dict[str, Any]]:
    """Every SecBench instance in the samples dir, sorted by instance_id."""
    sdir = sdir or samples_dir()
    files = sorted(
        p for p in glob.glob(str(sdir / "*.json"))
        if Path(p).name.lower() != "manifest.json"
    )
    out = []
    for f in files:
        try:
            d = load_instance(Path(f))
        except (ValueError, OSError):
            continue
        if "instance_id" in d:
            out.append(d)
    return out
