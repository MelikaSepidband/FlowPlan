"""CFG MCP server package (study condition C2).

Exposes ``create_server() -> ReprServer`` per INTERFACES.md section 10. The
four representation tools (get_cfg, cfg_paths, dominators, reindex) live in
``servers.cfg_server.server``; the pure-Python "python-ast" engine lives in
``servers.cfg_server.engine``; the navigation kit is auto-registered by
ReprServer.
"""

from servers.cfg_server.server import create_server

__all__ = ["create_server"]
