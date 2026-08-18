"""Shared utilities for sampleproj."""


def normalize(text: str) -> str:
    """Lowercase and strip surrounding whitespace from text."""
    return text.strip().lower()


def log_message(message: str) -> None:
    """Print a tagged log message."""
    print(f"[sampleproj] {message}")


def helper_unused() -> int:
    """Return a constant; intentionally never called by anything."""
    return 42
