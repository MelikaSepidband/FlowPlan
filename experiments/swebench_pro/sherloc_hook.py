"""SHERLOC self-recovery hook (arm baseline_sherloc; arXiv 2606.24820).

Their exploration loop (recipes/sherloc/inference/sherloc.py) does two things
between model steps that Claude Code's loop does not: it detects a repeated
identical tool call and injects an intervention message
(ContextManager.inject_loop_intervention, threshold 3), and on the last
allowed step it injects the instruction to commit to <findings> +
<locations> (ContextManager.inject_final_turn_instruction, total_steps=20).
This PostToolUse hook, matched on the four mcp__sherloc__* tools, returns the
same texts as `additionalContext`, which the CLI appends to the tool result
the model reads next -- the same mechanism as the "More with Less" reminder
hook. The intervention wording is theirs verbatim; the final-turn text is
theirs with the <think>/<tool_call> tag rules replaced by the native-tool
equivalents (the model calls tools natively here).

Step accounting: their step = one model reply; the final-turn instruction is
injected before step total_steps, i.e. after total_steps-1 tool calls. A call
is identified as in their detect_repetitive_tool_calls: the JSON of
{tool, **arguments} with sorted keys; a call seen >= threshold times over the
whole episode triggers the intervention.

Fails open (any error -> no output). Every call is logged to
RUN_DIR/sherloc_calls.jsonl for audit. Config (env): RUN_DIR (required),
SHERLOC_TOTAL_STEPS (default 20), SHERLOC_LOOP_THRESHOLD (default 3).
"""
import json
import os
import sys

FINAL_TURN_TEXT = (
    "\n\n**Interaction protocol update**\n"
    "You have reached the maximum number of tool calls. You **must** now reply with `<findings>` and `<locations>` blocks.\n\n"
    "**Strict rules for this final response:**\n"
    "- Your message must contain EXACTLY `<findings>...</findings>` followed by `<locations>...</locations>`\n"
    "- **DO NOT** call any more tools\n"
    "- **DO NOT** include any text outside the required tags\n"
    "- In `<findings>`, provide bullet points explaining why each location needs modification\n"
    "- In `<locations>`, emit **every** file and line range that needs editing\n"
    "- If uncertain about exact lines, include your best assessment\n\n"
    "**Output format:**\n"
    "```xml\n"
    "<findings>\n"
    "• Location explanation: Why this specific location requires modification\n"
    "• Root cause: What the underlying issue is\n"
    "• Solution idea: How the fix should be approached (without showing code)\n"
    "</findings>\n"
    "\n"
    "<locations>\n"
    "path/to/file.py:L<start>-L<end>\n"
    "another/file.rs:L<start>-L<end>\n"
    "</locations>\n"
    "```\n\n"
)


def _intervention(tool: str, params: dict, reps: int) -> str:
    if tool == "view_file":
        file_path = params.get("path", "unknown")
        return (
            "SYSTEM INTERVENTION: Loop detected! You have attempted to view '%s' %d times with the same parameters.\n\n"
            "The file appears to be too large or the output is being truncated. Please try a different approach:\n"
            "1. View a specific section using line numbers (e.g., view_range: [1000, 1200])\n"
            "2. Search for specific content with codebase_search\n"
            "3. Inspect the repository structure with repo_tree\n"
            "4. Inspect related imports with connected_tree\n\n"
            "DO NOT repeat the same view_file command. Think of an alternative strategy."
            % (file_path, reps))
    return (
        "SYSTEM INTERVENTION: Loop detected! You have repeated the same %s command %d times.\n\n"
        "This suggests the current approach isn't working. Please:\n"
        "1. Analyze why the previous attempts didn't provide useful information\n"
        "2. Try a completely different tool or approach\n"
        "3. Break down the problem into smaller steps\n"
        "4. Consider if you're looking in the wrong place\n\n"
        "DO NOT repeat the same command. Think of an alternative strategy."
        % (tool, reps))


def main():
    try:
        payload = json.load(sys.stdin)
        name = payload.get("tool_name") or ""
        if not name.startswith("mcp__sherloc__"):
            return
        run_dir = os.environ["RUN_DIR"]
        total_steps = int(os.environ.get("SHERLOC_TOTAL_STEPS", "20"))
        threshold = int(os.environ.get("SHERLOC_LOOP_THRESHOLD", "3"))
        tool = name.split("__", 2)[-1]
        params = payload.get("tool_input") or {}
        call = json.dumps({"tool": tool, **params}, sort_keys=True)
        log = os.path.join(run_dir, "sherloc_calls.jsonl")
        history = []
        if os.path.exists(log):
            with open(log, encoding="utf-8") as f:
                for line in f:
                    try:
                        history.append(json.loads(line)["call"])
                    except Exception:
                        pass
        history.append(call)
        n = len(history)
        reps = history.count(call)
        texts = []
        if reps >= threshold:
            texts.append(_intervention(tool, params, reps))
        if n >= total_steps - 1:
            texts.append(FINAL_TURN_TEXT)
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps({"n": n, "call": call, "reps": reps,
                                "injected": [t[:24] for t in texts]}) + "\n")
        if texts:
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": "\n\n".join(texts)}}))
    except Exception:
        return


if __name__ == "__main__":
    main()
