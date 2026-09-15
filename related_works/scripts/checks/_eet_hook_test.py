"""Offline end-to-end test of EET lever B (no LLM calls): drive eet_hook.py with
a realistic PostToolUse sequence and check it reproduces their state machine --
confidence check at key steps only, strict score parsing from the transcript,
<submission_prompt> at >=81 and only once, and nothing at all when EET_ENABLED != 1."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOOK = ROOT / "experiments" / "swebench_pro" / "eet_hook.py"
PY = ROOT / ".venv" / "bin" / "python"

tmp = Path(tempfile.mkdtemp(prefix="eet_hook_"))
run_dir = tmp / "cell"; (run_dir / "eet").mkdir(parents=True)
transcript = tmp / "t.jsonl"
transcript.write_text("")


def set_assistant_text(text: str) -> None:
    with open(transcript, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "assistant", "message": {
            "content": [{"type": "text", "text": text}]}}) + "\n")


def fire(tool, tinput, out="", rc=0, enabled="1"):
    env = dict(os.environ, EET_RUN_DIR=str(run_dir), EET_ENABLED=enabled)
    payload = {"tool_name": tool, "tool_input": tinput,
               "tool_response": {"stdout": out, "returncode": rc},
               "transcript_path": str(transcript)}
    p = subprocess.run([str(PY), str(HOOK)], input=json.dumps(payload),
                       capture_output=True, text=True, env=env, timeout=60)
    return p.stdout.strip(), p.stderr.strip()


def set_thinking(text: str) -> None:
    with open(transcript, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "assistant", "message": {
            "content": [{"type": "thinking", "thinking": text}]}}) + "\n")


def kind(s):
    """Hook output must be hookSpecificOutput JSON carrying additionalContext --
    that is what the CLI appends to the tool result the model actually reads.
    Raw stdout arrives as a separate attachment the model does not answer."""
    if not s:
        return "none"
    try:
        d = json.loads(s)
        assert d["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
        ctx = d["hookSpecificOutput"]["additionalContext"]
    except Exception:
        return "RAW_STDOUT_BUG"
    if "<submission_prompt>" in ctx:
        return "SUBMISSION"
    if "<confidence_check>" in ctx:
        return "CHECK"
    return "other"


def ctx_of(s):
    return json.loads(s)["hookSpecificOutput"]["additionalContext"] if s else ""


checks = {}

# 1. non-key step -> nothing
out, _ = fire("Bash", {"command": "ls -la"})
checks["non-key step emits nothing"] = kind(out) == "none"

# 2. read-only view of a test file -> still nothing (their viewer exclusion)
out, _ = fire("Bash", {"command": "cat tests/test_foo.py"})
checks["cat of a test file is not a key step"] = kind(out) == "none"

# 3. an Edit (their sed -i equivalent) -> confidence check
out, _ = fire("Edit", {"file_path": "/repo/pkg/mod.py"})
checks["Edit triggers confidence check"] = kind(out) == "CHECK"

# 4. model answers with a low score -> no submission prompt; next key step re-asks
set_assistant_text("Making progress.\nCONFIDENCE_SCORE: 40")
out, _ = fire("Bash", {"command": "python -m pytest tests/test_mod.py", "d": 1},
              out="1 failed", rc=1)
checks["low score -> no submission prompt"] = kind(out) != "SUBMISSION"

# 5. model answers with a high score -> submission prompt, exactly once
set_assistant_text("Done and verified.\nCONFIDENCE_SCORE: 88")
out, _ = fire("Bash", {"command": "pytest -q"}, out="3 passed", rc=0)
checks["score >= 81 -> submission prompt"] = kind(out) == "SUBMISSION"
checks["submission prompt shows the score"] = "88/100" in ctx_of(out)

# their code evaluates the score of each RESPONSE; the same response is not
# re-scored, so a follow-up tool call with no new assistant text must not re-prompt
out2, _ = fire("Bash", {"command": "pytest -q"}, out="3 passed", rc=0)
checks["same response not re-scored"] = kind(out2) == "CHECK"
# but a NEW response repeating a high score prompts again -- their
# _get_submission_prompt has no once-only guard; reproduced faithfully
set_assistant_text("Still confident.\nCONFIDENCE_SCORE: 92")
out3, _ = fire("Bash", {"command": "pytest -q"}, out="3 passed", rc=0)
checks["new high-score response re-prompts (as theirs)"] = kind(out3) == "SUBMISSION"

# 5b. a score answered inside a THINKING block must count -- Claude Code models
# often answer an appended request there while going straight to the next tool call
set_thinking("Let me assess my progress.\nCONFIDENCE_SCORE: 95")
outT, _ = fire("Bash", {"command": "pytest -q"}, out="3 passed", rc=0)
checks["score in thinking block is seen"] = kind(outT) == "SUBMISSION"

# 6. disabled (no experience retrieved) -> nothing at all, their coupling
out, _ = fire("Edit", {"file_path": "/repo/pkg/mod.py"}, enabled="0")
checks["EET_ENABLED=0 emits nothing"] = kind(out) == "none"

st = json.loads((run_dir / "eet" / "state.json").read_text())
ev = [json.loads(l) for l in (run_dir / "eet" / "events.jsonl").read_text().splitlines() if l.strip()]
checks["state records a score"] = st.get("confidence_score") in (88, 92, 95)
checks["state marks test_passed"] = st.get("test_passed") is True
checks["events logged"] = len(ev) >= 3

for k, v in checks.items():
    print("%-40s %s" % (k, "OK" if v else "FAIL"))
print("\nevents:", json.dumps([{k: e[k] for k in ("step", "emitted", "score", "key_step")} for e in ev]))
print("state:", json.dumps({k: st[k] for k in ("steps", "asked", "confidence_score",
                                               "test_passed", "submission_prompted")}))
print("\nALL OK" if all(checks.values()) else "\nSOME FAILED")
sys.exit(0 if all(checks.values()) else 1)
