"""Interpreter-compatible test environment for a cell (the "ladder").

WHY: the test shim builds ``run_dir/testenv`` on the HOST's python3 (3.12).
Old checkouts cannot even be *imported* under 3.12 -- ansible 2020-2021
commits die at collection with
``'_AnsiblePathHookFinder' object has no attribute 'find_spec'`` or
``No module named 'ansible.module_utils.six.moves'`` (vendored six; 3.12
removed the ``find_module`` fallback those loaders relied on), so every
local pytest run errors there, in every arm.  Nothing grades on 3.12: the
grading images run 3.8-3.11 per instance.

WHAT, in order of authority:
  1. IMAGE-DERIVED (primary): the instance's grading image is usually
     already on this host (never pulled here).  Read its Python version and
     ``pip freeze`` (~2 s), build ``uv venv --python X.Y --seed
     --relocatable`` and install that freeze (whole file, then per line,
     tolerant) -- the local env mirrors the grader's.
  2. PROBE LADDER (fallback, no image): ``pytest --collect-only`` over the
     repo's unit-test root with grader-style PYTHONPATH; keep the host venv
     when it collects cleanly, else try uv-managed 3.11/3.10/3.9/3.8 with a
     generic test-dependency set + the repo's requirement files, and keep
     the NEWEST interpreter with no interpreter-class errors (find_spec /
     removed stdlib modules / in-repo module import failures / SyntaxError)
     and <=1% collection errors, else the fewest errors.
The winner becomes ``run_dir/testenv`` (seeded venvs carry ``pip``;
relocatable scripts survive the rename).  The decision -- including the
freeze -- is cached per instance under a file lock with atomic writes, so
EVERY arm on the same instance rebuilds the SAME environment; a decision
produced by a timeout/exception is never cached.

NEVER crashes a cell: every subprocess has a timeout (own session, whole
group killed), every failure lands in the returned info and the cell falls
back to the host venv.  The agent's own PYTHONPATH is untouched; only the
interpreter/package set changes -- which is why the ladder is an INSTANCE
property applied to every arm of a table, never to one arm.

Enabled by ``REPR_TEST_SHIM_LADDER=1`` (passed per cell via extra_env).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Optional

LADDER = ("3.11", "3.10", "3.9", "3.8")
PROBE_TIMEOUT = 300
BUILD_TIMEOUT = 600
LINE_TIMEOUT = 120
LADDER_BUDGET = 900          # seconds for the whole decision
DOCKER_REGISTRY = "jefzda/sweap-images"
# generic unit-test dependencies of old suites (fallback path only)
BASE_PKGS = ["pytest", "pytest-mock", "pytest-xdist", "mock", "pyyaml",
             "jinja2", "packaging", "resolvelib", "cryptography", "requests",
             "urllib3", "pytz", "passlib", "pexpect", "bcrypt"]
REQ_FILES = ("test/units/requirements.txt",
             "test/lib/ansible_test/_data/requirements/units.txt",
             "requirements.txt", "requirements-test.txt",
             "requirements_test.txt", "test-requirements.txt",
             "dev-requirements.txt", "requirements-dev.txt",
             "requirements/test.txt", "requirements/dev.txt",
             "requirements/testing.txt")
TEST_ROOTS = ("test/units", "tests/unit", "tests/units", "tests", "test")
# the repository itself (installed editable in the image) must not be
# reinstalled from the freeze
_SELF_PKGS = {"ansible", "ansible-core", "ansible-base", "ansible_core"}

_CACHE_ROOT = Path(os.environ.get("SWEBENCH_CACHE",
                                  str(Path.home() / "swebench_cache")))
CACHE_DIR = _CACHE_ROOT / "testenv_ladder"


def enabled(env: Optional[dict] = None) -> bool:
    e = env if env is not None else os.environ
    return str(e.get("REPR_TEST_SHIM_LADDER", "")).strip() == "1"


# -- tools ---------------------------------------------------------------------

def uv_path() -> Optional[str]:
    """uv is not on PATH in non-login shells (schtasks/setsid launches):
    resolve ~/.local/bin/uv explicitly."""
    for cand in (shutil.which("uv"),
                 str(Path.home() / ".local" / "bin" / "uv")):
        if cand and os.access(cand, os.X_OK):
            return cand
    return None


def uv_version() -> str:
    uv = uv_path()
    if not uv:
        return ""
    rc, out = _run([uv, "--version"], None, dict(os.environ), 30)
    return out.strip().splitlines()[0] if rc == 0 and out.strip() else ""


def ensure_pythons(versions=LADDER, timeout: int = 900) -> dict:
    """Pre-provision the uv-managed interpreters (idempotent; done once at
    runner start so cells never download mid-run)."""
    uv = uv_path()
    if not uv:
        return {"ok": False, "error": "uv not found"}
    rc, out = _run([uv, "python", "install"] + list(versions), None,
                   dict(os.environ), timeout)
    return {"ok": rc == 0, "tail": out[-300:]}


# -- repo layout ---------------------------------------------------------------

def source_roots(repo: Path) -> list[str]:
    """Grader-style: ``lib``/``src`` package roots, then ``test/lib`` and
    ``test`` when present (the SWE-Pro run scripts use
    PYTHONPATH=/app/lib:/app/test/lib:/app/test), then the repo root."""
    roots = []
    for sub in ("lib", "src"):
        d = repo / sub
        try:
            if d.is_dir() and any(p.is_dir() and (p / "__init__.py").is_file()
                                  for p in d.iterdir()):
                roots.append(str(d))
        except OSError:
            continue
    for sub in ("test/lib", "test"):
        d = repo / sub
        if d.is_dir():
            roots.append(str(d))
    roots.append(str(repo))
    return roots


def top_packages(repo: Path) -> set:
    """Top-level in-repo package names (for interpreter-class detection)."""
    names = set()
    for base in (repo / "lib", repo / "src", repo):
        try:
            if not base.is_dir():
                continue
            for p in base.iterdir():
                if p.is_dir() and (p / "__init__.py").is_file():
                    names.add(p.name)
        except OSError:
            continue
    return names


def _n_test_files(d: Path, cap: int = 5000) -> int:
    n = 0
    try:
        for p in d.rglob("*.py"):
            if p.name.startswith("test_") or p.name.endswith("_test.py"):
                n += 1
                if n >= cap:
                    break
    except OSError:
        pass
    return n


MIN_SUITE = 20


def find_test_root(repo: Path) -> Optional[Path]:
    """The conventional unit-test root when it holds a real suite (>= 20
    test files; test/units before test/, so ansible's test/integration
    scripts are never collected -- they abort collection with SystemExit),
    else the nested tests/ dir with the most test files (openlibrary's
    top-level tests/ has 3 files; the suites live in openlibrary/tests and
    openlibrary/plugins/*/tests), else any conventional root with a test."""
    conventional = []
    for rel in TEST_ROOTS:
        d = repo / rel
        if d.is_dir():
            n = _n_test_files(d)
            conventional.append((d, n))
            if n >= MIN_SUITE:
                return d
    best, best_n = None, 0
    seen = set()
    for pat in ("*/tests", "*/test", "*/*/tests", "*/*/test"):
        for d in repo.glob(pat):
            try:
                key = d.resolve()
            except OSError:
                continue
            if key in seen or not d.is_dir() or ".git" in d.parts \
                    or "node_modules" in d.parts or "vendor" in d.parts:
                continue
            seen.add(key)
            n = _n_test_files(d)
            if n > best_n:
                best, best_n = d, n
    if best is not None and best_n >= max(1, max((n for _, n in conventional),
                                                 default=0)):
        return best
    for d, n in conventional:
        if n:
            return d
    return best


def requirement_files(repo: Path) -> list[Path]:
    return [repo / rel for rel in REQ_FILES if (repo / rel).is_file()]


def ensure_submodules(repo: Path, timeout: int = 300) -> dict:
    """Populate git submodules the grading image ships checked out
    (openlibrary's /app/infogami is one: `import infogami` fails in a bare
    worktree). A gitlink checkout keeps `git status` clean, so the patch
    harvest never sees the submodule's files. No-op without .gitmodules;
    never raises."""
    if not (repo / ".gitmodules").is_file():
        return {"ran": False}
    try:
        # decide on the DIRECTORY, not on `git submodule status`: once the
        # shared clone's .git/modules/<name> exists (another worktree
        # initialized it), status shows no '-' marker although this
        # worktree's checkout is still empty (live cell 00bec1, 2026-08-28)
        cfg = subprocess.run(["git", "config", "-f", ".gitmodules",
                              "--get-regexp", r"submodule\..*\.path"],
                             cwd=str(repo), capture_output=True, text=True,
                             timeout=60)
        paths = [l.split(None, 1)[1].strip() for l in cfg.stdout.splitlines()
                 if len(l.split(None, 1)) == 2]

        def _populated(p):
            d = repo / p
            # a half-initialized checkout holds only the `.git` link file
            return d.is_dir() and any(e.name != ".git" for e in d.iterdir())
        empty = [p for p in paths if not _populated(p)]
        if paths and not empty:
            return {"ran": False, "note": "already populated"}
        rc, out = _run(["git", "submodule", "update", "--init", "--recursive",
                        "--force"], repo, dict(os.environ), timeout)
        still = [p for p in paths if not _populated(p)]
        return {"ran": True, "rc": rc, "paths": paths, "empty_after": still,
                "tail": (out or "")[-200:]}
    except Exception as ex:  # noqa: BLE001
        return {"ran": True, "rc": None, "tail": repr(ex)}


# -- the instance's own test environment (official harness) -------------------

_SKIP_ENV = {"PATH", "HOME", "DISPLAY", "DEBIAN_FRONTEND", "LANG", "LC_ALL",
             "LANGUAGE", "TZ", "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE",
             "PYTHONNOUSERSITE", "PYTHONUSERBASE", "VIRTUAL_ENV", "USER",
             "SHELL", "TERM", "PWD",
             # the images' "--tb=short -v --continue-on-collection-errors
             # --reruns=3" would change what our probe runs see
             # (and --reruns needs a plugin the fallback envs lack)
             "PYTEST_ADDOPTS", "PYTEST_PLUGINS"}
_APP_RE = re.compile(r"(?<![\w.-])/app(?=/|:|$)")
_ASSIGN_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(\"[^\"]*\"|'[^']*'|\S+)")
_INLINE_RE = re.compile(r"^((?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)+)"
                        r"(?:dbus-run-session|xvfb-run|python|pytest|"
                        r"coverage|tox)\b")


def _map_app(value: str, repo: Path) -> str:
    return _APP_RE.sub(str(repo), value)


def harness_env(instance_id: str, repo: Path,
                harness_dir: Optional[Path] = None) -> dict:
    """{'env': {K: V}, 'pythonpath': [...]} mirrored from the instance's
    official harness: Dockerfile ``ENV`` lines and run_script.sh
    ``export``/inline assignments (qutebrowser: QT_QPA_PLATFORM=offscreen,
    PYTEST_QT_API...; openlibrary: OL_CONFIG=/app/conf/..., PYTHONPATH=/app),
    with ``/app`` mapped to the worktree. Empty when the harness is absent;
    never raises."""
    empty = {"env": {}, "pythonpath": []}
    try:
        if harness_dir is None:
            from experiments.swebench_pro import grade as _g
            harness_dir = Path(_g.OFFICIAL_HARNESS)
        texts = []
        for sub in ("dockerfiles/base_dockerfile",
                    "dockerfiles/instance_dockerfile"):
            p = harness_dir / sub / instance_id / "Dockerfile"
            if p.is_file():
                texts.append(("df", p.read_text(encoding="utf-8",
                                                errors="replace")))
        rs = harness_dir / "run_scripts" / instance_id / "run_script.sh"
        if rs.is_file():
            texts.append(("rs", rs.read_text(encoding="utf-8",
                                             errors="replace")))
    except Exception:  # noqa: BLE001
        return empty
    raw: dict = {}
    for kind, text in texts:
        for line in text.splitlines():
            s = line.strip()
            if kind == "df":
                if not s.startswith("ENV "):
                    continue
                body = s[4:].strip()
                if "=" in body.split(None, 1)[0]:
                    for m in _ASSIGN_RE.finditer(body):
                        raw[m.group(1)] = m.group(2).strip("\"'")
                else:
                    parts = body.split(None, 1)
                    if len(parts) == 2:
                        raw[parts[0]] = parts[1].strip().strip("\"'")
            else:
                m = re.match(r"^export\s+([A-Za-z_][A-Za-z0-9_]*)="
                             r"(\"[^\"]*\"|'[^']*'|\S+)", s)
                if m:
                    raw[m.group(1)] = m.group(2).strip("\"'")
                    continue
                m = _INLINE_RE.match(s)
                if m:
                    for a in _ASSIGN_RE.finditer(m.group(1)):
                        raw[a.group(1)] = a.group(2).strip("\"'")
    env, pythonpath = {}, []
    for k, v in raw.items():
        if k in _SKIP_ENV or k.startswith(("PIP_", "UV_")):
            continue
        v = _map_app(v, repo)
        if k == "PYTHONPATH":
            pythonpath = [x for x in v.split(":") if x and "$" not in x]
            continue
        if "$" in v:
            continue                    # unresolved shell reference
        env[k] = v
    return {"env": env, "pythonpath": pythonpath}


_REQ = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[^\]]*\])?)\s*"
                  r"(?P<spec>[^;]*?)\s*(?:;(?P<marker>.*))?$")


def strip_pins(text: str, base_dir: Optional[Path] = None,
               exact_only: bool = True) -> str:
    """Relax a requirements file: drop ``==``/``===`` pins (or every
    specifier when exact_only=False), keep environment markers verbatim
    (quotes included), pass options/URLs through, and make ``-r``/``-c``
    targets absolute so a copy written elsewhere still resolves them."""
    out = []
    for raw in text.splitlines():
        # a comment starts at line start or after whitespace -- never
        # inside a URL fragment such as git+...#egg=name
        line = re.split(r"(?:^|\s)#", raw, 1)[0].strip()
        if not line:
            continue
        if line.startswith(("-r", "-c", "--requirement", "--constraint")):
            parts = line.split(None, 1)
            if len(parts) == 2 and base_dir is not None \
                    and not os.path.isabs(parts[1]):
                line = "%s %s" % (parts[0], (base_dir / parts[1]).resolve())
            out.append(line)
            continue
        if line.startswith("-") or "://" in line or "@" in line:
            out.append(line)
            continue
        m = _REQ.match(line)
        if not m:
            continue
        name, spec, marker = m.group("name"), m.group("spec") or "", \
            m.group("marker")
        if exact_only and not re.match(r"^={2,3}[^,]*$", spec.strip()):
            keep = spec.strip()
        else:
            keep = ""
        out.append(name + keep + (" ;" + marker if marker else ""))
    return "\n".join(out) + "\n"


# -- subprocess plumbing -------------------------------------------------------

def _base_env(base: Optional[dict] = None) -> dict:
    e = dict(base if base is not None else os.environ)
    for k in list(e):
        if k.startswith("REPR_") or k == "PYTEST_PLUGINS":
            e.pop(k, None)
    e["PYTHONNOUSERSITE"] = "1"
    e["PIP_USER"] = "0"
    e["PYTHONDONTWRITEBYTECODE"] = "1"
    e["PYTEST_ADDOPTS"] = ""
    e["UV_NO_PROGRESS"] = "1"
    e.setdefault("UV_PYTHON_DOWNLOADS", "automatic")
    e.pop("PYTHONPATH", None)
    e.pop("VIRTUAL_ENV", None)
    return e


# repos whose conftest probes optional native backends at startup: name the
# one the grading image actually uses, so a missing alternative does not
# abort the session with an INTERNALERROR before any test runs
# (qutebrowser: the image has QtWebEngine, never QtWebKit)
_BACKEND_ENV = {"QUTE_TESTS_BACKEND": "webengine"}


def probe_env(repo: Path, base: Optional[dict] = None,
              harness: Optional[dict] = None) -> dict:
    """Grader-style env: the instance's own variables (harness_env) over the
    hygiene base; PYTHONPATH = the harness's mapped entries, then the
    repo's source roots."""
    e = _base_env(base)
    if (repo / "qutebrowser").is_dir() and (repo / "tests").is_dir():
        for k, v in _BACKEND_ENV.items():
            e.setdefault(k, v)
    roots = []
    if harness:
        for k, v in (harness.get("env") or {}).items():
            if k not in ("PYTHONPATH", "PATH"):
                e[k] = v
        roots += [p for p in (harness.get("pythonpath") or [])
                  if os.path.isdir(p)]
    roots += source_roots(repo)
    e["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(roots))
    return e


def _run(cmd, cwd, env, timeout):
    """(rc, stdout+stderr); rc None on timeout/launch error. The child gets
    its own session and the whole group is killed on timeout (import-time
    grandchildren must not outlive the probe)."""
    try:
        p = subprocess.Popen(cmd, cwd=str(cwd) if cwd else None, env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, start_new_session=True)
    except Exception as ex:  # noqa: BLE001
        return None, "[launch error] %r" % ex
    try:
        out, _ = p.communicate(timeout=timeout)
        return p.returncode, out or ""
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except Exception:  # noqa: BLE001
            p.kill()
        try:
            out, _ = p.communicate(timeout=30)
        except Exception:  # noqa: BLE001
            out = ""
        return None, (out or "") + "\n[timeout after %ss]" % timeout


# -- the probe -----------------------------------------------------------------

_COLLECTED = re.compile(r"(\d+) tests? collected")
_ERRLINE = re.compile(r"^ERROR ", re.M)
_INTERP_PATTERNS = (
    re.compile(r"has no attribute 'find_spec'"),
    re.compile(r"No module named '(imp|distutils|asyncore|asynchat|smtpd|"
               r"cgi|telnetlib|nntplib|pipes|crypt)'"),
    re.compile(r"cannot import name '\w+' from 'collections'"),
    re.compile(r"^E\s+SyntaxError", re.M),
)


def interpreter_class_errors(out: str, pkgs) -> int:
    """Collection errors that no dependency install can fix: the
    interpreter itself cannot import this checkout."""
    n = 0
    for line in out.splitlines():
        if not line.startswith("E   "):
            continue
        if any(p.search(line) for p in _INTERP_PATTERNS):
            n += 1
            continue
        m = re.search(r"No module named '([\w.]+)'", line)
        if m and m.group(1).split(".")[0] in pkgs and "." in m.group(1):
            n += 1                     # an in-repo submodule failed to load
    return n


def probe(python: str, repo: Path, test_root: Path, *,
          timeout: int = PROBE_TIMEOUT,
          base_env: Optional[dict] = None,
          harness: Optional[dict] = None) -> dict:
    """Collect-only over ``test_root``. {rc, errors, interp_errors,
    collected, seconds, top, tail, usable}; ``usable`` False when pytest
    itself could not run (candidate skipped)."""
    rel = os.path.relpath(str(test_root), str(repo))
    # no `-p no:xdist`: pytest-rerunfailures registers an xdist hook and
    # pytest aborts (PluginValidationError) when xdist is disabled
    cmd = [python, "-m", "pytest", "--collect-only", "-q",
           "-p", "no:cacheprovider", "-p", "no:cov",
           "-o", "addopts=", rel]
    t0 = time.time()
    rc, out = _run(cmd, repo, probe_env(repo, base_env, harness), timeout)
    secs = round(time.time() - t0, 1)
    tail = "\n".join(out.strip().splitlines()[-3:])[:400]
    base = {"rc": rc, "seconds": secs, "tail": tail, "top": [],
            "collected": 0, "errors": None, "interp_errors": None,
            "usable": False}
    if rc is None:
        return base
    m = _COLLECTED.search(out)
    if m:
        collected = int(m.group(1))
    else:
        # older pytest (6.1) prints no "N tests collected" summary under
        # -q --collect-only: count the node ids it listed instead
        collected = sum(1 for l in out.splitlines()
                        if "::" in l and not l.startswith(("E ", "ERROR")))
    errors = len(_ERRLINE.findall(out))
    # unusable = pytest itself did not run (missing module, plugin crash
    # at startup, internal error): nothing collected AND no collection
    # errors reported -- live cell 1b7026 (2026-08-28) had rc=1 with a
    # pkg_resources traceback and was wrongly accepted
    if "No module named pytest" in out or rc not in (0, 1, 2, 5) \
            or (collected == 0 and errors == 0):
        base["collected"] = collected
        return base
    top = Counter(l.strip()[:120] for l in out.splitlines()
                  if l.startswith("E   ")).most_common(3)
    base.update({"collected": collected,
                 "errors": errors,
                 "interp_errors": interpreter_class_errors(
                     out, top_packages(repo)),
                 "top": [{"n": n, "line": s} for s, n in top],
                 "usable": True})
    return base


def clean_enough(r: dict) -> bool:
    """Stop the ladder here: no interpreter-class error and at most 1% of
    the collected tests erroring at collection (a handful of optional-
    dependency quirks in a 3,500-test suite must not push the cell onto an
    older interpreter -- the NEWEST workable interpreter is the default)."""
    if not r.get("usable"):
        return False
    if r.get("interp_errors"):
        return False
    return r["errors"] <= max(0, r["collected"] // 100)


def _better(a: dict, b: Optional[dict]) -> bool:
    if b is None:
        return True
    ka = (a["interp_errors"] or 0, a["errors"], -a["collected"])
    kb = (b["interp_errors"] or 0, b["errors"], -b["collected"])
    return ka < kb


# -- the grading image ---------------------------------------------------------

def _freeze_filter(lines) -> list[str]:
    """Installable lines of the image's `pip freeze`: drop editables and
    local-path installs (the repository itself), keep VCS pins
    (`infogami @ git+https://...` -- openlibrary's dependency lives only
    there) and ordinary pins."""
    keep = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith(("-e", "#")) or "@ file:" in line:
            continue
        head = line.split("@", 1)[0].split("==")[0]
        if "+" in head:
            continue
        name = re.split(r"[=<>!~ ]", line, 1)[0].strip().lower()
        if name in _SELF_PKGS:
            continue
        keep.append(line)
    return keep


def image_env(image: str, *, timeout: int = 120,
              base_env: Optional[dict] = None) -> Optional[dict]:
    """{'python': '3.8', 'python_full': '3.8.20', 'freeze': [...]} read
    from a LOCAL grading image (never pulled); None when docker/image are
    unavailable or the reads fail."""
    docker = shutil.which("docker")
    if not docker or not image:
        return None
    env = _base_env(base_env)
    rc, _ = _run([docker, "image", "inspect", image, "--format", "{{.Id}}"],
                 None, env, 60)
    if rc != 0:
        return None
    rc, out = _run([docker, "run", "--rm", "--entrypoint", "python", image,
                    "-c", "import platform;print(platform.python_version())"],
                   None, env, timeout)
    m = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", out or "") if rc == 0 else None
    if not m:
        return None
    full = m.group(0)
    version = "%s.%s" % (m.group(1), m.group(2))
    rc, out = _run([docker, "run", "--rm", "--entrypoint", "python", image,
                    "-m", "pip", "freeze"], None, env, timeout)
    freeze = _freeze_filter((out or "").splitlines()) if rc == 0 else []
    # setuptools is absent from `pip freeze`; the image's exact version
    # matters (qutebrowser turns warnings into errors, and setuptools >=67.5
    # warns on `import pkg_resources` at conftest import)
    rc, out = _run([docker, "run", "--rm", "--entrypoint", "python", image,
                    "-c", "import setuptools;print(setuptools.__version__)"],
                   None, env, timeout)
    m2 = re.search(r"\b\d+(\.\d+)+\b", out or "") if rc == 0 else None
    return {"python": version, "python_full": full, "freeze": freeze,
            "setuptools": m2.group(0) if m2 else None}


_IMAGE_PKG_LS = ("for d in /app/*/; do n=$(basename \"$d\"); "
                 "[ -f \"/app/$n/__init__.py\" ] && echo \"$n\"; done; true")


def image_extra_packages(image: str, repo: Path, python: str, *,
                         base_env: Optional[dict] = None,
                         timeout: int = 180) -> dict:
    """Top-level Python packages the grading image keeps under /app that the
    checkout does not have -- openlibrary's `infogami` is cloned by the
    Dockerfile (older commits carry only a .gitmodules entry, no gitlink) --
    copied from the image into the venv's site-packages. Never into the
    worktree: untracked files there would show up as the agent's source
    edits and leak into the harvested test patch. {'copied', 'candidates'}
    or {'error'}; never raises."""
    docker = shutil.which("docker")
    if not docker or not image:
        return {"copied": [], "candidates": []}
    env = _base_env(base_env)
    try:
        rc, out = _run([docker, "run", "--rm", "--entrypoint", "sh", image,
                        "-c", _IMAGE_PKG_LS], None, env, timeout)
        if rc != 0:
            return {"error": (out or "")[-200:]}
        names = [n.strip() for n in out.splitlines()
                 if n.strip() and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$",
                                           n.strip())]

        def _present(n):
            d = repo / n
            return d.is_dir() and any(e.name != ".git" for e in d.iterdir())
        missing = [n for n in names if not _present(n)]
        if not missing:
            return {"copied": [], "candidates": names}
        rc, sp = _run([python, "-c", "import sysconfig;"
                       "print(sysconfig.get_paths()['purelib'])"], None, env,
                      60)
        if rc != 0 or not sp.strip():
            return {"error": "no site-packages", "candidates": names}
        site = Path(sp.strip().splitlines()[-1])
        rc, cid = _run([docker, "create", image], None, env, 60)
        cid = cid.strip().splitlines()[-1] if rc == 0 and cid.strip() else None
        if not cid:
            return {"error": "docker create failed", "candidates": names}
        copied = []
        try:
            for n in missing:
                dest = site / n
                shutil.rmtree(dest, ignore_errors=True)
                rc2, _ = _run([docker, "cp", "-L", "%s:/app/%s" % (cid, n),
                               str(dest)], None, env, timeout)
                if rc2 == 0 and (dest / "__init__.py").is_file():
                    copied.append(n)
        finally:
            _run([docker, "rm", "-f", cid], None, env, 60)
        return {"copied": copied, "candidates": names}
    except Exception as ex:  # noqa: BLE001
        return {"error": repr(ex)}


# -- candidate venvs -----------------------------------------------------------

def _pip_install(uv, py, args, env, cwd, timeout, log, step):
    # --compile-bytecode: the grading images ship precompiled site-packages;
    # a fresh venv compiles on first import and emits compile-time
    # DeprecationWarnings (invalid escapes in old deps) that repos with
    # `filterwarnings = error` (qutebrowser) turn into collection errors
    rc, out = _run([uv, "pip", "install", "-q", "--no-config",
                    "--compile-bytecode", "--python", str(py)] + list(args),
                   cwd, env, timeout)
    log["steps"].append({"step": step, "rc": rc, "tail": out[-300:]})
    return rc == 0


# source-only distributions the images compile against system headers we
# do not have; the binary wheel is a drop-in for importability
_BINARY_ALT = {"psycopg2": "psycopg2-binary"}


def _binary_alternative(line: str) -> Optional[str]:
    m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(.*)$", line.strip())
    if not m:
        return None
    alt = _BINARY_ALT.get(m.group(1).lower())
    return (alt + m.group(2)) if alt else None


def _per_line(uv, py, lines, env, cwd, log, step, deadline=None):
    ok = fail = skipped = 0
    failed = []
    for line in lines:
        if deadline is not None and time.time() > deadline:
            skipped += 1
            continue
        if _pip_install(uv, py, [line], env, cwd, LINE_TIMEOUT,
                        {"steps": []}, step):
            ok += 1
            continue
        alt = _binary_alternative(line)
        if alt and _pip_install(uv, py, [alt], env, cwd, LINE_TIMEOUT,
                                {"steps": []}, step):
            ok += 1
            continue
        # the pinned wheel may predate this interpreter (psycopg2-binary
        # 2.9.6 has no 3.12 wheel): any version of the binary variant
        alt_any = re.split(r"[=<>!~ @]", alt, 1)[0] if alt else None
        if alt_any and alt_any != alt and _pip_install(
                uv, py, [alt_any], env, cwd, LINE_TIMEOUT, {"steps": []},
                step):
            ok += 1
            continue
        fail += 1
        failed.append(line[:60])
    log["steps"].append({"step": step, "ok": ok, "failed": fail,
                         "skipped": skipped, "failed_lines": failed[:8]})
    return ok


def build_venv(version: str, dest: Path, repo: Path, *,
               freeze: Optional[list] = None,
               base_env: Optional[dict] = None,
               timeout: int = BUILD_TIMEOUT,
               deadline: Optional[float] = None,
               setuptools: Optional[str] = None) -> tuple[Optional[str], dict]:
    """``uv venv --python <version> --seed --relocatable dest`` + packages:
    the image's freeze when given (whole file, then per line, tolerant),
    else the generic set + the repo's requirement files (joint, then
    relaxed, then per line). ``deadline`` (epoch seconds) bounds the
    per-line fallbacks so a 100-line freeze with unbuildable wheels cannot
    stall a cell. Returns (python path or None, log)."""
    log: dict = {"version": version, "steps": [],
                 "mode": "freeze" if freeze else "generic"}
    if deadline is not None:
        timeout = max(60, min(timeout, int(deadline - time.time())))
    uv = uv_path()
    if uv is None:
        log["error"] = "uv not found"
        return None, log
    env = _base_env(base_env)
    shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    cwd = dest.parent
    rc, out = _run([uv, "venv", "-q", "--no-config", "--seed",
                    "--relocatable", "--python", version, str(dest)],
                   cwd, env, 300)
    log["steps"].append({"step": "venv", "rc": rc, "tail": out[-300:]})
    py = dest / "bin" / "python"
    if rc != 0 or not py.exists():
        log["error"] = "venv creation failed"
        return None, log
    # `pip freeze` never lists setuptools/wheel, and uv's --seed does not
    # reliably add them; old pytest plugins import pkg_resources at load
    # (pytest-rerunfailures 9.x: pytest itself then crashes -- live cell
    # 1b7026, 2026-08-28). Mirror the image's exact setuptools when known
    # (>=67.5 warns on pkg_resources import; qutebrowser's pytest.ini turns
    # that into an error); current setuptools has no pkg_resources at all.
    spec = ("setuptools==%s" % setuptools) if setuptools else "setuptools<67.5"
    if not _pip_install(uv, py, [spec, "wheel"], env, cwd, timeout, log,
                        "seed-setuptools"):
        _pip_install(uv, py, ["setuptools<81", "wheel"], env, cwd, timeout,
                     log, "seed-setuptools-fallback")
    if freeze:
        req = dest / "_image_freeze.txt"
        req.write_text("\n".join(freeze) + "\n", encoding="utf-8")
        if not _pip_install(uv, py, ["-r", str(req)], env, cwd, timeout,
                            log, "freeze"):
            _per_line(uv, py, freeze, env, cwd, log, "freeze-per-line",
                      deadline)
    else:
        joint = list(BASE_PKGS)
        rfs = requirement_files(repo)
        for rf in rfs:
            joint += ["-r", str(rf)]
        if not _pip_install(uv, py, joint, env, cwd, timeout, log, "joint"):
            if not _pip_install(uv, py, BASE_PKGS, env, cwd, timeout, log,
                                "base-pkgs"):
                _per_line(uv, py, BASE_PKGS, env, cwd, log, "base-per-line",
                          deadline)
            for i, rf in enumerate(rfs):
                rel = rf.relative_to(repo)
                if _pip_install(uv, py, ["-r", str(rf)], env, cwd, timeout,
                                log, "req:%s" % rel):
                    continue
                try:
                    text = rf.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                relaxed = dest / ("_req_relaxed_%d.txt" % i)
                relaxed.write_text(strip_pins(text, rf.parent, True),
                                   encoding="utf-8")
                if _pip_install(uv, py, ["-r", str(relaxed)], env, cwd,
                                timeout, log, "req-relaxed:%s" % rel):
                    continue
                lines = [l for l in strip_pins(text, rf.parent, False)
                         .splitlines() if l and not l.startswith("-")]
                _per_line(uv, py, lines, env, cwd, log,
                          "req-per-line:%s" % rel, deadline)
    rc, _ = _run([str(py), "-c", "import pytest"], cwd, env, 60)
    if rc != 0 and not _pip_install(uv, py, ["pytest"], env, cwd, timeout,
                                    log, "pytest"):
        log["error"] = "could not install pytest"
        return None, log
    # belt and braces for the per-line path: precompile everything once
    rc, _ = _run([str(py), "-m", "compileall", "-q", "-j", "0",
                  str(dest / "lib")], cwd, env, 300)
    log["steps"].append({"step": "compileall", "rc": rc})
    rc, out = _run([str(py), "-c",
                    "import sys,pytest;print(sys.version.split()[0],"
                    "pytest.__version__)"], cwd, env, 60)
    log["python"] = out.strip()[-40:] if rc == 0 else None
    rc, _ = _run([str(py), "-m", "pip", "--version"], cwd, env, 60)
    log["pip"] = rc == 0
    return str(py), log


# -- cache ---------------------------------------------------------------------

def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._@-]+", "_", s)[:200]


def _cache_path(key: str) -> Path:
    return CACHE_DIR / (_safe(key) + ".json")


def cached_choice(key: Optional[str]) -> Optional[dict]:
    if not key:
        return None
    p = _cache_path(key)
    try:
        if p.is_file():
            rec = json.loads(p.read_text(encoding="utf-8"))
            return rec if isinstance(rec, dict) and rec.get("chosen") \
                else None
    except Exception:  # noqa: BLE001
        return None
    return None


def _store_choice(key: Optional[str], record: dict) -> None:
    if not key:
        return
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        p = _cache_path(key)
        tmp = p.with_suffix(".json.tmp.%d" % os.getpid())
        tmp.write_text(json.dumps(record, indent=1), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


class _Lock:
    """Serialize the decision per instance (two arms of one instance may
    start together); no-op where fcntl is unavailable."""

    def __init__(self, key: Optional[str]):
        self.key, self.fh = key, None

    def __enter__(self):
        if not self.key:
            return self
        try:
            import fcntl
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            self.fh = open(str(_cache_path(self.key)) + ".lock", "w")
            fcntl.flock(self.fh, fcntl.LOCK_EX)
        except Exception:  # noqa: BLE001
            self.fh = None
        return self

    def __exit__(self, *exc):
        if self.fh is not None:
            try:
                import fcntl
                fcntl.flock(self.fh, fcntl.LOCK_UN)
                self.fh.close()
            except Exception:  # noqa: BLE001
                pass
        return False


# -- the ladder ----------------------------------------------------------------

def _install_winner(run_dir: Path, ver: str, ladder=LADDER) -> None:
    testenv = run_dir / "testenv"
    winner = run_dir / ("testenv_%s" % ver)
    shutil.rmtree(run_dir / "testenv_system", ignore_errors=True)
    if testenv.exists():
        testenv.rename(run_dir / "testenv_system")
    winner.rename(testenv)
    for v in set(ladder) | {ver}:
        d = run_dir / ("testenv_%s" % v)
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)


def apply_ladder(run_dir: Path, repo: Path, *, cache_key: Optional[str] = None,
                 image: Optional[str] = None,
                 base_env: Optional[dict] = None,
                 harness: Optional[dict] = None,
                 ladder: tuple = LADDER,
                 budget: int = LADDER_BUDGET) -> dict:
    """Run after the host venv exists at ``run_dir/testenv``. Mutates the
    run_dir (the winner ends up at ``run_dir/testenv``) and returns the
    meta record {chosen, source, probes, builds, cached, test_root,
    image, uv, seconds, error?}."""
    info: dict = {"chosen": "system", "source": "system", "probes": {},
                  "builds": {}, "cached": False, "test_root": None,
                  "image": None, "uv": uv_version()}
    t0 = time.time()
    deadline = t0 + budget
    testenv = run_dir / "testenv"
    sys_py = testenv / "bin" / "python"
    try:
        with _Lock(cache_key):
            info["submodules"] = ensure_submodules(repo)
            test_root = find_test_root(repo)
            info["test_root"] = (str(test_root.relative_to(repo))
                                 if test_root else None)
            if test_root is None:
                info["error"] = "no test root found"
                return info

            def _probe(py):
                if test_root is None:
                    return {"rc": None, "errors": None, "interp_errors": None,
                            "collected": 0, "seconds": 0, "top": [],
                            "tail": "no test root", "usable": False}
                return probe(py, repo, test_root, base_env=base_env,
                             harness=harness)

            # 0. a cached decision for this instance
            cached = cached_choice(cache_key)
            if cached:
                info["cached"] = True
                ver = cached["chosen"]
                if ver == "system":
                    info["source"] = cached.get("source", "system")
                    return info
                py, log = build_venv(ver, run_dir / ("testenv_%s" % ver),
                                     repo, freeze=cached.get("freeze") or None,
                                     base_env=base_env, deadline=deadline,
                                     setuptools=cached.get("setuptools"))
                info["builds"][ver] = log
                if py is not None and cached.get("source") == "image" \
                        and cached.get("image"):
                    log["extra_packages"] = image_extra_packages(
                        cached["image"], repo, py, base_env=base_env)
                if py is not None:
                    info["probes"][ver] = _probe(py)
                    _install_winner(run_dir, ver, ladder)
                    info["chosen"] = ver
                    info["source"] = cached.get("source", "cache")
                    info["image"] = cached.get("image")
                    return info
                info["error"] = "cached %s could not be rebuilt" % ver
                return info

            # 1. does the host venv collect cleanly? (modern repos: ~3 s)
            best_ver, best = "system", None
            if sys_py.is_file():
                r = _probe(str(sys_py))
                info["probes"]["system"] = r
                if r["usable"]:
                    best = r
                    if clean_enough(r):
                        _store_choice(cache_key, {"chosen": "system",
                                                  "source": "system",
                                                  "probe": r})
                        return info

            # 2. the grading image's own interpreter + package set
            if image:
                ie = image_env(image, base_env=base_env)
                if ie:
                    info["image"] = {"python": ie["python_full"],
                                     "freeze": len(ie["freeze"]),
                                     "setuptools": ie.get("setuptools")}
                    ver = ie["python"]
                    py, log = build_venv(ver, run_dir / ("testenv_%s" % ver),
                                         repo, freeze=ie["freeze"],
                                         base_env=base_env,
                                         deadline=deadline,
                                         setuptools=ie.get("setuptools"))
                    info["builds"][ver] = log
                    if py is not None:
                        log["extra_packages"] = image_extra_packages(
                            image, repo, py, base_env=base_env)
                        r = _probe(py)
                        info["probes"][ver] = r
                        # the image is authoritative: accept unless the
                        # interpreter itself cannot import the checkout
                        # (a stray interpreter-class error in a 3,500-test
                        # suite is noise, not a verdict)
                        if r["usable"] and _better(r, best):
                            best_ver, best = ver, r
                        if r["usable"] and (r["interp_errors"] or 0) <= max(
                                1, r["collected"] // 500):
                            _install_winner(run_dir, ver, ladder)
                            info["chosen"], info["source"] = ver, "image"
                            _store_choice(cache_key, {
                                "chosen": ver, "source": "image",
                                "image": image, "python": ie["python_full"],
                                "freeze": ie["freeze"],
                                "setuptools": ie.get("setuptools"),
                                "probe": r})
                            return info

            # 3. the probe ladder (no image): newest clean-enough wins
            decided = best is not None
            for ver in ladder:
                if time.time() - t0 > budget:
                    info["budget_exhausted"] = True
                    decided = False
                    break
                if ver in info["builds"]:
                    continue
                py, log = build_venv(ver, run_dir / ("testenv_%s" % ver),
                                     repo, base_env=base_env,
                                     deadline=deadline)
                info["builds"][ver] = log
                if py is None:
                    continue
                r = _probe(py)
                info["probes"][ver] = r
                if not r["usable"]:
                    decided = False
                    continue
                if _better(r, best):
                    best_ver, best = ver, r
                if clean_enough(r):
                    break
            if best_ver != "system":
                _install_winner(run_dir, best_ver, ladder)
                info["chosen"], info["source"] = best_ver, "probe"
            else:
                for v in ladder:
                    d = run_dir / ("testenv_%s" % v)
                    if d.exists():
                        shutil.rmtree(d, ignore_errors=True)
            if decided and best is not None:
                _store_choice(cache_key, {"chosen": best_ver,
                                          "source": info["source"],
                                          "probe": best})
            return info
    except Exception as ex:  # noqa: BLE001 -- never crash a cell
        info["error"] = repr(ex)
        if not sys_py.is_file() and (run_dir / "testenv_system").is_dir():
            try:
                (run_dir / "testenv_system").rename(testenv)
            except OSError:
                pass
        return info
    finally:
        info["seconds"] = round(time.time() - t0, 1)
