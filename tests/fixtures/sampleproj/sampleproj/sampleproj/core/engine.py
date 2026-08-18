"""Core pipeline engine for sampleproj."""

from sampleproj.core import utils


def validate_input(records: list[str]) -> list[str]:
    """Keep only non-empty string records; calls nothing else."""
    validated = []
    for record in records:
        if isinstance(record, str) and record.strip():
            validated.append(record)
    return validated


def transform(records: list[str]) -> list[str]:
    """Normalize every record."""
    return [utils.normalize(record) for record in records]


def run_pipeline(records: list[str]) -> list[str]:
    """Validate, transform, and log the records; return the transformed list."""
    validated = validate_input(records)
    transformed = transform(validated)
    utils.log_message("pipeline complete")
    return transformed
