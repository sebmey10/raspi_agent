"""Run one agent turn end-to-end with the live LLM."""
import time
import sys

from raspi_agent.agent import build_agent


def main(prompt: str) -> int:
    agent, deps = build_agent(on_tool_call=lambda n, a, r: print(f"  >> {n}({a}) -> {r[:200]}"))
    print(f"[session {agent.session_id}] warming…")
    deps.brain.warm("fast")
    t0 = time.time()
    answer = agent.turn(prompt)
    dt = time.time() - t0
    print("---")
    print(answer)
    print("---")
    print(f"({dt:.1f}s)")
    deps.store.end_session(agent.session_id)
    deps.brain.client.close()
    return 0


if __name__ == "__main__":
    p = " ".join(sys.argv[1:]) or "Say hi in one short sentence."
    sys.exit(main(p))
