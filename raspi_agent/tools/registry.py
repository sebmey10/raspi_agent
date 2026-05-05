from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from . import fs, web
from . import edit_ladder
from .shell import ShellGate

ToolSource = Literal["native", "mcp"]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict], str]
    source: ToolSource = "native"
    extras: dict = field(default_factory=dict)


def build_tools(workspace: Path, shell: ShellGate) -> dict[str, Tool]:
    def _read(args: dict) -> str:
        return fs.read(
            workspace,
            args["path"],
            max_lines=int(args.get("max_lines", 2000)),
            start_line=int(args.get("start_line", 1)),
        )

    def _write(args: dict) -> str:
        return fs.write(workspace, args["path"], args["content"])

    def _edit(args: dict) -> str:
        return fs.edit(workspace, args["path"], args["old"], args["new"])

    def _grep(args: dict) -> str:
        return fs.search(
            workspace,
            args["query"],
            path=args.get("path", "."),
            max_matches=int(args.get("max_matches", 50)),
        )

    def _glob(args: dict) -> str:
        return fs.list_files(
            workspace,
            path=args.get("path", "."),
            pattern=args.get("pattern", "*"),
            max_files=int(args.get("max_files", 200)),
        )

    def _ast_edit(args: dict) -> str:
        return edit_ladder.ast_edit(
            workspace,
            args["path"],
            args.get("language", "python"),
            args.get("kind", "function"),
            args["name"],
            args["replacement"],
        )

    def _apply_patch(args: dict) -> str:
        return edit_ladder.apply_patch(workspace, args["path"], args["patch"])

    def _bash(args: dict) -> str:
        return shell.run(args["cmd"])

    def _web(args: dict) -> str:
        return web.fetch(args["url"])

    tools: dict[str, Tool] = {
        "read": Tool(
            name="read",
            description="Read a file page (or list a directory). Path is relative to workspace.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "max_lines": {"type": "integer", "default": 2000},
                    "start_line": {"type": "integer", "default": 1},
                },
                "required": ["path"],
            },
            handler=_read,
        ),
        "glob": Tool(
            name="glob",
            description="List workspace files by glob pattern. Use this to map a repo before reading.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "."},
                    "pattern": {"type": "string", "default": "*"},
                    "max_files": {"type": "integer", "default": 200},
                },
            },
            handler=_glob,
        ),
        # Back-compat alias for one release. Same handler as glob.
        "list_files": Tool(
            name="list_files",
            description="(deprecated alias for `glob`) List workspace files by glob pattern.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "."},
                    "pattern": {"type": "string", "default": "*"},
                    "max_files": {"type": "integer", "default": 200},
                },
            },
            handler=_glob,
        ),
        "write": Tool(
            name="write",
            description=(
                "Write a file (creates or overwrites). Workspace-relative. "
                "Use as last resort — prefer ast_edit / apply_patch / edit for changes."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
            handler=_write,
        ),
        "edit": Tool(
            name="edit",
            description=(
                "Replace the unique occurrence of `old` with `new` in a file. "
                "Fails if `old` matches 0 or >1 times."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                },
                "required": ["path", "old", "new"],
            },
            handler=_edit,
        ),
        "ast_edit": Tool(
            name="ast_edit",
            description=(
                "Replace a Python or JavaScript named definition (function/class/method) "
                "with new source text. Safer than `edit` for structural changes; "
                "verifies the result still parses."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "language": {"type": "string", "enum": ["python", "javascript"]},
                    "kind": {"type": "string", "enum": ["function", "class", "method"]},
                    "name": {"type": "string", "description": "identifier of the definition to replace"},
                    "replacement": {"type": "string", "description": "full new source for that node"},
                },
                "required": ["path", "language", "kind", "name", "replacement"],
            },
            handler=_ast_edit,
            extras={"available": edit_ladder.AST_AVAILABLE},
        ),
        "apply_patch": Tool(
            name="apply_patch",
            description=(
                "Apply a unified-diff patch (`@@ ... @@` hunks) to a single file. "
                "Context lines must match exactly. Use for multi-hunk edits."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "patch": {
                        "type": "string",
                        "description": "unified diff body, may contain multiple `@@` hunks",
                    },
                },
                "required": ["path", "patch"],
            },
            handler=_apply_patch,
        ),
        "grep": Tool(
            name="grep",
            description="Fast literal text search in the workspace. Prefer this over broad bash grep/find.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string", "default": "."},
                    "max_matches": {"type": "integer", "default": 50},
                },
                "required": ["query"],
            },
            handler=_grep,
        ),
        # Back-compat alias for one release. Same handler as grep.
        "search": Tool(
            name="search",
            description="(deprecated alias for `grep`) Fast literal text search in the workspace.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string", "default": "."},
                    "max_matches": {"type": "integer", "default": 50},
                },
                "required": ["query"],
            },
            handler=_grep,
        ),
        "bash": Tool(
            name="bash",
            description=(
                "Run a shell command in the workspace. Allowlisted commands only; "
                "30s timeout. Sandboxed in bubblewrap when available."
            ),
            parameters={
                "type": "object",
                "properties": {"cmd": {"type": "string"}},
                "required": ["cmd"],
            },
            handler=_bash,
        ),
        "web_fetch": Tool(
            name="web_fetch",
            description="Fetch a URL and return text content (HTML stripped).",
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
            handler=_web,
        ),
        "todo_write": Tool(
            name="todo_write",
            description=(
                "Write the agent's per-session plan (`<workspace>/.raspi/plan.md`). "
                "Call this FIRST for any task that needs more than one tool. Each "
                "step is one short imperative bullet. Replaces any previous plan."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "ordered plan steps, one imperative phrase each",
                    },
                    "note": {"type": "string", "description": "optional one-line context note"},
                },
                "required": ["steps"],
            },
            # handler is wired by Agent at build time (needs scratchpad ref).
            handler=lambda args: "<placeholder: bound at agent build time>",
        ),
        "remember": Tool(
            name="remember",
            description=(
                "Append a one-line bullet to the wiki under an existing H2 heading "
                "('User', 'Active Projects', 'Preferences', 'References', 'Cheatsheet', 'Notes'). "
                "Use when learning a durable fact, not for transient state."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "heading": {"type": "string", "description": "wiki H2 heading to append under"},
                    "note": {"type": "string", "description": "the bullet content (one line, no leading dash)"},
                },
                "required": ["heading", "note"],
            },
            handler=lambda args: "<placeholder: bound at agent build time>",
        ),
        "forget": Tool(
            name="forget",
            description="Remove the first wiki line containing `needle` (case-insensitive).",
            parameters={
                "type": "object",
                "properties": {"needle": {"type": "string"}},
                "required": ["needle"],
            },
            handler=lambda args: "<placeholder: bound at agent build time>",
        ),
    }
    return tools


def to_ollama_schema(tools: dict[str, Tool]) -> list[dict[str, Any]]:
    """Render the tool registry into Ollama's `tools` array format.

    `ast_edit` is omitted when tree-sitter wheels aren't installed so the
    model doesn't get tempted to call it.
    """
    out: list[dict[str, Any]] = []
    for t in tools.values():
        if t.name == "ast_edit" and not t.extras.get("available", True):
            continue
        out.append({
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        })
    return out
