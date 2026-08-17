"""Joern server REST client for the cfg/dfg representation servers.

Speaks the ``joern --server`` HTTP protocol (STUDY_PLAN.md section 3):

- ``POST /query`` with a JSON body ``{"query": "<CPGQL>"}`` returns
  ``{"uuid": "<uuid>"}``;
- ``GET /result/<uuid>`` is polled until the result is ready (the server
  answers a non-200 status while the query is still running, then 200 with
  a JSON body, conventionally ``{"success": bool, "stdout": str,
  "stderr": str}``);
- requests carry HTTP basic auth when credentials are configured.

Configuration is environment-gated and resolved at call time (never cached
at import), matching ``servers.common.env``:

- ``JOERN_SERVER_URL`` -- base URL of the running ``joern --server``; when
  unset (and no explicit ``base_url`` was passed), ``available()`` is False
  and every query raises :class:`JoernUnavailable`.
- ``JOERN_USER`` / ``JOERN_PASSWORD`` -- optional basic-auth credentials.

Only the stdlib is used (``urllib``); ``requests`` is intentionally not a
dependency. Connection-level failures (server down, refused, timed out,
auth rejected) raise :class:`JoernUnavailable` with a clear message so the
cfg/dfg servers can fall back to their pure-Python engines.

The query helpers ``cfg_for_method`` and ``reachable_by_flows`` are thin:
they submit a fixed CPGQL template and return the JSON parsed from the
result stdout. Interpretation of the returned nodes/flows is owned by the
cfg/dfg servers, not by this client.
"""

import base64
import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any

# ANSI SGR colour codes the Joern REPL embeds in its ``stdout`` (e.g.
# ``\x1b[33mval\x1b[0m``); they must be stripped before any JSON parse.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

__all__ = ["JoernClient", "JoernUnavailable"]

# Result-endpoint statuses that mean "query still running, keep polling".
_PENDING_STATUSES = (400, 404)

# The real ``joern --server`` answers GET /result/<uuid> with HTTP 200 and a
# body ``{"success": false, "err": "No result (yet?) found for specified
# UUID"}`` while the submitted query is still running -- it does NOT use a
# 400/404 status for "pending". Recognizing this sentinel (and only this one)
# as "still pending" is what lets the client poll a live Joern to completion
# without mistaking the not-ready body for a genuine query failure.
_PENDING_ERR_MARKER = "no result"


class JoernUnavailable(RuntimeError):
    """Raised when the Joern server is not configured or not reachable."""


def _escape(value: str) -> str:
    """Escape a string for safe embedding in a double-quoted CPGQL literal."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _extract_json(stdout: str) -> Any:
    """Parse the JSON payload out of a Joern query's stdout.

    The ``.toJson`` output reaches us as a Scala REPL value binding whose
    right-hand side is a ``String`` rendered one of two ways, depending on
    its length/content:

    - triple-quoted and raw, e.g. ``res0: String = \"\"\"[{...}]\"\"\"`` --
      the inner text is already the JSON document;
    - double-quoted and escaped, e.g.
      ``res1: String = "[{\\"code\\":...}]"`` -- the inner text is a JSON
      *string literal* of the document (inner quotes and newlines escaped),
      so it must be JSON-decoded once to recover the document, then parsed.

    Bare JSON (the test stub's convention) and arbitrary REPL output that
    merely contains a ``[...]``/``{...}`` slice are handled as fallbacks.
    ANSI colour codes the live Joern REPL emits are stripped first.
    """
    text = _ANSI_RE.sub("", stdout).strip()
    # Fast path: the payload is already bare JSON (test stub / direct json).
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass

    # REPL value binding ``... : String = <rhs>``; isolate the right-hand side.
    rhs = text
    eq = text.find(" = ")
    if eq != -1 and ": String" in text[:eq]:
        rhs = text[eq + 3 :].strip()

    # Triple-quoted raw JSON: take the content between the outer ``"""``.
    if rhs.startswith('"""'):
        inner = rhs[3:]
        close = inner.rfind('"""')
        if close != -1:
            inner = inner[:close]
        try:
            return json.loads(inner)
        except (json.JSONDecodeError, ValueError):
            pass

    # Double-quoted escaped JSON: decode the string literal, then parse it.
    if rhs.startswith('"') and rhs.endswith('"') and len(rhs) >= 2:
        try:
            decoded = json.loads(rhs)  # -> the inner JSON document as a str
        except (json.JSONDecodeError, ValueError):
            decoded = None
        if isinstance(decoded, str):
            try:
                return json.loads(decoded)
            except (json.JSONDecodeError, ValueError):
                pass

    # Last resort: slice the outermost bracketed region and parse it.
    starts = [idx for idx in (rhs.find("["), rhs.find("{")) if idx != -1]
    if not starts:
        raise ValueError(f"no JSON payload found in Joern stdout: {text[:200]!r}")
    start = min(starts)
    end = max(rhs.rfind("]"), rhs.rfind("}"))
    if end <= start:
        raise ValueError(f"no JSON payload found in Joern stdout: {text[:200]!r}")
    return json.loads(rhs[start : end + 1])


class JoernClient:
    """Minimal stdlib REST client for a running ``joern --server``.

    Parameters
    ----------
    base_url:
        Server base URL, e.g. ``http://localhost:8080``. When ``None`` the
        ``JOERN_SERVER_URL`` environment variable is consulted at call time.
    auth:
        ``(user, password)`` for HTTP basic auth. When ``None`` the
        ``JOERN_USER`` / ``JOERN_PASSWORD`` environment variables are
        consulted at call time; no auth header is sent when neither is set.
    timeout:
        Per-HTTP-request timeout in seconds.
    poll_interval:
        Sleep between ``GET /result/<uuid>`` polls in seconds.
    poll_timeout:
        Total seconds to wait for a submitted query before giving up.
    """

    def __init__(
        self,
        base_url: str | None = None,
        auth: tuple[str, str] | None = None,
        timeout: float = 10.0,
        poll_interval: float = 0.05,
        poll_timeout: float = 60.0,
    ) -> None:
        """Store configuration; env vars are resolved at call time."""
        self._explicit_base_url = base_url
        self._explicit_auth = auth
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.poll_timeout = poll_timeout

    # -- configuration -------------------------------------------------------

    def base_url(self) -> str:
        """Resolve the server base URL (explicit arg wins over env), '' if unset."""
        value = (
            self._explicit_base_url
            if self._explicit_base_url is not None
            else os.environ.get("JOERN_SERVER_URL", "")
        )
        return (value or "").rstrip("/")

    def auth(self) -> tuple[str, str] | None:
        """Resolve basic-auth credentials, or None when none are configured."""
        if self._explicit_auth is not None:
            return self._explicit_auth
        user = os.environ.get("JOERN_USER", "")
        password = os.environ.get("JOERN_PASSWORD", "")
        if not user and not password:
            return None
        return (user, password)

    def available(self) -> bool:
        """True iff a server base URL is configured (explicitly or via env)."""
        return bool(self.base_url())

    # -- protocol ------------------------------------------------------------

    def submit(self, query: str) -> str:
        """POST the CPGQL query to /query; return the result uuid."""
        payload = self._request(
            "POST", "/query", body=json.dumps({"query": query}).encode("utf-8")
        )
        uuid = payload.get("uuid") if isinstance(payload, dict) else None
        if not uuid:
            raise JoernUnavailable(
                f"Joern server at {self.base_url()} returned no uuid for "
                f"POST /query (got: {payload!r})"
            )
        return str(uuid)

    def result(self, uuid: str) -> dict | None:
        """GET /result/<uuid>; return the parsed JSON, or None while pending.

        Pending is signalled two ways and both are treated as "keep polling":
        a 400/404 status (the test stub's convention), or -- as the real
        ``joern --server`` actually behaves -- an HTTP 200 body of the form
        ``{"success": false, "err": "No result (yet?) found for specified
        UUID"}``. Any other ``success: false`` body is a genuine failure and
        is returned to the caller (``query_json`` raises on it).
        """
        try:
            payload = self._request("GET", f"/result/{uuid}")
        except _Pending:
            return None
        if not isinstance(payload, dict):
            raise JoernUnavailable(
                f"Joern server at {self.base_url()} returned a non-object "
                f"result for uuid {uuid}: {payload!r}"
            )
        if payload.get("success") is False:
            err = str(payload.get("err") or payload.get("error") or "")
            if _PENDING_ERR_MARKER in err.lower():
                return None  # query is still running on the server
        return payload

    def query(self, cpgql: str) -> dict:
        """Submit a CPGQL query and poll until done; return the result dict.

        Raises :class:`JoernUnavailable` when the server is not configured,
        not reachable, or the result does not arrive within ``poll_timeout``.
        """
        if not self.available():
            raise JoernUnavailable(
                "JOERN_SERVER_URL is not set and no base_url was provided; "
                "the Joern engine is unavailable (use the pure-Python engine)"
            )
        uuid = self.submit(cpgql)
        deadline = time.monotonic() + self.poll_timeout
        while True:
            payload = self.result(uuid)
            if payload is not None:
                return payload
            if time.monotonic() >= deadline:
                raise JoernUnavailable(
                    f"Joern server at {self.base_url()} did not produce a "
                    f"result for uuid {uuid} within {self.poll_timeout}s"
                )
            time.sleep(self.poll_interval)

    def query_json(self, cpgql: str) -> Any:
        """Run a query whose stdout is a JSON document; return it parsed."""
        payload = self.query(cpgql)
        if not payload.get("success", True):
            raise JoernUnavailable(
                "Joern query failed: "
                + str(
                    payload.get("stderr")
                    or payload.get("err")
                    or payload.get("error")
                    or payload.get("stdout")
                    or payload
                )
            )
        return _extract_json(str(payload.get("stdout", "")))

    # -- thin query helpers (interpretation is owned by the cfg/dfg servers) --

    def cfg_for_method(self, name: str) -> Any:
        """Return the CFG nodes of every method named ``name``, parsed JSON."""
        escaped = _escape(name)
        cpgql = (
            f'cpg.method.name("{escaped}").cfgNode.map(node => Map('
            '"id" -> node.id, "code" -> node.code, '
            '"lineNumber" -> node.lineNumber.getOrElse(-1))).toJson'
        )
        return self.query_json(cpgql)

    def reachable_by_flows(self, source: str, sink: str) -> Any:
        """Return reachableByFlows paths from ``source`` to ``sink``, parsed JSON."""
        src = _escape(source)
        snk = _escape(sink)
        cpgql = (
            f'def source = cpg.identifier.name("{src}"); '
            f'def sink = cpg.call.name("{snk}").argument; '
            "sink.reachableByFlows(source).map(flow => flow.elements.map(node => "
            'Map("code" -> node.code, '
            '"lineNumber" -> node.lineNumber.getOrElse(-1)))).toJson'
        )
        return self.query_json(cpgql)

    # -- internals -------------------------------------------------------------

    def _request(self, method: str, path: str, body: bytes | None = None) -> Any:
        """Perform one HTTP request; return the parsed JSON response body."""
        base = self.base_url()
        if not base:
            raise JoernUnavailable(
                "JOERN_SERVER_URL is not set and no base_url was provided; "
                "the Joern engine is unavailable (use the pure-Python engine)"
            )
        url = base + path
        request = urllib.request.Request(url, data=body, method=method)
        request.add_header("Content-Type", "application/json")
        auth = self.auth()
        if auth is not None:
            token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode("utf-8"))
            request.add_header("Authorization", "Basic " + token.decode("ascii"))
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            if method == "GET" and path.startswith("/result/") and (
                exc.code in _PENDING_STATUSES
            ):
                raise _Pending() from exc
            raise JoernUnavailable(
                f"Joern server at {base} rejected {method} {path}: "
                f"HTTP {exc.code} {exc.reason}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise JoernUnavailable(
                f"cannot reach Joern server at {base}: {reason}"
            ) from exc
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise JoernUnavailable(
                f"Joern server at {base} returned a non-JSON response for "
                f"{method} {path}: {raw[:200]!r}"
            ) from exc


class _Pending(Exception):
    """Internal marker: GET /result/<uuid> says the query is still running."""
