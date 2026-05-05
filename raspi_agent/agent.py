"""raspi agent loop: Plan -> Act -> Reflect, with compaction and streaming.

The agent runs a small open model on Ollama through a tier-escalating Brain.
Each user message becomes a *task*: the model writes a `plan.md` (via
`todo_write`) for non-trivial work, then iterates Act-Observe up to
`cfg.max_tool_loops` times, with a Reflect checkpoint every
`cfg.reflect_every` loops. Context-history compaction kicks in when the running
transcript exceeds `cfg.compact_threshold_ratio * cfg.history_prompt_chars`.
The final answer turn streams to stdout when a chunk callback is wired.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .config import CONFIG, Config
from .llm import Brain, LLMResponse, TIER_FAST, TIER_REASONER
from .memory.scratchpad import Scratchpad
from .memory.store import Store
from .memory.wiki import Wiki
from .prompts import load as load_prompt
from .tools.registry import Tool, build_tools, to_ollama_schema
from .tools.shell import ShellGate

REFLECT_PROMPT = (
    "[reflect-checkpoint] Reply with ONE word: `keep`, `replan`, or `give_up`. "
    "Do not call any tools. Do not write anything else."
)


@dataclass
class AgentDeps:
    cfg: Config
    brain: Brain
    store: Store
    wiki: Wiki
    scratch: Scratchpad
    tools: dict[str, Tool]
    shell: ShellGate
    on_tool_call: Callable[[str, dict, str], None] | None = None
    on_chunk: Callable[[str], None] | None = None
    cancel: threading.Event | None = None


class Agent:
    def __init__(self, deps: AgentDeps, session_id: str):
        self.d = deps
        self.session_id = session_id
        self.history: list[dict] = []
        self.fail_streak = 0
        self.forced_tier: str | None = None
        self.last_turn_stats: list[dict] = []
        self.session_stats: dict = {
            "prompt_tokens": 0,
            "eval_tokens": 0,
            "turns": 0,
            "compactions": 0,
            "tool_calls": 0,
        }
        self._system_template = load_prompt("system.md")
        self._tools_summary_cache: str | None = None
        self._tools_schema_cache: list[dict] | None = None
        self._wiki_prompt_cache: tuple[int, int, str] | None = None
        self._native_tools_disabled: set[str] = set()
        self._wire_handlers()

    # ---- handler wiring ----

    def _wire_handlers(self) -> None:
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

        def _todo_write(args: dict) -> str:
            steps = args.get("steps") or []
            if not isinstance(steps, list):
                return "<error>steps must be an array of strings</error>"
            steps = [str(s) for s in steps]
            note = args.get("note")
            if note is not None and not isinstance(note, str):
                note = str(note)
            return self.d.scratch.write_plan(steps, note)

        self.d.tools["remember"].handler = _remember
        self.d.tools["forget"].handler = _forget
        self.d.tools["todo_write"].handler = _todo_write

    # ---- prompt assembly ----

    def _system_message(self, tier: str) -> dict:
        body = (
            self._system_template
            .replace("{workspace}", str(self.d.cfg.workspace))
            .replace("{wiki}", self._wiki_for_prompt())
            .replace("{plan}", self._plan_for_prompt())
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

    def _plan_for_prompt(self) -> str:
        return self.d.scratch.render_for_prompt(self.d.cfg.plan_prompt_chars)

    def _tools_summary(self) -> str:
        if self._tools_summary_cache is not None:
            return self._tools_summary_cache
        lines: list[str] = []
        for name, t in self.d.tools.items():
            if t.name == "ast_edit" and not t.extras.get("available", True):
                continue
            lines.append(f"- **{name}** - {t.description}")
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
            for marker in ("qwen3", "qwen2.5", "llama3.1", "llama3.2", "llama3.3", "granite", "mistral")
        )

    def _pick_tier(self) -> str:
        if self.forced_tier:
            return self.forced_tier
        if self.fail_streak >= self.d.cfg.fail_streak_to_escalate:
            return TIER_REASONER
        return TIER_FAST

    # ---- history shaping ----

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

    def _compact_if_needed(self) -> None:
        budget = max(1000, self.d.cfg.history_prompt_chars)
        threshold = int(budget * self.d.cfg.compact_threshold_ratio)
        used = sum(len(str(m.get("content", ""))) + 80 for m in self.history)
        if used <= threshold or len(self.history) < 6:
            return
        keep_recent = max(4, len(self.history) // 2)
        old = self.history[:-keep_recent]
        recent = self.history[-keep_recent:]
        if not old:
            return
        bullets: list[str] = []
        for msg in old:
            role = msg.get("role", "?")
            text = str(msg.get("content", "")).strip().replace("\n", " ")
            if not text:
                continue
            if role == "tool":
                name = msg.get("name") or "tool"
                bullets.append(f"- {name}-result: {text[:160]}")
            else:
                bullets.append(f"- [{role}] {text[:200]}")
        synthetic = {
            "role": "assistant",
            "content": (
                f"[compacted {len(old)} earlier turns to fit context]\n"
                + "\n".join(bullets[:60])
            ),
        }
        self.history = [synthetic] + recent
        self.session_stats["compactions"] += 1

    # ---- stats / dedupe ----

    def _record_stats(self, raw: dict, tier: str) -> None:
        prompt_s = (raw.get("prompt_eval_duration") or 0) / 1_000_000_000
        eval_s = (raw.get("eval_duration") or 0) / 1_000_000_000
        load_s = (raw.get("load_duration") or 0) / 1_000_000_000
        prompt_tokens = raw.get("prompt_eval_count") or 0
        eval_tokens = raw.get("eval_count") or 0
        self.last_turn_stats.append({
            "tier": tier,
            "model": raw.get("model", self.d.brain.models.get(tier, "")),
            "prompt_tokens": prompt_tokens,
            "eval_tokens": eval_tokens,
            "prompt_tok_s": (prompt_tokens / prompt_s) if prompt_s else 0.0,
            "eval_tok_s": (eval_tokens / eval_s) if eval_s else 0.0,
            "load_s": load_s,
        })
        self.session_stats["prompt_tokens"] += prompt_tokens
        self.session_stats["eval_tokens"] += eval_tokens

    def _record_response(self, resp: LLMResponse) -> None:
        self._record_stats(resp.raw or {}, resp.tier)

    @staticmethod
    def _normalize_args(args: dict) -> str:
        def norm(v):
            if isinstance(v, str):
                return v.strip()
            if isinstance(v, list):
                return [norm(x) for x in v]
            if isinstance(v, dict):
                return {k: norm(x) for k, x in sorted(v.items())}
            return v
        try:
            return json.dumps(norm(args), sort_keys=True)
        except (TypeError, ValueError):
            return repr(args)

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
            t = schema.get("type")
            if t == "integer":
                try:
                    clean[name] = int(clean[name])
                except (TypeError, ValueError):
                    return clean, f"<error>field {name} must be an integer</error>"
            elif t == "string" and not isinstance(clean[name], str):
                clean[name] = str(clean[name])
            elif t == "array" and not isinstance(clean[name], list):
                return clean, f"<error>field {name} must be an array</error>"
        return clean, None

    def _tool_result_failed(self, result: str) -> bool:
        if "<error" in result:
            return True
        if result.startswith("<sh exit="):
            return not result.startswith("<sh exit=0>")
        return False

    # ---- turn entry point ----

    def turn(self, user_msg: str) -> str:
        self.last_turn_stats = []
        self.session_stats["turns"] += 1
        self.d.store.append_turn(self.session_id, "user", user_msg)
        self.history.append({"role": "user", "content": user_msg})
        self.d.scratch.begin_task(user_msg)

        max_loops = max(1, self.d.cfg.max_tool_loops)
        reflect_every = max(0, self.d.cfg.reflect_every)
        seen_calls: dict[tuple[str, str], int] = {}
        tier = self._pick_tier()
        answer_text = ""
        exhausted = True

        for loop in range(max_loops):
            if self._cancelled():
                return "<cancelled>"
            self._compact_if_needed()

            if reflect_every and loop > 0 and loop % reflect_every == 0:
                decision = self._reflect(tier)
                if decision == "give_up":
                    answer_text = self._final_answer(tier, "you chose `give_up`")
                    exhausted = False
                    break

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

            self._record_response(resp)
            self.history.append({
                "role": "assistant",
                "content": resp.content,
                **({"tool_calls": resp.raw["message"].get("tool_calls")}
                   if resp.raw.get("message", {}).get("tool_calls") else {}),
            })
            self.d.store.append_turn(self.session_id, "assistant", resp.content, tier=tier)

            if not resp.tool_calls:
                answer_text = self._maybe_restream(resp)
                exhausted = False
                break

            any_failed = False
            looped = False
            for tc in resp.tool_calls:
                if self._cancelled():
                    return "<cancelled>"
                name = tc["name"]
                args = tc["arguments"] or {}
                key = (name, self._normalize_args(args if isinstance(args, dict) else {}))
                seen_calls[key] = seen_calls.get(key, 0) + 1
                if seen_calls[key] > 1:
                    looped = True
                    msg = (f"<error>repeated call to {name}({args}); "
                           f"stop and answer the user</error>")
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

                self.session_stats["tool_calls"] += 1
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
                answer_text = self._final_answer(tier, "you repeated a tool call")
                exhausted = False
                break

            if any_failed:
                self.fail_streak += 1
                if self.fail_streak >= self.d.cfg.fail_streak_to_escalate:
                    tier = TIER_REASONER
            else:
                self.fail_streak = 0

        if exhausted:
            self.fail_streak += 1
            answer_text = self._final_answer(tier, "max tool loops reached")

        if self.fail_streak == 0 and tier == TIER_REASONER and not self.forced_tier:
            tier = TIER_FAST

        return answer_text

    # ---- reflect / final-answer ----

    def _reflect(self, tier: str) -> str:
        self.history.append({"role": "user", "content": REFLECT_PROMPT})
        self.d.store.append_turn(self.session_id, "user", REFLECT_PROMPT, tier=tier)
        try:
            resp = self.d.brain.chat(
                [self._system_message(tier)] + self._context_history(), None, tier
            )
        except Exception:
            return "keep"
        self._record_response(resp)
        text = (resp.content or "").strip().lower()
        self.history.append({"role": "assistant", "content": resp.content})
        self.d.store.append_turn(self.session_id, "assistant", resp.content, tier=tier)
        for choice in ("give_up", "replan", "keep"):
            if choice in text:
                return choice
        return "keep"

    def _final_answer(self, tier: str, reason: str) -> str:
        self.history.append({
            "role": "user",
            "content": (f"(system: {reason}; produce a final natural-language answer now, "
                        "without calling any more tools)"),
        })
        return self._chat_final(tier)

    def _maybe_restream(self, resp: LLMResponse) -> str:
        """Replay an already-produced final answer through the chunk hook so the
        UI gets a streaming feel without a second LLM call."""
        if self.d.on_chunk is None or not resp.content:
            return resp.content
        try:
            for ch in resp.content:
                if self._cancelled():
                    break
                self.d.on_chunk(ch)
        except Exception:
            pass
        return resp.content

    def _chat_final(self, tier: str) -> str:
        sys_msg = self._system_message(tier)
        messages = [sys_msg] + self._context_history()
        if self.d.on_chunk is not None and hasattr(self.d.brain, "stream"):
            try:
                buf: list[str] = []
                final_raw: dict = {}
                for event in self.d.brain.stream(messages, tier, cancel=self.d.cancel):
                    if event.get("cancelled"):
                        break
                    chunk = event.get("chunk")
                    if chunk:
                        buf.append(chunk)
                        try:
                            self.d.on_chunk(chunk)
                        except Exception:
                            pass
                    elif "done" in event:
                        final_raw = event["done"]
                if final_raw:
                    self._record_stats(final_raw, tier)
                text = "".join(buf)
                if text:
                    self.history.append({"role": "assistant", "content": text})
                    self.d.store.append_turn(self.session_id, "assistant", text, tier=tier)
                    return text
            except Exception:
                pass
        try:
            final = self.d.brain.chat(messages, None, tier)
            self._record_response(final)
            text = final.content
            self.history.append({"role": "assistant", "content": text})
            self.d.store.append_turn(self.session_id, "assistant", text, tier=tier)
            return text
        except Exception as e:
            return f"<llm-error>{e}</llm-error>"

    def _cancelled(self) -> bool:
        return self.d.cancel is not None and self.d.cancel.is_set()

    # ---- session lifecycle ----

    def end_session(self) -> None:
        try:
            stamp = time.strftime("%Y-%m-%d %H:%M")
            tools_used = self.session_stats.get("tool_calls", 0)
            turns = self.session_stats.get("turns", 0)
            note = (f"{stamp} - session {self.session_id}: {turns} turns, "
                    f"{tools_used} tool calls")
            self.d.wiki.append("Notes", note)
        except Exception:
            pass


def build_agent(
    cfg: Config = CONFIG,
    on_tool_call=None,
    on_chunk=None,
    confirm_callback=None,
    cancel: threading.Event | None = None,
) -> tuple[Agent, AgentDeps]:
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
    scratch = Scratchpad.from_workspace(cfg.workspace)
    scratch.start_session()

    def confirm(cmd: str) -> bool:
        return bool(confirm_callback and confirm_callback(cmd))

    shell = ShellGate(
        cfg.bash_allowlist, cfg.bash_timeout, cfg.workspace, confirm,
        sandbox_mode=cfg.sandbox,
    )
    tools = build_tools(cfg.workspace, shell)
    deps = AgentDeps(
        cfg=cfg, brain=brain, store=store, wiki=wiki, scratch=scratch,
        tools=tools, shell=shell,
        on_tool_call=on_tool_call, on_chunk=on_chunk, cancel=cancel,
    )
    sid = store.start_session()
    agent = Agent(deps, sid)
    return agent, deps
