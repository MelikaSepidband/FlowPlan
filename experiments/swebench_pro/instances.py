"""Load + normalize SWE-bench Pro instances from samples/swebench_pro_n266
(the 266 public Python instances; the evaluation sample is data/ids/swebench_pro_158.json).

Each ``instance_*.json`` describes one bug-fix task: a repo at ``base_commit``,
a natural-language ``problem_statement`` (+ ``requirements`` / ``interface``
hints), the gold ``patch`` (NOT shown to the agent -- grading only), and the
``fail_to_pass`` / ``pass_to_pass`` tests that decide success. ``before_repo_set_cmd``
resets the checkout to base_commit and lays down the test files; ``dockerhub_tag``
names the prebuilt image used as the test oracle.
"""

import ast
import glob
import json
import os
from pathlib import Path
from typing import Any

STUDY_ROOT = Path(__file__).resolve().parents[2]
SAMPLES_DIR = STUDY_ROOT / "samples" / "swebench_pro_n266"

REPO_URL = {
    "ansible/ansible": "https://github.com/ansible/ansible.git",
    "internetarchive/openlibrary": "https://github.com/internetarchive/openlibrary.git",
    "qutebrowser/qutebrowser": "https://github.com/qutebrowser/qutebrowser.git",
}


def _maybe_list(value: Any) -> list[str]:
    """fail_to_pass / pass_to_pass / selected_test_files are str-encoded lists."""
    if isinstance(value, list):
        return [str(v) for v in value]
    text = str(value or "").strip()
    if not text:
        return []
    try:
        parsed = ast.literal_eval(text)
        if isinstance(parsed, (list, tuple)):
            return [str(v) for v in parsed]
    except (ValueError, SyntaxError):
        pass
    return [text]


def load_instance(path: Path) -> dict[str, Any]:
    """One instance dict with the str-encoded list fields parsed to lists."""
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    d["fail_to_pass"] = _maybe_list(d.get("fail_to_pass"))
    d["pass_to_pass"] = _maybe_list(d.get("pass_to_pass"))
    d["selected_test_files_to_run"] = _maybe_list(
        d.get("selected_test_files_to_run"))
    d["_path"] = str(path)
    d["repo_url"] = REPO_URL.get(d["repo"], "")
    return d


def load_all(samples_dir: Path = SAMPLES_DIR) -> list[dict[str, Any]]:
    """Every instance in the samples dir, sorted by instance_id (stable order)."""
    files = sorted(glob.glob(str(Path(samples_dir) / "instance_*.json")))
    return [load_instance(Path(f)) for f in files]


def load_subset(instance_ids: list[str],
                samples_dir: Path = SAMPLES_DIR) -> list[dict[str, Any]]:
    """Load only the named instances (by instance_id)."""
    want = set(instance_ids)
    out = []
    for inst in load_all(samples_dir):
        if inst["instance_id"] in want:
            out.append(inst)
    return out


def manifest_ids(samples_dir: Path = SAMPLES_DIR) -> list[str]:
    """The accepted instance_ids from MANIFEST.json."""
    m = json.loads((Path(samples_dir) / "MANIFEST.json").read_text("utf-8"))
    return list(m.get("instance_ids", []))


if __name__ == "__main__":
    insts = load_all()
    from collections import Counter
    langs = Counter(i["repo_language"] for i in insts)
    repos = Counter(i["repo"] for i in insts)
    print("instances: %d" % len(insts))
    print("languages:", dict(langs))
    print("repos:", dict(repos))
    print("sample instance_id:", insts[0]["instance_id"])
    print("  fail_to_pass:", insts[0]["fail_to_pass"])
    print("  selected_test_files:", insts[0]["selected_test_files_to_run"])
