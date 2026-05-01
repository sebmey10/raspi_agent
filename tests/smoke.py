"""End-to-end smoke test: wiki memory + episodic store roundtrip."""
import time

from rasp_agent.config import CONFIG
from rasp_agent.memory.store import Store
from rasp_agent.memory.wiki import Wiki


def test_wiki_layer():
    cfg = CONFIG
    cfg.ensure_dirs()
    store = Store(cfg.db_path)
    wiki = Wiki(cfg.wiki_path)

    sid = store.start_session("smoke")
    store.append_turn(sid, "user", "I prefer Python over JavaScript.")

    print("initial sections:", [s.heading for s in wiki.sections()])
    print(wiki.append("User", "Prefers Python over JavaScript for new code"))
    print(wiki.append("Preferences", "Wants terse caveman-style replies"))

    text = wiki.text()
    assert "Prefers Python" in text, "wiki append round-trip failed"
    print("wiki bytes:", len(text))

    print(wiki.forget("caveman"))
    assert "caveman" not in wiki.text().lower()

    rendered = wiki.render_for_prompt(max_chars=1000)
    print("render_for_prompt len:", len(rendered))

    print("WIKI OK")


if __name__ == "__main__":
    test_wiki_layer()
