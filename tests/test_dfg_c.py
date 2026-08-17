"""Tests for the dfg server's Joern (C) path and the joern.py pending fix.

These cover behaviours specific to C kernel slicing through a
``joern --server`` (no live Joern required -- a fake client returns canned
payloads), plus the result-protocol fix in servers.common.joern:

- the slice/flow CPGQL matches files by BASENAME (scoped CPGs store
  ``location.filename`` relative to the c2cpg build root);
- the BACKWARD slice walks ``ddgIn`` (the only data-dependence step this
  Joern build exposes); the FORWARD slice uses ``reachableByFlows`` because
  ``ddgOut`` does not exist in this build -- it must NOT appear in the query;
- file basenames carrying a ``.`` produce no illegal Scala ``\\.`` escape;
- a configured-and-reachable Joern owns the answer (even an empty one) and
  the C source is never handed to the beniget engine, which cannot parse it;
- the real server's HTTP-200 "No result (yet?)" pending body is treated as
  "keep polling", not as a failure.
"""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from servers.dfg_server import server as dfg_server_mod
from servers.dfg_server.server import (
    _file_regex,
    _joern_backward_slice_query,
    _joern_forward_slice_query,
)
from servers.common.joern import JoernClient


# -- query building ------------------------------------------------------------


def test_file_regex_matches_by_basename() -> None:
    assert _file_regex("drivers/net/tg3.c") == ".*tg3.c$"
    assert _file_regex("drivers/gpu/vga/vga_switcheroo.c") == (
        ".*vga_switcheroo.c$"
    )
    # no illegal Scala escape (a literal \. is a compile error in a CPGQL
    # double-quoted string), even for paths with multiple dots
    assert "\\." not in _file_regex("fs/jfs/acl.c")
    assert "\\." not in _file_regex("a.b.c.c")


def test_backward_slice_query_uses_ddgin() -> None:
    query = _joern_backward_slice_query("drivers/net/tg3.c", 1500, "ret", 5)
    assert 'cpg.identifier.name("ret")' in query
    assert "lineNumber(1500)" in query
    assert "ddgIn" in query
    assert ".*tg3.c$" in query
    assert "location.filename" in query  # robust file field, not file.name
    assert query.endswith(".toJson")


def test_forward_slice_query_uses_reachable_by_flows_not_ddgout() -> None:
    query = _joern_forward_slice_query("drivers/net/tg3.c", 1500, "ret")
    # ddgOut does not exist in this Joern build; forward flow must use
    # reachableByFlows from a def-bound source.
    assert "ddgOut" not in query
    assert "reachableByFlows" in query
    assert "def src =" in query
    assert 'cpg.identifier.name("ret")' in query
    assert ".*tg3.c$" in query
    assert query.endswith(".toJson")


# -- engine selection / routing ------------------------------------------------


class _FakeJoernClient:
    """Reachable Joern: records queries, returns a canned payload."""

    queries: list = []
    payload: object = None

    def __init__(self, *args, **kwargs) -> None:
        pass

    def available(self) -> bool:
        return True

    def query_json(self, cpgql: str):
        type(self).queries.append(cpgql)
        return type(self).payload


def test_dfg_backward_slice_routes_to_joern_on_c(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_FakeJoernClient, "queries", [])
    monkeypatch.setattr(
        _FakeJoernClient,
        "payload",
        [
            {"code": "ret = f(x)", "lineNumber": 235, "file": "vga_switcheroo.c"},
            {"code": "x", "lineNumber": 216, "file": "vga_switcheroo.c"},
        ],
    )
    monkeypatch.setattr(dfg_server_mod, "JoernClient", _FakeJoernClient)
    monkeypatch.setattr(
        dfg_server_mod.engine,
        "resolve_file",
        lambda f: ("/abs/vga_switcheroo.c", "drivers/gpu/vga/vga_switcheroo.c"),
    )
    items = dfg_server_mod.backward_slice(
        file="drivers/gpu/vga/vga_switcheroo.c", line=235, var="ret"
    )
    assert items
    assert all(it["engine"] == "joern" for it in items)
    assert all("scope" not in it for it in items)  # whole-program engine
    (query,) = _FakeJoernClient.queries
    assert "ddgIn" in query and ".*vga_switcheroo.c$" in query


def test_dfg_forward_slice_routes_to_joern_on_c(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_FakeJoernClient, "queries", [])
    monkeypatch.setattr(
        _FakeJoernClient,
        "payload",
        [
            {"code": "active", "lineNumber": 220, "file": "vga_switcheroo.c"},
            {"code": "active", "lineNumber": 243, "file": "vga_switcheroo.c"},
        ],
    )
    monkeypatch.setattr(dfg_server_mod, "JoernClient", _FakeJoernClient)
    monkeypatch.setattr(
        dfg_server_mod.engine,
        "resolve_file",
        lambda f: ("/abs/vga_switcheroo.c", "drivers/gpu/vga/vga_switcheroo.c"),
    )
    items = dfg_server_mod.forward_slice(
        file="drivers/gpu/vga/vga_switcheroo.c", line=220, var="active"
    )
    assert items
    assert all(it["engine"] == "joern" for it in items)
    (query,) = _FakeJoernClient.queries
    assert "reachableByFlows" in query and "ddgOut" not in query


def test_dfg_joern_empty_does_not_fall_back_to_beniget_on_c(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Reachable Joern returns empty (var/line outside scope). The C source
    # must NOT be handed to beniget (which would SyntaxError); an empty list
    # is the correct, non-crashing answer.
    monkeypatch.setattr(_FakeJoernClient, "queries", [])
    monkeypatch.setattr(_FakeJoernClient, "payload", [])
    monkeypatch.setattr(dfg_server_mod, "JoernClient", _FakeJoernClient)

    def _boom(*a, **k):  # would be called only if beniget ran
        raise AssertionError("beniget must not run when Joern is reachable")

    monkeypatch.setattr(
        dfg_server_mod.engine,
        "resolve_file",
        lambda f: ("/abs/sbs.c", "drivers/acpi/sbs.c"),
    )
    monkeypatch.setattr(dfg_server_mod.engine, "module_dfg", _boom)
    items = dfg_server_mod.backward_slice(
        file="drivers/acpi/sbs.c", line=120, var="result"
    )
    assert items == []


# -- joern.py pending-result protocol ------------------------------------------


class _PendingThenReadyHandler(BaseHTTPRequestHandler):
    """Mimics the real joern server: POST /query -> uuid; the first GET
    /result/<uuid> answers HTTP 200 with a 'No result (yet?)' body, the next
    answers HTTP 200 with the real result.
    """

    state = {"polls": 0}

    def log_message(self, *args) -> None:  # silence
        pass

    def _send(self, payload: dict) -> None:
        body = __import__("json").dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        type(self).state["polls"] = 0
        self._send({"success": True, "uuid": "abc"})

    def do_GET(self) -> None:
        type(self).state["polls"] += 1
        if type(self).state["polls"] == 1:
            # HTTP 200 pending sentinel (the real server's behaviour)
            self._send(
                {"success": False, "err": "No result (yet?) found for specified UUID"}
            )
        else:
            self._send(
                {
                    "success": True,
                    "uuid": "abc",
                    "stdout": 'val res0: String = "[{\\"v\\":1}]"',
                }
            )


def test_joern_client_treats_http200_no_result_as_pending() -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _PendingThenReadyHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{httpd.server_address[1]}"
        client = JoernClient(
            base_url=url, poll_interval=0.01, poll_timeout=5.0
        )
        # The first /result is the HTTP-200 pending sentinel; the client must
        # keep polling and return the real payload from the second poll.
        result = client.query_json('cpg.method.size')
        assert result == [{"v": 1}]
        assert _PendingThenReadyHandler.state["polls"] >= 2
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


# -- dfg flow-first on C (dfg_flow_text via the live CPG def-use) --------------
#
# The SecBench dfg_perrep_flow arm: flow_of on a C function must render its
# def-use graph from the running Joern server's DDG (no beniget -- it cannot
# parse C), and flow_check must fingerprint the C function's def-use from the
# same server so a data-flow edit round-trips.


def _fake_def_use_items():
    # (var, def_line, use_line) rows as _joern_def_use_items yields them
    # (a def-use + a use-def item per pair).
    return [
        {"from": "buf.c:10:len", "to": "buf.c:14:len", "kind": "def-use",
         "var": "len", "file": "buf.c", "engine": "joern"},
        {"from": "buf.c:14:len", "to": "buf.c:10:len", "kind": "use-def",
         "var": "len", "file": "buf.c", "engine": "joern"},
        {"from": "buf.c:10:len", "to": "buf.c:16:len", "kind": "def-use",
         "var": "len", "file": "buf.c", "engine": "joern"},
        {"from": "buf.c:16:len", "to": "buf.c:10:len", "kind": "use-def",
         "var": "len", "file": "buf.c", "engine": "joern"},
    ]


def test_dfg_flow_text_c_routes_to_joern(monkeypatch, tmp_path):
    from servers.common import flowedit, joern_cfg, env

    c_file = tmp_path / "buf.c"
    c_file.write_text("int f(int n) {\n  int len = n;\n...\n}\n", "utf-8")
    monkeypatch.setattr(env, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(joern_cfg, "is_c_file", lambda p: str(p).endswith(".c"))
    monkeypatch.setattr(
        flowedit, "resolve_func_file", lambda spec: (c_file, 10))

    class _Cli:
        def __init__(self, *a, **k):
            pass

        def available(self):
            return True

    monkeypatch.setattr("servers.common.joern.JoernClient", _Cli)
    from servers.dfg_server import server as dfg_srv
    monkeypatch.setattr(dfg_srv, "_joern_def_use_items",
                        lambda cl, rel, fn, var: _fake_def_use_items())

    text = flowedit.dfg_flow_text("f")
    assert text is not None
    assert "value-flow (def-use) graph of f()" in text
    assert "engine=joern" in text
    assert "len" in text and "-> uses L14, L16" in text


def test_dfg_flow_text_c_no_joern_gives_note(monkeypatch, tmp_path):
    from servers.common import flowedit, joern_cfg, env

    c_file = tmp_path / "buf.c"
    c_file.write_text("int f(void){}\n", "utf-8")
    monkeypatch.setattr(env, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(joern_cfg, "is_c_file", lambda p: True)
    monkeypatch.setattr(
        flowedit, "resolve_func_file", lambda spec: (c_file, 1))

    class _Cli:
        def __init__(self, *a, **k):
            pass

        def available(self):
            return False

    monkeypatch.setattr("servers.common.joern.JoernClient", _Cli)
    text = flowedit.dfg_flow_text("f")
    assert text is not None and "no value-flow graph" in text


def test_dfg_c_fingerprint_from_snippet_cpg(monkeypatch, tmp_path):
    """C dfg fingerprints come from a SNIPPET CPG built from the function's
    CURRENT on-disk source -- NOT the per-instance joern server. The server's
    CPG is built once PRE-edit and never refreshed, so post-edit fingerprints
    read from it always equaled the pre-edit graph and every C data-flow
    verification vacuously observed no_flow_change (review finding)."""
    from servers.common import flowedit, joern_cfg

    c_file = tmp_path / "buf.c"
    c_file.write_text(
        "\n" * 9 + "int f(int n){int len=n; return len;}\n", "utf-8")
    monkeypatch.setattr(joern_cfg, "is_c_file", lambda p: True)
    monkeypatch.setattr(
        flowedit, "_resolve_row", lambda spec: (c_file, 10, 10, "f"))

    captured = {}

    def fake_fp(src, suffix=".c"):
        captured["src"], captured["suffix"] = src, suffix
        return (2, frozenset({("len", 0, 1), ("len", 0, 2)}))

    monkeypatch.setattr(joern_cfg, "c_dfg_fingerprint", fake_fp)
    fp = flowedit.func_fingerprint_now("dfg", "f")
    assert fp == (2, frozenset({("len", 0, 1), ("len", 0, 2)}))
    assert "int len=n" in captured["src"]   # the CURRENT source was extracted
    assert captured["suffix"] == ".c"


def test_c_dfg_snippet_fingerprint_detects_add_def_use():
    """Live snippet-DDG round-trip: introducing a new def-and-use in a C
    function changes the snippet fingerprint and classifies as a dfg delta.
    Skipped where Joern/JDK are absent (e.g. CI)."""
    import pytest
    from servers.common import flowedit, joern_cfg
    if not joern_cfg.available():
        pytest.skip("Joern CLI / JDK not installed")
    before = ("int f(int n) {\n"
              "  int t = n;\n"
              "  return t;\n"
              "}\n")
    after = ("int f(int n) {\n"
             "  int t = n;\n"
             "  int len = t + 1;\n"
             "  return len;\n"
             "}\n")
    fb = joern_cfg.c_dfg_fingerprint(before)
    fa = joern_cfg.c_dfg_fingerprint(after)
    assert fb is not None and fa is not None, (fb, fa)
    assert fb != fa, fb
    assert flowedit.classify_delta("dfg", fb, fa) in ("add_def_use",
                                                      "move_def_use")
