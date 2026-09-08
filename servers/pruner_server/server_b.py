"""SWE-Pruner comparator, Route B (arm baseline_prunerB): the paper's pruning
MECHANISM inside Claude Code.

The first arm (server.py) ported the authors' Claude Agent SDK demo, whose
pruner is an optional parameter described only in the tool text; it fired on
44 of 468 calls. This module instead ports the pruner client of the fork the
paper's SWE-bench numbers come from
(downstream_eval/multi_turn/swebench/mini-swe-agent--with-pruning,
src/minisweagent/utils/pruner.py + agents/default.py::_apply_pruner) and
their experiment config (templates/pruner.yaml):

  * request = {query, code, threshold=0.5, always_keep_first_frags=False,
    chunk_overlap_tokens=50}, timeout 120 s, 3 retries;
  * outputs of <= 500 characters are never pruned (min_chars gate);
  * the observation is wrapped with their exact feedback strings:
      "All outputs are judged as relevent! Output:\n" + text   (nothing removed)
      "Filtered some unrelevant parts judged by your context_focus_question,
       good try! Filtered Output:\n" + pruned_code
      "[Pruner Error]: <msg>\n\nOriginal Output:\n" + text        (service error)
  * every prune call is logged (JSONL) with the fork's pruned_stats fields.

Tool names/descriptions stay those of the authors' Claude demo (bash, cat with
an optional context_focus_question). The arm disables the native Bash, Read,
Grep and Glob tools so every observation passes through these wrappers, as in
the bash-only scaffold of the paper. Disclosed deviations: stdio MCP instead
of the in-process SDK server; bash timeout 300 s instead of the demo's 10 s.
"""
import json
import os
import subprocess
import time
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("pruned_tools")
PRUNER_URL = os.environ.get("PRUNER_URL", "http://localhost:8000/prune")
THRESHOLD = float(os.environ.get("PRUNER_THRESHOLD", "0.5"))
MIN_CHARS = int(os.environ.get("PRUNER_MIN_CHARS", "500"))
OVERLAP = int(os.environ.get("PRUNER_CHUNK_OVERLAP", "50"))
TIMEOUT = float(os.environ.get("PRUNER_TIMEOUT", "120"))
RETRIES = int(os.environ.get("PRUNER_RETRIES", "3"))
BASH_TIMEOUT = int(os.environ.get("PRUNER_BASH_TIMEOUT", "300"))
LOG_PATH = os.environ.get("PRUNER_B_LOG", "/tmp/prunerB_events.jsonl")
_client = httpx.Client(timeout=TIMEOUT)


def _log(rec: dict) -> None:
    try:
        rec["ts"] = time.time()
        rec["cwd"] = os.getcwd()
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def _apply_pruner(text: str, question: str, tool: str) -> str:
    """Their fork's PrunerClient.prune + _apply_pruner, verbatim in effect."""
    if not question or len(text) <= MIN_CHARS:
        _log({"tool": tool, "question": question, "skipped": "short" if question else "no_question",
              "chars": len(text)})
        return text
    payload = {"query": question, "code": text, "threshold": THRESHOLD,
               "always_keep_first_frags": False, "chunk_overlap_tokens": OVERLAP}
    last_error = None
    for _ in range(max(RETRIES, 1)):
        try:
            r = _client.post(PRUNER_URL, json=payload,
                             headers={"Content-Type": "application/json"})
            r.raise_for_status()
            res = r.json()
            break
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            res = None
    if res is None:
        _log({"tool": tool, "question": question, "error": str(last_error), "chars": len(text)})
        return "[Pruner Error]: %s\n\nOriginal Output:\n%s" % (last_error, text)
    origin = res.get("origin_token_cnt", 0)
    left = res.get("left_token_cnt", 0)
    _log({"tool": tool, "question": question, "chars": len(text), "score": res.get("score"),
          "origin_token_cnt": origin, "left_token_cnt": left,
          "model_input_token_cnt": res.get("model_input_token_cnt"),
          "pruned_chars": len(res.get("pruned_code") or "")})
    if left == origin:
        return "All outputs are judged as relevent! Output:\n" + text
    return ("Filtered some unrelevant parts judged by your context_focus_question, "
            "good try! Filtered Output:\n" + (res.get("pruned_code") or ""))


@mcp.tool(description=(
    "Execute bash commands, supporting automatic pruning of output content "
    "based on queries."
    "ARGS: "
    "- command (str): The command to execute"
    "- context_focus_question (str | None): The full question used to focus "
    "the context."
    "  If provided, only the output content related to this question will "
    "be returned. If left empty, the complete output will be returned."))
def bash(command: str, context_focus_question: str = None) -> str:
    try:
        result = subprocess.run(command, shell=True, capture_output=True,
                                text=True, timeout=BASH_TIMEOUT)
        output = result.stdout.strip() if result.stdout.strip() \
            else "(No output)"
    except subprocess.TimeoutExpired:
        return "Error: Command timed out after %d seconds" % BASH_TIMEOUT
    if not output or output == "(No output)":
        return output
    return _apply_pruner(output, (context_focus_question or "").strip(), "bash")


@mcp.tool(description=(
    "Read file contents, supporting automatic pruning of content based on "
    "queries."
    "ARGS: "
    "- file_path (str): The path to the file to read"
    "- context_focus_question (str | None): The full question used to focus "
    "the context."
    "  If provided, only the code parts related to this question will be "
    "returned. If left empty, the complete file content will be returned."))
def cat(file_path: str, context_focus_question: str = None) -> str:
    if not Path(file_path).is_file():
        return "Error: File not found: %s" % file_path
    content = open(file_path).read()
    return _apply_pruner(content, (context_focus_question or "").strip(), "cat")


if __name__ == "__main__":
    mcp.run()
