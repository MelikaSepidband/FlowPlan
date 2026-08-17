"""Index-staleness detection against the build-time manifest.

The indexer (indexer/manifest.py) writes ``manifest.json`` at the top level
of the index directory, recording per-file sha256/mtime/size at build time.
On every tool call the server stat-compares the manifest entries against the
live repository and discloses staleness with a wording that is byte-identical
across all servers (see INTERFACES.md section 5).
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["MANIFEST_NAME", "Manifest", "format_note", "index_state"]

MANIFEST_NAME = "manifest.json"

_NOTE_TEMPLATE = (
    "results reflect repository state at commit {commit} unless reindexed; "
    "{n} file(s) edited since index build"
)


def format_note(commit: str | None, stale_files: int) -> str | None:
    """Return the frozen staleness disclosure, or None when nothing is stale."""
    if stale_files <= 0:
        return None
    return _NOTE_TEMPLATE.format(commit=commit or "unknown", n=stale_files)


@dataclass
class Manifest:
    """Parsed index manifest (see INTERFACES.md section 5 for the schema)."""

    built_at: float | None
    commit: str | None
    files: dict[str, dict]
    repo_root: str | None = None
    exts: list[str] = field(default_factory=lambda: [".py"])

    @classmethod
    def load(cls, index_dir: Path | str) -> "Manifest | None":
        """Load ``<index_dir>/manifest.json``; return None when it is missing."""
        path = Path(index_dir) / MANIFEST_NAME
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            built_at=data.get("built_at"),
            commit=data.get("commit"),
            files=dict(data.get("files", {})),
            repo_root=data.get("repo_root"),
            exts=list(data.get("exts", [".py"])),
        )

    def check(self, repo_root: Path | str) -> tuple[int, str | None]:
        """Stat-compare manifest entries against the live repo.

        A file is stale when it is missing or its mtime or size differ from
        the recorded values. Returns ``(stale_files, note)`` where note is
        the frozen disclosure string, or None when stale_files == 0.
        """
        root = Path(repo_root)
        stale = 0
        for rel, entry in self.files.items():
            path = root / rel
            try:
                st = path.stat()
            except OSError:
                stale += 1
                continue
            if st.st_size != entry.get("size") or st.st_mtime != entry.get("mtime"):
                stale += 1
        return stale, format_note(self.commit, stale)


def index_state(index_dir: Path | str, repo_root: Path | str) -> dict:
    """Build the envelope ``index_state`` dict. Never raises.

    Returns ``{"built_at": float|None, "commit": str|None, "stale_files": int,
    "note": str|None}``; all-default values when no manifest exists or it
    cannot be read.
    """
    try:
        manifest = Manifest.load(index_dir)
    except Exception:
        manifest = None
    if manifest is None:
        return {"built_at": None, "commit": None, "stale_files": 0, "note": None}
    try:
        stale, note = manifest.check(repo_root)
    except Exception:
        stale, note = 0, None
    return {
        "built_at": manifest.built_at,
        "commit": manifest.commit,
        "stale_files": stale,
        "note": note,
    }
