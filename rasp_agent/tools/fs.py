from __future__ import annotations

import shutil
import subprocess
from html import escape
from pathlib import Path

MAX_READ_BYTES = 2 * 1024 * 1024
MAX_WRITE_BYTES = 1 * 1024 * 1024
MAX_SEARCH_BYTES = 512 * 1024


def _resolve_in(workspace: Path, raw: str) -> Path:
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (workspace / p)
    p = p.resolve()
    ws = workspace.resolve()
    if ws not in p.parents and p != ws:
        raise PermissionError(f"path {p} is outside workspace {ws}")
    return p


def read(workspace: Path, path: str, max_lines: int = 2000) -> str:
    p = _resolve_in(workspace, path)
    if not p.exists():
        return f"<error>file not found: {path}</error>"
    if p.is_dir():
        items = sorted(c.name + ("/" if c.is_dir() else "") for c in p.iterdir())
        return f"<dir path={path}>\n" + "\n".join(items) + "\n</dir>"
    size = p.stat().st_size
    if size > MAX_READ_BYTES:
        return f"<error>file too large ({size} bytes; cap {MAX_READ_BYTES})</error>"
    text = p.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    truncated = len(lines) > max_lines
    if truncated:
        lines = lines[:max_lines]
    body = "\n".join(f"{i+1:>5}\t{ln}" for i, ln in enumerate(lines))
    tag = f"<file path={path}{' truncated=true' if truncated else ''}>"
    return f"{tag}\n{body}\n</file>"


def write(workspace: Path, path: str, content: str) -> str:
    if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
        return f"<error>content too large (cap {MAX_WRITE_BYTES} bytes)</error>"
    p = _resolve_in(workspace, path)
    p.parent.mkdir(parents=True, exist_ok=True)
    existed = p.exists()
    p.write_text(content, encoding="utf-8")
    return f"<wrote path={path} bytes={len(content.encode('utf-8'))} new={'no' if existed else 'yes'}/>"


def edit(workspace: Path, path: str, old: str, new: str) -> str:
    p = _resolve_in(workspace, path)
    if not p.exists():
        return f"<error>file not found: {path}</error>"
    text = p.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 0:
        return f"<error>old string not found in {path}</error>"
    if count > 1:
        return f"<error>old string appears {count} times — make it unique</error>"
    p.write_text(text.replace(old, new, 1), encoding="utf-8")
    return f"<edited path={path} replaced=1/>"


def search(workspace: Path, query: str, path: str = ".", max_matches: int = 50) -> str:
    query = (query or "").strip()
    if not query:
        return "<error>empty query</error>"
    max_matches = max(1, min(int(max_matches), 200))
    p = _resolve_in(workspace, path or ".")
    if not p.exists():
        return f"<error>path not found: {path}</error>"

    if shutil.which("rg"):
        try:
            proc = subprocess.run(
                [
                    "rg",
                    "-n",
                    "--no-heading",
                    "--color",
                    "never",
                    "--fixed-strings",
                    "--",
                    query,
                    str(p),
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except subprocess.TimeoutExpired:
            return "<error>search timeout</error>"
        if proc.returncode not in (0, 1):
            err = (proc.stderr or "").strip()
            return f"<error>rg exit {proc.returncode}: {err}</error>"
        lines = (proc.stdout or "").splitlines()
        return _format_matches(workspace, query, lines[:max_matches], len(lines), max_matches)

    lines: list[str] = []
    roots = [p] if p.is_file() else p.rglob("*")
    skip_dirs = {".git", ".venv", "__pycache__", ".mypy_cache", ".pytest_cache"}
    for candidate in roots:
        if len(lines) >= max_matches:
            break
        if any(part in skip_dirs for part in candidate.parts):
            continue
        if not candidate.is_file():
            continue
        try:
            if candidate.stat().st_size > MAX_SEARCH_BYTES:
                continue
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if query in line:
                rel = _rel(workspace, candidate)
                lines.append(f"{rel}:{lineno}:{line}")
                if len(lines) >= max_matches:
                    break
    return _format_matches(workspace, query, lines, len(lines), max_matches)


def _format_matches(workspace: Path, query: str, lines: list[str], total: int, max_matches: int) -> str:
    normalized = [_normalize_match_path(workspace, line) for line in lines]
    truncated = total > max_matches
    attrs = f' query="{escape(query, quote=True)}" matches={len(normalized)}'
    if truncated:
        attrs += f" truncated=true total={total}"
    body = "\n".join(normalized)
    return f"<search{attrs}>\n{body}\n</search>"


def _normalize_match_path(workspace: Path, line: str) -> str:
    ws = str(workspace.resolve())
    if line.startswith(ws + "/"):
        return line[len(ws) + 1:]
    return line


def _rel(workspace: Path, path: Path) -> str:
    try:
        return str(path.relative_to(workspace.resolve()))
    except ValueError:
        return str(path)
