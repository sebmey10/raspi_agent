from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import fs, web
from .shell import ShellGate


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict], str]


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

    def _search(args: dict) -> str:
        return fs.search(
            workspace,
            args["query"],
            path=args.get("path", "."),
            max_matches=int(args.get("max_matches", 50)),
        )

    def _list_files(args: dict) -> str:
        return fs.list_files(
            workspace,
            path=args.get("path", "."),
            pattern=args.get("pattern", "*"),
            max_files=int(args.get("max_files", 200)),
        )

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
        "list_files": Tool(
            name="list_files",
            description="List workspace files by glob pattern. Use this to map a repo before reading.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "default": "."},
                    "pattern": {"type": "string", "default": "*"},
                    "max_files": {"type": "integer", "default": 200},
                },
            },
            handler=_list_files,
        ),
        "write": Tool(
            name="write",
            description="Write a file (creates or overwrites). Workspace-relative.",
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
            description="Replace the unique occurrence of `old` with `new` in a file.",
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
        "search": Tool(
            name="search",
            description="Fast literal text search in the workspace. Prefer this before broad bash grep/find.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "path": {"type": "string", "default": "."},
                    "max_matches": {"type": "integer", "default": 50},
                },
                "required": ["query"],
            },
            handler=_search,
        ),
        "bash": Tool(
            name="bash",
            description="Run a shell command in the workspace. Allowlisted commands only; "
                        "30s timeout.",
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
        "remember": Tool(
            name="remember",
            description="Append a one-line bullet to the wiki under an existing H2 heading "
                        "('User', 'Active Projects', 'Preferences', 'References', 'Notes'). "
                        "Use when learning a durable fact, not for transient state.",
            parameters={
                "type": "object",
                "properties": {
                    "heading": {"type": "string",
                                 "description": "wiki H2 heading to append under"},
                    "note": {"type": "string",
                             "description": "the bullet content (one line, no leading dash)"},
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
    out = []
    for t in tools.values():
        out.append({
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        })
    return out
