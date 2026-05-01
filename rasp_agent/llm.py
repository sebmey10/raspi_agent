from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterator

import httpx


TIER_FAST = "fast"
TIER_REASONER = "reasoner"


@dataclass
class LLMResponse:
    content: str
    tool_calls: list[dict]
    raw: dict
    tier: str


TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
NAKED_TOOL_RE = re.compile(
    r"\{\s*\"(?:name|tool)\"\s*:\s*\"([a-z_]+)\"\s*,\s*\"(?:arguments|args|parameters)\"\s*:\s*(\{.*?\})\s*\}",
    re.DOTALL,
)


def _norm_tool_call(data: dict) -> dict | None:
    name = data.get("name") or data.get("tool")
    args = data.get("arguments") or data.get("args") or data.get("parameters") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {"_raw": args}
    if not name:
        return None
    return {"name": name, "arguments": args or {}}


def _try_extract_tool_calls_from_text(text: str) -> list[dict]:
    """Fallback for models that emit tool calls as text. Three formats supported:
       1. <tool_call>{"name": ..., "arguments": ...}</tool_call>
       2. ```json { "name": ..., "arguments": ... } ```
       3. Bare {"name": "tool", "arguments": {...}} JSON object.
    """
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def _add(raw_json: str) -> None:
        try:
            data = json.loads(raw_json)
        except json.JSONDecodeError:
            return
        if not isinstance(data, dict):
            return
        norm = _norm_tool_call(data)
        if norm:
            key = (norm["name"], json.dumps(norm["arguments"], sort_keys=True))
            if key not in seen:
                seen.add(key)
                out.append(norm)

    for m in TOOL_CALL_RE.finditer(text):
        _add(m.group(1))
    if out:
        return out
    for m in FENCED_JSON_RE.finditer(text):
        _add(m.group(1))
    if out:
        return out
    for m in NAKED_TOOL_RE.finditer(text):
        _add(m.group(0))
    return out


class OllamaClient:
    def __init__(self, base_url: str, keep_alive: int | str, timeout: float = 1800.0):
        self.base_url = base_url.rstrip("/")
        self.keep_alive = keep_alive
        self.client = httpx.Client(timeout=timeout)

    def warm(self, model: str) -> None:
        try:
            self.client.post(
                f"{self.base_url}/api/generate",
                json={"model": model, "prompt": "", "keep_alive": self.keep_alive},
            )
        except httpx.HTTPError:
            pass

    def list_models(self) -> list[str]:
        try:
            r = self.client.get(f"{self.base_url}/api/tags")
            r.raise_for_status()
            return [m["name"] for m in r.json().get("models", [])]
        except httpx.HTTPError:
            return []

    def chat(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None,
        ctx: int,
        tier: str,
        temperature: float = 0.3,
        num_predict: int | None = None,
        num_thread: int | None = None,
    ) -> LLMResponse:
        opts: dict[str, Any] = {"num_ctx": ctx, "temperature": temperature}
        if num_predict is not None:
            opts["num_predict"] = num_predict
        if num_thread is not None:
            opts["num_thread"] = num_thread
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": opts,
        }
        if tools:
            payload["tools"] = tools

        r = self.client.post(f"{self.base_url}/api/chat", json=payload)
        r.raise_for_status()
        data = r.json()
        msg = data.get("message", {}) or {}
        content = msg.get("content", "") or ""
        tool_calls_raw = msg.get("tool_calls") or []

        normalized: list[dict] = []
        for tc in tool_calls_raw:
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            name = fn.get("name") or tc.get("name")
            args = fn.get("arguments") if isinstance(fn, dict) else None
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"_raw": args}
            if name:
                normalized.append({"name": name, "arguments": args or {}})

        if not normalized and content:
            normalized = _try_extract_tool_calls_from_text(content)

        return LLMResponse(content=content, tool_calls=normalized, raw=data, tier=tier)

    def stream_chat(
        self,
        model: str,
        messages: list[dict],
        ctx: int,
        temperature: float = 0.4,
    ) -> Iterator[str]:
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "keep_alive": self.keep_alive,
            "options": {"num_ctx": ctx, "temperature": temperature},
        }
        with self.client.stream("POST", f"{self.base_url}/api/chat", json=payload) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                chunk = (obj.get("message") or {}).get("content", "")
                if chunk:
                    yield chunk
                if obj.get("done"):
                    break

    def close(self) -> None:
        self.client.close()


class Brain:
    """Wraps OllamaClient with the two-tier escalation knobs."""

    def __init__(self, client: OllamaClient, model_fast: str, model_reasoner: str,
                 ctx_fast: int, ctx_reasoner: int,
                 num_predict_fast: int = 384, num_predict_reasoner: int = 768,
                 num_thread: int | None = None):
        self.client = client
        self.models = {TIER_FAST: model_fast, TIER_REASONER: model_reasoner}
        self.ctx = {TIER_FAST: ctx_fast, TIER_REASONER: ctx_reasoner}
        self.num_predict = {TIER_FAST: num_predict_fast, TIER_REASONER: num_predict_reasoner}
        self.num_thread = num_thread

    def chat(self, messages: list[dict], tools: list[dict] | None, tier: str) -> LLMResponse:
        return self.client.chat(
            model=self.models[tier],
            messages=messages,
            tools=tools,
            ctx=self.ctx[tier],
            tier=tier,
            num_predict=self.num_predict[tier],
            num_thread=self.num_thread,
        )

    def stream(self, messages: list[dict], tier: str) -> Iterator[str]:
        return self.client.stream_chat(
            model=self.models[tier],
            messages=messages,
            ctx=self.ctx[tier],
        )

    def warm(self, tier: str) -> None:
        self.client.warm(self.models[tier])
