from __future__ import annotations

from pathlib import Path

MAX_READ_BYTES = 2 * 1024 * 1024
MAX_WRITE_BYTES = 1 * 1024 * 1024


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
    text = p.read_text(errors="replace")
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
    p.write_text(content)
    return f"<wrote path={path} bytes={len(content.encode('utf-8'))} new={'no' if existed else 'yes'}/>"


def edit(workspace: Path, path: str, old: str, new: str) -> str:
    p = _resolve_in(workspace, path)
    if not p.exists():
        return f"<error>file not found: {path}</error>"
    text = p.read_text()
    count = text.count(old)
    if count == 0:
        return f"<error>old string not found in {path}</error>"
    if count > 1:
        return f"<error>old string appears {count} times — make it unique</error>"
    p.write_text(text.replace(old, new, 1))
    return f"<edited path={path} replaced=1/>"
