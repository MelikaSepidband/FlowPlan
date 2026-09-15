"""AgentDiet comparator proxy (arm baseline_agentdiet).

Paper: "Reducing Cost of LLM Agents with Trajectory Reduction" (Xiao, Gao,
Peng, Xiong; FSE 2026, DOI 10.1145/3797084; arXiv 2509.23586). Artifact:
figshare 30073654, code/trae_agent/agents/traj_analyzer.py (the reflection
module) + agents/expert.py (MessageManager) -- unpacked at
tools/agentdiet/src/artifact, unmodified.

Their mechanism, as run for the paper's numbers (main_args.txt "ours"):
  after every agent step, look at the step idx = turns-1-ctx_after (ctx_after
  = 2, ctx_before = 1). Render steps idx-1..idx+2 in their <step><think>
  <call><result> form (idx-1 = -1 is the task prompt). If step idx is
  >= 500 tokens (tiktoken gpt-4o), ask a reflection LLM (their default:
  gpt-5-mini, temperature 0) with SYS_PROMPT below to compress that step;
  accept the rewrite only if it saves >= 400 tokens or is < 80% of the
  original. The step is then replaced in the history by ONE assistant
  message: "(System reminder: compressed for better efficiency) <compressed
  text>" (MessageManager.perform_erase_step); every later request carries
  the compressed form.

This proxy replays that rule for Claude Code as a transparent
ANTHROPIC_BASE_URL proxy (one process per cell). Claude Code keeps its own
untouched transcript and re-sends the full history on every call, so the
proxy re-derives the step list from each request and applies the stored
rewrites; state is keyed by the task prompt so subagent sessions are kept
apart. The reflection LLM is a bare headless Claude Code call (Haiku 4.5,
`--system-prompt` = their SYS_PROMPT verbatim, no tools, one turn), which is
the only way to make side calls under OAuth; its usage is logged per call
and folded into the cell's result.json by the harness.

Disclosed deviations: reflection model Haiku 4.5 at the arm's effort
instead of gpt-5-mini at temperature 0 (their design space includes Claude
3.5 Haiku as the reflection model); no assistant prefill / stop sequence
(the instruction to answer with the step body only is appended to the user
prompt and the reply is cut at </step>, as their parser does); on the
Anthropic API the compressed step cannot stand as a bare assistant message
between two tool exchanges, so its text is placed at the head of the NEXT
assistant turn (after that turn's thinking blocks) while the erased step's
tool_use / tool_result pair is dropped -- the model sees the same text in
the same position of the dialogue. Rewriting history invalidates the prompt
cache from the rewritten step on (inherent; report cache_creation and $).
"""
import asyncio
import hashlib
import json
import os
import time
from pathlib import Path

from aiohttp import web
import httpx
import tiktoken

UPSTREAM = "https://api.anthropic.com"
PORT = int(os.environ["AGENTDIET_PORT"])
RUN_DIR = Path(os.environ["RUN_DIR"])
CLAUDE_BIN = os.environ["AGENTDIET_CLAUDE_BIN"]
MODEL = os.environ.get("AGENTDIET_MODEL", "claude-haiku-4-5")
EFFORT = os.environ.get("AGENTDIET_EFFORT", "low")
HOME = Path(os.environ["AGENTDIET_HOME"])
N_CTX_BEFORE = int(os.environ.get("AGENTDIET_CTX_BEFORE", "1"))
N_CTX_AFTER = int(os.environ.get("AGENTDIET_CTX_AFTER", "2"))
THRESHOLD_TOKENS = int(os.environ.get("AGENTDIET_THRESHOLD", "500"))
REFLECT_TIMEOUT = int(os.environ.get("AGENTDIET_REFLECT_TIMEOUT", "300"))
EVENTS = RUN_DIR / "agentdiet_events.jsonl"
REFLECTIONS = RUN_DIR / "agentdiet_reflections.jsonl"
ENC = tiktoken.encoding_for_model("gpt-4o")

# traj_analyzer.SYS_PROMPT, verbatim
SYS_PROMPT = """
You will analyze and compress a given step in a trajectory of an AI agent solving a software bug.

In the trajectory, each step is marked in <step id="..."></step>.
The agent will think in <think>, call external tools as marked in <call tool="..."></call>. Its result is marked in <result></result> within the <step> tag.

Your job is to compress the text within the given id to avoid harming efficiency, typically shortening it to 20%-50% of the original length.
Meanwhile, keep the compressed text useful such that you are able to continue the trajectory as close as the original path.

- You should ONLY remove redundant texts, which are either irrelevant to future steps or duplicated by other texts in the trajectory.
- Replace the text to remove to "..." and a short takeaway, e.g. "... (same as the content below)".
- You should keep the original structure unchanged, e.g., XML tags, Python indentation and line numbers.
- Again, keep useful details in the original content unchanged, e.g., XML tags, Python indentation and line numbers.

Typical examples:
- If the step opens a huge file but only one part is necessary for future steps, replace other parts to "... (unrelated function XXX, YYY)".
- If the step runs a verbose test script and everything goes fine, replace the verbose part to "... (expected output)".
- If the step uses str_replace_editor to modify a file and the content can be inferred by the content after it, replace the tool call argument to "... (see results below)".

You should only process the text within the <step> tag with the given id. STOP OUTPUT IMMEDIATELY AFTER </step>.
""".strip()

# stands in for their assistant prefill ("Sure. Here is the compressed content
# of step {idx}: <step id="{idx}">") and stop="</step>", neither of which a
# headless Claude Code call can set
NO_PREFILL_SUFFIX = ('\n\nReply with ONLY the compressed content of step {idx}: '
                     'start directly with what follows `<step id="{idx}">` and '
                     'end with `</step>`. No preamble, no commentary.')

SESSIONS: dict = {}


def _log(path, rec):
    try:
        rec["ts"] = time.time()
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def count_token(s):
    return len(ENC.encode(s or "", disallowed_special=()))


# ---- message helpers ----------------------------------------------------------
def _blocks(msg):
    c = msg.get("content")
    if isinstance(c, str):
        return [{"type": "text", "text": c}]
    return [b for b in (c or []) if isinstance(b, dict)]


def _text(blocks, types=("text",)):
    return "\n".join(b.get("text", "") for b in blocks if b.get("type") in types)


def _result_text(blk):
    c = blk.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(p.get("text", "") for p in c
                         if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _has_tool_result(msg):
    return msg.get("role") == "user" and any(
        b.get("type") == "tool_result" for b in _blocks(msg))


def split_steps(msgs):
    """(task prompt text, steps). A step = an assistant message plus the
    following tool_result user message when there is one -- the unit their
    MessageManager.push_step stores."""
    init = ""
    i = 0
    if msgs and msgs[0].get("role") == "user" and not _has_tool_result(msgs[0]):
        init = _text(_blocks(msgs[0]))
        i = 1
    steps = []
    while i < len(msgs):
        m = msgs[i]
        if m.get("role") != "assistant":
            i += 1
            continue
        step = {"a": i, "u": None}
        if i + 1 < len(msgs) and _has_tool_result(msgs[i + 1]):
            step["u"] = i + 1
            i += 2
        else:
            i += 1
        steps.append(step)
    return init, steps


def render_step(msgs, steps, init, idx):
    """MessageManager.extract_step_into_traj, for Claude Code messages."""
    if idx == -1:
        return init
    st = steps[idx]
    out = ['<step id="%d">' % idx]
    a = _blocks(msgs[st["a"]])
    think = _text(a)
    if think.strip():
        out.append("<think>%s</think>" % think)
    for b in a:
        if b.get("type") == "tool_use":
            out.append('<call tool="%s">%s</call>' % (
                b.get("name", ""), json.dumps(b.get("input", {}))))
    if st["u"] is not None:
        for b in _blocks(msgs[st["u"]]):
            if b.get("type") == "tool_result":
                r = _result_text(b)
                if r.strip():
                    out.append("<result>%s</result>" % r)
            elif b.get("type") == "text" and b.get("text", "").strip():
                out.append("<user>%s</user>" % b["text"])
    out.append("</step>")
    return "\n".join(out)


# ---- the reflection call (their perform_analysis_step_ours) -------------------
async def reflect(user_prompt, idx):
    cmd = [CLAUDE_BIN, "-p", "--output-format", "json", "--strict-mcp-config",
           "--model", MODEL, "--permission-mode", "bypassPermissions",
           "--max-turns", "1", "--max-budget-usd", "1.0",
           "--tools", "", "--disallowedTools", "WebFetch,WebSearch",
           "--system-prompt", SYS_PROMPT]
    if EFFORT:
        cmd += ["--effort", EFFORT]
    env = dict(os.environ)
    env.pop("ANTHROPIC_BASE_URL", None)      # never back through this proxy
    env["CLAUDE_CONFIG_DIR"] = str(HOME)
    env["DISABLE_AUTOUPDATER"] = "1"
    env["ENABLE_TOOL_SEARCH"] = "false"
    # A reflection call is a single turn that is never continued, so the
    # prompt-cache write Claude Code adds to every request (~22K tokens per
    # call in the smoke, billed at the cache-write rate) can never be read
    # back -- pure overhead of the client, not of their method (their
    # reflection request caches only the shared system prompt). Off by
    # default; AGENTDIET_REFLECT_CACHE=1 keeps Claude Code's behaviour.
    if os.environ.get("AGENTDIET_REFLECT_CACHE", "0") != "1":
        env["DISABLE_PROMPT_CACHING"] = "1"
    # Their reflection model (gpt-5-mini) reasons before answering, so the
    # default keeps Haiku's thinking at the arm's effort; AGENTDIET_REFLECT_
    # THINKING=0 disables it (MAX_THINKING_TOKENS=0) for a cheaper variant.
    if os.environ.get("AGENTDIET_REFLECT_THINKING", "1") == "0":
        env["MAX_THINKING_TOKENS"] = "0"
    t0 = time.time()
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env=env, cwd=str(RUN_DIR))
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(user_prompt.encode("utf-8")), timeout=REFLECT_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        _log(REFLECTIONS, {"idx": idx, "error": "timeout", "seconds": REFLECT_TIMEOUT})
        return None
    try:
        r = json.loads(out.decode("utf-8", "replace") or "{}")
    except Exception:
        r = {}
    rec = {"idx": idx, "subtype": r.get("subtype"), "is_error": r.get("is_error"),
           "num_turns": r.get("num_turns"), "usage": r.get("usage"),
           "modelUsage": r.get("modelUsage"), "total_cost_usd": r.get("total_cost_usd"),
           "duration_ms": r.get("duration_ms"),
           "wall_ms": int((time.time() - t0) * 1000), "rc": proc.returncode,
           "prompt_chars": len(user_prompt)}
    if not r or r.get("is_error"):
        rec["stderr"] = err.decode("utf-8", "replace")[-500:]
    _log(REFLECTIONS, rec)
    if not r or r.get("is_error"):
        return None
    return r.get("result") or ""


async def maybe_analyze(sess, msgs, steps, init):
    """Their maybe_perform_analysis_step, once per new idx."""
    t = len(steps)
    if t < N_CTX_BEFORE + N_CTX_AFTER:
        return
    idx = t - 1 - N_CTX_AFTER
    if idx < 0 or idx in sess["analyzed"]:
        return
    sess["analyzed"].add(idx)
    step_text = render_step(msgs, steps, init, idx)
    tokens = count_token(step_text)
    sess["m"]["seen_tokens"] += tokens
    if tokens < THRESHOLD_TOKENS:
        _log(EVENTS, {"idx": idx, "action": "no_analysis_too_short", "tokens": tokens})
        return
    sess["m"]["analysis_count"] += 1
    ctx = [render_step(msgs, steps, init, i)
           for i in range(idx - N_CTX_BEFORE, idx + N_CTX_AFTER + 1)]
    user_prompt = ("%s\n\nNow, compress the step %d." % ("\n".join(ctx), idx)
                   + NO_PREFILL_SUFFIX.format(idx=idx))
    answer = await reflect(user_prompt, idx)
    if answer is None:
        _log(EVENTS, {"idx": idx, "action": "reflection_error", "tokens": tokens})
        return
    content, splitter, _rest = answer.partition("</step>")
    if "<step" in content[:200]:
        content = content.partition("<step")[2]
        if ">" in content[:20]:
            content = content.partition(">")[2]
    old_tokens = count_token(ctx[N_CTX_BEFORE])
    new_tokens = count_token(content)
    if old_tokens - new_tokens >= 400 or new_tokens < .8 * old_tokens:
        sess["erased"][idx] = content
        sess["m"]["erase_tot_count"] += 1
        sess["m"]["erase_in_tokens"] += old_tokens
        sess["m"]["erase_out_tokens"] += new_tokens
        _log(EVENTS, {"idx": idx, "action": "erased", "old_tokens": old_tokens,
                      "new_tokens": new_tokens, "closed": bool(splitter)})
        # keep both texts on disk so every rewrite can be audited afterwards
        _log(RUN_DIR / "agentdiet_erasures.jsonl",
             {"idx": idx, "original": ctx[N_CTX_BEFORE], "compressed": content})
    else:
        _log(EVENTS, {"idx": idx, "action": "no_erase", "old_tokens": old_tokens,
                      "new_tokens": new_tokens})


def apply_erasures(msgs, steps, erased):
    """Their perform_erase_step, in Anthropic message form: drop the erased
    step's assistant + tool_result messages and put the compressed text at
    the head of the next surviving assistant turn (after its thinking)."""
    if not erased:
        return msgs, 0
    drop = set()
    for idx in sorted(erased):
        st = steps[idx]
        drop.add(st["a"])
        if st["u"] is not None:
            drop.add(st["u"])
    out = []
    pending = []
    applied = 0
    for i, m in enumerate(msgs):
        if i in drop:
            # the erased step's text becomes pending for the next assistant turn
            for idx in sorted(erased):
                if steps[idx]["a"] == i:
                    pending.append("(System reminder: compressed for better efficiency) "
                                   + erased[idx].strip())
            continue
        if pending and m.get("role") == "assistant":
            blocks = _blocks(m)
            k = 0
            while k < len(blocks) and blocks[k].get("type") in ("thinking", "redacted_thinking"):
                k += 1
            new_blocks = blocks[:k] + [{"type": "text", "text": p} for p in pending] + blocks[k:]
            m = dict(m)
            m["content"] = new_blocks
            applied += len(pending)
            pending = []
        out.append(m)
    return out, applied


def _session_key(init):
    return hashlib.sha1(init.encode("utf-8", "replace")).hexdigest()[:12]


async def transform(body):
    msgs = body.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return 0
    init, steps = split_steps(msgs)
    key = _session_key(init)
    sess = SESSIONS.setdefault(key, {
        "analyzed": set(), "erased": {}, "lock": asyncio.Lock(),
        "m": {"seen_tokens": 0, "analysis_count": 0, "erase_tot_count": 0,
              "erase_in_tokens": 0, "erase_out_tokens": 0}})
    async with sess["lock"]:
        await maybe_analyze(sess, msgs, steps, init)
        new_msgs, applied = apply_erasures(msgs, steps, sess["erased"])
    body["messages"] = new_msgs
    return applied


def write_summary():
    summ = {k: {"metrics": v["m"], "n_erased": len(v["erased"]),
                "analyzed": sorted(v["analyzed"])}
            for k, v in SESSIONS.items()}
    try:
        (RUN_DIR / "agentdiet_summary.json").write_text(
            json.dumps(summ, indent=2), encoding="utf-8")
    except Exception:
        pass


HOP = {"host", "content-length", "transfer-encoding", "connection",
       "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
       "trailers", "upgrade", "accept-encoding"}


async def handle(request):
    if request.path == "/agentdiet-health":
        return web.Response(text="ok")
    if request.path == "/agentdiet-summary":
        write_summary()
        return web.Response(text="ok")
    raw = await request.read()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
    applied = 0
    if request.method == "POST" and request.path.endswith("/v1/messages"):
        try:
            body = json.loads(raw)
            before = len(raw)
            applied = await transform(body)
            raw = json.dumps(body).encode("utf-8")
            if applied:
                _log(EVENTS, {"action": "request_rewritten", "applied": applied,
                              "bytes": [before, len(raw)]})
        except Exception as e:  # noqa: BLE001 -- pass the request through untouched
            _log(EVENTS, {"action": "transform_error", "error": repr(e)})
    client = request.app["client"]
    try:
        req = client.build_request(request.method, UPSTREAM + request.path_qs,
                                   headers=headers, content=raw)
        resp = await client.send(req, stream=True)
    except Exception as e:  # noqa: BLE001
        return web.Response(status=502, text="proxy upstream error: %r" % e)
    out = web.StreamResponse(status=resp.status_code)
    for k, v in resp.headers.items():
        if k.lower() not in HOP and k.lower() != "content-encoding":
            out.headers[k] = v
    await out.prepare(request)
    try:
        async for chunk in resp.aiter_bytes():
            await out.write(chunk)
    finally:
        await resp.aclose()
    await out.write_eof()
    return out


async def make_app():
    app = web.Application(client_max_size=256 * 1024 * 1024)
    app["client"] = httpx.AsyncClient(timeout=httpx.Timeout(600.0), http2=False)
    app.router.add_route("*", "/{tail:.*}", handle)
    return app


if __name__ == "__main__":
    print("agentdiet proxy ready on 127.0.0.1:%d (ctx_before=%d ctx_after=%d threshold=%d model=%s)"
          % (PORT, N_CTX_BEFORE, N_CTX_AFTER, THRESHOLD_TOKENS, MODEL), flush=True)
    try:
        web.run_app(make_app(), host="127.0.0.1", port=PORT, print=None)
    finally:
        write_summary()
