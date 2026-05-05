"""Multi-turn timing test."""
import time
import sys

from raspi_agent.agent import build_agent


def main() -> int:
    agent, deps = build_agent(on_tool_call=lambda n, a, r: print(f"  >> {n}({_short(a)}) -> {r[:140]}"))
    print(f"[session {agent.session_id}] warming…")
    t0 = time.time()
    deps.brain.warm("fast")
    print(f"  warm took {time.time()-t0:.1f}s")

    prompts = [
        "Reply with one short word.",
        "What is 2+2? Just say the number.",
        "Remember that I prefer Python over JavaScript. "
        "Call the remember tool with heading='User' and a one-line note.",
        "What do you know about me? Just answer in one short sentence; do not call any tools.",
    ]
    for p in prompts:
        print(f"\n> {p}")
        t = time.time()
        a = agent.turn(p)
        print(f"   ({time.time()-t:.1f}s) -> {a[:200]}")

    print("\n--- wiki after run ---")
    print(agent.d.wiki.text())

    deps.store.end_session(agent.session_id)
    deps.brain.client.close()
    return 0


def _short(d):
    s = str(d)
    return s if len(s) <= 80 else s[:77] + "…"


if __name__ == "__main__":
    sys.exit(main())
