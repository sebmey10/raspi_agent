"""Karpathy-style LLM Wiki: one canonical markdown file the model reads each turn.

No retrieval, no embeddings, no chunking. Just a single ~3-6k token markdown file
with fixed top-level sections (## Identity, ## User, ## Active Projects, etc.).
The model edits it through targeted append/forget/replace operations, and a
nightly `dream` pass rewrites it tighter.
"""
from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

DEFAULT_HEADINGS: tuple[str, ...] = (
    "Identity",
    "User",
    "Active Projects",
    "Preferences",
    "References",
    "Notes",
    "Archive",
)

DEFAULT_BODY: dict[str, str] = {
    "Identity": (
        "I am rasp, a small persistent coding agent on a Raspberry Pi. "
        "I keep this wiki up to date as my long-term memory."
    ),
    "User": "(nothing yet — append facts about the user as you learn them)",
    "Active Projects": "(nothing yet)",
    "Preferences": "(nothing yet — collaboration style, tone, do/don't rules)",
    "References": "(nothing yet — pointers to external systems, URLs, channels)",
    "Notes": "(nothing yet — durable project notes that don't fit above)",
    "Archive": "(rotated content from past dream cycles lands here)",
}

# Match any "## Heading" up to the next "## " or EOF.
_SECTION_RE = re.compile(r"^## ([^\n]+)\n(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)


def _normalize(name: str) -> str:
    return name.strip().lower()


@dataclass
class Section:
    heading: str
    body: str


class Wiki:
    """Single-file markdown memory. All edits anchor on H2 headings."""

    def __init__(self, path: Path, headings: tuple[str, ...] = DEFAULT_HEADINGS):
        self.path = path
        self.headings = headings
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write_initial()

    def _write_initial(self) -> None:
        out = ["# Agent Wiki", ""]
        out.append(f"_initialized: {time.strftime('%Y-%m-%d %H:%M:%S')}_")
        out.append("")
        for h in self.headings:
            out.append(f"## {h}")
            out.append(DEFAULT_BODY.get(h, "(nothing yet)"))
            out.append("")
        self.path.write_text("\n".join(out).rstrip() + "\n")

    def text(self) -> str:
        return self.path.read_text()

    def sections(self) -> list[Section]:
        text = self.text()
        return [Section(h.strip(), b.strip()) for h, b in _SECTION_RE.findall(text)]

    def _save(self, sections: list[Section]) -> None:
        out = ["# Agent Wiki", ""]
        out.append(f"_updated: {time.strftime('%Y-%m-%d %H:%M:%S')}_")
        out.append("")
        for s in sections:
            out.append(f"## {s.heading}")
            out.append(s.body.strip() if s.body.strip() else "(empty)")
            out.append("")
        self.path.write_text("\n".join(out).rstrip() + "\n")

    def find(self, heading: str) -> Section | None:
        target = _normalize(heading)
        for s in self.sections():
            if _normalize(s.heading) == target:
                return s
        return None

    def append(self, heading: str, note: str) -> str:
        """Append `note` as a bullet under `heading`. Creates section if missing."""
        note = note.strip()
        if not note:
            return "<error>empty note</error>"
        secs = self.sections()
        target = _normalize(heading)
        for s in secs:
            if _normalize(s.heading) == target:
                body = s.body
                if body.startswith("(") and body.endswith(")"):
                    body = ""  # replace placeholder
                bullet = note if note.startswith("- ") else f"- {note}"
                s.body = (body.rstrip() + "\n" + bullet).strip()
                self._save(secs)
                return f"<remembered heading={s.heading!r} bytes={len(bullet)}/>"
        secs.append(Section(heading=heading, body=f"- {note}"))
        self._save(secs)
        return f"<remembered heading={heading!r} new_section=true/>"

    def replace(self, heading: str, body: str) -> str:
        """Replace the entire body under `heading`."""
        secs = self.sections()
        target = _normalize(heading)
        for s in secs:
            if _normalize(s.heading) == target:
                s.body = body.strip() or "(empty)"
                self._save(secs)
                return f"<replaced heading={s.heading!r} bytes={len(body)}/>"
        secs.append(Section(heading=heading, body=body.strip() or "(empty)"))
        self._save(secs)
        return f"<replaced heading={heading!r} new_section=true/>"

    def forget(self, needle: str) -> str:
        """Remove the first line containing `needle` (case-insensitive)."""
        if not needle.strip():
            return "<error>empty needle</error>"
        n = needle.strip().lower()
        secs = self.sections()
        for s in secs:
            lines = s.body.splitlines()
            for i, ln in enumerate(lines):
                if n in ln.lower():
                    removed = ln.strip()
                    del lines[i]
                    s.body = "\n".join(lines).strip() or "(empty)"
                    self._save(secs)
                    return f"<forgot from={s.heading!r} line={removed[:80]!r}/>"
        return f"<error>no matching line for {needle!r}</error>"

    def archive_section(self, heading: str) -> str:
        """Move a section's body into ## Archive, leave the section empty."""
        secs = self.sections()
        target = _normalize(heading)
        archive = next((s for s in secs if _normalize(s.heading) == "archive"), None)
        if archive is None:
            archive = Section("Archive", "")
            secs.append(archive)
        for s in secs:
            if _normalize(s.heading) == target and s is not archive:
                stamp = time.strftime("%Y-%m-%d")
                chunk = f"\n\n### {s.heading} ({stamp})\n{s.body.strip()}"
                archive.body = (archive.body.rstrip() + chunk).strip()
                s.body = "(empty)"
                self._save(secs)
                return f"<archived heading={s.heading!r}/>"
        return f"<error>no section {heading!r}/>"

    def render_for_prompt(self, max_chars: int = 12000) -> str:
        """Full wiki for system prompt. If too long, drop ## Archive first."""
        text = self.text()
        if len(text) <= max_chars:
            return text
        secs = self.sections()
        kept = [s for s in secs if _normalize(s.heading) != "archive"]
        # rebuild without archive
        out = ["# Agent Wiki", ""]
        for s in kept:
            out.append(f"## {s.heading}")
            out.append(s.body.strip())
            out.append("")
        text = "\n".join(out)
        if len(text) <= max_chars:
            return text + "\n\n_(archive section omitted, over budget)_"
        return text[:max_chars] + "\n…\n_(truncated, over budget)_"

    def snapshot(self, dest_dir: Path) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = dest_dir / f"WIKI.{stamp}.md"
        shutil.copy2(self.path, dest)
        return dest
