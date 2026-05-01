"""Persistence test: write a wiki note in session A, read it in session B (simulated reboot)."""
import time
import sys

from rasp_agent.agent import build_agent


def main() -> int:
    print("=== session A: writing a wiki note ===")
    a, deps_a = build_agent()
    print(deps_a.wiki.append("User", "Prefers Python over JavaScript for new code"))
    print("  WIKI.md ->")
    print(deps_a.wiki.text())
    deps_a.store.end_session(a.session_id)
    deps_a.brain.client.close()

    print("\n=== session B: fresh agent recall ===")
    b, deps_b = build_agent(on_tool_call=lambda n, a, r: print(f"  >> {n}({a}) -> {r[:160]}"))
    deps_b.brain.warm("fast")
    t = time.time()
    answer = b.turn("In one short sentence, what programming language does the user prefer?")
    dt = time.time() - t
    print(f"\nanswer ({dt:.1f}s): {answer}")
    deps_b.store.end_session(b.session_id)
    deps_b.brain.client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
