"""Indexer backend plugins.

Each backend is a module ``indexer/backends/<name>.py`` exposing exactly

    def build(repo_root: Path, index_dir: Path) -> dict

which (re)creates that backend's database under index_dir and returns a
stats dict including at least ``{"backend": "<name>"}``. Backends own exactly
one database each; the fixed SQLite schemas live in INTERFACES.md section 6.
"""

import importlib
from types import ModuleType

__all__ = ["get_backend"]


def get_backend(name: str) -> ModuleType:
    """Import and validate the backend module ``indexer.backends.<name>``.

    Raises ValueError for unsafe names, ModuleNotFoundError when the backend
    does not exist, and AttributeError when it lacks a callable ``build``.
    """
    if not name or not name.replace("_", "").isalnum() or name.startswith("_"):
        raise ValueError(f"invalid backend name: {name!r}")
    module = importlib.import_module(f"indexer.backends.{name}")
    build = getattr(module, "build", None)
    if not callable(build):
        raise AttributeError(
            f"backend {name!r} does not expose build(repo_root, index_dir)"
        )
    return module
