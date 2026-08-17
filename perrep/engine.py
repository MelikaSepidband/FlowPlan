"""The shared explore + node engine -- codegraph's algorithm over ANY rep's map.

One engine, parameterized by the rep's edge kind(s). Ported faithfully from the
codegraph port in servers/common/cgidx.py:
  explore: FTS seed -> graph-walk expansion -> RWR centrality -> verbatim source
  node:    symbol/anchor -> verbatim source + neighbor trail in this rep's map

A rep is described by an EngineConfig (its db path, edge kinds for the walk, the
edge kind to surface, whether it's FTS-only like docs). The SAME math runs for
every rep; only the edges differ -- which is the entire point.
"""

import math
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_NAME_TOK = re.compile(r"^[A-Za-z_$][\w$]*(?:(?:::|\.)[\w$]+)*$")
_FILE_EXT = re.compile(r"\.(?:c|h|cpp|cc|cxx|hpp|py|ts|tsx|js|go|rs|java)$", re.I)
_FTS_STRIP = re.compile(r"""['"*():^]""")
_BOOL = re.compile(r"^(AND|OR|NOT|NEAR)$", re.I)


@dataclass
class EngineConfig:
    """How one representation's explore/node behave over its map."""
    rep: str
    db_path: Path
    walk_edges: tuple = ("calls",)     # edge kinds the explore graph-walk follows
    rank_edges: Optional[tuple] = None  # edge kinds RWR ranks on (default: walk_edges)
    rel_kind: Optional[str] = "calls"  # edge kind surfaced in the relationships list
    neighbor_in: str = "called by"     # node(): label for incoming edges
    neighbor_out: str = "calls"        # node(): label for outgoing edges
    fts_only: bool = False             # docs: rank by FTS, no graph walk
    file_centric: bool = False         # deps: the graph connects FILES, not defs
    seed_rank: bool = False            # hybrid: rank by FTS seed-density then RWR
    repo_root: Optional[Path] = None   # for reading verbatim source off disk
    concise: bool = False              # explore returns a MAP (signatures +
    # file:line), NOT full source bodies. A small persistent map the agent
    # navigates from + fetches specific source via node/get_source only when
    # editing -- avoids dumping 5 whole files (~5K tok) that ride the context
    # every turn. Set via REPR_CONCISE=1.


#: Per-rep engine settings -- the ONLY thing that differs between reps. Each maps
#: the rep to its edge kinds + the human labels node() uses for in/out neighbors.
REP_SETTINGS = {
    "callgraph": dict(walk_edges=("calls",), rel_kind="calls",
                      neighbor_out="calls", neighbor_in="called_by"),
    "ast":       dict(walk_edges=("contains",), rel_kind="contains",
                      neighbor_out="contains", neighbor_in="contained_by"),
    # deps walks BOTH imports (the dependency graph -- its ranking signal) and
    # contains (file -> its defs), so explore can pull a ranked file's actual
    # function source to show. rel_kind stays 'imports': the relationships list
    # surfaces the file dependency graph, which is what makes deps distinct.
    "deps":      dict(walk_edges=("imports", "contains"), rank_edges=("imports",),
                      rel_kind="imports", file_centric=True,
                      neighbor_out="imports", neighbor_in="imported_by"),
    "docs":      dict(walk_edges=(), rel_kind=None, fts_only=True,
                      neighbor_out="related", neighbor_in="related_from"),
    "hybrid":    dict(walk_edges=("calls", "imports", "contains"),
                      rank_edges=("calls", "imports"), rel_kind="calls",
                      seed_rank=True,
                      neighbor_out="calls_imports", neighbor_in="called_imported_by"),
    # cfg walks containment like ast, but its nodes are FUNCTIONS ONLY and
    # each carries a control-flow doc (branch conditions, loop headers), so
    # FTS ranks by flow content; block detail lives in the native tools.
    "cfg":       dict(walk_edges=("contains",), rel_kind="contains",
                      neighbor_out="contains", neighbor_in="contained_by"),
    # dfg ranks by value flow: flows_to edges connect the function defining a
    # name to the functions using it (module scope attaches to the file
    # node); contains is walked so a ranked file still surfaces its defs.
    "dfg":       dict(walk_edges=("flows_to", "contains"),
                      rank_edges=("flows_to",), rel_kind="flows_to",
                      neighbor_out="flows_to", neighbor_in="flows_from"),
}


def config_for(rep: str, db_path, repo_root=None) -> "EngineConfig":
    """Build the EngineConfig for ``rep`` from REP_SETTINGS.

    REPR_CONCISE=1 -> explore returns a signature/location MAP instead of full
    source bodies (small persistent context vs a 5-file source dump)."""
    import os as _os
    s = dict(REP_SETTINGS.get(rep, REP_SETTINGS["callgraph"]))
    concise = _os.environ.get("REPR_CONCISE", "").strip().lower() in (
        "1", "true", "yes")
    return EngineConfig(rep=rep, db_path=Path(db_path),
                        repo_root=Path(repo_root) if repo_root else None,
                        concise=concise, **s)


class Engine:
    def __init__(self, cfg: EngineConfig):
        self.cfg = cfg
        self._conn = None

    # -- connection --------------------------------------------------------
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            p = self.cfg.db_path
            self._conn = sqlite3.connect("file:%s?mode=ro" % p, uri=True)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def available(self) -> bool:
        return Path(self.cfg.db_path).is_file()

    # -- low-level map queries --------------------------------------------
    def _fts_query(self, text: str) -> str:
        cleaned = _FTS_STRIP.sub("", text.replace("::", " "))
        terms = [t for t in cleaned.split() if t and not _BOOL.match(t)]
        return " OR ".join('"%s"*' % t for t in terms)

    def search(self, query: str, limit: int = 20) -> list:
        fq = self._fts_query(query)
        if not fq:
            return []
        sql = ("SELECT n.*, bm25(nodes_fts, 20, 5, 2, 1) AS score "
               "FROM nodes_fts JOIN nodes n ON nodes_fts.rowid = n.id "
               "WHERE nodes_fts MATCH ? ORDER BY score LIMIT ?")
        try:
            rows = self.conn().execute(sql, [fq, max(limit, 1)]).fetchall()
        except sqlite3.Error:
            return []
        return [self._row(r) for r in rows]

    def _row(self, r) -> dict:
        return {"id": r["id"], "kind": r["kind"], "name": r["name"],
                "qualname": r["qualname"], "file": r["file"],
                "line": r["start_line"], "end_line": r["end_line"],
                "signature": r["signature"], "doc": r["doc"]}

    def get_node(self, nid) -> Optional[dict]:
        r = self.conn().execute("SELECT * FROM nodes WHERE id=?", (nid,)).fetchone()
        return self._row(r) if r else None

    def _resolve_name(self, name: str, limit: int = 50) -> list:
        rows = self.conn().execute(
            "SELECT id FROM nodes WHERE name=? COLLATE NOCASE "
            "OR qualname=? COLLATE NOCASE LIMIT ?", (name, name, limit)).fetchall()
        return [r["id"] for r in rows]

    def _resolve_symbol(self, name: str, file_hint: str = "", limit: int = 6) -> list:
        """Resolve a SYMBOL name to node ids, tolerant of the forms the agent
        actually passes ``node`` (which were 23% failing): bare name, dotted
        ``Class.method``, a fully-qualified ``qualname``, and an optional file
        scope. Tries, most- to least-specific:
          1. exact name / qualname (the old behavior);
          2. qualname ending in the dotted target (``...TabWidget.set_tab_pinned``);
          3. the LAST dotted component as a bare name (``set_tab_pinned``),
             preferring a node whose qualname contains the parent (``TabWidget``);
          4. any of the above, scoped to ``file_hint`` when given.
        Returns ids ranked so the file-scoped / parent-matching hits come first."""
        name = (name or "").strip()
        if not name:
            return []
        fh = (file_hint or "").replace("\\", "/").strip()
        last = name.rsplit(".", 1)[-1]
        parent = name.rsplit(".", 1)[0] if "." in name else ""
        c = self.conn()
        # candidate rows: exact name/qualname, qualname-suffix, or last-component name
        rows = c.execute(
            "SELECT id, name, qualname, file FROM nodes WHERE "
            "name=? COLLATE NOCASE OR qualname=? COLLATE NOCASE "
            "OR qualname LIKE ? COLLATE NOCASE OR name=? COLLATE NOCASE "
            "LIMIT 200",
            (name, name, "%." + name, last)).fetchall()
        if not rows:
            return []

        def score(r):
            s = 0
            q = (r["qualname"] or "")
            nm = (r["name"] or "")
            f = (r["file"] or "").replace("\\", "/")
            if nm.lower() == name.lower() or q.lower() == name.lower():
                s += 8                      # exact
            if q.lower().endswith("." + name.lower()):
                s += 6                      # qualname suffix (Class.method)
            if parent and parent.lower() in q.lower():
                s += 3                      # right enclosing class
            if nm.lower() == last.lower():
                s += 2                      # last-component name match
            if fh and (f == fh or f.endswith("/" + fh) or fh.endswith("/" + f)):
                s += 5                      # in the file the agent named
            return s

        ranked = sorted(rows, key=score, reverse=True)
        return [r["id"] for r in ranked[:limit]]

    def _edges(self, node_ids, kinds, incoming):
        if not node_ids:
            return []
        col = "dst" if incoming else "src"
        ph = ",".join("?" * len(node_ids))
        kp = ",".join("?" * len(kinds))
        sql = ("SELECT src, dst, kind, line FROM edges "
               "WHERE %s IN (%s) AND kind IN (%s)" % (col, ph, kp))
        try:
            return self.conn().execute(sql, [*node_ids, *kinds]).fetchall()
        except sqlite3.Error:
            return []

    def _file_node_id(self, path: str):
        """deps: the file node for ``path`` (kind='file', qualname==path)."""
        if not path:
            return None
        r = self.conn().execute(
            "SELECT id FROM nodes WHERE kind='file' AND (qualname=? OR file=?) "
            "LIMIT 1", (path, path)).fetchone()
        return r["id"] if r else None

    def _contains_children(self, file_ids, limit=400):
        """deps: the def nodes a file CONTAINS (for showing real source)."""
        if not file_ids:
            return []
        ph = ",".join("?" * len(file_ids))
        sql = ("SELECT dst FROM edges WHERE kind='contains' AND src IN (%s) "
               "LIMIT ?" % ph)
        rows = self.conn().execute(sql, [*file_ids, limit]).fetchall()
        return [r["dst"] for r in rows]

    def _import_indegree(self, node_ids):
        """deps: how many files import each node (its global hub-ness). A file
        imported by half the repo (linux/fs.h, kernel.h) is a hub -- high
        in-degree means LOW localizing value, so explore damps its rank by it
        (IDF over the import graph)."""
        if not node_ids:
            return {}
        ph = ",".join("?" * len(node_ids))
        sql = ("SELECT dst, COUNT(*) c FROM edges WHERE kind='imports' "
               "AND dst IN (%s) GROUP BY dst" % ph)
        try:
            rows = self.conn().execute(sql, list(node_ids)).fetchall()
        except sqlite3.Error:
            return {}
        return {r["dst"]: r["c"] for r in rows}

    def _dep_neighborhood(self, file_node_id):
        """deps: (imports, importers) for a FILE node -- what it depends on and
        what depends on it. The actual dependency-graph map around a file."""
        imports, importers = [], []
        try:
            for r in self.conn().execute(
                    "SELECT dst FROM edges WHERE kind='imports' AND src=?",
                    (file_node_id,)).fetchall():
                n = self.get_node(r["dst"])
                if n and n.get("file"):
                    imports.append(n["file"])
            for r in self.conn().execute(
                    "SELECT src FROM edges WHERE kind='imports' AND dst=?",
                    (file_node_id,)).fetchall():
                n = self.get_node(r["src"])
                if n and n.get("file"):
                    importers.append(n["file"])
        except sqlite3.Error:
            pass
        # de-dup, keep order
        return (list(dict.fromkeys(imports)), list(dict.fromkeys(importers)))

    def _node_at_anchor(self, file_part, line):
        rows = self.conn().execute(
            "SELECT * FROM nodes WHERE (file=? OR file LIKE ?) "
            "AND start_line<=? AND end_line>=? ORDER BY start_line DESC LIMIT 1",
            (file_part, "%" + file_part, line, line)).fetchall()
        return self._row(rows[0]) if rows else None

    def _nodes_in_file(self, file_part: str, limit: int = 20) -> list:
        """The def nodes contained in a file, by line order. Used when the agent
        passes a bare FILE PATH to node() (no :symbol / :line) -- instead of
        erroring, return the file's symbols + source. file_part may be a full or
        partial repo-relative path (suffix-matched)."""
        fp = (file_part or "").replace("\\", "/").strip().strip("/")
        if not fp:
            return []
        rows = self.conn().execute(
            "SELECT * FROM nodes WHERE (file=? OR file LIKE ?) "
            "AND start_line IS NOT NULL AND kind != 'file' "
            "ORDER BY start_line LIMIT ?",
            (fp, "%" + fp, limit)).fetchall()
        return [self._row(r) for r in rows]

    # -- verbatim source off disk -----------------------------------------
    def _source_of(self, node, max_lines=80) -> Optional[str]:
        f, s = node.get("file"), node.get("line")
        e = node.get("end_line") or (s + max_lines if s else None)
        if not f or not s or not self.cfg.repo_root:
            return None
        path = Path(self.cfg.repo_root) / f
        if not path.is_file():
            return None
        try:
            raw = path.read_bytes().split(b"\n")
        except OSError:
            return None
        end = min(e or s, s + max_lines, len(raw))
        return b"\n".join(raw[s - 1:end]).decode("utf-8", "replace")

    # -- RWR centrality (codegraph computeGraphRelevance) ------------------
    def _rwr(self, node_ids, edges, seed_ids):
        # rank on rank_edges if the rep declares them (deps ranks on 'imports'
        # only, even though the walk also follows 'contains' to fetch source);
        # otherwise rank on the same edges the walk follows.
        keep = set(self.cfg.rank_edges or self.cfg.walk_edges)
        n = len(node_ids)
        if n == 0:
            return {}
        idx = {nid: i for i, nid in enumerate(node_ids)}
        adj = [[] for _ in range(n)]
        for src, dst, kind in edges:
            if kind not in keep:
                continue
            i, j = idx.get(src), idx.get(dst)
            if i is None or j is None or i == j:
                continue
            adj[i].append(j)
            adj[j].append(i)
        r = [0.0] * n
        rsum = 0.0
        for sid in seed_ids:
            i = idx.get(sid)
            if i is not None:
                r[i] = 1.0
                rsum += 1.0
        if rsum == 0:
            r = [1.0] * n
            rsum = float(n)
        r = [x / rsum for x in r]
        alpha, s = 0.25, list(r)
        for _ in range(25):
            nxt = [0.0] * n
            for i in range(n):
                si = s[i]
                if si == 0:
                    continue
                d = len(adj[i])
                if d == 0:
                    nxt[i] += si
                    continue
                share = si / d
                for j in adj[i]:
                    nxt[j] += share
            s = [(1 - alpha) * nxt[i] + alpha * r[i] for i in range(n)]
        return {node_ids[i]: s[i] for i in range(n)}

    # -- EXPLORE (codegraph handleExplore, per rep) -----------------------
    def explore(self, query: str, max_files: int = 12, max_chars: int = 20000) -> dict:
        cfg = self.cfg
        # NOTE: explore's source budget (max_chars=20000, 6 defs/file, 60 lines/
        # symbol) was A/B-tested raised 2.5x (REPR_EXPLORE_* env knobs, 2026-06-22)
        # to see if richer returns cut the agent's reads. It did NOT -- Read stayed
        # ~7.2/cell, tokens got WORSE (-31% -> -22%), an empty patch reappeared.
        # The agent re-reads for surrounding context (imports/full class), not
        # because the symbol bodies are truncated, so dumping more source just cost
        # tokens. Reverted to the original caps; do NOT re-raise them.
        # docs (no graph) + hybrid (seed-density-ranked) need MORE FTS seeds so
        # the per-file density signal is meaningful: 8 seeds spread across files
        # can't separate on-topic from incidental. callgraph keeps 8 (its RWR
        # already discriminates).
        seeds = self.search(query, limit=40 if (cfg.fts_only or cfg.seed_rank) else 8)
        seed_ids = {s["id"] for s in seeds}
        nodes = {s["id"]: s for s in seeds}

        toks = []
        for t in re.split(r"[\s,()\[\]]+", query):
            t = _FILE_EXT.sub("", t).strip()
            if len(t) >= 3 and _NAME_TOK.match(t):
                toks.append(t)
        toks = list(dict.fromkeys(toks))[:16]
        named_ids = set()
        for t in toks:
            for nid in self._resolve_name(t):
                nd = self.get_node(nid)
                if nd:
                    nodes[nid] = nd
                    named_ids.add(nid)

        seed_hits = {}   # file-centric: file node id -> # query defs it held
        if cfg.file_centric:
            # deps: the import graph connects FILES, not defs. Lift every
            # FTS/name seed up to its containing FILE node, so the unit of
            # ranking is the file (over the import graph), not random
            # *alloc*/*block* def matches. The FTS-matched files are a STRONG
            # query prior -- they hold the symbols the bug report named -- so we
            # mark them BOTH seed (RWR restart) AND named (gate-exempt,
            # sort-priority). The import graph then only adds local neighbours
            # and breaks ties; it never gates out a file the query pointed at.
            # The def nodes stay reachable via 'contains' for source display.
            lifted_seeds, lifted_named, lifted_nodes = set(), set(), {}
            for nid, nd in list(nodes.items()):
                fp = nd.get("file")
                fnid = self._file_node_id(fp) if fp else None
                if fnid is None:
                    continue
                fnode = self.get_node(fnid)
                if not fnode:
                    continue
                lifted_nodes[fnid] = fnode
                lifted_seeds.add(fnid)
                lifted_named.add(fnid)   # every query-matched file is protected
                seed_hits[fnid] = seed_hits.get(fnid, 0) + 1
            if lifted_nodes:
                seed_ids, named_ids, nodes = lifted_seeds, lifted_named, lifted_nodes

        roots = list(seed_ids | named_ids)
        if cfg.fts_only:
            edges = []
        else:
            ein = self._edges(roots, cfg.walk_edges, incoming=True)
            eout = self._edges(roots, cfg.walk_edges, incoming=False)
            edges = list(ein) + list(eout)
            expand = set()
            for src, dst, kind, line in edges:
                for other in (src, dst):
                    if other not in nodes:
                        expand.add(other)
            for nid in list(expand)[:200]:
                nd = self.get_node(nid)
                if nd and nd.get("file"):
                    nodes[nid] = nd

        all_ids = list(nodes.keys())
        etuples = [(e[0], e[1], e[2]) for e in edges]
        graph_score = ({} if cfg.fts_only
                       else self._rwr(all_ids, etuples, seed_ids | named_ids))

        file_nodes, file_graph, file_terms, named_files = {}, {}, {}, set()
        file_seed_density = {}   # file -> # of distinct FTS seed-symbols it holds
        qlow = [t.lower() for t in toks]
        for nid, nd in nodes.items():
            fp = nd.get("file")
            if not fp:
                continue
            file_nodes.setdefault(fp, []).append(nd)
            file_graph[fp] = max(file_graph.get(fp, 0.0), graph_score.get(nid, 0.0))
            hits = sum(1 for q in set(qlow)
                       if any(q in (x.get("name") or "").lower()
                              for x in file_nodes[fp]))
            file_terms[fp] = hits
            if nid in seed_ids:
                file_seed_density[fp] = file_seed_density.get(fp, 0) + 1
            if nid in named_ids:
                named_files.add(fp)

        seed_hits_fp = {}   # file path -> # query defs FTS-matched in it
        if cfg.file_centric and file_graph:
            # IDF damping: divide each file's import-centrality by log(1+importers)
            # so global hubs (linux/fs.h, kernel.h -- imported by thousands) sink
            # and the query's local import cluster (ext4_jbd2.h, the ext4 .c files)
            # rises. Without this, RWR centrality just surfaces the repo's hubs.
            fids = {fp: self._file_node_id(fp) for fp in file_graph}
            indeg = self._import_indegree([i for i in fids.values() if i])
            for fp, sc in list(file_graph.items()):
                d = indeg.get(fids.get(fp), 0)
                file_graph[fp] = sc / math.log(2.0 + d)
                fnid = fids.get(fp)
                if fnid is not None and fnid in seed_hits:
                    seed_hits_fp[fp] = seed_hits[fnid]

        max_graph = max(file_graph.values()) if file_graph else 0.0
        files = list(file_nodes.keys())
        if max_graph > 0:
            gated = [fp for fp in files
                     if file_graph.get(fp, 0) >= max_graph * 0.06
                     or fp in named_files or file_terms.get(fp, 0) >= 2]
            if len(gated) >= 2:
                files = gated

        if cfg.file_centric:
            # FTS relevance leads: a file holding 5 query symbols beats one with a
            # single incidental name match, regardless of import centrality (which
            # is noisy -- a file that #includes a lot gets high RWR mass). Order:
            # # of query symbols, then damped import-centrality, then path.
            files.sort(key=lambda fp: (
                -seed_hits_fp.get(fp, 0), -file_graph.get(fp, 0.0), fp.lower()))
        elif cfg.fts_only:
            # docs: NO graph to localise with, so rank by DOC RELEVANCE DENSITY --
            # how many distinct query-matching documented symbols a file holds.
            # A file with several on-topic doc-hits (the ext4 cluster) beats one
            # with a single incidental name match (a struct literally named
            # "block"). seed-density first, then name-term hits, then path.
            files.sort(key=lambda fp: (
                -file_seed_density.get(fp, 0), -file_terms.get(fp, 0), fp.lower()))
        elif cfg.seed_rank:
            # hybrid: rank by FTS seed-density FIRST, then RWR centrality. The
            # named-token resolver (query word -> exact-named symbol) leaks noise:
            # "block" in "ext4 ... block" resolves to a struct literally named
            # `block` in an unrelated driver, which lands in named_files at
            # priority 0 and outranks the real ext4 cluster. Ranking by
            # seed-density (how many FTS-matched query symbols a file holds -- the
            # ext4 files hold many ext4_* hits, the noise file holds zero) demotes
            # those incidental name matches; RWR centrality then orders the
            # query-relevant files. Hybrid-only so callgraph's locked n=70 sort is
            # untouched.
            files.sort(key=lambda fp: (
                -file_seed_density.get(fp, 0), -file_graph.get(fp, 0.0),
                -file_terms.get(fp, 0), fp.lower()))
        else:
            files.sort(key=lambda fp: (
                0 if fp in named_files else 1, -file_graph.get(fp, 0.0),
                -file_terms.get(fp, 0), fp.lower()))
        files = files[:max_files]

        rel_lines = []
        if cfg.rel_kind:
            for src, dst, kind, line in edges[:60]:
                if kind != cfg.rel_kind:
                    continue
                sn, tn = self.get_node(src), self.get_node(dst)
                if sn and tn:
                    rel_lines.append("- %s -> %s" % (sn["name"], tn["name"]))

        parts = ["## %s_explore: %s" % (cfg.rep, query), "",
                 "Found %d symbols across %d files (ranked by the %s map)." % (
                     len(nodes), len(file_nodes), cfg.rep), ""]
        if rel_lines:
            parts.append("### Relationships (%s)" % (cfg.rel_kind or "graph"))
            parts += rel_lines[:20]
            parts.append("")
        if cfg.concise:
            # CONCISE MAP -- currently DEPS-ONLY (file_centric). The dependency
            # neighborhood (imports / imported-by) is deps' natural map. Each
            # OTHER rep should get its OWN concise map (callgraph=caller/callee
            # neighborhood, ast=skeleton tree, docs=docstrings, hybrid=merged) --
            # NOT this one. TODO: per-rep concise maps once deps proves the idea.
            # For now non-deps reps fall through to symbol pointers only (no
            # graph), which is harmless but not their true map.
            #
            # DEPS dep-graph map: per top file show what it imports + what
            # imports it, plus a minimal symbol pointer (signatures + line
            # ranges) for the top file so the agent knows where to edit. NO
            # bodies (fetch via <rep>_node when editing). Small + persistent.
            out_files = []
            top = files[:3]   # focus the map on the most relevant files only
            for i, fp in enumerate(top):
                fnid = self._file_node_id(fp) if cfg.file_centric else None
                parts.append("")
                parts.append("### %s" % fp)
                if fnid:
                    imps, imprs = self._dep_neighborhood(fnid)
                    if imps:
                        parts.append("  imports: " + ", ".join(
                            os.path.basename(x) for x in imps[:12]))
                    if imprs:
                        parts.append("  imported by: " + ", ".join(
                            os.path.basename(x) for x in imprs[:12]))
                # symbols only for the TOP file (where the edit likely goes)
                if i == 0:
                    defs = self._display_defs(fp, file_nodes[fp], qlow)
                    if defs:
                        parts.append("  symbols (fetch a body with %s_node):"
                                     % cfg.rep)
                        for nd in defs[:10]:
                            nm = nd.get("name") or "?"
                            ln, end = nd.get("line"), nd.get("end_line")
                            sig = (nd.get("signature") or "").strip()
                            loc = "%s%s" % (ln, "-%s" % end if end else "")
                            parts.append("    - `%s` (%s)%s" % (
                                nm, loc, ("  %s" % sig[:110]) if sig else ""))
                out_files.append({"file": fp,
                                  "score": round(file_graph.get(fp, 0.0), 4)})
            return {"query": query, "rep": cfg.rep, "n_symbols": len(nodes),
                    "n_files": len(file_nodes), "files": out_files,
                    "body": "\n".join(parts)}

        parts.append("### Source Code")
        parts.append("> Verbatim on-disk source of the relevant symbols. Treat as "
                     "already read; do not re-open these files.")
        total = sum(len(p) for p in parts)
        out_files = []
        for fp in files:
            defs = self._display_defs(fp, file_nodes[fp], qlow)
            block = ["", "#### %s" % fp]
            for nd in defs[:6]:
                if total > max_chars:
                    break
                src_txt = self._source_of(nd, max_lines=60)
                # docs rep: the doc-comment IS the signal -- show it even when the
                # node has no readable body (a 1-line doc paragraph / file header).
                doc = (nd.get("doc") or "").strip() if cfg.fts_only else ""
                if not src_txt and not doc:
                    continue
                doc_seg = ""
                if doc:
                    doc_seg = "\n/** %s */" % doc[:600]
                body = src_txt or ""
                seg = "```\n// %s  (%s:%s)%s\n%s\n```" % (
                    nd.get("name"), fp, nd.get("line"), doc_seg, body)
                block.append(seg)
                total += len(seg)
            if len(block) > 2:
                parts += block
                out_files.append({"file": fp,
                                  "score": round(file_graph.get(fp, 0.0), 4)})
            if total > max_chars:
                break

        return {"query": query, "rep": cfg.rep, "n_symbols": len(nodes),
                "n_files": len(file_nodes), "files": out_files,
                "body": "\n".join(parts)}

    def _display_defs(self, fp: str, grouped: list, qlow) -> list:
        """Pick which defs to show as a file's source in explore.

        Non-file-centric reps: the nodes already grouped under the file, by line.
        File-centric (deps): the file node carries no real body, and the capped
        graph-walk may not have reached this file's defs -- so fetch the file's
        ``contains`` children directly (guaranteed) and lead with the defs whose
        names match the query, then fill by line order."""
        if not self.cfg.file_centric:
            return sorted(grouped, key=lambda d: d.get("line") or 0)
        fnid = self._file_node_id(fp)
        kids = [self.get_node(i) for i in self._contains_children([fnid])] if fnid else []
        kids = [k for k in kids if k and (k.get("line") or 0) > 1]
        if not kids:   # no defs (e.g. a header with only macros) -> show the file
            return sorted([g for g in grouped if g.get("line")],
                          key=lambda d: d.get("line") or 0)
        ql = set(qlow)

        def matches(k):
            nm = (k.get("name") or "").lower()
            return any(q in nm for q in ql)
        kids.sort(key=lambda k: (0 if matches(k) else 1, k.get("line") or 0))
        return kids

    # -- codegraph's SUPPORTING tools, per rep ----------------------------
    def search_tool(self, query: str, limit: int = 10) -> list:
        """codegraph_search: quick symbol lookup, locations only (no source)."""
        return [{"name": r["name"], "qualname": r["qualname"], "kind": r["kind"],
                 "file": r["file"], "line": r["line"]}
                for r in self.search(query, limit=limit)]

    def _neighbors(self, symbol: str, incoming: bool, limit: int) -> list:
        """Resolve symbol -> its in/out neighbors along THIS rep's edges."""
        ids = self._resolve_name(symbol)
        if not ids:
            ids = [s["id"] for s in self.search(symbol, limit=3)]
        if self.cfg.fts_only or not ids:
            return []
        rows = self._edges(ids, self.cfg.walk_edges, incoming=incoming)
        seen, out = set(), []
        other = "src" if incoming else "dst"
        for e in rows:
            nid = e[other]
            if nid in seen:
                continue
            seen.add(nid)
            nd = self.get_node(nid)
            if nd:
                out.append({"name": nd["name"], "qualname": nd["qualname"],
                            "file": nd["file"], "line": nd["line"],
                            "site_line": e["line"]})
            if len(out) >= limit:
                break
        return out

    def callers(self, symbol: str, limit: int = 20) -> list:
        """codegraph_callers, per rep: incoming neighbors (callers / containers /
        importers depending on the rep's edge)."""
        return self._neighbors(symbol, incoming=True, limit=limit)

    def callees(self, symbol: str, limit: int = 20) -> list:
        """codegraph_callees, per rep: outgoing neighbors (callees / nested /
        imports)."""
        return self._neighbors(symbol, incoming=False, limit=limit)

    def impact(self, symbol: str, depth: int = 2, limit: int = 60) -> list:
        """codegraph_impact, per rep: transitive INCOMING closure (what is
        affected by changing <symbol>) up to ``depth`` hops along this rep's
        edges. For callgraph = transitive callers; deps = transitive importers."""
        if self.cfg.fts_only:
            return []
        ids = self._resolve_name(symbol)
        if not ids:
            ids = [s["id"] for s in self.search(symbol, limit=3)]
        frontier = set(ids)
        seen_ids = set(ids)
        out = []
        for d in range(1, depth + 1):
            rows = self._edges(list(frontier), self.cfg.walk_edges, incoming=True)
            nxt = set()
            for e in rows:
                sid = e["src"]
                if sid in seen_ids:
                    continue
                seen_ids.add(sid)
                nxt.add(sid)
                nd = self.get_node(sid)
                if nd:
                    out.append({"name": nd["name"], "file": nd["file"],
                                "line": nd["line"], "depth": d})
                if len(out) >= limit:
                    return out
            if not nxt:
                break
            frontier = nxt
        return out

    def files(self, under: Optional[str] = None, limit: int = 200) -> list:
        """codegraph_files, per rep: files in the map with their symbol counts."""
        sql = ("SELECT file, COUNT(*) AS n FROM nodes WHERE file IS NOT NULL ")
        params = []
        if under:
            sql += "AND file LIKE ? "
            params.append(under.rstrip("/") + "/%")
        sql += "GROUP BY file ORDER BY file LIMIT ?"
        params.append(max(limit, 1))
        try:
            rows = self.conn().execute(sql, params).fetchall()
        except sqlite3.Error:
            return []
        return [{"file": r["file"], "symbols": r["n"]} for r in rows]

    def status(self) -> dict:
        """codegraph_status, per rep: index health (nodes / edges / files)."""
        c = self.conn()
        nn = c.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        ne = c.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        nf = c.execute("SELECT COUNT(DISTINCT file) FROM nodes").fetchone()[0]
        kinds = [r[0] for r in c.execute("SELECT DISTINCT kind FROM edges")]
        return {"rep": self.cfg.rep, "nodes": nn, "edges": ne, "files": nf,
                "edge_kinds": kinds}

    # -- NODE (codegraph_node, per rep) -----------------------------------
    def node(self, target: str, max_lines: int = 80) -> list:
        cfg = self.cfg
        t = (target or "").strip()
        if not t:
            raise ValueError("target must not be empty")
        nodes = []
        file_view = False
        # Form 0: a bare FILE PATH (ends in a code ext, no :symbol / :line) ->
        # return the file's contained symbols + source instead of erroring. The
        # agent frequently calls node("path/to/file.py") to "show me this file";
        # this was the #1 cause of node errors on callgraph (81%) and docs (97%).
        if ":" not in t and _FILE_EXT.search(t):
            fnodes = self._nodes_in_file(t, limit=20)
            if fnodes:
                nodes = fnodes
                file_view = True
        # Form 1: file:line anchor -> the def spanning that line.
        if not nodes and ":" in t:
            path_part, _, line_part = t.rpartition(":")
            if path_part and line_part.isdigit():
                nd = self._node_at_anchor(path_part.replace("\\", "/"),
                                          int(line_part))
                if nd:
                    nodes = [nd]
        # Form 2: <file_or_scope>:<symbol> (symbol not a digit) -> resolve the
        # SYMBOL, scoped to the file/parent hint. This was the 23%-failing path:
        # the old code passed the whole "file.py:Symbol" string to _resolve_name,
        # which only matched exact name/qualname, so file-qualified targets and
        # Class.method targets never resolved -> the agent fell back to Read.
        if not nodes and ":" in t:
            hint, _, sym = t.rpartition(":")
            if sym and not sym.isdigit():
                ids = self._resolve_symbol(sym, file_hint=hint, limit=6)
                nodes = [n for n in (self.get_node(i) for i in ids) if n]
        # Form 3: a bare symbol / Class.method (no colon).
        if not nodes:
            ids = self._resolve_symbol(t, limit=6) or self._resolve_name(t, limit=6)
            nodes = [n for n in (self.get_node(i) for i in ids[:6]) if n]
        if not nodes:
            return [{"target": t, "error": "no symbol/anchor %r in the %s map"
                     % (t, cfg.rep)}]

        out = []
        # file-view returns many defs from one file -> cap each body so the whole
        # response stays reasonable (the agent wants the file's shape, not 20 full
        # bodies). Single-symbol lookups keep the full max_lines.
        per_node_lines = min(max_lines, 40) if file_view else max_lines
        for nd in nodes:
            body = self._source_of(nd, max_lines=per_node_lines)
            if body is None:
                continue
            # neighbor display walks the rep's HEADLINE edge (rel_kind) only, not
            # every walk edge -- so deps shows real file imports, not the
            # 'contains' edge to the def's own file (which would read as a
            # nonsense "imported_by: [its own file]"). For single-edge reps
            # rel_kind == the walk edge, so this is a no-op there.
            ek = ((cfg.rel_kind,) if cfg.rel_kind else ()) if not cfg.fts_only else ()
            outg = [self.get_node(e["dst"]) for e in self._edges(
                [nd["id"]], ek, incoming=False)[:8]] if ek else []
            inc = [self.get_node(e["src"]) for e in self._edges(
                [nd["id"]], ek, incoming=True)[:8]] if ek else []
            out.append({
                "name": nd["name"], "file": nd["file"], "line": nd["line"],
                "end_line": nd["end_line"], "signature": nd.get("signature"),
                cfg.neighbor_out: [n["name"] for n in outg if n],
                cfg.neighbor_in: [n["name"] for n in inc if n],
                "body": body,
            })
        return out or [{"target": t, "error": "could not read source for %r" % t}]
