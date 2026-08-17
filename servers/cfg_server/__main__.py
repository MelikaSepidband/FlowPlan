"""Serve the cfg server over stdio: python -m servers.cfg_server."""

from servers.cfg_server import create_server


def main() -> None:
    """Run the cfg server on the stdio transport (blocking)."""
    create_server().serve()


if __name__ == "__main__":
    main()
