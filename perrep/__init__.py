"""Engine support for the perrep MCP server.

``servers.perrep.server`` imports ``perrep.engine`` at module load. In this
repository the server always runs in flow-first mode (REPR_FLOW_FIRST=1, set by
the runners), where it serves the five flow tools; the engine module is import
support only.
"""
