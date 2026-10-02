"""Tests for experiments.common.flow_protocol -- the flow3 (v3) protocol
text, the flowfix skill wrapper, and the skill-vs-inline prompt modes.

The module serves ONLY the *_perrep_flow3 conditions (repair and security)."""

import pytest

from experiments.common import flow_protocol as fp

ORDER_REPAIR = "Do NOT call flow_plan until you can see the bug in a graph"


def _steps(task, rep):
    return fp.build_steps(task, rep, fp.tool_names(rep))


def test_tool_names_shape():
    assert fp.tool_names("cfg") == (
        "mcp__cfg_perrep__flow_of", "mcp__cfg_perrep__flow_locate",
        "mcp__cfg_perrep__flow_plan", "mcp__cfg_perrep__flow_check",
        "mcp__cfg_perrep__flow_verify")
    assert fp.tool_names("cfgdfg")[0] == "mcp__cfgdfg_perrep__flow_of"
    with pytest.raises(ValueError):
        fp.tool_names("ast")


def test_build_steps_validates_task_and_rep():
    with pytest.raises(ValueError):
        fp.build_steps("nonsense", "cfg", fp.tool_names("cfg"))
    with pytest.raises(ValueError):
        fp.build_steps("repair", "ast", fp.tool_names("cfg"))
    with pytest.raises(ValueError):
        fp.build_steps("repair", "cfg",
                       ("of", "locate", "plan", "check"))  # need 5


def test_build_steps_v3_elements_every_cell():
    """Every (task, rep) cell carries the v3 protocol elements: step-0
    ordering, fault_site in every delta, re-check ONCE, verify REQUIRED
    LAST, the add_function op -- and never the sentinel (skill-only)."""
    for task in fp.TASKS:
        for rep in fp.REPS:
            s = _steps(task, rep)
            assert "fault_site" in s, (task, rep)
            assert "re-check ONCE" in s, (task, rep)
            assert "REQUIRED LAST" in s, (task, rep)
            assert "add_function" in s, (task, rep)
            assert "no_flow_change" in s, (task, rep)
            assert fp.SENTINEL not in s, (task, rep)
            # the five tools are all named (incl. the step-2 flow_locate)
            for tool in fp.tool_names(rep):
                assert tool in s, (task, rep, tool)
            assert "flow_locate" in s, (task, rep)
            # step-0 ordering sentence
            assert ORDER_REPAIR in s, (task, rep)
            # the Grep/Read localization fallback
            assert "IF THE BUG IS NOT IN THE FLOW" in s, (task, rep)
            assert "Grep" in s and "Read" in s, (task, rep)


def test_step2_flow_locate_wording_per_task():
    """Step 2 is the SEPARATE flow_locate call in all 6 combos, with
    task-matched wording: repair = the faulty spot the graph shows;
    security = the crash-path spot that needs the guard. All inherit into
    flow_plan's fault_site, and step 3 stays the Grep/Read fallback."""
    for rep in fp.REPS:
        locate = "mcp__%s_perrep__flow_locate" % rep
        graph_word = "graphs" if rep == "cfgdfg" else "graph"
        r = _steps("repair", rep)
        s = _steps("security", rep)
        for x in (r, s):
            assert ("2. (REQUIRED) Call `%s`" % locate) in x, rep
            assert "inherit it as their fault_site" in x, rep
            assert "3. IF THE BUG IS NOT IN THE FLOW" in x, rep
            # renumbering: plan=4, edit=5, check=6, verify=7 (LAST)
            assert "\n4." in x and "\n5." in x and "\n6." in x, rep
            assert "7. (REQUIRED LAST)" in x, rep
        assert ("faulty spot the %s shows" % graph_word) in r, rep
        assert "why the flow is wrong there" in r, rep
        assert "crash-path spot" not in r, rep
        assert "crash-path spot that needs the guard" in s, rep
        assert "sanitizer frames" in s, rep


def test_build_steps_task_wording_isolation():
    """repair and security each keep their own wording: security has the C
    guard/crash wording, repair does not."""
    for rep in fp.REPS:
        r = _steps("repair", rep)
        s = _steps("security", rep)
        # repair speaks of fixing by editing source
        assert "You fix this bug" in r
        assert "Make the SMALL code edit" in r
        assert "non-empty patch" in r
        # security keeps the vulnerability/guard wording, absent in repair
        assert "vulnerability" in s and "crash" in s
        assert "vulnerability" not in r
        assert "Make the SMALL C edit" in s
        assert "Make the SMALL C edit" not in r


def test_build_steps_rep_dispatch_cfgdfg_before_dfg():
    """The builder dispatches on the EXACT rep: the cfgdfg cell gets the
    combined two-graph wording (never the dfg wording, even though
    'dfg_perrep' is a substring of every cfgdfg tool name), cfg gets no
    value-flow wording, dfg no control-flow wording."""
    for task in fp.TASKS:
        c = _steps(task, "cfgdfg")
        assert "TWO flow graphs" in c
        assert "CONTROL-FLOW GRAPH" in c and "VALUE-FLOW" in c
        assert "add_guard" in c and "add_def_use" in c
        d = _steps(task, "dfg")
        assert "TWO flow graphs" not in d
        assert "CONTROL-FLOW GRAPH" not in d
        assert "VALUE-FLOW" in d and "add_def_use" in d
        assert "add_guard" not in d
        g = _steps(task, "cfg")
        assert "TWO flow graphs" not in g
        assert "VALUE-FLOW" not in g and "add_def_use" not in g
        assert "CONTROL-FLOW GRAPH" in g and "add_guard" in g


def test_skill_md_frontmatter_and_sentinel():
    md = fp.build_skill_md("repair", "cfg", fp.tool_names("cfg"))
    lines = md.splitlines()
    assert lines[0] == "---"
    assert lines[1] == "name: flowfix"
    assert lines[2].startswith("description: ") and len(lines[2]) > 20
    assert "\n" not in lines[2]  # one-line description
    assert lines[3] == "---"
    # body's FIRST line is the plain-text sentinel LINE, then the protocol
    assert lines[4] == fp.SENTINEL_LINE
    assert lines[5] == "## Work IN THE FLOW (there is no code map)"
    assert ORDER_REPAIR in md
    # per-task descriptions stay task-appropriate
    md_s = fp.build_skill_md("security", "cfg", fp.tool_names("cfg"))
    assert "security" in md_s.splitlines()[2]


def test_sentinel_is_plain_rendered_line_not_html_comment():
    """The sentinel must survive markdown rendering: it is a PLAIN visible
    line, not an HTML comment (which rendering drops -> false-negative
    skill_expanded). It contains SENTINEL as a stable substring and appears
    as a real rendered body line in every task x rep SKILL.md."""
    # the sentinel itself is a plain, distinctive substring -- no comment
    # markers, no leading '#', no code fence, single line
    assert fp.SENTINEL == "FLOWFIX-PROTOCOL-V3"
    assert "<!--" not in fp.SENTINEL and "-->" not in fp.SENTINEL
    assert "<!--" not in fp.SENTINEL_LINE and "-->" not in fp.SENTINEL_LINE
    assert fp.SENTINEL in fp.SENTINEL_LINE  # substring guaranteed present
    assert not fp.SENTINEL_LINE.startswith("#")
    assert "`" not in fp.SENTINEL_LINE  # not fenced/inline-code
    assert "\n" not in fp.SENTINEL_LINE
    for task in fp.TASKS:
        for rep in fp.REPS:
            md = fp.build_skill_md(task, rep, fp.tool_names(rep))
            lines = md.splitlines()
            # a REAL rendered line equal to the sentinel line (index 4,
            # right after the 4-line frontmatter), and it carries SENTINEL
            assert lines[4] == fp.SENTINEL_LINE, (task, rep)
            assert fp.SENTINEL in lines[4], (task, rep)
            assert "<!--" not in md, (task, rep)


def test_skill_expanded_detects_sentinel_in_transcript(tmp_path):
    """skill_expanded is True iff a transcript carries the sentinel, and it
    fires on the rendered sentinel LINE (what a real expanded skill puts in
    the stream), not only on the bare token."""
    t = tmp_path / "stream.jsonl"
    assert fp.skill_expanded(t) is False  # absent file
    t.write_text('{"type":"user","text":"/flowfix"}\n', encoding="utf-8")
    assert fp.skill_expanded(t) is False  # invoked but never expanded
    # the rendered skill line (as it would surface in the stream) is detected
    t.write_text('{"type":"assistant","text":%r}\n' % fp.SENTINEL_LINE,
                 encoding="utf-8")
    assert fp.skill_expanded(t) is True
    # and the bare sentinel token anywhere in the transcript is detected too
    t.write_text('{"type":"assistant","text":"...%s..."}\n' % fp.SENTINEL,
                 encoding="utf-8")
    assert fp.skill_expanded(t) is True


def test_sentinel_absent_from_inline_prompt_and_steps(monkeypatch):
    """No false-positive in inline mode: neither the plain steps nor the
    inline (REPR_FLOW_SKILL=0) user prompt may contain the sentinel -- it
    lives ONLY in SKILL.md, so skill_expanded reflects skill delivery only.
    Checks BOTH the bare SENTINEL and the rendered SENTINEL_LINE, every
    task x rep."""
    body = "# Task\n\ndo the thing"
    monkeypatch.setenv("REPR_FLOW_SKILL", "0")  # inline mode
    for task in fp.TASKS:
        for rep in fp.REPS:
            tools = fp.tool_names(rep)
            s = fp.build_steps(task, rep, tools)
            assert fp.SENTINEL not in s, (task, rep)
            assert fp.SENTINEL_LINE not in s, (task, rep)
            q = fp.build_user_prompt(task, rep, tools, body)
            assert fp.SENTINEL not in q, (task, rep)
            assert fp.SENTINEL_LINE not in q, (task, rep)


def test_all_tasks_reps_build_skill_and_prompt(monkeypatch):
    """All 3 tasks x 3 reps still build a skill and both prompt modes."""
    body = "# Task\n\ndo the thing"
    for task in fp.TASKS:
        for rep in fp.REPS:
            tools = fp.tool_names(rep)
            md = fp.build_skill_md(task, rep, tools)
            assert md.startswith("---\nname: flowfix\n")
            assert fp.SENTINEL_LINE in md
            monkeypatch.setenv("REPR_FLOW_SKILL", "1")  # skill mode
            p = fp.build_user_prompt(task, rep, tools, body)
            assert p.startswith("/flowfix\n\n") and p.endswith(body)
            monkeypatch.setenv("REPR_FLOW_SKILL", "0")  # inline mode
            q = fp.build_user_prompt(task, rep, tools, body)
            assert q.startswith("## Work IN THE FLOW") and q.endswith(body)


def test_skill_mode_env(monkeypatch):
    monkeypatch.delenv("REPR_FLOW_SKILL", raising=False)
    assert fp.skill_mode() is True  # default ON for flow3
    monkeypatch.setenv("REPR_FLOW_SKILL", "0")
    assert fp.skill_mode() is False
    monkeypatch.setenv("REPR_FLOW_SKILL", "1")
    assert fp.skill_mode() is True


def test_build_user_prompt_skill_and_inline_modes(monkeypatch):
    body = "# Task\n\ndo the thing"
    tools = fp.tool_names("cfg")
    monkeypatch.delenv("REPR_FLOW_SKILL", raising=False)
    p = fp.build_user_prompt("repair", "cfg", tools, body)
    lines = p.splitlines()
    assert lines[0] == "/flowfix" and lines[1] == ""  # first line + blank
    assert p.endswith(body)
    assert fp.SENTINEL not in p
    assert ORDER_REPAIR not in p  # protocol arrives via the skill
    monkeypatch.setenv("REPR_FLOW_SKILL", "0")
    q = fp.build_user_prompt("repair", "cfg", tools, body)
    assert "/flowfix" not in q
    assert q.startswith("## Work IN THE FLOW")
    assert ORDER_REPAIR in q  # same protocol text, inlined above the body
    assert q.endswith(body)
    assert fp.SENTINEL not in q  # inline mode can't fake skill_expanded


def test_write_skill_and_sentinel_detection(tmp_path):
    md = fp.build_skill_md("security", "cfgdfg", fp.tool_names("cfgdfg"))
    path = fp.write_skill(tmp_path, md)
    assert path == tmp_path / "skills" / "flowfix" / "SKILL.md"
    assert path.read_text(encoding="utf-8") == md
    # sentinel detection over a transcript file
    t = tmp_path / "transcript.jsonl"
    assert fp.skill_expanded(t) is False  # absent file
    t.write_text('{"type":"user","text":"/flowfix"}\n', encoding="utf-8")
    assert fp.skill_expanded(t) is False  # invoked but never expanded
    t.write_text('{"type":"user","text":"x %s y"}\n' % fp.SENTINEL,
                 encoding="utf-8")
    assert fp.skill_expanded(t) is True


def test_secbench_reads_project_transcript_not_stdout_stream(tmp_path):
    """SecBench skill telemetry must read the HARVESTED project transcript,
    not the stdout stream-json. A live probe (haiku, --output-format
    stream-json) proved the CLI writes the expanded skill body ONLY into
    config_dir/projects/**, never into stdout: the sentinel was absent from
    stream.jsonl but present in the project transcript. This pins the exact
    harvest-then-detect path run_agent now uses so the check can't regress to
    grepping the (sentinel-less) stream and false-negative every flow cell."""
    from experiments.swebench_pro import agent as swe_agent

    # Reproduce the live-probe layout: a per-run home whose project transcript
    # carries the sentinel, and a stdout stream that does NOT.
    home = tmp_path / "claude-home"
    proj = home / "projects" / "-home-tempadmin-repo"
    proj.mkdir(parents=True)
    (proj / "session.jsonl").write_text(
        '{"type":"user","message":{"content":"...%s..."}}\n' % fp.SENTINEL,
        encoding="utf-8")

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    stream = run_dir / "stream.jsonl"  # what the OLD code grepped
    stream.write_text(
        '{"type":"result","result":"ACKNOWLEDGED."}\n', encoding="utf-8")

    # OLD target (stdout stream): false-negative even though the skill expanded
    assert fp.skill_expanded(stream, fp.SENTINEL) is False

    # NEW path: harvest the project transcript (as run_agent now does), then
    # detect on transcript.jsonl -> True.
    swe_agent._harvest_transcript(home, run_dir, 0.0)
    transcript = run_dir / "transcript.jsonl"
    assert transcript.is_file()
    assert fp.skill_expanded(transcript, fp.SENTINEL) is True
