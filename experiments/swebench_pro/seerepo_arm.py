"""SeeRepo comparator arm (baseline_seerepo): "LLM Agents Can See Code
Repositories" (arXiv 2606.14061, ASE 2026; code github.com/cslsolow/SeeRepo,
commit 30ba01c at tools/seerepo_src, unmodified).

Their method, as run for the paper's main results (config extra/SeeRepo.yaml,
scripts/run_swebench.sh): an AST repository graph (contains / imports /
invokes / inherits) is built per instance with build_graph(fuzzy_search=True,
global_import=False); the agent's task prompt carries their graph
instructions ("always query graph first"); a graph call returns a Graphviz
PNG which the agent sees as an image next to its normal text tools
(hybrid text + vision). They report, with mini-swe-agent and GPT-5-mini on
SWE-bench Verified, input tokens -25% and cost -26% at +0.4 pp resolve.

Per cell here: their build_graph() over the worktree at the base commit ->
run_dir/seerepo/repo_graph.pkl (build time logged, not billed -- their index
is built offline too); the graph tool served by servers/seerepo_server over
MCP (their rendering code; the PNG returned as image content); their
instructions appended to the baseline task prompt (tools/seerepo_steer.md:
their SeeRepo.yaml text with the bash command lines replaced by the native
tool call, node formats / edge sections / guidelines / workflow verbatim).
Baseline tools, prompt and budget otherwise unchanged. Graphviz `dot` comes
from the no-root extraction under ~/.local/graphviz (GV_* env below).
Deviations: mini-swe-agent -> Claude Code; GPT-5-mini -> Haiku 4.5.
"""
import json
import os
import pickle
import sys
import time
from pathlib import Path

from experiments.swebench_pro import agent

STUDY_ROOT = agent.STUDY_ROOT
SEEREPO_SRC = Path(os.environ.get("SEEREPO_SRC", str(STUDY_ROOT / "tools" / "seerepo_src" / "src")))
STEER_FILE = STUDY_ROOT / "tools" / "seerepo_steer.md"
GV_ROOT = Path(os.environ.get("SEEREPO_GRAPHVIZ", str(Path.home() / ".local" / "graphviz")))
STAGE_DIRNAME = "seerepo"


def graphviz_env() -> dict:
    """PATH / LD_LIBRARY_PATH / GVBINDIR for the extracted graphviz (no root)."""
    lib = GV_ROOT / "usr" / "lib" / "x86_64-linux-gnu"
    return {"PATH": "%s:%s" % (GV_ROOT / "usr" / "bin", os.environ.get("PATH", "")),
            "LD_LIBRARY_PATH": "%s:%s" % (lib, lib / "graphviz"),
            "GVBINDIR": str(lib / "graphviz")}


def build_graph_pkl(wt: Path, out_pkl: Path) -> dict:
    """Their build_graph() with their run defaults (fuzzy_search=True,
    global_import=False), pickled the way build_and_save_repo_graph_pkl does."""
    if str(SEEREPO_SRC) not in sys.path:
        sys.path.insert(0, str(SEEREPO_SRC))
    from minisweagent.run.extra.utils.build_graph import build_graph  # theirs
    t0 = time.time()
    G = build_graph(str(wt), fuzzy_search=True, global_import=False)
    out_pkl.parent.mkdir(parents=True, exist_ok=True)
    with open(out_pkl, "wb") as f:
        pickle.dump(G, f)
    from collections import Counter
    et = Counter(d.get("type") for _u, _v, d in G.edges(data=True))
    nt = Counter(d.get("type") for _n, d in G.nodes(data=True))
    info = {"build_seconds": round(time.time() - t0, 1), "nodes": G.number_of_nodes(),
            "edges": G.number_of_edges(), "node_types": dict(nt), "edge_types": dict(et),
            "pkl_bytes": out_pkl.stat().st_size}
    (out_pkl.parent / "graph_info.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    return info


def write_mcp_config(stage: Path, pkl: Path) -> Path:
    env = {"PYTHONPATH": str(STUDY_ROOT), "SEEREPO_SRC": str(SEEREPO_SRC),
           "SEEREPO_PKL": str(pkl), "SEEREPO_OUT": str(stage / "graph_views"),
           "SEEREPO_LOG": str(stage / "seerepo_tools.jsonl")}
    env.update(graphviz_env())
    cfg = {"mcpServers": {"seerepo": {"command": str(agent.VENV_PYTHON),
                                      "args": ["-m", "servers.seerepo_server.server"],
                                      "env": env}}}
    p = stage / "mcp.json"
    p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return p


def steer_block() -> str:
    return STEER_FILE.read_text(encoding="utf-8").strip()


def prepare(inst: dict, run_dir: Path, wt: Path) -> tuple[Path, str]:
    """Build the graph + per-cell MCP config; return (mcp_config, prompt block)."""
    stage = Path(run_dir).resolve() / STAGE_DIRNAME
    stage.mkdir(parents=True, exist_ok=True)
    pkl = stage / "repo_graph.pkl"
    info = build_graph_pkl(Path(wt), pkl)
    mcp = write_mcp_config(stage, pkl)
    (stage / "arm.txt").write_text(
        "baseline_seerepo: their build_graph (fuzzy_search=True, global_import=False) "
        "over the worktree; %(nodes)d nodes / %(edges)d edges in %(build_seconds)ss; "
        "graph tool over MCP returning their Graphviz PNG as an image; their SeeRepo.yaml "
        "instructions appended to the baseline prompt.\n" % info, encoding="utf-8")
    return mcp, steer_block()
