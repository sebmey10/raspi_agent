"""Per-session ephemeral state: plan.md and scratchpad.md under <ws>/.raspi/.

These files are gitignored and cleared at session start. The plan file is
injected into the system prompt below the wiki so the agent can refer to its
TODO list each turn. Scratchpad is for the model's own scratch notes within a
session. `context.md` is a compact task-state ledger and is injected alongside
the plan so long sessions survive compaction without losing the plot.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

GITIGNORE_BODY = "# raspi per-session state\n*\n!.gitignore\n"
PROMPT_PIN = "_user-task: {task}_\n_started: {ts}_\n\n"
CONTEXT_SECTIONS = (
    "Task",
    "Constraints",
    "Files Inspected",
    "Decisions",
    "Failed Attempts",
    "Next Steps",
)


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
    context_path: Path

    @classmethod
    def from_workspace(cls, workspace: Path) -> "Scratchpad":
        state_dir = workspace / ".raspi"
        return cls(
            state_dir=state_dir,
            plan_path=state_dir / "plan.md",
            scratch_path=state_dir / "scratchpad.md",
            context_path=state_dir / "context.md",
        )

    # ---- lifecycle ----

    def prepare(self) -> None:
        """Ensure the state directory exists without clearing existing state."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        gi = self.state_dir / ".gitignore"
        if not gi.exists():
            gi.write_text(GITIGNORE_BODY, encoding="utf-8")

    def start_session(self, preserve_context: bool = False) -> None:
        """Clear per-session state and ensure .gitignore exists."""
        self.prepare()
        paths = (self.plan_path, self.scratch_path)
        if not preserve_context:
            paths = (*paths, self.context_path)
        for p in paths:
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

    def update_plan_step(self, step: int, status: str, note: str | None = None) -> str:
        if not self.plan_path.exists():
            return "<error>no active plan</error>"
        status = status.strip().lower()
        marks = {"pending": " ", "doing": ">", "done": "x", "blocked": "!"}
        if status not in marks:
            return "<error>status must be one of: pending, doing, done, blocked</error>"
        lines = self.plan_path.read_text(encoding="utf-8").splitlines()
        target_prefix = "- ["
        step_token = f"{step}. "
        changed = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith(target_prefix) and step_token in stripped:
                suffix = stripped.split(step_token, 1)[1]
                extra = f" — {note.strip()}" if note else ""
                lines[i] = f"- [{marks[status]}] {step}. {suffix.split(' — ', 1)[0]}{extra}"
                changed = True
                break
        if not changed:
            return f"<error>plan step not found: {step}</error>"
        _atomic_write(self.plan_path, "\n".join(lines).rstrip() + "\n")
        return f"<plan_step step={step} status={status}/>"

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

    # ---- context ledger ----

    def update_context(self, section: str, note: str) -> str:
        section = _canonical_context_section(section)
        note = note.strip()
        if not note:
            return "<error>empty context note</error>"
        sections = self._context_sections()
        body = sections.get(section, "").strip()
        bullet = note if note.startswith("- ") else f"- {note}"
        existing = {line.strip().lower() for line in body.splitlines() if line.strip()}
        if bullet.lower() not in existing:
            sections[section] = (body + "\n" + bullet).strip()
        self._write_context_sections(sections)
        return f"<context section={section!r} bytes={len(bullet)}/>"

    def render_context_for_prompt(self, max_chars: int) -> str:
        if not self.context_path.exists():
            return "_(no compact context yet)_"
        text = self.context_path.read_text(encoding="utf-8")
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + "\n…\n_(context truncated, over budget)_"

    def _context_sections(self) -> dict[str, str]:
        if not self.context_path.exists():
            return {s: "" for s in CONTEXT_SECTIONS}
        text = self.context_path.read_text(encoding="utf-8")
        sections = {s: "" for s in CONTEXT_SECTIONS}
        current: str | None = None
        buf: list[str] = []
        for line in text.splitlines():
            if line.startswith("## "):
                if current:
                    sections[current] = "\n".join(buf).strip()
                heading = line[3:].strip()
                current = _canonical_context_section(heading)
                buf = []
            elif current:
                buf.append(line)
        if current:
            sections[current] = "\n".join(buf).strip()
        return sections

    def _write_context_sections(self, sections: dict[str, str]) -> None:
        out = ["# Raspi Context", ""]
        for heading in CONTEXT_SECTIONS:
            out.append(f"## {heading}")
            out.append(sections.get(heading, "").strip() or "(empty)")
            out.append("")
        _atomic_write(self.context_path, "\n".join(out).rstrip() + "\n")


def _canonical_context_section(section: str) -> str:
    raw = section.strip().lower().replace("_", " ").replace("-", " ")
    aliases = {
        "task": "Task",
        "constraint": "Constraints",
        "constraints": "Constraints",
        "files": "Files Inspected",
        "file": "Files Inspected",
        "files inspected": "Files Inspected",
        "decision": "Decisions",
        "decisions": "Decisions",
        "failed": "Failed Attempts",
        "failed attempt": "Failed Attempts",
        "failed attempts": "Failed Attempts",
        "next": "Next Steps",
        "next step": "Next Steps",
        "next steps": "Next Steps",
    }
    return aliases.get(raw, "Decisions")
