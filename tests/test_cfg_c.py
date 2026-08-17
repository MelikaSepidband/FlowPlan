"""Tests for the cfg server's Joern (C) path and the joern.py protocol fix.

These exercise the behaviours that only matter for C kernel sources analysed
through a ``joern --server``, WITHOUT requiring a live Joern (a fake client
returns realistic kernel-shaped payloads):

- the CPGQL the cfg server builds matches a file by BASENAME (the scoped
  kernel CPGs store ``location.filename`` relative to the c2cpg build root, so
  the full repo-relative path the caller passes would not match);
- a get_cfg item from Joern remaps Joern's opaque 64-bit node ids to small
  sequential ints and keeps a single (large) item within the 4k-token
  envelope budget, so a big CFG is not silently dropped;
- ``first_line_code`` is one trimmed line, not a whole method body;
- a function outside the loaded CPG's scope yields an explicit empty
  ``engine="joern"`` note item (no crash, no python-ast fallback on C);
- the real ``joern --server`` result protocol -- HTTP 200 "No result (yet?)"
  while pending, ANSI-coloured REPL stdout, escaped vs triple-quoted ``.toJson``
  strings -- is parsed correctly by servers.common.joern.
"""

import json

import pytest

from servers.cfg_server import server as cfg_server_mod
from servers.cfg_server.server import (
    _file_name_regex,
    _first_line,
    _joern_cfg_item,
    _joern_cfg_query,
)
from servers.common import budget
from servers.common.joern import _extract_json


# -- query building ------------------------------------------------------------


def test_file_name_regex_matches_by_basename() -> None:
    # repo-relative path in -> basename-anchored regex out (the CPG stores
    # only the basename when built with a subsystem dir as root).
    assert _file_name_regex("drivers/gpu/vga/vga_switcheroo.c") == (
        ".*vga_switcheroo.c$"
    )
    assert _file_name_regex("tg3.c") == ".*tg3.c$"
    # backslash paths are normalized; no illegal Scala escape is produced.
    assert "\\." not in _file_name_regex("drivers\\acpi\\sbs.c")


def test_joern_cfg_query_filters_by_basename_not_full_path() -> None:
    query = _joern_cfg_query(
        "vga_switchto_stage1", "drivers/gpu/vga/vga_switcheroo.c"
    )
    assert query.startswith('cpg.method.name("vga_switchto_stage1")')
    assert '_.file.name(".*vga_switcheroo.c$")' in query
    # the full repo path must NOT appear (it would match nothing in the CPG)
    assert "drivers/gpu/vga" not in query
    assert query.endswith(".toJson")


def test_first_line_trims_and_caps() -> None:
    body = "{\n\tint ret;\n\tint i;\n\treturn 0;\n}"
    assert _first_line(body) == "{"
    assert _first_line("  \n  ret = f(x)  \n") == "ret = f(x)"
    assert _first_line("") == ""
    assert len(_first_line("a" * 5000)) <= 200


# -- get_cfg item shaping (id remap + budget) ----------------------------------


def _kernel_method_payload(n_nodes: int = 96) -> dict:
    """A Joern method record shaped like vga_switchto_stage1's real output.

    The first node carries the whole method body as ``code`` and every node
    has an opaque 64-bit id, mirroring real Joern output -- the two things
    that, unmitigated, blow the envelope budget.
    """
    base_id = 25769803801
    nodes = [
        {
            "id": base_id,
            "code": "{\n\tint ret;\n\tint i;\n\treturn 0;\n}",
            "line": 217,
        }
    ]
    for k in range(1, n_nodes):
        nodes.append(
            {
                "id": base_id + k * 1000,
                "code": f"some_statement_number_{k}(arg_a, arg_b, arg_c)",
                "line": 217 + k,
            }
        )
    edges = [
        {"src": nodes[k]["id"], "dst": nodes[k + 1]["id"]}
        for k in range(n_nodes - 1)
    ]
    return {
        "name": "vga_switchto_stage1",
        "file": "vga_switcheroo.c",
        "line": 216,
        "entry": 107374182412,  # METHOD node id (not a cfgNode)
        "exit": 124554051595,  # METHOD_RETURN node id (not a cfgNode)
        "nodes": nodes,
        "edges": edges,
    }


def test_joern_cfg_item_remaps_ids_to_small_ints() -> None:
    item = _joern_cfg_item(_kernel_method_payload(96), "vga_switchto_stage1", 100)
    assert item["engine"] == "joern"
    # block ids are small sequential ints, not the opaque 64-bit Joern ids
    block_ids = [b["id"] for b in item["blocks"]]
    assert all(isinstance(bid, int) and bid < 1000 for bid in block_ids)
    assert max(block_ids) < len(item["blocks"]) + 2  # entry/exit also mapped
    # edges reference remapped ids only
    for edge in item["edges"]:
        assert edge["src"] in block_ids and edge["dst"] in block_ids
        assert edge["kind"] == "next"
    # entry/exit got remapped (METHOD/METHOD_RETURN -> small ints, not None)
    assert isinstance(item["entry"], int)
    assert isinstance(item["exit"], int)
    # first block's first_line_code is one trimmed line, not the whole body
    assert item["blocks"][0]["first_line_code"] == "{"


def test_joern_cfg_item_fits_token_budget() -> None:
    # A 96-node CFG with opaque ids would exceed 4k tokens unmitigated; after
    # id remapping (and a defensive node cap) a single item must fit so the
    # envelope does not drop it entirely.
    item = _joern_cfg_item(_kernel_method_payload(96), "vga_switchto_stage1", 100)
    assert budget.estimate_tokens([item]) <= budget.TOKEN_BUDGET
    kept, truncated = budget.fit([item])
    assert kept == [item]
    assert truncated is False
    assert len(item["blocks"]) >= 1


# -- engine selection / graceful fallback --------------------------------------


class _FakeJoernClient:
    """A reachable Joern that returns a fixed get_cfg payload (or empty)."""

    payload: object = None

    def __init__(self, *args, **kwargs) -> None:
        pass

    def available(self) -> bool:
        return True

    def query_json(self, cpgql: str):
        return type(self).payload


def test_get_cfg_returns_joern_item(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        _FakeJoernClient, "payload", [_kernel_method_payload(40)]
    )
    monkeypatch.setattr(cfg_server_mod, "JoernClient", _FakeJoernClient)
    monkeypatch.setattr(
        cfg_server_mod,
        "_resolve_functions",
        lambda function, file=None: [
            {
                "name": "vga_switchto_stage1",
                "qualname": "vga_switchto_stage1",
                "file": "drivers/gpu/vga/vga_switcheroo.c",
                "line": 216,
                "end_line": 246,
            }
        ],
    )
    items = cfg_server_mod.get_cfg(
        "vga_switchto_stage1", file="drivers/gpu/vga/vga_switcheroo.c"
    )
    assert len(items) == 1
    assert items[0]["engine"] == "joern"
    assert items[0]["blocks"]
    assert items[0]["edges"]


def test_get_cfg_out_of_scope_returns_joern_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Reachable Joern but the function is not in the loaded CPG, and the
    # snippet build cannot help either -> explicit engine="joern" note item,
    # never a python-ast SyntaxError on the C source. The note must NOT name
    # the CPG's scoping (the old "covers only the benchmark subsystem"
    # wording was an oracle tell on the ground-truth-scoped kernel CPGs).
    monkeypatch.setattr(_FakeJoernClient, "payload", [])
    monkeypatch.setattr(cfg_server_mod, "JoernClient", _FakeJoernClient)
    monkeypatch.setattr(
        cfg_server_mod, "_snippet_cfg_item", lambda row, max_nodes: None
    )
    row = {
        "name": "some_c_func",
        "qualname": "some_c_func",
        "file": "drivers/acpi/sbs.c",
        "line": 100,
        "end_line": 150,
    }
    monkeypatch.setattr(
        cfg_server_mod, "_resolve_functions", lambda function, file=None: [row]
    )
    items = cfg_server_mod.get_cfg("some_c_func")
    assert len(items) == 1
    assert items[0]["engine"] == "joern"
    assert items[0]["blocks"] == []
    assert "note" in items[0]
    for tell in ("subsystem", "scope", "benchmark"):
        assert tell not in items[0]["note"]
    assert "subsystem" not in cfg_server_mod._CPG_SCOPE_NOTE


def test_get_cfg_python_ast_on_c_does_not_crash(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No Joern configured + a C source -> python-ast cannot parse it, but the
    # tool must return a clear note item instead of raising SyntaxError.
    c_file = tmp_path / "drv.c"
    c_file.write_text("int f(int x) { return x; }\n", encoding="utf-8")

    class _NoJoern:
        def __init__(self, *a, **k) -> None:
            pass

        def available(self) -> bool:
            return False

    monkeypatch.setattr(cfg_server_mod, "JoernClient", _NoJoern)
    monkeypatch.setattr(
        cfg_server_mod, "_snippet_cfg_item", lambda row, max_nodes: None
    )
    row = {
        "name": "f",
        "qualname": "f",
        "file": "drv.c",
        "line": 1,
        "end_line": 1,
    }
    monkeypatch.setattr(
        cfg_server_mod, "_resolve_functions", lambda function, file=None: [row]
    )
    monkeypatch.setattr(
        cfg_server_mod, "_cfg_for_row",
        lambda r: (_ for _ in ()).throw(SyntaxError("unterminated string")),
    )
    items = cfg_server_mod.get_cfg("f")
    assert len(items) == 1
    assert items[0]["engine"] == "python-ast"
    assert items[0]["blocks"] == []
    assert "note" in items[0]


# -- snippet-CPG fallback (the LinuxFLBench scoped-CPG leak fix) ---------------


class _NoJoernClient:
    def __init__(self, *a, **k) -> None:
        pass

    def available(self) -> bool:
        return False


def _snippet_row() -> dict:
    return {
        "name": "acpi_sbs_add",
        "qualname": "acpi_sbs_add",
        "file": "drivers/acpi/sbs.c",
        "line": 100,
        "end_line": 130,
    }


def test_get_cfg_snippet_fallback_no_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # NO joern --server at all: a C function must still get a real CFG via a
    # per-function snippet CPG (c2cpg on just its source) -- the leak fix
    # that removes any need for the ground-truth-scoped kernel CPGs. Node
    # lines come back snippet-relative (1 = first line of the function) and
    # must be re-offset to real file lines.
    from servers.common import flowedit, joern_cfg

    monkeypatch.setattr(cfg_server_mod, "JoernClient", _NoJoernClient)
    row = _snippet_row()
    monkeypatch.setattr(
        cfg_server_mod, "_resolve_functions", lambda function, file=None: [row]
    )
    monkeypatch.setattr(
        flowedit, "_func_source_ranged",
        lambda path, line, end: "static int acpi_sbs_add(void)\n{\n...\n}\n",
    )
    monkeypatch.setattr(joern_cfg, "available", lambda: True)
    monkeypatch.setattr(
        joern_cfg, "c_cfg_method_records",
        lambda src, suffix=".c": [
            {
                "name": "acpi_sbs_add",
                "line": 1,
                "entry": 900,
                "exit": 901,
                "nodes": [
                    {"id": 1, "code": "if (!sbs)", "line": 3},
                    {"id": 2, "code": "return -ENOMEM;", "line": 4},
                ],
                "edges": [{"src": 1, "dst": 2}],
            }
        ],
    )
    items = cfg_server_mod.get_cfg("acpi_sbs_add")
    assert len(items) == 1
    item = items[0]
    assert item["engine"] == "joern_snippet"
    assert item["file"] == "drivers/acpi/sbs.c"
    assert item["line"] == 100
    # snippet line 3 -> real file line 100 + (3 - 1) = 102
    ranges = [b["line_range"] for b in item["blocks"] if b["line_range"]]
    assert [102, 102] in ranges and [103, 103] in ranges
    assert all(e["kind"] == "next" for e in item["edges"])
    assert isinstance(item["entry"], int) and isinstance(item["exit"], int)


def test_get_cfg_snippet_fallback_on_cpg_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Reachable server whose loaded CPG lacks the function: the snippet build
    # must be tried BEFORE giving up with a note.
    from servers.common import flowedit, joern_cfg

    monkeypatch.setattr(_FakeJoernClient, "payload", [])
    monkeypatch.setattr(cfg_server_mod, "JoernClient", _FakeJoernClient)
    row = _snippet_row()
    monkeypatch.setattr(
        cfg_server_mod, "_resolve_functions", lambda function, file=None: [row]
    )
    monkeypatch.setattr(
        flowedit, "_func_source_ranged", lambda path, line, end: "int f() {}\n"
    )
    monkeypatch.setattr(joern_cfg, "available", lambda: True)
    monkeypatch.setattr(
        joern_cfg, "c_cfg_method_records",
        lambda src, suffix=".c": [
            {
                "name": "acpi_sbs_add",
                "line": 1,
                "entry": 10,
                "exit": 11,
                "nodes": [{"id": 1, "code": "int f() {}", "line": 1}],
                "edges": [],
            }
        ],
    )
    items = cfg_server_mod.get_cfg("acpi_sbs_add")
    assert len(items) == 1
    assert items[0]["engine"] == "joern_snippet"
    assert items[0]["blocks"]


def test_snippet_method_records_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    # Repeated flow_of calls on the same function must not rebuild the
    # snippet CPG (each build costs ~6.5s of subprocess JVM).
    from servers.common import joern_cfg

    calls = {"n": 0}

    def fake_dump(src, work, suffix=".c", timeout=300, script=None):
        calls["n"] += 1
        return [{"name": "g", "nodes": [], "edges": []}]

    monkeypatch.setattr(joern_cfg, "available", lambda: True)
    monkeypatch.setattr(joern_cfg, "_dump_cfg", fake_dump)
    joern_cfg._METHOD_RECORDS_CACHE.clear()
    src = "int g(void) { return 1; }\n"
    first = joern_cfg.c_cfg_method_records(src, suffix=".c")
    second = joern_cfg.c_cfg_method_records(src, suffix=".c")
    assert first == second
    assert calls["n"] == 1
    joern_cfg._METHOD_RECORDS_CACHE.clear()


# -- joern.py result protocol (the real-server behaviour) ----------------------


def test_extract_json_unwraps_escaped_repl_string() -> None:
    # The real REPL renders short .toJson results as an escaped String literal.
    stdout = 'val res0: String = "[{\\"code\\":\\"ret\\",\\"lineNumber\\":235}]"'
    assert _extract_json(stdout) == [{"code": "ret", "lineNumber": 235}]


def test_extract_json_unwraps_triple_quoted_repl_string() -> None:
    payload = [{"name": "f", "nodes": [{"code": "{\n\tint x;\n}"}]}]
    stdout = 'val res1: String = """' + json.dumps(payload) + '"""'
    assert _extract_json(stdout) == payload


def test_extract_json_strips_ansi_codes() -> None:
    stdout = '\x1b[33mval\x1b[0m res2: \x1b[32mString\x1b[0m = "[]"'
    assert _extract_json(stdout) == []


def test_extract_json_empty_list() -> None:
    assert _extract_json('val res3: String = "[]"') == []
