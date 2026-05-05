"""Per-session ephemeral state: plan.md and scratchpad.md under <ws>/.raspi/.

These files are gitignored and cleared at session start. The plan file is
injected into the system prompt below the wiki so the agent can refer to its
TODO list each turn. Scratchpad is for the model's own scratch notes within a
session — read/write but never auto-injected.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

GITIGNORE_BODY = "# raspi per-session state\n*\n!.gitignore\n"
PROMPT_PIN = "_user-task: {task}_\n_started: {ts}_\n\n"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


@dataclass
class Scratchpad:
    """Wraps <workspace>/.raspi/{plan.md,scratchpad.md}."""

    state_dir: Path
    plan_path: Path
    scratch_path: Path

    @classmethod
    def from_workspace(cls, workspace: Path) -> "Scratchpad":
        state_dir = workspace / ".raspi"
        return cls(
            state_dir=state_dir,
            plan_path=state_dir / "plan.md",
            scratch_path=state_dir / "scratchpad.md",
        )

    # ---- lifecycle ----

    def start_session(self) -> None:
        """Clear plan + scratchpad and ensure .gitignore exists."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        gi = self.state_dir / ".gitignore"
        if not gi.exists():
            gi.write_text(GITIGNORE_BODY, encoding="utf-8")
        for p in (self.plan_path, self.scratch_path):
            if p.exists():
                p.unlink()

    # ---- plan ----

    def begin_task(self, task: str) -> None:
        """Pin the user's verbatim task at the top of plan.md."""
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        header = PROMPT_PIN.format(task=task.replace("\n", " ")[:500], ts=ts)
        body = ""
        if self.plan_path.exists():
            existing = self.plan_path.read_text(encoding="utf-8")
            # Keep only the body after a previous pin, drop the old pin.
            parts = existing.split("\n\n", 1)
            body = parts[1] if len(parts) > 1 else existing
        _atomic_write(self.plan_path, header + body.strip() + ("\n" if body.strip() else ""))

    def write_plan(self, steps: list[str], note: str | None = None) -> str:
        """Replace plan body with a fresh TODO list. Pin (top of file) is preserved."""
        steps = [s.strip() for s in steps if s and s.strip()]
        if not steps:
            return "<error>plan must have at least one step</error>"
        pin = self._read_pin()
        lines = ["## Plan", ""]
        for i, step in enumerate(steps, 1):
            lines.append(f"- [ ] {i}. {step}")
        if note:
            lines.append("")
            lines.append(f"> {note.strip()}")
        body = "\n".join(lines).strip() + "\n"
        _atomic_write(self.plan_path, pin + body)
        return f"<plan steps={len(steps)} bytes={len(body)}/>"

    def render_for_prompt(self, max_chars: int) -> str:
        if not self.plan_path.exists():
            return "_(no active plan — call `todo_write` if this task needs >1 tool)_"
        text = self.plan_path.read_text(encoding="utf-8")
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "\n…\n_(plan truncated, over budget)_"

    def _read_pin(self) -> str:
        if not self.plan_path.exists():
            return ""
        text = self.plan_path.read_text(encoding="utf-8")
        # Pin is the first paragraph (non-empty lines until a blank line).
        lines: list[str] = []
        for line in text.splitlines():
            if not line.strip():
                break
            lines.append(line)
        if not lines or not lines[0].startswith("_user-task:"):
            return ""
        return "\n".join(lines) + "\n\n"

    # ---- scratchpad ----

    def write_scratch(self, text: str, append: bool = True) -> str:
        existing = ""
        if append and self.scratch_path.exists():
            existing = self.scratch_path.read_text(encoding="utf-8").rstrip() + "\n\n"
        _atomic_write(self.scratch_path, existing + text.strip() + "\n")
        return f"<scratch bytes={len(text)} append={append}/>"

    def read_scratch(self) -> str:
        if not self.scratch_path.exists():
            return ""
        return self.scratch_path.read_text(encoding="utf-8")
