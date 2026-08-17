"""Application entry point for sampleproj."""

from sampleproj.core import engine
from sampleproj.services import db


def main() -> dict:
    """Run the full pipeline and open a database connection."""
    result = engine.run_pipeline([" Alpha ", " Beta ", ""])
    connection = db.connect("sampleproj.sqlite")
    return {"result": result, "connection": connection}
