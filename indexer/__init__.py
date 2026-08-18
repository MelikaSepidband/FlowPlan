"""Offline index construction for the repr-study servers.

``python -m indexer.build_index --repo R --index-dir I --reps a,b`` builds the
manifest (staleness baseline) and the symbols backend, then each requested
backend from indexer/backends/. See INTERFACES.md section 7.
"""
