"""Per-representation codegraph: one map + two tools (explore, node) per rep.

Mirrors codegraph's architecture exactly, but builds a DIFFERENT map per
representation and serves rep-specific explore/node over it. Each map is a
SQLite DB with codegraph's shape (nodes + edges + nodes_fts) so a single
explore/node engine (perrep.engine) works for every rep -- only the map's
node/edge semantics differ:

    callgraph -> functions + ``calls`` edges
    ast       -> all defs    + ``contains`` edges (file->func->block)
    deps      -> files       + ``imports`` edges (the #include graph)
    docs      -> documented symbols + FTS (no structural edge)
    hybrid    -> functions+files + merged calls/imports/references

Build:  python -m perrep.build --repo R --rep callgraph  -> <repo>/.perrep/callgraph.db
Serve:  servers.perrep.server (REPR_PERREP_REP=<rep>) exposes <rep>_explore + <rep>_node.
"""
