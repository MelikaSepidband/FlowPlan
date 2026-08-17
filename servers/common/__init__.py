"""Shared server infrastructure: envelope, budget, staleness, paging, navkit.

Import submodules directly (``from servers.common import env``); this package
init intentionally imports nothing so that light consumers (e.g. the indexer)
do not pull in the MCP SDK.
"""
