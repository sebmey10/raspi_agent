"""Dream pass: rewrite the wiki tighter using recent transcript turns."""
from __future__ import annotations

import time

from ..config import CONFIG, Config
from ..llm import Brain, OllamaClient, TIER_FAST
from ..prompts import load as load_prompt
from .store import Store
from .wiki import DEFAULT_HEADINGS, Wiki, _SECTION_RE


def _format_transcript(turns: list[dict]) -> str:
    out = []
    for t in turns:
        role = t["role"]
        content = (t["content"] or "").strip()
        if not content:
            continue
        if role == "tool":
            tn = t.get("tool_name") or "tool"
            out.append(f"[{tn}-result] {content[:600]}")
        else:
            out.append(f"[{role}] {content[:1200]}")
    return "\n".join(out)


def consolidate(cfg: Config = CONFIG) -> dict:
    cfg.ensure_dirs()
    store = Store(cfg.db_path)
    wiki = Wiki(cfg.wiki_path)

    last_run = float(store.get_kv("last_dream_ts") or 0.0)
    turns = store.turns_since(last_run)
    if len(turns) < cfg.dream_min_turns:
        return {
            "skipped": True,
            "reason": f"only {len(turns)} new turns (< {cfg.dream_min_turns})",
        }

    transcript = _format_transcript(turns)
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
        # Use the fast tier for consolidation. The reasoner (gemma3n-e2b-iq3xs)
        # is both slower *and* lower-quality for long structured rewrites on a
        # Pi — qwen2.5-coder:1.5b at Q4_K_M produces a coherent wiki diff in a
        # fraction of the time.
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
        cfg.wiki_path.write_text(cleaned)
        written = 1
        summary = f"Rewrote wiki ({len(cleaned)} chars)."

    store.log_dream(summary=summary, turns_consumed=len(turns), memories_written=written)
    store.set_kv("last_dream_ts", str(time.time()))

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
