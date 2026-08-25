# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Teaching examples for Anthropic's built-in tools, split along the line that matters — where the tool actually executes.

- `builtin-tools.py` — **server-side tools.** Two independent, unrelated `messages.create()` calls: one with `web_search`, one with `code_execution`.
- `client-tools.py` — **client-side tools**, `bash` and `memory`. Anthropic-defined and schema-less, but executed in your process, so they need a real agent loop. Part 1 hand-writes the loop for `bash`; Part 2 hands `memory` to the SDK's `client.beta.messages.tool_runner`.

## Running

```bash
python builtin-tools.py
python client-tools.py   # writes ./claude_memories/ (gitignored)
```

Requires `anthropic` (installed: 0.84.0, i.e. the 0.x line — not 1.x) and credentials in the environment (`ANTHROPIC_API_KEY`, or an `ant auth login` profile). There is no test suite, linter config, or package manifest; this directory is untracked inside the git repo rooted at `../` (`/Users/dpetersonpebblepost.com/projects`).

## The point `builtin-tools.py` is making

Server tools have **no agent loop**. Nothing switches on `stop_reason`, and no `tool_result` is ever pushed back into `messages` — Anthropic runs the tool and the single response already contains both the tool call and its output. Contrast with client-defined tools, where you (or the SDK's tool runner) own the loop.

Consequence for reading responses: iterate `response.content` and branch on `block.type`, expecting server-tool block types alongside the usual `text`:

- `server_tool_use` — the tool invocation (`.name`, `.input`)
- `web_search_tool_result` — `.content` is a **list** of results on success, but a single **error object** (e.g. `{error_code: "max_uses_exceeded"}`) on failure. Server-tool errors return HTTP 200; they do not raise. Branch on the shape before indexing.
- `bash_code_execution_tool_result` — `.content.stdout` / `.stderr` / `.return_code`

## The point `client-tools.py` is making

Same declaration style — `{"type": ..., "name": ...}` with **no `input_schema`** — but the mirror image at runtime: Claude emits a `tool_use` block and blocks until you send a `tool_result` with a matching `tool_use_id`. Both tools' schemas are baked into the model; a custom tool named `"bash"` is a *different* tool with none of the trained-in behavior.

- `bash_20250124` — input is either `{"command": ...}` or `{"restart": true}`; check `restart` first, it has no `command` key. Commands are untrusted model output: the file allowlists executables, rejects shell operators, runs with `shell=False` and a timeout. A blocklist is not sufficient.
- `memory_20250818` — six commands (`view`, `create`, `str_replace`, `insert`, `delete`, `rename`) over a `/memories` tree whose real storage you choose. Model-supplied paths are untrusted: resolve, then check containment (`Path.is_relative_to`) before touching the filesystem. `anthropic.lib.tools.BetaAbstractMemoryTool` declares the tool, parses commands into typed objects, and dispatches to those six methods; raise `ToolError` to return `is_error: True` instead of crashing the loop.

Whichever loop you use, append the **whole** `response.content` to history — keeping only the text drops `tool_use` and thinking blocks and corrupts the next turn. Multiple `tool_result` blocks for one turn go back in a single user message.

## Editing these files

The tool `type` strings are dated version identifiers, and the correct one depends on the model. Current values:

| Tool | Current `type` | Notes |
|---|---|---|
| `web_search` | `web_search_20260209` | What `builtin-tools.py` uses. Dynamic filtering built in — do **not** also declare `code_execution` in the same `tools` array. |
| `code_execution` | `code_execution_20260521` | `builtin-tools.py` uses the older `code_execution_20260120`. The newer variant still returns `bash_code_execution_tool_result`. |
| `bash` | `bash_20250124` | Current. |
| `memory` | `memory_20250818` | Current. |

`builtin-tools.py` pins `claude-opus-4-8` and `max_tokens=1024` — both demo-scale. `client-tools.py` uses `claude-opus-5` and `max_tokens=16000`, which is the right default for new non-streaming code here.

Invoke the `claude-api` skill before changing model IDs, tool version strings, or thinking/effort parameters — the dated identifiers drift and are not safe to write from memory.
