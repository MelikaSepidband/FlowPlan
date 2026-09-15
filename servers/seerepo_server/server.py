"""SeeRepo comparator (arm baseline_seerepo; "LLM Agents Can See Code
Repositories", arXiv 2606.14061, ASE 2026): their repository-graph
visualization tool, served to Claude Code over MCP as an IMAGE.

Their method: a static AST repository graph (contains / imports / invokes /
inherits edges; scripts and run/extra/utils/build_graph.py) is built offline
per instance; at run time the agent calls
`python -m minisweagent.run.extra.utils.graph_visualization --pkl ... --node
<NODE> --edge-type <TYPE> --up-depth N --down-depth N`, which renders the
bidirectional neighbourhood of the node with Graphviz, and the agent's
observation for that step is the PNG alone (run/extra/swebench.py
get_observation: when a graph PNG is attached, the message content is the
image, not the text). Hybrid text+vision is the paper's reported condition.

This server runs THEIR code unmodified (tools/seerepo_src, commit 30ba01c):
the loaded pickle is the one their build_graph() produced for the worktree,
and every call goes through their _extract_edge_digraph / _ensure_root /
_render_bidir_edges exactly as their CLI's main() does, then applies their
_resize_png_to_fit cap (35 M pixels; copied here because their swebench.py
pulls in the whole mini-swe-agent stack). The PNG is returned as MCP image
content, which Claude Code passes to the model as an image block -- the same
modality their agent sees. Node-not-found is returned as their KeyError text.

Deviations (disclosed): native tool call instead of a bash command that the
runner intercepts; the Anthropic API's own image limits (8000 px per side,
5 MB) are applied after their 35 M-pixel cap, since a request would otherwise
be rejected. Every call is logged (SEEREPO_LOG) with their record fields.
"""
import base64
import io
import json
import os
import pickle
import sys
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP, Image

SRC = Path(os.environ.get("SEEREPO_SRC", "/home/tempadmin/Code_rep_mcp_tools/tools/seerepo_src/src"))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
# Their package prints a banner + "Loading global config" via rich.Console()
# at import time, to stdout -- which is the MCP protocol channel of a stdio
# server. Point stdout at stderr for the duration of the import only.
_real_stdout = sys.stdout
sys.stdout = sys.stderr
try:
    from minisweagent.run.extra.utils import graph_visualization as gv  # noqa: E402  (theirs, unmodified)
finally:
    sys.stdout = _real_stdout

PKL = Path(os.environ["SEEREPO_PKL"])
OUT = Path(os.environ.get("SEEREPO_OUT", str(PKL.parent / "graph_views")))
LOG = os.environ.get("SEEREPO_LOG")
OUT.mkdir(parents=True, exist_ok=True)
with open(PKL, "rb") as _f:
    G = pickle.load(_f)

_MAX_IMAGE_PIXELS = 35_000_000          # theirs (swebench.py)
_API_MAX_SIDE, _API_MAX_BYTES = 8000, 5 * 1024 * 1024   # Anthropic image limits
VALID_EDGES = ("contains", "imports", "inherits", "invokes")

mcp = FastMCP("seerepo")


def _resize_png_to_fit(png_bytes: bytes) -> bytes:
    """Their swebench.py _resize_png_to_fit, verbatim, plus the API's per-side
    and byte caps (a further downscale only when their cap still exceeds
    what the API accepts)."""
    try:
        from PIL import Image as PILImage
    except ImportError:
        return png_bytes
    try:
        img = PILImage.open(io.BytesIO(png_bytes)).convert("RGB")
        w, h = img.size
        scale = 1.0
        if w * h > _MAX_IMAGE_PIXELS:
            scale = (_MAX_IMAGE_PIXELS / (w * h)) ** 0.5
        side = max(w, h) * scale
        if side > _API_MAX_SIDE:
            scale *= _API_MAX_SIDE / side
        if scale < 1.0:
            resample = getattr(PILImage.Resampling, "LANCZOS", getattr(PILImage, "LANCZOS", PILImage.BICUBIC))
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), resample)
            buf = io.BytesIO(); img.save(buf, format="PNG", optimize=True); png_bytes = buf.getvalue()
        while len(png_bytes) > _API_MAX_BYTES:
            w, h = img.size
            img = img.resize((max(1, int(w * 0.8)), max(1, int(h * 0.8))))
            buf = io.BytesIO(); img.save(buf, format="PNG", optimize=True); png_bytes = buf.getvalue()
        return png_bytes
    except Exception:
        return png_bytes


def _log(rec):
    if not LOG:
        return
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


@mcp.tool()
def repo_graph(node: str, edge_type: str = "contains", up_depth: int = 1, down_depth: int = 1):
    """Repository graph visualization tool. Renders, as an image, the
    bidirectional neighbourhood of `node` in the repository graph for one edge
    type: contains (directory layout), imports (file-level dependencies),
    inherits (class hierarchy), invokes (caller -> callee). Node formats:
    directory `path/to/dir` (contains only), file `path/to/file.py`, class
    `path/to/file.py:ClassName`, function/method `path/to/file.py:ClassName.methodname`.
    Keep up_depth / down_depth low (max 2)."""
    t0 = time.time()
    if edge_type not in VALID_EDGES:
        msg = "Error: --edge-type must be one of %s, got %r" % ("/".join(VALID_EDGES), edge_type)
        _log({"tool": "mswea_graph", "ts": t0, "node": node, "edge_type": edge_type, "error": msg})
        return msg
    # their main(), step by step
    H = gv._extract_edge_digraph(G, edge_type=edge_type)
    if node in G and node not in H:
        H.add_node(node, **G.nodes[node])
    if node == ".":
        node = "/"
    center = node if node else "/"
    if center == "/":
        gv._ensure_root(H, "/")
    safe = gv._sanitize_filename(center)
    out_prefix = OUT / ("%s__%s__u%d__d%d" % (safe, edge_type, up_depth, down_depth))
    try:
        out_path = gv._render_bidir_edges(H, center=center, out_prefix=out_prefix,
                                          up_depth=up_depth, down_depth=down_depth,
                                          graph_name="%s_bidir" % edge_type, edge_type=edge_type)
    except KeyError as e:   # their CLI dies with this message; the agent sees it
        msg = "KeyError: %s" % (e,)
        _log({"tool": "mswea_graph", "ts": t0, "node": center, "edge_type": edge_type,
              "up_depth": up_depth, "down_depth": down_depth, "error": msg,
              "runtime_sec": round(time.time() - t0, 4)})
        return msg
    png = _resize_png_to_fit(Path(out_path).read_bytes())
    try:
        from PIL import Image as PILImage
        w, h = PILImage.open(io.BytesIO(png)).size
    except Exception:
        w = h = None
    _log({"tool": "mswea_graph", "ts": t0, "pkl": str(PKL), "node": center, "edge_type": edge_type,
          "up_depth": up_depth, "down_depth": down_depth, "out_png": str(out_path),
          "png_bytes": len(png), "png_px": [w, h],
          "runtime_sec": round(time.time() - t0, 4),
          "contains_nodes_total": int(H.number_of_nodes()),
          "contains_edges_total": int(H.number_of_edges())})
    return Image(data=png, format="png")


if __name__ == "__main__":
    mcp.run()
