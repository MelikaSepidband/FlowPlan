"""Start / wait-for-ready / stop a ``joern --server`` with a CPG preloaded.

The runner uses ONE joern server per CPG: it starts the server with that
scoped CPG loaded (passed as the positional ``<cpg.bin>`` argument, which
Joern loads into the implicit ``cpg``), waits until POST /query answers HTTP
200, exposes JOERN_SERVER_URL / JOERN_USER / JOERN_PASSWORD so
``servers.common.joern.JoernClient`` connects, runs the cfg/dfg instances
for that CPG, then stops the server.

Design notes:

- The server is detached with ``setsid`` and its stdin redirected from
  /dev/null so it survives the launching shell and is killable as a process
  group (``os.killpg``).
- Readiness is detected by POSTing a trivial query and polling /query for a
  200; this is the same endpoint the JoernClient uses.
- ``JoernServer`` is a context manager: ``with JoernServer(cpg, ...) as srv:``
  starts+waits on enter and stops on exit. ``srv.client_env()`` returns the
  three env vars to pass to a spawned MCP server.

CLI usage (manual validation / runner glue):

    .venv/bin/python -m experiments.common.joern_server start \
        --cpg <path> [--host 127.0.0.1] [--port 8765] [--heap-gb 4]
    .venv/bin/python -m experiments.common.joern_server stop --pidfile <f>
"""

import argparse
import base64
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Tool locations, overridable via env (defaults match the study machine).
JDK_HOME = Path(os.environ.get("JOERN_JAVA_HOME",
                               str(Path.home() / "tools" / "jdk-21.0.5+11")))
JOERN_CLI = Path(os.environ.get("JOERN_CLI_DIR",
                                str(Path.home() / "tools" / "joern-cli")))
JOERN_LAUNCHER = JOERN_CLI / "joern"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_USER = "repr"
DEFAULT_PASSWORD = "repr-study"


class JoernServerError(RuntimeError):
    """Raised when the server fails to start or become ready."""


def _server_env(heap_gb: int) -> dict:
    """Environment for the joern launcher: JDK 21 + a bounded heap."""
    env = dict(os.environ)
    env["JAVA_HOME"] = str(JDK_HOME)
    env["PATH"] = str(JDK_HOME / "bin") + os.pathsep + env.get("PATH", "")
    env["JAVA_OPTS"] = f"-Xmx{int(heap_gb)}g"
    return env


def _probe_ready(base_url: str, user: str, password: str, timeout: float) -> bool:
    """POST a trivial query; True iff the server answers HTTP 200."""
    body = json.dumps({"query": "1"}).encode("utf-8")
    request = urllib.request.Request(
        base_url + "/query", data=body, method="POST"
    )
    request.add_header("Content-Type", "application/json")
    token = base64.b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    request.add_header("Authorization", "Basic " + token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status == 200
    except urllib.error.HTTPError as exc:
        return exc.code == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


class JoernServer:
    """Lifecycle wrapper around one ``joern --server`` with a preloaded CPG."""

    def __init__(
        self,
        cpg_path: Path,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        user: str = DEFAULT_USER,
        password: str = DEFAULT_PASSWORD,
        heap_gb: int = 4,
        log_path: Path | None = None,
    ) -> None:
        self.cpg_path = Path(cpg_path).resolve()
        self.host = host
        self.port = int(port)
        self.user = user
        self.password = password
        self.heap_gb = heap_gb
        self.log_path = (
            Path(log_path)
            if log_path
            else self.cpg_path.with_suffix(".server.log")
        )
        self._proc: subprocess.Popen | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def client_env(self) -> dict:
        """The env a spawned MCP server needs to reach this Joern server."""
        return {
            "JOERN_SERVER_URL": self.base_url,
            "JOERN_USER": self.user,
            "JOERN_PASSWORD": self.password,
        }

    def start(self, ready_timeout: float = 120.0) -> "JoernServer":
        """Launch the server (detached) and block until /query is ready."""
        if not JOERN_LAUNCHER.is_file():
            raise JoernServerError(f"joern launcher not found at {JOERN_LAUNCHER}")
        if not self.cpg_path.is_file():
            raise JoernServerError(f"CPG not found at {self.cpg_path}")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        log = open(self.log_path, "wb")
        cmd = [
            "bash",
            str(JOERN_LAUNCHER),
            "--server",
            "--server-host",
            self.host,
            "--server-port",
            str(self.port),
            "--server-auth-username",
            self.user,
            "--server-auth-password",
            self.password,
            str(self.cpg_path),
        ]
        # start_new_session=True => own process group, killable via killpg and
        # not torn down with the launching shell.
        self._proc = subprocess.Popen(
            cmd,
            env=_server_env(self.heap_gb),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.monotonic() + ready_timeout
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise JoernServerError(
                    f"joern server exited early (code {self._proc.returncode}); "
                    f"see {self.log_path}"
                )
            if _probe_ready(self.base_url, self.user, self.password, 5.0):
                return self
            time.sleep(1.0)
        self.stop()
        raise JoernServerError(
            f"joern server not ready within {ready_timeout}s; see {self.log_path}"
        )

    def stop(self) -> None:
        """Terminate the server process group; idempotent."""
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        self._proc = None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    def __enter__(self) -> "JoernServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


# -- CLI (manual control / runner glue) ----------------------------------------


def _cli_start(args: argparse.Namespace) -> int:
    """Start a server, write its pid to --pidfile, print connection info."""
    server = JoernServer(
        cpg_path=Path(args.cpg),
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        heap_gb=args.heap_gb,
        log_path=Path(args.log) if args.log else None,
    )
    server.start(ready_timeout=args.ready_timeout)
    if args.pidfile:
        Path(args.pidfile).write_text(str(server.pid), encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": True,
                "pid": server.pid,
                "base_url": server.base_url,
                **server.client_env(),
                "log": str(server.log_path),
            }
        )
    )
    return 0


def _cli_stop(args: argparse.Namespace) -> int:
    """Kill the process group recorded in --pidfile."""
    if not args.pidfile or not Path(args.pidfile).is_file():
        print(json.dumps({"ok": False, "error": "pidfile missing"}))
        return 1
    pid = int(Path(args.pidfile).read_text(encoding="utf-8").strip())
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    print(json.dumps({"ok": True, "killed": pid}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)

    start = sub.add_parser("start", help="start a server and wait for ready")
    start.add_argument("--cpg", required=True)
    start.add_argument("--host", default=DEFAULT_HOST)
    start.add_argument("--port", type=int, default=DEFAULT_PORT)
    start.add_argument("--user", default=DEFAULT_USER)
    start.add_argument("--password", default=DEFAULT_PASSWORD)
    start.add_argument("--heap-gb", type=int, default=4)
    start.add_argument("--ready-timeout", type=float, default=120.0)
    start.add_argument("--pidfile", default="")
    start.add_argument("--log", default="")
    start.set_defaults(func=_cli_start)

    stop = sub.add_parser("stop", help="stop the server in --pidfile")
    stop.add_argument("--pidfile", required=True)
    stop.set_defaults(func=_cli_stop)

    args = parser.parse_args()
    try:
        return args.func(args)
    except JoernServerError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
