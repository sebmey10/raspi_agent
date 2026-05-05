from __future__ import annotations

import ipaddress
import os
import shlex
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse

MAX_OUTPUT = 16 * 1024
CONTROL_TOKENS = {";", "&&", "||", "|", "|&"}
DENIED_SHELL_PATTERNS = ("$(", "`", "\n", "\r")
PATHY_COMMANDS = {"cat", "head", "tail", "wc", "grep", "rg", "find", "tree", "sed", "awk", "stat", "file"}
PACKAGE_COMMANDS = {"pip", "npm", "pnpm", "yarn"}
DESTRUCTIVE_GIT = {"clean", "reset", "checkout", "restore", "rebase"}
NET_COMMANDS = {"curl", "wget", "git"}  # need --share-net under bwrap


class ShellGate:
    def __init__(self, allowlist: tuple[str, ...], timeout: int, workspace: Path,
                 confirm_callback=None, sandbox_mode: str = "auto"):
        self.allowlist = set(allowlist)
        self.timeout = timeout
        self.workspace = workspace
        self.confirm = confirm_callback
        self.sandbox_mode = sandbox_mode  # "auto" | "on" | "off"
        self._bwrap_path = self._detect_bwrap()

    def _detect_bwrap(self) -> str | None:
        if self.sandbox_mode == "off":
            return None
        path = shutil.which("bwrap")
        if path is None and self.sandbox_mode == "on":
            return "bwrap"
        return path

    def sandbox_active(self) -> bool:
        return self._bwrap_path is not None

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
        tokens = self._tokens(cmd)
        heads = self._command_heads(cmd)
        if not heads:
            return "empty command"
        denied = [h for h in heads if h not in self.allowlist]
        if denied:
            return f"command(s) not in allowlist: {', '.join(denied)}"
        risky = self._risky_reason(tokens)
        if risky:
            return risky
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
            run_args = self._tokens(cmd)
            use_shell = _needs_shell(run_args)
            invocation, sh_flag = self._wrap_for_sandbox(cmd, run_args, use_shell)
            proc = subprocess.run(
                invocation,
                shell=sh_flag,
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

    def _wrap_for_sandbox(self, cmd: str, run_args: list[str], use_shell: bool):
        """Return (invocation, shell_flag) for subprocess.run.

        Without bwrap: pass through (existing behaviour). With bwrap: build a
        bubblewrap invocation that read-only-binds /, rw-binds the workspace,
        unshares network by default, and routes shell-feature commands through
        /bin/sh -c inside the sandbox.
        """
        if not self.sandbox_active():
            return (cmd if use_shell else run_args, use_shell)
        bwrap_args = self._bwrap_args(run_args)
        inner = ["/bin/sh", "-c", cmd] if use_shell else run_args
        return ([self._bwrap_path, *bwrap_args, *inner], False)

    def _bwrap_args(self, run_args: list[str]) -> list[str]:
        ws = str(self.workspace.resolve())
        args = [
            "--ro-bind", "/", "/",
            "--bind", ws, ws,
            "--proc", "/proc",
            "--dev", "/dev",
            "--tmpfs", "/tmp",
            "--die-with-parent",
        ]
        gitconfig = Path.home() / ".gitconfig"
        if gitconfig.exists():
            args += ["--ro-bind", str(gitconfig), str(gitconfig)]
        head = run_args[0] if run_args else ""
        head_basename = head.rsplit("/", 1)[-1] if "/" in head else head
        if head_basename in NET_COMMANDS:
            args += ["--share-net"]
        else:
            args += ["--unshare-net"]
        for var in ("HOME", "PATH", "LANG", "LC_ALL", "TERM", "USER"):
            v = os.environ.get(var)
            if v is not None:
                args += ["--setenv", var, v]
        return args

    def _risky_reason(self, tokens: list[str]) -> str | None:
        command: str | None = None
        args: list[str] = []

        def flush() -> str | None:
            if not command:
                return None
            return self._risky_command_reason(command, args)

        for tok in tokens:
            if tok in CONTROL_TOKENS or tok == "&":
                reason = flush()
                if reason:
                    return reason
                command = None
                args = []
                continue
            if command is None:
                if _is_assignment(tok):
                    continue
                command = tok.rsplit("/", 1)[-1] if "/" in tok else tok
            else:
                args.append(tok)
        return flush()

    def _risky_command_reason(self, command: str, args: list[str]) -> str | None:
        if command in {"python", "python3"} and "-c" in args:
            return "inline Python is denied; write a script inside the workspace instead"
        if command in PACKAGE_COMMANDS and any(a in {"install", "uninstall", "update", "add", "remove"} for a in args):
            return f"{command} package mutation requires confirmation"
        if command == "git" and args:
            subcmd = next((a for a in args if not a.startswith("-")), "")
            if subcmd in DESTRUCTIVE_GIT:
                return f"git {subcmd} requires confirmation"
        if command == "curl":
            for arg in args:
                if self._curl_target_is_local(arg):
                    return f"curl local/private target requires confirmation: {arg}"
        if command in PATHY_COMMANDS or command in {"python", "python3", "node"}:
            for arg in args:
                if self._path_arg_escapes_workspace(arg):
                    return f"path argument escapes workspace: {arg}"
        return None

    def _path_arg_escapes_workspace(self, arg: str) -> bool:
        if not _looks_like_path(arg):
            return False
        raw = Path(arg).expanduser()
        p = raw if raw.is_absolute() else self.workspace / raw
        try:
            resolved = p.resolve()
            ws = self.workspace.resolve()
        except OSError:
            return False
        return resolved != ws and ws not in resolved.parents

    def _curl_target_is_local(self, arg: str) -> bool:
        if arg.startswith("-"):
            return False
        u = urlparse(arg)
        if u.scheme == "file":
            return True
        host = (u.hostname or "").lower()
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
            return True
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return False
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast


def _is_assignment(token: str) -> bool:
    if "=" not in token:
        return False
    name, _, _ = token.partition("=")
    return bool(name) and (name[0].isalpha() or name[0] == "_") and all(
        ch.isalnum() or ch == "_" for ch in name
    )


def _needs_shell(tokens: list[str]) -> bool:
    if any(tok in CONTROL_TOKENS or tok == "&" for tok in tokens):
        return True
    if tokens and _is_assignment(tokens[0]):
        return True
    return any(any(ch in tok for ch in "*?[]{}~") for tok in tokens)


def _looks_like_path(token: str) -> bool:
    if not token or token.startswith("-") or "://" in token:
        return False
    if token in {".", ".."}:
        return True
    return token.startswith(("/", "~/", "./", "../")) or "/" in token
