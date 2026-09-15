"""EET arm B: build an experience base from OUR SWE-bench Pro transcripts with
THEIR pipeline, so their method runs in its intended setting (a base that covers
the target repositories) instead of off-distribution.

Their pipeline, reproduced:
  stage 1  extract_with_llm_judge.JUDGE_PROMPT_TEMPLATE  -> N per-step records
           per trajectory {task_summary, confidence, output, reasoning}
  stage 2  summarize_experiences.SUMMARY_PROMPT_TEMPLATE -> ONE record per issue
           {issue_id, issue_description (LLM-summarised), task_summary (list of
           the deduped step summaries), confidence (mean), created_at, metadata}
Both prompts are imported/copied verbatim from tools/eet_src. The LLM is Haiku
via headless `claude -p` (they used claude-sonnet-4 / gpt-5 / gpt-5-mini through
litellm; we have no API key, and the arm's model must stay Haiku) -- disclosed.

Source: results/swe183 baseline cells (183 trajectories: ansible 66, openlibrary
61, qutebrowser 56), ALL of them, with ZERO overlap with the 30 test instances.
No success filter -- their own exp_ids.txt has 207 entries for a 205-record base,
i.e. they extracted from every trajectory of their run, failures included.

Usage:
  .venv/bin/python _eet_build_base.py convert       # transcripts -> their message format
  .venv/bin/python _eet_build_base.py extract [N]   # stage 1 (resumable)
  .venv/bin/python _eet_build_base.py summarize     # stage 2 (resumable)
  .venv/bin/python _eet_build_base.py status
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "tools" / "eet_src"
MINI = SRC / "mini-swe-agent" / "src"
OUT = ROOT / "results" / "_eet_base_ours"
CONV = OUT / "converted"
STEP1 = OUT / "extracted_experiences_llm.jsonl"
STEP2 = OUT / "extracted_experiences_summarized_ours.jsonl"
LOGD = OUT / "logs"
CLAUDE = "/home/tempadmin/.local/share/claude/versions/2.1.220"
HOME = OUT / "claude-home"
# The FROZEN split (results/_eet_split.json, see _freeze_eet_split.py): 158
# instances reserved for testing now and in future, 108 for the base. Building
# from "everything except today's 30" would leak as soon as the test set grows,
# so the base is drawn only from base_ids and every instance in test_ids is
# refused, permanently.
_SPLIT = json.load(open(ROOT / "results" / "_eet_split.json"))
TEST_IDS = set(_SPLIT["test_ids"])
BASE_IDS = set(_SPLIT["base_ids"])

sys.path.insert(0, str(MINI))


def _their_extract_module():
    """Load their extract_with_llm_judge for its prompt + formatter WITHOUT
    executing its litellm import guard (it sys.exit()s when litellm is absent;
    we call Haiku through the CLI instead, so that dependency is irrelevant).
    The prompt text and formatter are taken from their file unmodified."""
    import types
    src = (MINI / "minisweagent" / "experience" / "extract_with_llm_judge.py").read_text(encoding="utf-8")
    # keep everything up to the first function definition after the guard:
    # the module-level template plus format_conversation_for_judge.
    tpl = re.search(r'JUDGE_PROMPT_TEMPLATE = """(.*?)"""', src, re.S)
    fmt = re.search(r"(def format_conversation_for_judge.*?return \"\\n\".join\(formatted_lines\))", src, re.S)
    if not tpl or not fmt:
        raise SystemExit("could not lift their prompt/formatter -- inspect their file")
    mod = types.ModuleType("their_extract")
    exec("from typing import List, Dict\n" + fmt.group(1), mod.__dict__)
    return tpl.group(1), mod.format_conversation_for_judge


JUDGE_PROMPT_TEMPLATE, format_conversation_for_judge = _their_extract_module()

# their SUMMARY_PROMPT_TEMPLATE, verbatim from
# trae-agent/trae_agent/experience/summarize_experiences.py:28-47
SUMMARY_PROMPT_TEMPLATE = """You are an expert at summarizing technical issues. Your task is to create a concise, clear summary of a programming issue that captures:
1. The core problem
2. The root cause (if mentioned)
3. The impact or expected behavior

## Instructions
- Write a clear, technical summary (2-4 sentences)
- Focus on the essential problem, not implementation details
- Include root cause if it's clearly stated
- Use technical terminology appropriately
- Keep it concise but informative

## Original Issue Description:
{issue_description}

## Task Summary Steps:
{task_summaries}

Now provide a concise summary of this issue:"""


def _seed_home() -> Path:
    HOME.mkdir(parents=True, exist_ok=True)
    src = Path.home() / ".claude"
    for name in (".credentials.json", "settings.json"):
        if (src / name).is_file():
            import shutil
            shutil.copy2(src / name, HOME / name)
    if (Path.home() / ".claude.json").is_file():
        import shutil
        shutil.copy2(Path.home() / ".claude.json", HOME / ".claude.json")
    return HOME


def ask(prompt: str, tag: str, timeout: int = 900) -> str:
    """One headless Haiku call, prompt on stdin (their calls are single-shot,
    no tools). Usage is harvested from the shared claude-home afterwards."""
    env = dict(os.environ)
    env.pop("ANTHROPIC_BASE_URL", None)
    env.pop("CLAUDECODE", None)
    env["CLAUDE_CONFIG_DIR"] = str(_seed_home())
    env["DISABLE_AUTOUPDATER"] = "1"
    env["ENABLE_TOOL_SEARCH"] = "false"
    cmd = [CLAUDE, "-p", "--output-format", "json", "--model", "claude-haiku-4-5",
           "--effort", "low", "--permission-mode", "bypassPermissions",
           "--max-turns", "1", "--tools", "", "--disallowedTools", "WebFetch,WebSearch"]
    p = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                       env=env, timeout=timeout, cwd=str(OUT))
    LOGD.mkdir(parents=True, exist_ok=True)
    try:
        d = json.loads(p.stdout)
        txt = d.get("result") or ""
    except Exception:
        txt = ""
    with open(LOGD / ("%s.log" % tag), "w", encoding="utf-8") as fh:
        fh.write("RC=%s\n--- stdout ---\n%s\n--- stderr ---\n%s\n" % (p.returncode, p.stdout[-4000:], p.stderr[-2000:]))
    return txt


def parse_json_array(text: str):
    """Their _parse_llm_output equivalent: pull the first JSON array out of the
    reply, tolerating ```json fences and surrounding prose."""
    if not text:
        return []
    m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.S)
    cand = m.group(1) if m else None
    if cand is None:
        i, j = text.find("["), text.rfind("]")
        cand = text[i:j + 1] if i >= 0 and j > i else None
    if not cand:
        return []
    try:
        v = json.loads(cand)
        return v if isinstance(v, list) else []
    except Exception:
        return []


def convert() -> None:
    """Our transcript.jsonl -> their {"messages": [{role, content}...]} shape."""
    CONV.mkdir(parents=True, exist_ok=True)
    n = 0
    for d in sorted(ROOT.glob("results/swe183/*__baseline/")):
        inst = d.name[: -len("__baseline")]
        if inst not in BASE_IDS or inst in TEST_IDS:
            continue                       # frozen split: base_ids only, never a test id
        t = d / "transcript.jsonl"
        if not t.is_file():
            continue
        msgs = []
        for line in open(t, encoding="utf-8", errors="replace"):
            try:
                r = json.loads(line)
            except Exception:
                continue
            typ = r.get("type")
            if typ not in ("user", "assistant"):
                continue
            c = (r.get("message") or {}).get("content")
            if isinstance(c, str):
                text = c
            elif isinstance(c, list):
                parts = []
                for b in c:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text":
                        parts.append(b.get("text") or "")
                    elif b.get("type") == "thinking":
                        parts.append("THOUGHT: " + (b.get("thinking") or ""))
                    elif b.get("type") == "tool_use":
                        parts.append("COMMAND (%s): %s" % (b.get("name"), json.dumps(b.get("input"))[:2000]))
                    elif b.get("type") == "tool_result":
                        cc = b.get("content")
                        s = cc if isinstance(cc, str) else " ".join(
                            x.get("text", "") for x in cc if isinstance(x, dict)) if isinstance(cc, list) else ""
                        parts.append("OUTPUT: " + s[:3000])
                text = "\n".join(p for p in parts if p)
            else:
                continue
            if text.strip():
                msgs.append({"role": typ, "content": text})
        if len(msgs) < 4:
            continue
        (CONV / ("%s_cleaned.json" % inst)).write_text(
            json.dumps({"issue_id": inst, "messages": msgs}), encoding="utf-8")
        n += 1
    print("converted %d trajectories -> %s" % (n, CONV))


def _done_ids(path: Path, key: str = "issue_id") -> set:
    s = set()
    if path.exists():
        for line in open(path, encoding="utf-8"):
            try:
                s.add(json.loads(line)[key])
            except Exception:
                pass
    return s


def extract(limit: int = 0) -> None:
    """Stage 1: their judge prompt, one call per trajectory. Resumable."""
    files = sorted(CONV.glob("*_cleaned.json"))
    done = _done_ids(STEP1)
    todo = [f for f in files if f.name.replace("_cleaned.json", "") not in done]
    if limit:
        todo = todo[:limit]
    print("stage 1: %d done, %d to do (of %d)" % (len(done), len(todo), len(files)))
    for i, f in enumerate(todo, 1):
        d = json.loads(f.read_text(encoding="utf-8"))
        iid = d["issue_id"]
        desc = ""
        for m in d["messages"]:
            if m["role"] == "user":
                desc = m["content"][:1500]
                break
        prompt = JUDGE_PROMPT_TEMPLATE.format(
            issue_id=iid, issue_description=desc,
            conversation_text=format_conversation_for_judge(d["messages"]))
        t0 = time.time()
        recs = parse_json_array(ask(prompt, "s1_%s" % iid[:60]))
        with open(STEP1, "a", encoding="utf-8") as fh:
            for r in recs:
                if not isinstance(r, dict):
                    continue
                r["issue_id"] = iid
                r.setdefault("created_at", "1970-01-01T00:00:00")
                fh.write(json.dumps(r) + "\n")
        print("  [%d/%d] %s -> %d records (%.0fs)" % (i, len(todo), iid[:55], len(recs), time.time() - t0))


def summarize() -> None:
    """Stage 2: their summary prompt, one call per issue -> the shipped schema."""
    if not STEP1.exists():
        print("no stage-1 output"); return
    groups = {}
    for line in open(STEP1, encoding="utf-8"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        groups.setdefault(r["issue_id"], []).append(r)
    done = _done_ids(STEP2)
    todo = [k for k in sorted(groups) if k not in done]
    print("stage 2: %d done, %d to do (of %d issues)" % (len(done), len(todo), len(groups)))
    for i, iid in enumerate(todo, 1):
        exps = groups[iid]
        summaries, seen = [], set()
        for e in exps:                       # their dedupe, order-preserving
            ts = (e.get("task_summary") or "").strip()
            if ts and ts not in seen:
                summaries.append(ts); seen.add(ts)
        if not summaries:
            continue
        src = CONV / ("%s_cleaned.json" % iid)
        desc = ""
        if src.exists():
            for m in json.loads(src.read_text(encoding="utf-8"))["messages"]:
                if m["role"] == "user":
                    desc = m["content"][:2000]
                    break
        out = ask(SUMMARY_PROMPT_TEMPLATE.format(
            issue_description=desc,
            task_summaries="\n".join("- %s" % s for s in summaries)), "s2_%s" % iid[:60])
        confs = [e.get("confidence") for e in exps if isinstance(e.get("confidence"), (int, float))]
        rec = {"issue_id": iid,
               "issue_description": (out or desc).strip(),
               "task_summary": summaries,
               "confidence": int(round(sum(confs) / len(confs))) if confs else 50,
               "created_at": min((e.get("created_at") or "") for e in exps) or "1970-01-01T00:00:00",
               "metadata": {"original_count": len(exps), "summarization_method": "llm",
                            "model": "claude-haiku-4-5", "source": "swe183 baseline (held out from the 30)"}}
        with open(STEP2, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
        print("  [%d/%d] %s -> %d steps, conf %d" % (i, len(todo), iid[:55], len(summaries), rec["confidence"]))


def status() -> None:
    files = list(CONV.glob("*_cleaned.json"))
    print("converted trajectories: %d" % len(files))
    print("stage 1 issues done   : %d" % len(_done_ids(STEP1)))
    print("stage 2 records       : %d  -> %s" % (len(_done_ids(STEP2)), STEP2))
    if STEP2.exists():
        import collections
        recs = [json.loads(l) for l in open(STEP2, encoding="utf-8")]
        repos = collections.Counter(r["issue_id"].split("instance_")[-1].split("-")[0] for r in recs)
        print("  repos:", dict(repos))
        leaked = [r for r in recs if r["issue_id"] in TEST_IDS]
        print("  LEAKAGE (test instances in base):", len(leaked), "<- must be 0")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "convert":
        convert()
    elif cmd == "extract":
        extract(int(sys.argv[2]) if len(sys.argv) > 2 else 0)
    elif cmd == "summarize":
        summarize()
    else:
        status()
