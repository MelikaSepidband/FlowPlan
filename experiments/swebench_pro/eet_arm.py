"""EET comparator arm (baseline_eet).

Paper: "EET: Experience-Driven Early Termination for Cost-Efficient Software
Engineering Agents" (ACL 2026 Findings; arXiv 2601.05777). Code:
github.com/IanWalls/EET @ 91d9936 (cloned unmodified to tools/eet_src).

Their method has two coupled levers, ported from their reference implementation
for mini-swe-agent (`agents/experience_retrieval.py`), which is the scaffold
closest to Claude Code:

  A. EXPERIENCE INJECTION. Their hand-rolled TF-IDF/cosine store (their
     `experience/store.py`, imported here unmodified) retrieves the single most
     similar past "experience object" by issue-text similarity from THEIR
     shipped base (`extracted_experiences_summarized.jsonl`, 205 records they
     extracted from SWE-bench Lite/Verified with GPT-5-mini) and renders it with
     THEIR `<retrieved_experiences>` template into the task prompt.
     Their defaults: top_k=1, min_similarity=0.15, use_tfidf=True,
     search_by_issue=True (retrieve top_k*2, then truncate to top_k).

  B. CONFIDENCE-GATED EARLY TERMINATION (see eet_hook.py). At "key steps" the
     agent is asked to self-assess `CONFIDENCE_SCORE: <n>`; at >= 81 their
     `<submission_prompt>` block is appended, inviting submission. This is a
     SOFT stop suggestion, not a hard kill -- as shipped, not the abstract's
     "stop at 90 / abandon at 40".

  COUPLING: their agent falls back to plain DefaultAgent behaviour when nothing
  was retrieved (experience_retrieval.py:531-535), i.e. lever B only runs on
  cells where lever A fired. We reproduce that: no experience -> no hook.

Their base shares no instance and no repository with our 30 test instances
(verified: zero overlap), so using their data is leakage-free and strictly more
faithful than regenerating a base from our own transcripts.
"""
import json
import sys
from pathlib import Path
from typing import Any, Optional

from experiments.swebench_pro import agent

STUDY_ROOT = agent.STUDY_ROOT
EET_SRC = STUDY_ROOT / "tools" / "eet_src"
MINI_SRC = EET_SRC / "mini-swe-agent" / "src"
# Arm A (baseline_eet): THEIR shipped base, 205 records from SWE-bench
# Lite/Verified -- tests whether their method transfers OFF-distribution (none of
# our three repos appear in it).
STORE_THEIRS = MINI_SRC / "minisweagent" / "experience" / "extracted_experiences_summarized.jsonl"
# Arm B (baseline_eetB): a base built from OUR held-out baseline trajectories
# with THEIR pipeline (_eet_build_base.py) -- their INTENDED setting, where the
# base covers the target repositories. SWE-bench Pro: the 108 pool instances
# outside the 158-instance evaluation sample (data/eet/_eet_split.json).
_OURS_SHIPPED = STUDY_ROOT / "data" / "eet" / "extracted_experiences_summarized_ours.jsonl"
_OURS_STUDY = STUDY_ROOT / "results" / "_eet_base_ours" / "extracted_experiences_summarized_ours.jsonl"
STORE_OURS = _OURS_SHIPPED if _OURS_SHIPPED.is_file() else _OURS_STUDY
# Arm B on SecBench: the same pipeline over baseline trajectories of the
# evaluation-split instances OUTSIDE the 169-instance sample, so the base and
# the evaluation sample share no instance.
_SEC_SHIPPED = STUDY_ROOT / "data" / "eet" / "extracted_experiences_summarized_ours_sec169.jsonl"
_SEC_STUDY = STUDY_ROOT / "results" / "_eet_base_ours_sec169" / "extracted_experiences_summarized_ours.jsonl"
STORE_OURS_SEC169 = _SEC_SHIPPED if _SEC_SHIPPED.is_file() else _SEC_STUDY
STORE_JSONL = STORE_THEIRS      # default; overridden per condition by set_store()


def set_store(which: str) -> Path:
    """'theirs' (arm A), 'ours' (arm B, SWE-bench Pro base) or 'ours_sec169'
    (arm B, SecBench base). Everything else about the method -- thresholds,
    template, coupling -- is identical between the arms; only the experience
    base changes, which is exactly the comparison."""
    global STORE_JSONL
    STORE_JSONL = {"ours": STORE_OURS,
                   "ours_sec169": STORE_OURS_SEC169}.get(which, STORE_THEIRS)
    return STORE_JSONL

# their ExperienceRetrievalAgentConfig defaults (experience_retrieval.py:18-45)
TOP_K = 1
MIN_SIMILARITY = 0.15
USE_TFIDF = True
SEARCH_BY_ISSUE = True

# their experience_template, verbatim (experience_retrieval.py:26-42)
EXPERIENCE_TEMPLATE = """
<retrieved_experiences>
{% if experiences %}
Here are some relevant experiences from similar tasks that might help you:

{% for exp, similarity in experiences %}
## Experience {{ loop.index }}
**Issue Description:** {{ exp.issue_description }}
**Task Summary:** {{ exp.task_summary }}
---
{% endfor %}
</retrieved_experiences>
{% endif %}
"""


def _their_modules():
    """Import their store/models from the clone, unmodified.

    Their package prints a banner and loads a global config at import; keep
    stdout clean for callers that care.
    """
    # EET and SeeRepo are both forks of mini-swe-agent under the same package
    # name; drop the other fork if it is already imported in this process and
    # put THIS fork's src first on sys.path so the fresh import resolves to it.
    _loaded = sys.modules.get("minisweagent")
    if _loaded is not None and not str(getattr(_loaded, "__file__", "") or
                                      getattr(_loaded, "__path__", [""])[0]).startswith(str(MINI_SRC)):
        for k in [k for k in sys.modules if k == "minisweagent" or k.startswith("minisweagent.")]:
            del sys.modules[k]
    if str(MINI_SRC) in sys.path:
        sys.path.remove(str(MINI_SRC))
    sys.path.insert(0, str(MINI_SRC))
    import contextlib
    import io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        from minisweagent.experience.store import ExperienceStore
        from minisweagent.experience.models import Experience
    return ExperienceStore, Experience


def their_project_name(issue_id: str) -> Optional[str]:
    """Their _extract_project_name, verbatim (experience_retrieval.py:101-121).

    It requires the id to end in `-<digits>`; SWE-bench Pro ids end in a commit
    hash, so this returns None for every one of our instances. In their
    retrieve_experiences that makes `current_project_name` None, which SKIPS the
    same-project filter entirely -- so cross-project matches are kept. That is
    their code's own behaviour on this id format, not a relaxation by us.
    """
    if not issue_id:
        return None
    parts = issue_id.replace(".traj", "").rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0]
    return None


def retrieve(problem_statement: str, issue_id: str = "") -> list[tuple[Any, float]]:
    """Their retrieve_experiences(), with their defaults.

    experience_retrieval.py:124-182 -- search top_k*2, drop duplicates, apply the
    same-project filter ONLY when their _extract_project_name yields a project
    (it does not for SWE-bench Pro ids, see above), sort by similarity, truncate
    to top_k.
    """
    ExperienceStore, _ = _their_modules()
    store = ExperienceStore(str(STORE_JSONL))
    if SEARCH_BY_ISSUE:
        res = store.search_by_issue_similarity(
            query=problem_statement, top_k=TOP_K * 2, min_similarity=MIN_SIMILARITY,
            use_tfidf=USE_TFIDF)
    else:
        res = store.search_by_task_similarity(
            query=problem_statement, top_k=TOP_K * 2, min_similarity=MIN_SIMILARITY,
            use_tfidf=USE_TFIDF)
    current_project = their_project_name(issue_id)
    out: list[tuple[Any, float]] = []
    seen: set = set()
    for exp, sim in res:
        if exp.issue_id in seen:
            continue
        if current_project and their_project_name(exp.issue_id) != current_project:
            continue
        out.append((exp, sim))
        seen.add(exp.issue_id)
    out.sort(key=lambda x: x[1], reverse=True)
    return out[:TOP_K]


def format_experiences(experiences: list[tuple[Any, float]]) -> str:
    """Their format_experiences(): render their template with jinja2.

    A list task_summary is rendered as a '- ' bullet per item, joined by
    newlines (experience_retrieval.py:184-215).
    """
    from jinja2 import StrictUndefined, Template
    processed = []
    for exp, sim in experiences:
        ts = exp.task_summary
        if isinstance(ts, list):
            ts = "\n".join("- %s" % t for t in ts)
        shallow = type("Exp", (), {"issue_description": exp.issue_description,
                                   "task_summary": ts, "issue_id": exp.issue_id})
        processed.append((shallow, sim))
    return Template(EXPERIENCE_TEMPLATE, undefined=StrictUndefined).render(
        experiences=processed)


def prepare(inst: dict, run_dir: Path, store: str = "theirs") -> tuple[Optional[str], dict]:
    """Retrieve for this instance and return (prompt_suffix, info).

    prompt_suffix is None when nothing cleared their similarity threshold; the
    caller then runs the plain baseline prompt AND disables the hook, which is
    their own coupling.
    """
    run_dir = Path(run_dir)
    stage = run_dir / "eet"
    stage.mkdir(parents=True, exist_ok=True)
    set_store(store)
    ps = inst.get("problem_statement") or ""
    iid = inst.get("instance_id") or ""
    res = retrieve(ps, iid)
    info: dict[str, Any] = {
        "instance_id": inst.get("instance_id"),
        "fired": bool(res),
        "top_k": TOP_K, "min_similarity": MIN_SIMILARITY,
        "use_tfidf": USE_TFIDF, "search_by_issue": SEARCH_BY_ISSUE,
        "store": STORE_JSONL.name, "store_kind": store,
        "their_project_name": their_project_name(iid),
        "project_filter_active": bool(their_project_name(iid)),
        "matches": [{"issue_id": e.issue_id, "similarity": round(s, 4),
                     "confidence": getattr(e, "confidence", None)} for e, s in res],
    }
    block = None
    if res:
        block = format_experiences(res)
        info["injected_chars"] = len(block)
        (stage / "injected_block.txt").write_text(block, encoding="utf-8")
    (stage / "retrieval.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    return block, info
