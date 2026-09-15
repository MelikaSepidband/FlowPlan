"""EET lever B: confidence-gated early termination, as a Claude Code PostToolUse hook.

Ports `ExperienceRetrievalAgent.get_observation` / `_is_key_step` /
`_assess_workflow_progress` / `_extract_confidence_score` / `_get_submission_prompt`
from tools/eet_src/mini-swe-agent/src/minisweagent/agents/experience_retrieval.py.

Their loop, per step:
  1. `_is_key_step(action, output)` -- true for `git add`, `sed -i`, pytest runs,
     `python ...` on a test/verify/check script (not a viewer command), or once
     workflow_progress['test_passed'] is set.
  2. If a key step and no score yet this step: append their <confidence_check>
     block to the observation, demanding `CONFIDENCE_SCORE: <n>`.
  3. When the model answers, parse with their strict regex; at >= 81 append their
     <submission_prompt> block (a soft invitation, NOT a hard stop); below 81 the
     check may be re-asked at the next key step.

Claude Code mapping: an "action" is a Bash command string (or an Edit/Write on a
source file, which is their `sed -i` equivalent); the "observation" is the tool
result, and a hook's stdout is appended to it. The agent's own next message is
where the score appears, so scores are parsed from the transcript on the
following hook invocation.

State lives in <run_dir>/eet/state.json; every trigger, score and submission
prompt is appended to <run_dir>/eet/events.jsonl.

Env: EET_RUN_DIR (required), EET_ENABLED ("1" only when an experience was
retrieved -- their coupling: no experience => plain DefaultAgent, no checks).
"""
import hashlib
import json
import os
import re
import sys
from pathlib import Path

SUBMISSION_THRESHOLD = 81           # their `submission_threshold` (:482)

# their _get_confidence_check_prompt(), verbatim (:425-470)
CONFIDENCE_CHECK_PROMPT = """
<confidence_check>
## Confidence Self-Assessment Request

You've reached a key milestone in your task. Please take a moment to assess your progress based on the **complete context** of your work so far, including:

- The issue description and requirements
- All the commands you've executed and their results
- Code changes you've made (if any)
- Tests or verification you've run (if any)
- Your understanding of the problem and solution

**Please provide your confidence assessment:**

**Confidence Score (0-100):**
Based on your understanding of the entire task context, how confident are you that the task is complete?

Consider:
- Have you addressed the core issue described in the PR?
- Are your changes appropriate and complete?
- Have you verified that your solution works correctly?
- Is the solution robust and handles edge cases?
- Have you checked that your fix logic is correct? (e.g., function parameters, return types, logic flow)

**Please respond with your confidence score using this exact format:**
```
CONFIDENCE_SCORE: <number>
```

Where `<number>` is an integer between 0 and 100.

Example: `CONFIDENCE_SCORE: 85`

After providing your score, continue with your next command as usual. The system will evaluate your score and provide guidance accordingly.
</confidence_check>
"""

# their _get_submission_prompt(), verbatim (:471-528) minus the mini-swe-agent
# specific `echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` submit command, which is
# that scaffold's exit protocol; in Claude Code the session ends when the agent
# stops, so their "To submit, execute" block is replaced by the equivalent
# instruction to stop. Everything else is their wording.
SUBMISSION_PROMPT = """
<submission_prompt>
## Ready to Submit?

Based on your confidence score of **{score}/100**, you appear to be ready for submission.

**Before submitting, please verify:**

1. **Only source code files are being submitted** (No test files, debug scripts, or temporary files)
   - Test files: `test_*.py`, `*_test.py`, `reproduce_issue*.py`
   - Debug files: `debug_*.py`
   - These files should be created in temporary locations or excluded from git

2. **Your fix logic is correct**
   - Function parameters are correct
   - Return types match expectations
   - Logic flow handles all cases

**To submit, stop working and end your turn now.** Your edits to the source files
are the submission; do not create a patch file or commit.

If you need to make more changes, continue working as usual.{files_warning}
</submission_prompt>
"""

VIEW_CMDS = ("cat", "grep", "less", "more", "head", "tail")
TEST_PATTERNS = ("test_", "_test.py", "/test", "verify", "check")


def _state_path(run_dir: Path) -> Path:
    return run_dir / "eet" / "state.json"


def load_state(run_dir: Path) -> dict:
    p = _state_path(run_dir)
    if p.exists():
        try:
            return json.loads(p.read_text("utf-8"))
        except Exception:
            pass
    # their workflow_progress dict (:92-98, :603-610)
    return {"steps": 0, "has_code_changes": False, "test_passed": False,
            "last_confidence_check": None, "confidence_score": None,
            "asked": 0, "submission_prompted": False}


def save_state(run_dir: Path, st: dict) -> None:
    p = _state_path(run_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(st, indent=2), encoding="utf-8")


def log_event(run_dir: Path, ev: dict) -> None:
    p = run_dir / "eet" / "events.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(ev) + "\n")


def extract_confidence_score(text: str):
    """Their _extract_confidence_score (:407-423): strict format only."""
    m = re.search(r"CONFIDENCE_SCORE:\s*(\d{1,3})", text or "", re.IGNORECASE)
    if not m:
        return None
    score = int(m.group(1))
    return score if 0 <= score <= 100 else None


def latest_assistant_text(transcript_path: str, after_uuid: str = "") -> list:
    """Text of every assistant message in the session transcript, oldest first.

    In mini-swe-agent the score arrives in the same response that carries the
    next command, so their code reads it off `response_content` directly. In
    Claude Code the model's text and its tool call are in the same assistant
    message, which is already written to the transcript by the time PostToolUse
    fires -- so reading the last assistant message here is the same signal.

    Both `text` and `thinking` blocks are read: Claude Code models often answer
    an appended request inside reasoning while going straight to the next tool
    call, and their code reads the whole response content, so restricting to
    visible text would miss scores their implementation would have seen.

    Returns ALL assistant messages (oldest first). Their agent evaluates every
    response as it is produced; a hook only runs on tool calls, so between two
    invocations the model may have emitted several messages -- scanning just the
    latest would drop a score answered in an earlier one (measured: scores 35
    and 55 were both missed that way).
    """
    if not transcript_path or not os.path.isfile(transcript_path):
        return []
    texts = []
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") != "assistant":
                    continue
                parts = []
                for b in (d.get("message") or {}).get("content") or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "text":
                        parts.append(b.get("text") or "")
                    elif b.get("type") == "thinking":
                        parts.append(b.get("thinking") or "")
                if parts:
                    texts.append("\n".join(parts))
    except OSError:
        return []
    return texts


def assess_workflow_progress(action: str, output_text: str, returncode: int,
                             st: dict) -> None:
    """Their _assess_workflow_progress (:268-300)."""
    a = (action or "").lower()
    o = (output_text or "").lower()
    if "git diff" in a or "git status" in a:
        if "diff --git" in (output_text or "") or "modified:" in (output_text or "") \
                or "+++" in (output_text or ""):
            st["has_code_changes"] = True
    elif "sed -i" in a:
        st["has_code_changes"] = True
    elif "git add" in a:
        st["has_code_changes"] = True

    if any(c in a for c in ("pytest", "python -m pytest")):
        if returncode == 0 and any(i in o for i in ("passed", "test passed", " passed ")):
            if "failed" not in o and "error" not in o:
                st["test_passed"] = True
    elif "python" in a and ("test" in a or "verify" in a):
        if returncode == 0 and any(i in o for i in ("passed", "success", "ok", "test passed")):
            if "failed" not in o and "error" not in o:
                st["test_passed"] = True


def is_key_step(action: str, st: dict) -> bool:
    """Their _is_key_step (:229-266). `st` must already be updated."""
    a = (action or "").lower()
    if "git add" in a:
        return True
    if "sed -i" in a:
        return True
    if any(c in a for c in ("pytest", "python -m pytest")):
        return True
    if "python" in a:
        if any(p in a for p in TEST_PATTERNS):
            if not any(v in a for v in VIEW_CMDS):
                return True
    if st.get("test_passed"):
        return True
    return False


def action_of(tool_name: str, tool_input: dict) -> str:
    """The mini-swe-agent 'action' equivalent for a Claude Code tool call.

    Bash -> the command string. Edit/Write/MultiEdit on a file -> treated as
    their `sed -i` case (an in-place source modification), which is a key step
    and sets has_code_changes.
    """
    if tool_name == "Bash":
        return tool_input.get("command") or ""
    if tool_name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return "sed -i %s" % (tool_input.get("file_path") or "")
    return ""


def main() -> int:
    if os.environ.get("EET_ENABLED") != "1":
        return 0
    run_dir = Path(os.environ.get("EET_RUN_DIR", ""))
    if not run_dir.is_dir():
        return 0
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    tool_name = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input") or {}
    tool_response = payload.get("tool_response") or {}
    out_text = ""
    if isinstance(tool_response, dict):
        out_text = str(tool_response.get("stdout") or tool_response.get("output")
                       or tool_response.get("content") or "")
        rc = tool_response.get("returncode")
        rc = 0 if rc is None else rc
    else:
        out_text = str(tool_response)
        rc = 0

    st = load_state(run_dir)
    st["steps"] = st.get("steps", 0) + 1
    action = action_of(tool_name, tool_input)
    if not action:
        save_state(run_dir, st)
        return 0

    assess_workflow_progress(action, out_text, rc, st)
    key = is_key_step(action, st)

    # Their step 1 (:559-566): did this response carry a score? Read it off the
    # latest assistant message in the session transcript. Only count a score we
    # have not already acted on (their code sets it once per response).
    # NOTE: hash() is per-process salted, so markers must be stable digests
    # (the hook is a fresh process on every tool call). Scan every assistant
    # message and take the newest score not yet acted on, so a score answered
    # in a message that is no longer the latest is still honoured exactly once.
    score = None
    seen_digests = set(st.get("scored_digests") or [])
    for msg in latest_assistant_text(payload.get("transcript_path") or ""):
        s = extract_confidence_score(msg)
        if s is None:
            continue
        digest = hashlib.sha256(msg.encode("utf-8", "replace")).hexdigest()
        if digest in seen_digests:
            continue
        score = s
        seen_digests.add(digest)
    if score is not None:
        st["scored_digests"] = sorted(seen_digests)[-50:]

    # Their get_observation steps 1-3 (:559-586), branch for branch:
    #   score present -> if >= threshold emit submission prompt AND NOT the
    #   confidence check ("Don't add confidence check prompt in the same
    #   observation"); else if key step, emit the check.
    #   score absent  -> if key step, emit the check.
    # Note their _get_submission_prompt has no once-only guard: a score >= 81
    # on a later response prompts submission again. Reproduced as-is.
    emitted = ""
    if score is not None:
        st["confidence_score"] = score
        st["last_confidence_check"] = st["steps"]
        if score >= SUBMISSION_THRESHOLD:
            emitted = SUBMISSION_PROMPT.format(score=score, files_warning="")
            st["submission_prompted"] = st.get("submission_prompted", 0) + 1 \
                if isinstance(st.get("submission_prompted"), int) else 1
        elif key:
            emitted = CONFIDENCE_CHECK_PROMPT
            st["asked"] = st.get("asked", 0) + 1
    elif key:
        emitted = CONFIDENCE_CHECK_PROMPT
        st["asked"] = st.get("asked", 0) + 1

    save_state(run_dir, st)
    if emitted:
        log_event(run_dir, {"step": st["steps"], "tool": tool_name,
                            "action": action[:300], "key_step": key,
                            "score": score, "emitted":
                            "submission" if "submission_prompt" in emitted else "confidence_check",
                            "test_passed": st["test_passed"],
                            "has_code_changes": st["has_code_changes"]})
        # Must be `additionalContext`, not raw stdout: the CLI appends this to
        # the TOOL RESULT the model reads, which is exactly their semantics
        # ("observation = observation + confidence_prompt"). Raw stdout arrives
        # as a separate attachment record that the model treats as ambient
        # context and does not answer -- measured in the first smoke, where 6
        # requests produced 0 scores.
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": emitted}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
