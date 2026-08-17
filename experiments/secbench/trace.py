"""SecBench dynamic signal: the sanitizer crash stack as an execution trace.

Unlike SWE-bench Pro (where the dynamic trace is a coverage.py run of a
failing pytest), SecBench instances SHIP the dynamic signal already: every
``sanitizer_report`` is an AddressSanitizer/UBSan/etc. dump whose stack frames
are the exact ``file:line`` path the crashing input actually executed. That is
a real executed path (the sanitizer ran the code), and it is the signal the
original study found made the call graph win on SecBench ("follow caller/callee
context from the crash frames").

This module parses that report into the same trace schema the cfg/dfg overlay
tools already consume (``{ok, files: {rel: [lines]}, ...}``), plus richer
crash-stack structure (ordered frames, allocation site) for the SecBench-native
``crash_path`` tool. NO Docker, NO instrumentation -- pure parse of a field the
agent's prompt already contains, so the trace tool adds queryability and a
graph overlay, not new information.
"""

import json
import re
from pathlib import Path

# "#0 0x... in funcname /abs/path/file.c:123[:col]"  (col optional)
_FRAME_RE = re.compile(
    r"#(?P<no>\d+)\s+0x[0-9a-f]+\s+in\s+(?P<func>\S+)\s+"
    # file may be absolute (/root/x/f.c), repo-relative (libarchive/f.c), or
    # ../-prefixed (../../programs/escape.c). Require a source-ish path (has a
    # '/' or a C/C++ extension) ending in ':line' so we don't match the
    # "(binary+0xNNN)" trailing token of no-symbol frames.
    r"(?P<file>(?:/|\.\./|\w)\S*?(?:\.(?:c|cc|cpp|cxx|h|hpp|hh|hxx|C|c\+\+))?)"
    r":(?P<line>\d+)(?::\d+)?"
)
# Sections of an ASan report: the crash stack is first; "allocated by",
# "freed by", "previously allocated by" are secondary provenance stacks.
_ALLOC_MARKERS = ("allocated by thread", "freed by thread",
                  "previously allocated by")


# System/runtime path roots whose frames are never in the target repo.
_SYSTEM_ROOTS = ("/usr/", "/lib/", "/lib64/", "/build/", "/root/.",
                 "/opt/llvm", "/opt/rh", "/proc/", "<null>")


def _rel(path: str, work_dir: str) -> str:
    """Container-absolute frame path -> repo-relative (best effort).

    Frames read e.g. ``/src/jq/src/jv.c`` (work_dir ``/src/jq``) -> ``src/jv.c``.
    But SecBench images vary: the ASan report may reference a DIFFERENT build
    root than work_dir (e.g. exiv2 frames say ``/root/fuzzing/exiv2-trunk/src/
    types.cpp`` while work_dir is ``/src/exiv2``). So after the work_dir strip
    we fall back to stripping any ``.../<proj>[-trunk|-src]?/`` build-root
    prefix and keeping the trailing source path. System/runtime frames
    (libc, libasan, /usr, /build) are recognized and left absolute so the
    caller drops them.
    """
    p = path.replace("\\", "/")
    if any(p.startswith(r) for r in _SYSTEM_ROOTS):
        return p  # system frame -> stays absolute -> dropped
    wd = (work_dir or "").rstrip("/")
    if wd and p.startswith(wd + "/"):
        return p[len(wd) + 1:]
    # Different build root: keep the path from the FIRST recognizable source
    # segment (src/, lib/, include/, or the project dir) onward.
    m = re.search(r"/((?:src|lib|libavcodec|libavformat|source|include|"
                  r"modules)/.+)$", p)
    if m:
        return m.group(1)
    # Last resort: a bare "<proj>-trunk/<rest>" or "<something>/<file>.<ext>"
    # under a non-system root -> take the segment after the last build-root-ish
    # component. Keep only if it ends in a C/C++ source extension.
    if re.search(r"\.(c|cc|cpp|cxx|h|hpp|hh|c\+\+)$", p):
        # strip the leading absolute build root: drop everything up to and
        # including a "-trunk/" / "-src/" marker if present.
        mm = re.search(r"(?:-trunk|-src|/src|/source)/(.+)$", p)
        if mm:
            return mm.group(1)
    return p


def _in_repo(rel: str) -> bool:
    """A frame path is in the target repo iff normalization made it relative."""
    return not rel.startswith("/")


def _recover_rel_from_repo(abs_path: str, root: "Path") -> "str | None":
    """Map a build-absolute frame path to a repo-relative one that EXISTS.

    Layout-agnostic fallback for when ``_rel``'s prefix heuristics miss the
    project's source dir (e.g. faad2's ``libfaad/`` is not in the known-dir
    regex, so ``/root/faad2_asan/libfaad/sbr_dec.c`` stays absolute). Tries
    progressively longer trailing suffixes of the path against the checkout
    and returns the SHORTEST suffix that is a real file -- so a phantom guess
    is never accepted (mirrors build_trace's is_file() contract). Requires a
    ``.<c/cpp/h/...>`` extension so non-source frames are left alone.
    """
    p = abs_path.replace("\\", "/")
    if not re.search(r"\.(c|cc|cpp|cxx|h|hpp|hh|c\+\+|C)$", p):
        return None
    parts = [seg for seg in p.split("/") if seg]
    # shortest suffix first (most specific match wins; avoids a same-basename
    # file in an unrelated dir shadowing the real one)
    for i in range(len(parts) - 1, -1, -1):
        rel = "/".join(parts[i:])
        if (Path(root) / rel).is_file():
            return rel
    return None


def parse_sanitizer_report(report: str, work_dir: str) -> dict:
    """Split an ASan-style report into the crash stack + provenance stacks.

    Returns {crash_frames, alloc_frames, sanitizer_kind} where each frame is
    {frame_no, func, file (repo-rel or abs), line, in_repo}. The crash stack is
    every frame before the first provenance marker; alloc frames are those in
    the first provenance section (the allocation/free site, key for
    overflow/UAF root cause).
    """
    lines = (report or "").splitlines()
    # index of the first provenance marker splits crash vs alloc.
    split_at = len(lines)
    for i, ln in enumerate(lines):
        low = ln.lower()
        if any(m in low for m in _ALLOC_MARKERS):
            split_at = i
            break

    def _frames(chunk: str) -> list[dict]:
        out = []
        for m in _FRAME_RE.finditer(chunk):
            raw = m.group("file")
            rel = _rel(raw, work_dir)
            out.append({
                "frame_no": int(m.group("no")),
                "func": m.group("func"),
                "file": rel,
                "raw_file": raw,
                "line": int(m.group("line")),
                "in_repo": _in_repo(rel),
            })
        return out

    crash = _frames("\n".join(lines[:split_at]))
    alloc = _frames("\n".join(lines[split_at:]))
    kind = ""
    m = re.search(r"(AddressSanitizer|LeakSanitizer|MemorySanitizer|"
                  r"UndefinedBehaviorSanitizer|runtime error)", report or "")
    if m:
        kind = m.group(1)
    return {"crash_frames": crash, "alloc_frames": alloc,
            "sanitizer_kind": kind}


def build_trace(inst: dict, repo_root: "Path | None" = None) -> dict:
    """The agent-visible trace for one SecBench instance.

    Schema is a superset of the cfg/dfg overlay contract:
      - files: {repo_rel: [executed lines]}  (union of in-repo crash+alloc
        frame lines per file) -> executed_path/covered_blocks work unchanged.
      - crash_frames / alloc_frames / sanitizer: for the crash_path tool.
    ``ok`` is False (with a reason) when no in-repo frame is parseable.

    When ``repo_root`` is given, a frame is only counted ``in_repo`` if its
    normalized path ACTUALLY EXISTS in the checkout -- so a path-shape guess
    that does not correspond to a real file is dropped (no phantom frames).
    """
    report = inst.get("sanitizer_report") or ""
    work_dir = inst.get("work_dir") or "/src"
    parsed = parse_sanitizer_report(report, work_dir)

    if repo_root is not None:
        root = Path(repo_root)
        for fr in parsed["crash_frames"] + parsed["alloc_frames"]:
            if fr["in_repo"] and not (root / fr["file"]).is_file():
                fr["in_repo"] = False  # normalized guess is not a real file
            if not fr["in_repo"]:
                # prefix heuristics missed this project's source layout; try a
                # repo-anchored suffix match against the real checkout (only
                # accepts a path that EXISTS -> no phantom frames).
                rec = _recover_rel_from_repo(fr.get("raw_file") or fr["file"],
                                             root)
                if rec:
                    fr["file"] = rec
                    fr["in_repo"] = True

    files: dict[str, list[int]] = {}
    for fr in parsed["crash_frames"] + parsed["alloc_frames"]:
        if not fr["in_repo"]:
            continue
        files.setdefault(fr["file"], [])
        if fr["line"] not in files[fr["file"]]:
            files[fr["file"]].append(fr["line"])
    for k in files:
        files[k].sort()
    in_repo_crash = [f for f in parsed["crash_frames"] if f["in_repo"]]
    ok = bool(files)
    return {
        "ok": ok,
        "reason": None if ok else "no in-repo file:line frames in the "
                                  "sanitizer report",
        "kind": "sanitizer_stack",
        "sanitizer": parsed["sanitizer_kind"] or inst.get("sanitizer") or "",
        "test_id": "(sanitizer crash — %s)" % (parsed["sanitizer_kind"] or
                                               inst.get("sanitizer") or "?"),
        "crash_frames": parsed["crash_frames"],
        "alloc_frames": parsed["alloc_frames"],
        "n_crash_in_repo": len(in_repo_crash),
        "files": files,
    }


def collect_trace(inst: dict, dest_file: Path,
                  repo_root: "Path | None" = None) -> dict:
    """Write the SecBench trace to ``dest_file`` (run_dir/trace.json) and return
    it. No Docker; deterministic from the instance's sanitizer_report. When
    ``repo_root`` is given, frames are validated against the real checkout."""
    trace = build_trace(inst, repo_root=repo_root)
    dest = Path(dest_file)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(trace), encoding="utf-8")
    return trace
