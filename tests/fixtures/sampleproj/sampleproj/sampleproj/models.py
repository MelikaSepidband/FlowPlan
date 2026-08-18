"""Data models for sampleproj."""

from sampleproj.services import db


class User:
    """A user record that can persist itself."""

    def __init__(self, name: str) -> None:
        """Store the user name."""
        self.name = name

    def save(self) -> dict:
        """Persist the user by connecting to the database."""
        connection = db.connect("users.sqlite")
        return {"user": self.name, "connection": connection}
