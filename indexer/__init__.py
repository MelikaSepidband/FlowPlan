"""Offline index construction for the flow servers.

The runners build the symbols index per instance via
``indexer.backends.get_backend("symbols")`` (see ensure_repr_index in the
benchmark runners); it resolves the bug report's function names to their
definitions so ``flow_of`` can find the right graphs.
"""
