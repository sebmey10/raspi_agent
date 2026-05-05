"""Dream pass: rewrite the wiki tighter using recent transcript turns."""
from __future__ import annotations

import time

from ..config import CONFIG, Config
from ..llm import Brain, OllamaClient, TIER_FAST
from ..prompts import load as load_prompt
from .store import Store
from .wiki import DEFAULT_HEADINGS, Wiki, _SECTION_RE, atomic_write_text


def _format_transcript(turns: list[dict], max_chars: int) -> str:
    out: list[str] = []
    used = 0
    omitted = 0
    for t in reversed(turns):
        role = t["role"]
        content = (t["content"] or "").strip()
        if not content:
            continue
        if role == "tool":
            tn = t.get("tool_name") or "tool"
            line = f"[{tn}-result] {content[:600]}"
        else:
            line = f"[{role}] {content[:1200]}"
        cost = len(line) + 1
        if out and used + cost > max_chars:
            omitted += 1
            continue
        out.append(line)
        used += cost
    out.reverse()
    if omitted:
        out.insert(0, f"[system] omitted {omitted} older transcript item(s) over budget")
    return "\n".join(out)


def consolidate(cfg: Config = CONFIG) -> dict:
    cfg.ensure_dirs()
    store = Store(cfg.db_path)
    wiki = Wiki(cfg.wiki_path)

    last_run = float(store.get_kv("last_dream_ts") or 0.0)
    turns = store.turns_since(last_run, limit=cfg.dream_max_turns)
    if len(turns) < cfg.dream_min_turns:
        store.close()
        return {
            "skipped": True,
            "reason": f"only {len(turns)} new turns (< {cfg.dream_min_turns})",
        }

    transcript = _format_transcript(turns, max(1000, cfg.dream_transcript_chars))
    prompt = (
        load_prompt("consolidate.md")
        .replace("{wiki}", wiki.text())
        .replace("{transcript}", transcript)
    )

    client = OllamaClient(cfg.ollama_url, cfg.keep_alive)
    brain = Brain(
        client, cfg.model_fast, cfg.model_reasoner,
        cfg.ctx_fast, cfg.ctx_reasoner,
        num_predict_fast=cfg.num_predict_fast,
        num_predict_reasoner=cfg.num_predict_reasoner,
        num_thread=cfg.num_thread,
        think_fast=cfg.think_fast,
        think_reasoner=cfg.think_reasoner,
    )
    try:
        # Use the fast tier for consolidation: the rewrite is bounded and
        # structure-heavy, so keeping prompt cost low matters more than depth.
        resp = brain.chat(
            messages=[{"role": "user", "content": prompt}],
            tools=None,
            tier=TIER_FAST,
        )
    finally:
        client.close()

    new_wiki = (resp.content or "").strip()
    new_wiki = _strip_fences(new_wiki)
    cleaned = _keep_canonical_sections(new_wiki)

    summary = "no rewrite produced (model output too short)"
    written = 0
    if cleaned and len(cleaned) > 200:
        wiki.snapshot(cfg.wiki_snapshots_dir)
        atomic_write_text(cfg.wiki_path, cleaned)
        written = 1
        summary = f"Rewrote wiki ({len(cleaned)} chars)."

    store.log_dream(summary=summary, turns_consumed=len(turns), memories_written=written)
    last_processed = max(float(t["ts"]) for t in turns)
    store.set_kv("last_dream_ts", str(last_processed))
    store.close()

    return {
        "skipped": False,
        "turns": len(turns),
        "written": written,
        "summary": summary,
    }


def _keep_canonical_sections(text: str) -> str:
    """Filter the model's rewrite down to the canonical headings.

    Models on a Pi will often regurgitate the prompt's transcript section
    or invent extra H2s — we only keep what belongs in the wiki.
    """
    found: dict[str, str] = {}
    for h, b in _SECTION_RE.findall(text):
        h_norm = h.strip()
        for canon in DEFAULT_HEADINGS:
            if h_norm.lower() == canon.lower():
                found[canon] = b.strip()
                break
    if not found:
        return ""
    out = ["# Agent Wiki", ""]
    out.append(f"_updated: {time.strftime('%Y-%m-%d %H:%M:%S')}_")
    out.append("")
    for canon in DEFAULT_HEADINGS:
        out.append(f"## {canon}")
        out.append(found.get(canon, "(empty)") or "(empty)")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _strip_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        # drop opening fence (with or without language tag) and trailing fence
        s = s.split("\n", 1)[1] if "\n" in s else ""
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3].rstrip()
    return s


def cli_main() -> int:
    res = consolidate()
    if res.get("skipped"):
        print(f"skipped: {res['reason']}")
        return 0
    print(f"dream: {res['summary']}")
    print(f"  turns_consumed={res['turns']} written={res['written']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli_main())
