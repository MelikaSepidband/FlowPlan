"""Serve a per-rep codegraph server over stdio: python -m servers.perrep.

The representation is chosen by the REPR_PERREP_REP env var (callgraph/ast/deps/
docs/hybrid). Exposes <rep>_explore + <rep>_node + the rep's own native tools.
"""

from servers.perrep import create_server


def main() -> None:
    create_server().serve()


if __name__ == "__main__":
    main()
