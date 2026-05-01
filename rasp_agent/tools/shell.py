from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

MAX_OUTPUT = 16 * 1024


class ShellGate:
    def __init__(self, allowlist: tuple[str, ...], timeout: int, workspace: Path,
                 confirm_callback=None):
        self.allowlist = set(allowlist)
        self.timeout = timeout
        self.workspace = workspace
        self.confirm = confirm_callback

    def _split_first(self, cmd: str) -> str:
        try:
            parts = shlex.split(cmd)
        except ValueError:
            return ""
        if not parts:
            return ""
        first = parts[0]
        if "/" in first:
            first = first.rsplit("/", 1)[-1]
        return first

    def is_allowed(self, cmd: str) -> bool:
        head = self._split_first(cmd)
        if not head:
            return False
        if head in self.allowlist:
            return True
        return False

    def run(self, cmd: str) -> str:
        head = self._split_first(cmd)
        if not head:
            return "<error>empty command</error>"
        if head not in self.allowlist:
            if self.confirm is None or not self.confirm(cmd):
                return f"<error>command '{head}' not in allowlist (denied)</error>"
        self.workspace.mkdir(parents=True, exist_ok=True)
        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                cwd=self.workspace,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            return f"<error>timeout after {self.timeout}s</error>"
        out = (proc.stdout or "")
        err = (proc.stderr or "")
        combined = out
        if err:
            combined += ("\n--- stderr ---\n" + err) if combined else err
        if len(combined) > MAX_OUTPUT:
            combined = combined[:MAX_OUTPUT] + f"\n<truncated at {MAX_OUTPUT} bytes>"
        return f"<sh exit={proc.returncode}>\n{combined}\n</sh>"
