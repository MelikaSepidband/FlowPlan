"""Per-representation codegraph MCP server.

One server per representation (selected by ``REPR_PERREP_REP``). Exposes exactly
two tools, mirroring codegraph: ``<rep>_explore`` and ``<rep>_node``, both backed
by the rep's prebuilt map at ``<REPO_ROOT>/.perrep/<rep>.db`` via perrep.engine.
"""

from servers.perrep.server import create_server

__all__ = ["create_server"]
