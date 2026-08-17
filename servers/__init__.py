"""MCP server packages for the repr-study project.

Each representation lives in servers/<rep>_server/ and is served over stdio
via ``python -m servers.<rep>_server``. Shared infrastructure (the uniform
envelope, token budget, staleness, paging, and the navigation kit) lives in
servers/common/. See INTERFACES.md for the binding contract.
"""
