from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .config import CONFIG, Config
from .llm import Brain, LLMResponse, TIER_FAST, TIER_REASONER
from .memory.store import Store
from .memory.wiki import Wiki
from .prompts import load as load_prompt
from .tools.registry import Tool, build_tools, to_ollama_schema
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
        self.last_turn_stats: list[dict] = []
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

    def _system_message(self, tier: str) -> dict:
        sys_template = load_prompt("system.md")
        body = (
            sys_template
            .replace("{workspace}", str(self.d.cfg.workspace))
            .replace("{wiki}", self.d.wiki.render_for_prompt(self.d.cfg.wiki_prompt_chars))
            .replace("{tools}", self._tools_summary())
            .replace("{tool_mode}", self._tool_mode_text(tier))
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

    def _tool_mode_text(self, tier: str) -> str:
        if self._use_native_tools(tier):
            return (
                "Use Ollama native tool calls when you need a tool. If the model cannot "
                "emit a native call, fall back to the XML `<tool_call>{...}</tool_call>` "
                "format exactly."
            )
        return (
            "When you need a tool, emit exactly one XML `<tool_call>{...}</tool_call>` "
            "block and no other text in that turn."
        )

    def _use_native_tools(self, tier: str) -> bool:
        mode = self.d.cfg.native_tools
        if mode == "off":
            return False
        if mode == "on":
            return True
        model = self.d.brain.models[tier].lower()
        return any(
            marker in model
            for marker in ("qwen3", "llama3.1", "llama3.2", "llama3.3", "granite", "mistral")
        )

    def _pick_tier(self) -> str:
        if self.forced_tier:
            return self.forced_tier
        if self.fail_streak >= self.d.cfg.fail_streak_to_escalate:
            return TIER_REASONER
        return TIER_FAST

    def _context_history(self) -> list[dict]:
        budget = max(1000, self.d.cfg.history_prompt_chars)
        max_messages = max(2, self.d.cfg.max_history_messages)
        kept: list[dict] = []
        used = 0
        for msg in reversed(self.history):
            content = str(msg.get("content", ""))
            cost = len(content) + 80
            if kept and (used + cost > budget or len(kept) >= max_messages):
                break
            kept.append(msg)
            used += cost
        return list(reversed(kept))

    def _clip_tool_result_for_history(self, text: str) -> str:
        limit = max(500, self.d.cfg.tool_result_chars)
        if len(text) <= limit:
            return text
        return text[:limit] + f"\n<truncated_for_prompt original_chars={len(text)}>"

    def _record_stats(self, resp: LLMResponse) -> None:
        raw = resp.raw or {}
        prompt_s = (raw.get("prompt_eval_duration") or 0) / 1_000_000_000
        eval_s = (raw.get("eval_duration") or 0) / 1_000_000_000
        load_s = (raw.get("load_duration") or 0) / 1_000_000_000
        prompt_tokens = raw.get("prompt_eval_count") or 0
        eval_tokens = raw.get("eval_count") or 0
        self.last_turn_stats.append({
            "tier": resp.tier,
            "model": raw.get("model", self.d.brain.models.get(resp.tier, "")),
            "prompt_tokens": prompt_tokens,
            "eval_tokens": eval_tokens,
            "prompt_tok_s": (prompt_tokens / prompt_s) if prompt_s else 0.0,
            "eval_tok_s": (eval_tokens / eval_s) if eval_s else 0.0,
            "load_s": load_s,
        })

    def turn(self, user_msg: str) -> str:
        self.last_turn_stats = []
        self.d.store.append_turn(self.session_id, "user", user_msg)
        self.history.append({"role": "user", "content": user_msg})

        answer_text = ""
        tier = self._pick_tier()
        seen_calls: dict[tuple[str, str], int] = {}
        for loop in range(MAX_TOOL_LOOPS):
            sys_msg = self._system_message(tier)
            tools_schema = to_ollama_schema(self.d.tools) if self._use_native_tools(tier) else None
            messages = [sys_msg] + self._context_history()
            try:
                resp: LLMResponse = self.d.brain.chat(messages, tools_schema, tier)
            except Exception as e:
                self.fail_streak += 1
                err = f"<llm-error>{e}</llm-error>"
                self.d.store.append_turn(self.session_id, "assistant", err, tier=tier)
                return err

            self._record_stats(resp)
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
                self.history.append({
                    "role": "tool",
                    "name": name,
                    "content": self._clip_tool_result_for_history(result),
                })

            if looped:
                self.history.append({
                    "role": "user",
                    "content": "(system: you repeated a tool call; produce a final "
                               "natural-language answer to the previous user message now, "
                               "without calling any more tools)",
                })
                try:
                    final = self.d.brain.chat(
                        [self._system_message(tier)] + self._context_history(), None, tier
                    )
                    self._record_stats(final)
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
        think_fast=cfg.think_fast,
        think_reasoner=cfg.think_reasoner,
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
