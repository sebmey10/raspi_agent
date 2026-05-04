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

MAX_TOOL_LOOPS = 3
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
        self._system_template = load_prompt("system.md")
        self._tools_summary_cache: str | None = None
        self._tools_schema_cache: list[dict] | None = None
        self._wiki_prompt_cache: tuple[int, int, str] | None = None
        self._native_tools_disabled: set[str] = set()
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
        sys_template = self._system_template
        body = (
            sys_template
            .replace("{workspace}", str(self.d.cfg.workspace))
            .replace("{wiki}", self._wiki_for_prompt())
            .replace("{tools}", self._tools_summary())
            .replace("{tool_mode}", self._tool_mode_text(tier))
        )
        return {"role": "system", "content": body}

    def _wiki_for_prompt(self) -> str:
        try:
            mtime = self.d.wiki.path.stat().st_mtime_ns
        except OSError:
            mtime = 0
        limit = self.d.cfg.wiki_prompt_chars
        cached = self._wiki_prompt_cache
        if cached and cached[0] == mtime and cached[1] == limit:
            return cached[2]
        rendered = self.d.wiki.render_for_prompt(limit)
        self._wiki_prompt_cache = (mtime, limit, rendered)
        return rendered

    def _tools_summary(self) -> str:
        if self._tools_summary_cache is not None:
            return self._tools_summary_cache
        # One short line per tool — the model gets the full args from system.md
        # examples plus the names/descriptions here. Verbose JSON schemas blow
        # up prompt-eval time on a Pi without measurably improving tool calls.
        lines = []
        for name, t in self.d.tools.items():
            lines.append(f"- **{name}** — {t.description}")
        self._tools_summary_cache = "\n".join(lines)
        return self._tools_summary_cache

    def _tool_mode_text(self, tier: str) -> str:
        if self._use_native_tools(tier):
            return (
                "Each turn must be EXACTLY ONE of: (a) a single native tool call, or "
                "(b) a final natural-language answer with no tool calls. Never mix the two."
            )
        return (
            "Each turn must be EXACTLY ONE of: (a) one `<tool_call>{...}</tool_call>` "
            "XML block and nothing else, or (b) a final natural-language answer with no "
            "tool-call block. Never mix the two."
        )

    def _use_native_tools(self, tier: str) -> bool:
        mode = self.d.cfg.native_tools
        model = self.d.brain.models[tier].lower()
        if model in self._native_tools_disabled or not self.d.brain.supports_tools(tier):
            return False
        if mode == "off":
            return False
        if mode == "on":
            return True
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

    def _tools_schema(self) -> list[dict]:
        if self._tools_schema_cache is None:
            self._tools_schema_cache = to_ollama_schema(self.d.tools)
        return self._tools_schema_cache

    def _validate_tool_args(self, tool: Tool, args: object) -> tuple[dict, str | None]:
        if not isinstance(args, dict):
            return {}, "<error>tool arguments must be an object</error>"
        clean = dict(args)
        required = tool.parameters.get("required", [])
        missing = [name for name in required if clean.get(name) in (None, "")]
        if missing:
            return clean, f"<error>missing required field(s): {', '.join(missing)}</error>"
        props = tool.parameters.get("properties", {})
        for name, schema in props.items():
            if name not in clean:
                continue
            if schema.get("type") == "integer":
                try:
                    clean[name] = int(clean[name])
                except (TypeError, ValueError):
                    return clean, f"<error>field {name} must be an integer</error>"
            elif schema.get("type") == "string" and not isinstance(clean[name], str):
                clean[name] = str(clean[name])
        return clean, None

    def _tool_result_failed(self, result: str) -> bool:
        if "<error" in result:
            return True
        if result.startswith("<sh exit="):
            return not result.startswith("<sh exit=0>")
        return False

    def _final_answer_after_tools(self, tier: str, reason: str) -> str:
        self.history.append({
            "role": "user",
            "content": f"(system: {reason}; produce a final natural-language answer now, "
                       "without calling any more tools)",
        })
        try:
            final = self.d.brain.chat(
                [self._system_message(tier)] + self._context_history(), None, tier
            )
            self._record_stats(final)
            answer_text = final.content
            self.d.store.append_turn(self.session_id, "assistant", answer_text, tier=tier)
            return answer_text
        except Exception as e:
            return f"<llm-error>{e}</llm-error>"

    def turn(self, user_msg: str) -> str:
        self.last_turn_stats = []
        self.d.store.append_turn(self.session_id, "user", user_msg)
        self.history.append({"role": "user", "content": user_msg})

        answer_text = ""
        tier = self._pick_tier()
        seen_calls: dict[tuple[str, str], int] = {}
        for loop in range(MAX_TOOL_LOOPS):
            sys_msg = self._system_message(tier)
            using_native_tools = self._use_native_tools(tier)
            tools_schema = self._tools_schema() if using_native_tools else None
            messages = [sys_msg] + self._context_history()
            try:
                resp: LLMResponse = self.d.brain.chat(messages, tools_schema, tier)
            except Exception as e:
                self.fail_streak += 1
                err = f"<llm-error>{e}</llm-error>"
                self.d.store.append_turn(self.session_id, "assistant", err, tier=tier)
                return err

            if using_native_tools and resp.raw.get("_rasp_tools_unsupported"):
                self._native_tools_disabled.add(self.d.brain.models[tier].lower())
                continue

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
                    args, validation_error = self._validate_tool_args(tool, args)
                    if validation_error:
                        result = validation_error
                        any_failed = True
                    else:
                        try:
                            result = tool.handler(args)
                        except Exception as e:
                            result = f"<error>{type(e).__name__}: {e}</error>"
                            any_failed = True
                if self._tool_result_failed(result):
                    any_failed = True

                if self.d.on_tool_call:
                    try:
                        self.d.on_tool_call(name, args, result)
                    except Exception:
                        pass

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
                answer_text = self._final_answer_after_tools(tier, "you repeated a tool call")
                break

            if any_failed:
                self.fail_streak += 1
                if self.fail_streak >= self.d.cfg.fail_streak_to_escalate:
                    tier = TIER_REASONER
            else:
                self.fail_streak = 0
        else:
            self.fail_streak += 1
            answer_text = self._final_answer_after_tools(tier, "max tool loops reached")

        if self.fail_streak == 0 and tier == TIER_REASONER and not self.forced_tier:
            tier = TIER_FAST

        return answer_text


def build_agent(cfg: Config = CONFIG, on_tool_call=None, confirm_callback=None) -> tuple[Agent, AgentDeps]:
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
        return bool(confirm_callback and confirm_callback(cmd))

    shell = ShellGate(cfg.bash_allowlist, cfg.bash_timeout, cfg.workspace, confirm)
    tools = build_tools(cfg.workspace, shell)
    deps = AgentDeps(cfg, brain, store, wiki, tools, shell, on_tool_call)
    sid = store.start_session()
    agent = Agent(deps, sid)
    return agent, deps
