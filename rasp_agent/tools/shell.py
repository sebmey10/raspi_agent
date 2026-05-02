from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

MAX_OUTPUT = 16 * 1024
CONTROL_TOKENS = {";", "&&", "||", "|", "|&"}
DENIED_SHELL_PATTERNS = ("$(", "`", "\n", "\r")


class ShellGate:
    def __init__(self, allowlist: tuple[str, ...], timeout: int, workspace: Path,
                 confirm_callback=None):
        self.allowlist = set(allowlist)
        self.timeout = timeout
        self.workspace = workspace
        self.confirm = confirm_callback

    def _tokens(self, cmd: str) -> list[str]:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        try:
            return list(lex)
        except ValueError:
            return []

    def _split_first(self, cmd: str) -> str:
        heads = self._command_heads(cmd)
        return heads[0] if heads else ""

    def _command_heads(self, cmd: str) -> list[str]:
        tokens = self._tokens(cmd)
        if not tokens:
            return []
        heads: list[str] = []
        expect_command = True
        for tok in tokens:
            if tok in CONTROL_TOKENS:
                expect_command = True
                continue
            if tok == "&":
                heads.append("&")
                expect_command = True
                continue
            if expect_command:
                if _is_assignment(tok):
                    continue
                head = tok.rsplit("/", 1)[-1] if "/" in tok else tok
                heads.append(head)
                expect_command = False
        return heads

    def _deny_reason(self, cmd: str) -> str | None:
        if any(pattern in cmd for pattern in DENIED_SHELL_PATTERNS):
            return "command substitution or multiline shell is denied"
        if "<" in cmd or ">" in cmd:
            return "shell redirection is denied; use read/write/edit tools for files"
        heads = self._command_heads(cmd)
        if not heads:
            return "empty command"
        denied = [h for h in heads if h not in self.allowlist]
        if denied:
            return f"command(s) not in allowlist: {', '.join(denied)}"
        return None

    def is_allowed(self, cmd: str) -> bool:
        return self._deny_reason(cmd) is None

    def run(self, cmd: str) -> str:
        reason = self._deny_reason(cmd)
        if reason:
            if self.confirm is None or not self.confirm(cmd):
                return f"<error>{reason} (denied)</error>"
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


def _is_assignment(token: str) -> bool:
    if "=" not in token:
        return False
    name, _, _ = token.partition("=")
    return bool(name) and (name[0].isalpha() or name[0] == "_") and all(
        ch.isalnum() or ch == "_" for ch in name
    )
