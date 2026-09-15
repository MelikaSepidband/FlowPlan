"""Import SHERLOC's own tool code (recipes/sherloc/inference/sherloc_utils) from
the sparse clone under tools/sherloc_src, unmodified.

Their modules import ``nemo_skills.utils.get_logger_name`` for logger names
only; the NeMo-Skills package itself (hydra, vllm client, ...) is not needed
by the tools, so a two-function stand-in is registered instead of installing
it. Nothing in their tree is edited."""
import os
import sys
import types
from pathlib import Path

STUDY_ROOT = Path(__file__).resolve().parents[2]
SHERLOC_SRC = Path(os.environ.get("SHERLOC_SRC",
                                  str(STUDY_ROOT / "tools" / "sherloc_src")))


def their_modules():
    """Return (RepoManager, ToolExecutor, DEFAULT_EXCLUDE_DIRS,
    DEFAULT_FILE_EXTENSIONS) from their code."""
    if "nemo_skills.utils" not in sys.modules:
        pkg = types.ModuleType("nemo_skills")
        utils = types.ModuleType("nemo_skills.utils")
        utils.get_logger_name = lambda f: "sherloc." + Path(str(f)).stem
        pkg.utils = utils
        sys.modules["nemo_skills"] = pkg
        sys.modules["nemo_skills.utils"] = utils
    if str(SHERLOC_SRC) not in sys.path:
        sys.path.insert(0, str(SHERLOC_SRC))
    from recipes.sherloc.inference.sherloc_utils.defaults import (
        DEFAULT_EXCLUDE_DIRS, DEFAULT_FILE_EXTENSIONS)
    from recipes.sherloc.inference.sherloc_utils.repo_manager import RepoManager
    from recipes.sherloc.inference.sherloc_utils.tool_executor import ToolExecutor
    return (RepoManager, ToolExecutor, list(DEFAULT_EXCLUDE_DIRS),
            list(DEFAULT_FILE_EXTENSIONS))
