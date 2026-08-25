"""Client tools — bash and memory.

Counterpart to builtin-tools.py. Those tools ran on Anthropic's servers and the
result came back in the same response. These two are Anthropic-*defined* but
client-*executed*: the model emits a tool_use block, your process does the work,
and you ship a tool_result back. That round trip is the agent loop.

Both are schema-less. You declare them by type and name only — the input schema
is baked into the model. Passing an input_schema, or hand-rolling a custom tool
named "bash", gets you an ordinary user-defined tool without the trained-in
behavior.

Part 1 writes the loop by hand. Part 2 lets the SDK's tool runner drive it.
"""

import shlex
import subprocess
from pathlib import Path, PurePosixPath

import anthropic
from anthropic.lib.tools import BetaAbstractMemoryTool, ToolError

client = anthropic.Anthropic()
MODEL = "claude-opus-5"

# =============================================================================
# Part 1: bash — a hand-written agent loop
# =============================================================================

BASH_TOOL = {"type": "bash_20250124", "name": "bash"}

# Every command is untrusted model output. Allowlist the executables you accept
# and refuse shell metacharacters; a blocklist of "bad commands" is not
# sufficient. Anything beyond a demo also belongs in a container or VM with
# resource limits and an audit log.
ALLOWED_BINARIES = {"cat", "date", "echo", "head", "ls", "uname", "wc"}
SHELL_OPERATORS = ("&&", "||", "|", ";", ">", "<", "`", "$(", "\n")


def run_bash(tool_input: dict) -> tuple[str, bool]:
    """Execute one bash tool_use. Returns (result_text, is_error)."""
    # input is either {"command": "..."} or {"restart": true}. Check restart
    # first — that variant carries no "command" key at all.
    if tool_input.get("restart"):
        return "Shell session restarted.", False

    command = tool_input.get("command", "")
    if any(op in command for op in SHELL_OPERATORS):
        return f"Refused: shell operators are not permitted in {command!r}.", True

    argv = shlex.split(command)
    if not argv or argv[0] not in ALLOWED_BINARIES:
        offender = argv[0] if argv else "(empty command)"
        allowed = ", ".join(sorted(ALLOWED_BINARIES))
        return f"Refused: {offender} is not on the allowlist ({allowed}).", True

    print(f"  [bash] $ {command}")
    try:
        # shell=False, so argv goes straight to execve and there is no shell
        # left to reinterpret anything that slipped past the checks above.
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return "Error: command timed out after 10s.", True
    except FileNotFoundError:
        return f"Error: {argv[0]} not found on this system.", True

    # Claude expects stdout and stderr combined, the way a terminal shows them.
    output = proc.stdout + proc.stderr
    return output or "(no output)", proc.returncode != 0


def bash_demo() -> None:
    print("=== bash (manual loop) ===")
    messages = [
        {
            "role": "user",
            "content": "How many Python files are in the current directory, "
            "and what are they called? Use bash to find out.",
        }
    ]

    while True:
        response = client.messages.create(
            model=MODEL,
            max_tokens=16000,
            tools=[BASH_TOOL],
            messages=messages,
        )

        # Append the entire content list. Extracting just the text and
        # appending that drops the tool_use blocks (and any thinking blocks —
        # Opus 5 thinks by default), which corrupts the next turn's history.
        messages.append({"role": "assistant", "content": response.content})

        # end_turn, max_tokens and refusal all mean "stop"; only tool_use means
        # the model is waiting on us.
        if response.stop_reason != "tool_use":
            break

        # Claude can request several commands in one turn. Every tool_use needs
        # a matching tool_result, and they all go back in a SINGLE user message
        # — splitting them across messages teaches it to stop batching calls.
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            text, is_error = run_bash(block.input)
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,  # must match the tool_use id
                    "content": text,
                    "is_error": is_error,  # let Claude see failures and adapt
                }
            )
        messages.append({"role": "user", "content": results})

    if response.stop_reason == "refusal":
        print(f"Refused: {response.stop_details}")
    for block in response.content:
        if block.type == "text":
            print(block.text)


# =============================================================================
# Part 2: memory — the same contract, with the loop handled by the SDK
# =============================================================================

# Claude addresses memory as files under /memories. Where that actually lives is
# entirely your choice — this maps it onto a local directory, but a database or
# an encrypted per-user bucket works the same way.
MEMORY_ROOT = Path("./claude_memories").resolve()

MEMORY_TOOL = {"type": "memory_20250818", "name": "memory"}  # the raw declaration


class FileMemoryTool(BetaAbstractMemoryTool):
    """A filesystem memory backend.

    Subclassing BetaAbstractMemoryTool means the SDK declares the tool
    (`to_dict()` emits MEMORY_TOOL above), parses each command into a typed
    object, and dispatches to the six methods below — view, create,
    str_replace, insert, delete, rename.

    Never persist secrets here, and note there is no access control: a
    multi-user system needs one root per authenticated user.
    """

    def _resolve(self, path: str) -> Path:
        """Map a model-supplied /memories/... path into MEMORY_ROOT, safely."""
        # path is untrusted. "../../.ssh/id_rsa", an absolute path, or a
        # symlink must not escape the root — so resolve first, then verify
        # containment. Never hand the raw string to open() or unlink().
        posix = PurePosixPath(path)
        if posix.is_absolute():
            posix = posix.relative_to("/")
        if posix.parts and posix.parts[0] == "memories":
            posix = posix.relative_to("memories")

        resolved = (MEMORY_ROOT / posix).resolve()
        if resolved != MEMORY_ROOT and not resolved.is_relative_to(MEMORY_ROOT):
            # ToolError returns is_error: True to Claude instead of crashing the
            # loop, so it can correct itself and retry.
            raise ToolError(f"Refused: {path!r} escapes the memory directory.")
        return resolved

    def view(self, command) -> str:
        target = self._resolve(command.path)
        if target.is_dir():
            entries = sorted(p.name for p in target.iterdir())
            return f"Directory {command.path}:\n" + ("\n".join(entries) or "(empty)")
        if not target.exists():
            return f"{command.path} does not exist yet."

        lines = target.read_text().splitlines()
        if command.view_range:
            start, end = command.view_range  # 1-indexed, inclusive; -1 = EOF
            lines = lines[start - 1 : None if end == -1 else end]
        # Numbered lines give Claude the line references insert needs.
        return "\n".join(f"{i:>4}  {line}" for i, line in enumerate(lines, 1))

    def create(self, command) -> str:
        target = self._resolve(command.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(command.file_text)
        print(f"  [memory] wrote {command.path}")
        return f"Created {command.path}."

    def str_replace(self, command) -> str:
        target = self._resolve(command.path)
        content = target.read_text()
        # Match the built-in editor semantics: exactly one occurrence, or error.
        found = content.count(command.old_str)
        if found != 1:
            raise ToolError(
                f"Found {found} occurrences of that text in {command.path}; "
                "expected exactly 1. Use a longer, unique snippet."
            )
        target.write_text(content.replace(command.old_str, command.new_str))
        print(f"  [memory] edited {command.path}")
        return f"Replaced text in {command.path}."

    def insert(self, command) -> str:
        target = self._resolve(command.path)
        lines = target.read_text().splitlines(keepends=True)
        # insert_line is the line to insert *after*; 0 means top of file.
        lines.insert(command.insert_line, command.insert_text.rstrip("\n") + "\n")
        target.write_text("".join(lines))
        return f"Inserted text into {command.path} at line {command.insert_line}."

    def delete(self, command) -> str:
        target = self._resolve(command.path)
        if target.is_dir():
            raise ToolError(f"{command.path} is a directory; delete the files in it.")
        target.unlink(missing_ok=True)
        print(f"  [memory] deleted {command.path}")
        return f"Deleted {command.path}."

    def rename(self, command) -> str:
        source = self._resolve(command.old_path)
        destination = self._resolve(command.new_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        source.rename(destination)
        return f"Renamed {command.old_path} to {command.new_path}."


def memory_demo() -> None:
    print("\n=== memory (SDK tool runner) ===")
    MEMORY_ROOT.mkdir(parents=True, exist_ok=True)
    memory = FileMemoryTool()

    conversations = [
        "Remember that I deploy with `make ship` and that I dislike emoji in commit messages.",
        "What do you already know about how I work?",
    ]

    for prompt in conversations:
        print(f"\n> {prompt}")
        # Two separate conversations — no shared messages list, no history
        # passed between them. The only thing that survives is what Claude
        # wrote under MEMORY_ROOT, which is the whole point of the tool.
        #
        # tool_runner replaces the while loop from Part 1: it calls the API,
        # runs the requested command through FileMemoryTool, feeds the result
        # back, and repeats. until_done() returns the final message.
        message = client.beta.messages.tool_runner(
            model=MODEL,
            max_tokens=16000,
            system="Check your memory directory before answering, and record "
            "durable facts about the user's preferences as you learn them.",
            tools=[memory],  # the object, not MEMORY_TOOL — the SDK declares it
            max_iterations=10,  # bound the loop
            messages=[{"role": "user", "content": prompt}],
        ).until_done()

        for block in message.content:
            if block.type == "text":
                print(block.text)


if __name__ == "__main__":
    bash_demo()
    memory_demo()
