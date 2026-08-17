"""Data-flow-graph MCP server package (study condition C3).

Exposes ``create_server() -> ReprServer`` per INTERFACES.md section 10. The
five representation tools (def_use, backward_slice, forward_slice,
data_flow_between, reindex) live in ``servers.dfg_server.server``; the
beniget def-use engine lives in ``servers.dfg_server.engine``; the
navigation kit is auto-registered by ReprServer.
"""

from servers.dfg_server.server import create_server

__all__ = ["create_server"]
