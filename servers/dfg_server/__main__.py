"""Serve the dfg server over stdio: python -m servers.dfg_server."""

from servers.dfg_server import create_server


def main() -> None:
    """Run the dfg server on the stdio transport (blocking)."""
    create_server().serve()


if __name__ == "__main__":
    main()
