from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from .config import CONFIG, Config
from .llm import Brain, LLMResponse, TIER_FAST, TIER_REASONER
from .memory.store import Store
from .memory.wiki import Wiki
from .prompts import load as load_prompt
from .tools.registry import Tool, build_tools
from .tools.shell import ShellGate

MAX_TOOL_LOOPS = 4
MAX_REPEAT_CALLS = 1  # same (name, args) tuple this many times -> break


@dataclass
class AgentDeps:
    cfg: Config
    brain: Brain
    store: Store
    wiki: Wiki
    tools: dict[str, Tool]
    shell: ShellGate
    on_tool_call: Callable[[str, dict, str], None] | None = None


class Agent:
    def __init__(self, deps: AgentDeps, session_id: str):
        self.d = deps
        self.session_id = session_id
        self.history: list[dict] = []
        self.fail_streak = 0
        self.forced_tier: str | None = None
        self._wire_memory_tools()

    def _wire_memory_tools(self) -> None:
        def _remember(args: dict) -> str:
            heading = (args.get("heading") or "").strip()
            note = (args.get("note") or "").strip()
            if not heading:
                return "<error>missing field: heading</error>"
            if not note:
                return "<error>missing field: note</error>"
            return self.d.wiki.append(heading, note)

        def _forget(args: dict) -> str:
            needle = (args.get("needle") or "").strip()
            if not needle:
                return "<error>missing field: needle</error>"
            return self.d.wiki.forget(needle)

        self.d.tools["remember"].handler = _remember
        self.d.tools["forget"].handler = _forget

    def _system_message(self) -> dict:
        sys_template = load_prompt("system.md")
        body = (
            sys_template
            .replace("{workspace}", str(self.d.cfg.workspace))
            .replace("{wiki}", self.d.wiki.render_for_prompt(self.d.cfg.wiki_prompt_chars))
            .replace("{tools}", self._tools_summary())
        )
        return {"role": "system", "content": body}

    def _tools_summary(self) -> str:
        # One short line per tool — the model gets the full args from system.md
        # examples plus the names/descriptions here. Verbose JSON schemas blow
        # up prompt-eval time on a Pi without measurably improving tool calls.
        lines = []
        for name, t in self.d.tools.items():
            lines.append(f"- **{name}** — {t.description}")
        return "\n".join(lines)

    def _pick_tier(self) -> str:
        if self.forced_tier:
            return self.forced_tier
        if self.fail_streak >= self.d.cfg.fail_streak_to_escalate:
            return TIER_REASONER
        return TIER_FAST

    def turn(self, user_msg: str) -> str:
        self.d.store.append_turn(self.session_id, "user", user_msg)
        self.history.append({"role": "user", "content": user_msg})

        sys_msg = self._system_message()
        # We don't send ollama's `tools` field — Gemma 3n's custom Modelfile
        # template doesn't advertise tool-call support, and the in-prompt
        # `<tool_call>{...}</tool_call>` format (parsed in llm.py) works on every
        # model, including ones that lack a tool-aware chat template.
        tools_schema = None

        answer_text = ""
        tier = self._pick_tier()
        seen_calls: dict[tuple[str, str], int] = {}
        for loop in range(MAX_TOOL_LOOPS):
            messages = [sys_msg] + self.history
            try:
                resp: LLMResponse = self.d.brain.chat(messages, tools_schema, tier)
            except Exception as e:
                self.fail_streak += 1
                err = f"<llm-error>{e}</llm-error>"
                self.d.store.append_turn(self.session_id, "assistant", err, tier=tier)
                return err

            self.history.append({
                "role": "assistant",
                "content": resp.content,
                **({"tool_calls": resp.raw["message"].get("tool_calls")}
                   if resp.raw.get("message", {}).get("tool_calls") else {}),
            })
            self.d.store.append_turn(self.session_id, "assistant", resp.content, tier=tier)

            if not resp.tool_calls:
                answer_text = resp.content
                break

            any_failed = False
            looped = False
            import json as _json
            for tc in resp.tool_calls:
                name = tc["name"]
                args = tc["arguments"] or {}
                key = (name, _json.dumps(args, sort_keys=True))
                seen_calls[key] = seen_calls.get(key, 0) + 1
                if seen_calls[key] > MAX_REPEAT_CALLS:
                    looped = True
                    msg = f"<error>repeated call to {name}({args}); stop and answer the user</error>"
                    self.d.store.append_turn(
                        self.session_id, "tool", msg, tool_name=name,
                        tool_args=args, tool_result=msg, tier=tier,
                    )
                    self.history.append({"role": "tool", "name": name, "content": msg})
                    continue

                tool = self.d.tools.get(name)
                if tool is None:
                    result = f"<error>unknown tool: {name}</error>"
                    any_failed = True
                else:
                    try:
                        result = tool.handler(args)
                    except Exception as e:
                        result = f"<error>{type(e).__name__}: {e}</error>"
                        any_failed = True
                if "<error" in result:
                    any_failed = True

                if self.d.on_tool_call:
                    self.d.on_tool_call(name, args, result)

                self.d.store.append_turn(
                    self.session_id, "tool", result, tool_name=name,
                    tool_args=args, tool_result=result, tier=tier,
                )
                self.history.append({"role": "tool", "name": name, "content": result})

            if looped:
                self.history.append({
                    "role": "user",
                    "content": "(system: you repeated a tool call; produce a final "
                               "natural-language answer to the previous user message now, "
                               "without calling any more tools)",
                })
                try:
                    final = self.d.brain.chat([sys_msg] + self.history, None, tier)
                    answer_text = final.content
                    self.d.store.append_turn(self.session_id, "assistant", answer_text, tier=tier)
                except Exception as e:
                    answer_text = f"<llm-error>{e}</llm-error>"
                break

            if any_failed:
                self.fail_streak += 1
                if self.fail_streak >= self.d.cfg.fail_streak_to_escalate:
                    tier = TIER_REASONER
            else:
                self.fail_streak = 0
        else:
            answer_text = "<warning>max tool loops reached</warning>"

        if self.fail_streak == 0 and tier == TIER_REASONER and not self.forced_tier:
            tier = TIER_FAST

        return answer_text


def build_agent(cfg: Config = CONFIG, on_tool_call=None) -> tuple[Agent, AgentDeps]:
    cfg.ensure_dirs()
    from .llm import OllamaClient
    client = OllamaClient(cfg.ollama_url, cfg.keep_alive)
    brain = Brain(
        client, cfg.model_fast, cfg.model_reasoner,
        cfg.ctx_fast, cfg.ctx_reasoner,
        num_predict_fast=cfg.num_predict_fast,
        num_predict_reasoner=cfg.num_predict_reasoner,
        num_thread=cfg.num_thread,
    )
    store = Store(cfg.db_path)
    wiki = Wiki(cfg.wiki_path)

    def confirm(cmd: str) -> bool:
        return False

    shell = ShellGate(cfg.bash_allowlist, cfg.bash_timeout, cfg.workspace, confirm)
    tools = build_tools(cfg.workspace, shell)
    deps = AgentDeps(cfg, brain, store, wiki, tools, shell, on_tool_call)
    sid = store.start_session()
    agent = Agent(deps, sid)
    return agent, deps
