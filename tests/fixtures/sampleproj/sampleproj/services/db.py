"""Database service for sampleproj."""

from sampleproj.core import utils


def connect(dsn: str) -> dict:
    """Open a fake database connection to dsn and log the attempt."""
    utils.log_message(f"connecting to {dsn}")
    return {"dsn": dsn, "connected": True}
