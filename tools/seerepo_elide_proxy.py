"""SeeRepo image-elision proxy (arm baseline_seerepo).

Their runner (run/extra/swebench.py, ProgressTrackingAgent) calls
_elide_old_image_messages() before EVERY model query: each earlier user
message that carried a graph PNG is replaced by the text

    <graph_png_elided>Graph visualization was shown in the previous turn.</graph_png_elided>

so the model sees each graph image exactly once -- on the turn right after
the call -- and never again. Claude Code keeps tool results verbatim in the
history, so without this the image would be re-sent (and re-billed) on every
later turn and the agent could keep looking at it. This transparent
ANTHROPIC_BASE_URL proxy (one per cell) replays their rule on each request:
in every user message except the LAST one, an image block inside a
tool_result is replaced by their placeholder text. Nothing else is touched;
auth passes through; SSE is streamed back. Every elision is logged.
"""
import json
import os
import time

from aiohttp import web
import httpx

UPSTREAM = "https://api.anthropic.com"
PORT = int(os.environ["SEEREPO_ELIDE_PORT"])
LOG_PATH = os.path.join(os.environ["RUN_DIR"], "seerepo_elisions.jsonl")
PLACEHOLDER = "<graph_png_elided>Graph visualization was shown in the previous turn.</graph_png_elided>"


def _log(rec):
    try:
        rec["ts"] = time.time()
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def transform(body):
    """Their _elide_old_image_messages, on Anthropic-format messages."""
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        return 0
    last_user = max((i for i, m in enumerate(msgs) if isinstance(m, dict) and m.get("role") == "user"), default=-1)
    n = 0
    for i, m in enumerate(msgs):
        if i == last_user or not isinstance(m, dict) or m.get("role") != "user":
            continue
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for blk in content:
            if not (isinstance(blk, dict) and blk.get("type") == "tool_result"):
                continue
            inner = blk.get("content")
            if not isinstance(inner, list):
                continue
            if any(isinstance(p, dict) and p.get("type") == "image" for p in inner):
                blk["content"] = [{"type": "text", "text": PLACEHOLDER}] + [
                    p for p in inner if not (isinstance(p, dict) and p.get("type") == "image")]
                n += 1
    return n


HOP = {"host", "content-length", "transfer-encoding", "connection", "keep-alive",
       "proxy-authenticate", "proxy-authorization", "te", "trailers", "upgrade", "accept-encoding"}


async def handle(request):
    if request.path == "/seerepo-health":
        return web.Response(text="ok")
    raw = await request.read()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
    if request.method == "POST" and request.path.endswith("/v1/messages"):
        try:
            body = json.loads(raw)
            n = transform(body)
            if n:
                before = len(raw)
                raw = json.dumps(body).encode("utf-8")
                _log({"elided_images": n, "bytes": [before, len(raw)]})
        except Exception as e:  # noqa: BLE001 -- pass through untouched
            _log({"error": repr(e)})
    client = request.app["client"]
    try:
        req = client.build_request(request.method, UPSTREAM + request.path_qs, headers=headers, content=raw)
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
    print("seerepo elide proxy ready on 127.0.0.1:%d" % PORT, flush=True)
    web.run_app(make_app(), host="127.0.0.1", port=PORT, print=None)
