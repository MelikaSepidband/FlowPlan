## Tools in this session

The native Bash, Read, Grep, Glob, Edit and Write tools are disabled. Everything goes through the pruned shell tools, exactly like a shell-only agent:

- `mcp__pruned_tools__bash` — run a shell command (find, grep, sed, cat -n, nl -ba, python, pytest, git, …) in the repository. Optional parameter `context_focus_question`. Each command runs in a fresh subshell, so directory changes and environment variables do not persist; prefix commands with `cd /path && ...` when needed.
- `mcp__pruned_tools__cat` — read a whole file. Optional parameter `context_focus_question`.
- To change files, use the bash tool: `sed -i` for line edits, `python - <<'EOF' ... EOF` scripts, or `cat > file <<'EOF' ... EOF` to rewrite a file. Read with `cat -n` or `nl -ba` first so you know the exact line numbers.

## Context Focus Question (Optional)

The `context_focus_question` is an optional field that helps filter large command outputs to show only relevant information. A pruner model reads the output together with your question and returns only the parts that help answer it; outputs of 500 characters or less are always returned in full.

**Question requirements:**
- question **MUST be a complete, self-contained question** (not keywords or phrases)
- question should be specific enough to filter effectively
- questions should **NOT** contain file-level info (e.g., filenames, line numbers), as the filter model only processes direct command output. For such info, use tools like grep/sed and leave the question blank.
- pass the question in the `context_focus_question` parameter of the same tool call

### Examples of a good context_focus_question:
- Find where the [some logic] is implemented in the [some class/function]?
- Given [some background], [some problem]?
- Locate the [some logic]?
- How can the code implement [some feature]?
- [some combination of the above, like background + origin problem + current purpose + specific attention area]

### Examples of a bad context_focus_question:
- load_raw function (too vague)
- lines 50-100 of data_loader.py (contains file-level info)
- fix the bug in rwkv6.py (too vague)

Gives some context for the query is better for more effective filtering.

**Important:** Any output that does not help answer the question will be filtered out. Use this feature judiciously and frame questions as if asking a code agent for specific information. Actually you can put some thoughts directly as background in it.

## Recommended Workflow

1. Analyze the codebase by finding and reading relevant files to figure out why this issue happens (with a `context_focus_question` for large outputs like reading a whole file).
   **HINT**: since we have the pruner enabled, prefer reading files fully instead of using grep/find first.
   **ATTENTION**: pruned code might miss some details, so it is **highly recommended** to use `cat -n` or `nl -ba` with a `context_focus_question`, so you can see the line numbers. If you looked at the filtered output and need more context, you can always use `sed` without a `context_focus_question` to read more lines before/after the relevant lines.
2. When you have enough information, edit the source code to resolve the issue; you have only one chance, no tests, no retry, so read widely first (with `context_focus_question`s) before making changes.

Example of a correct call with no question (because `ls` will not output large content, and we want to see the full output): `mcp__pruned_tools__bash` with `command: "ls -la"`.

Example of a correct call with a question: `mcp__pruned_tools__bash` with `command: "cat -n src/module.py"` and `context_focus_question: "Some question about your purpose for reading the file."`
