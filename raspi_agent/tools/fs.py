from __future__ import annotations

import selectors
import shutil
import subprocess
import time
from html import escape
from pathlib import Path

MAX_READ_BYTES = 2 * 1024 * 1024
MAX_WRITE_BYTES = 1 * 1024 * 1024
MAX_SEARCH_BYTES = 512 * 1024
SKIP_DIRS = {".git", ".venv", "__pycache__", ".mypy_cache", ".pytest_cache", "node_modules"}


def _resolve_in(workspace: Path, raw: str) -> Path:
    p = Path(raw).expanduser()
    if not p.is_absolute():
        p = (workspace / p)
    p = p.resolve()
    ws = workspace.resolve()
    if ws not in p.parents and p != ws:
        raise PermissionError(f"path {p} is outside workspace {ws}")
    return p


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def read(workspace: Path, path: str, max_lines: int = 2000, start_line: int = 1) -> str:
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
    max_lines = max(1, min(int(max_lines), 5000))
    start_line = max(1, int(start_line))
    start_idx = start_line - 1
    selected = lines[start_idx:start_idx + max_lines]
    truncated = start_idx > 0 or start_idx + len(selected) < len(lines)
    body = "\n".join(f"{i:>5}\t{ln}" for i, ln in enumerate(selected, start=start_line))
    tag = f"<file path={path} start_line={start_line} total_lines={len(lines)}"
    if truncated:
        tag += " truncated=true"
    tag += ">"
    return f"{tag}\n{body}\n</file>"


def write(workspace: Path, path: str, content: str) -> str:
    if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
        return f"<error>content too large (cap {MAX_WRITE_BYTES} bytes)</error>"
    p = _resolve_in(workspace, path)
    existed = p.exists()
    _atomic_write(p, content)
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
    _atomic_write(p, text.replace(old, new, 1))
    return f"<edited path={path} replaced=1/>"


def list_files(workspace: Path, path: str = ".", pattern: str = "*", max_files: int = 200) -> str:
    p = _resolve_in(workspace, path or ".")
    if not p.exists():
        return f"<error>path not found: {path}</error>"
    max_files = max(1, min(int(max_files), 1000))
    matches: list[str] = []
    iterator = [p] if p.is_file() else p.rglob(pattern or "*")
    for candidate in iterator:
        if any(part in SKIP_DIRS for part in candidate.parts):
            continue
        if not candidate.is_file():
            continue
        matches.append(_rel(workspace, candidate))
        if len(matches) > max_files:
            break
    matches = sorted(matches)
    truncated = len(matches) > max_files
    shown = matches[:max_files]
    attrs = f' path="{escape(path or ".", quote=True)}" pattern="{escape(pattern or "*", quote=True)}"'
    attrs += f" matches={len(shown)}"
    if truncated:
        attrs += " truncated=true"
    return f"<files{attrs}>\n" + "\n".join(shown) + "\n</files>"


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
            lines, truncated, returncode, stderr = _rg_limited(query, p, max_matches)
        except subprocess.TimeoutExpired:
            return "<error>search timeout</error>"
        if returncode not in (0, 1, -15):
            return f"<error>rg exit {returncode}: {stderr.strip()}</error>"
        return _format_matches(workspace, query, lines[:max_matches], truncated)

    lines: list[str] = []
    roots = [p] if p.is_file() else p.rglob("*")
    truncated = False
    for candidate in roots:
        if len(lines) > max_matches:
            truncated = True
            break
        if any(part in SKIP_DIRS for part in candidate.parts):
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
                if len(lines) > max_matches:
                    truncated = True
                    break
    return _format_matches(workspace, query, lines[:max_matches], truncated)


def _rg_limited(query: str, path: Path, max_matches: int) -> tuple[list[str], bool, int, str]:
    proc = subprocess.Popen(
        [
            "rg",
            "-n",
            "--no-heading",
            "--color",
            "never",
            "--fixed-strings",
            "--max-filesize",
            str(MAX_SEARCH_BYTES),
            "--",
            query,
            str(path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stderr is not None
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    lines: list[str] = []
    truncated = False
    deadline = time.monotonic() + 15
    try:
        while True:
            if time.monotonic() > deadline:
                proc.kill()
                raise subprocess.TimeoutExpired(proc.args, 15)
            if proc.poll() is not None:
                rest = proc.stdout.read()
                if rest:
                    lines.extend(rest.splitlines())
                    if len(lines) > max_matches:
                        truncated = True
                        lines = lines[:max_matches + 1]
                break
            for key, _ in sel.select(timeout=0.05):
                line = key.fileobj.readline()
                if not line:
                    continue
                lines.append(line.rstrip("\n"))
                if len(lines) > max_matches:
                    truncated = True
                    proc.terminate()
                    break
            if truncated:
                break
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=1)
    finally:
        sel.close()
    stderr = proc.stderr.read()
    return lines, truncated, proc.returncode if proc.returncode is not None else -15, stderr


def _format_matches(
    workspace: Path,
    query: str,
    lines: list[str],
    truncated: bool,
) -> str:
    normalized = [_normalize_match_path(workspace, line) for line in lines]
    attrs = f' query="{escape(query, quote=True)}" matches={len(normalized)}'
    if truncated:
        attrs += " truncated=true"
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
