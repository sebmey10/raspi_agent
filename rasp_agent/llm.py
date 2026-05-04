from __future__ import annotations

import copy
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
    thinking: str = ""


TOOL_BLOCK_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL)
THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


def _norm_tool_call(data: dict) -> dict | None:
    fn = data.get("function") if isinstance(data.get("function"), dict) else {}
    has_call_shape = bool(fn) or any(k in data for k in ("tool", "arguments", "args", "parameters"))
    name = data.get("name") or data.get("tool") or fn.get("name")
    args = (
        data.get("arguments")
        or data.get("args")
        or data.get("parameters")
        or fn.get("arguments")
        or {}
    )
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {"_raw": args}
    elif not isinstance(args, dict):
        args = {"_raw": args}
    if not name or not has_call_shape:
        return None
    return {"name": name, "arguments": args or {}}


def _iter_tool_call_payloads(data: Any) -> Iterator[dict]:
    if isinstance(data, list):
        for item in data:
            yield from _iter_tool_call_payloads(item)
        return
    if not isinstance(data, dict):
        return
    if isinstance(data.get("tool_calls"), list):
        for item in data["tool_calls"]:
            yield from _iter_tool_call_payloads(item)
    yield data


def _json_objects(text: str) -> Iterator[dict]:
    decoder = json.JSONDecoder()
    i = 0
    while i < len(text):
        start = text.find("{", i)
        if start < 0:
            break
        try:
            obj, end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            i = start + 1
            continue
        if isinstance(obj, dict):
            yield obj
        i = start + max(end, 1)


def _single_json_payload(text: str) -> Any | None:
    decoder = json.JSONDecoder()
    stripped = text.strip()
    if not stripped:
        return None
    try:
        obj, end = decoder.raw_decode(stripped)
    except json.JSONDecodeError:
        return None
    if stripped[end:].strip():
        return None
    return obj


def _only_fenced_json(text: str) -> str | None:
    stripped = text.strip()
    m = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.DOTALL)
    return m.group(1) if m else None


def _try_extract_tool_calls_from_text(text: str) -> list[dict]:
    """Fallback for models that emit tool calls as text. Three formats supported:
       1. <tool_call>{"name": ..., "arguments": ...}</tool_call>
       2. Entire response is ```json { "name": ..., "arguments": ... } ```
       3. Entire response is {"name": "tool", "arguments": {...}}.

       Fenced or bare JSON embedded in prose is intentionally ignored so a final
       answer with an example cannot be mistaken for an executable tool call.
    """
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def _add(data: dict) -> None:
        for payload in _iter_tool_call_payloads(data):
            norm = _norm_tool_call(payload)
            if norm:
                key = (norm["name"], json.dumps(norm["arguments"], sort_keys=True))
                if key not in seen:
                    seen.add(key)
                    out.append(norm)

    for m in TOOL_BLOCK_RE.finditer(text):
        for obj in _json_objects(m.group(1)):
            _add(obj)
    if out:
        return out

    fenced = _only_fenced_json(text)
    payload = _single_json_payload(fenced) if fenced is not None else _single_json_payload(text)
    for obj in _iter_tool_call_payloads(payload):
        _add(obj)
    return out


class OllamaClient:
    def __init__(self, base_url: str, keep_alive: int | str, timeout: float = 1800.0):
        self.base_url = base_url.rstrip("/")
        self.keep_alive = keep_alive
        self.client = httpx.Client(timeout=timeout)
        self._unsupported: dict[str, set[str]] = {}

    def supports_tools(self, model: str) -> bool:
        return "tools" not in self._unsupported.get(model, set())

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
        think: bool | str | None = None,
    ) -> LLMResponse:
        opts: dict[str, Any] = {"num_ctx": ctx, "temperature": temperature}
        if num_predict is not None:
            opts["num_predict"] = num_predict
        if num_thread is not None:
            opts["num_thread"] = num_thread
        unsupported = self._unsupported.get(model, set())
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": opts,
        }
        tools_requested = bool(tools)
        if tools and "tools" not in unsupported:
            payload["tools"] = tools
        if think is not None and "think" not in unsupported:
            payload["think"] = think

        data = self._post_chat_with_fallback(model, payload)
        if tools_requested and "tools" in self._unsupported.get(model, set()):
            data["_rasp_tools_unsupported"] = True
        msg = data.get("message", {}) or {}
        content = msg.get("content", "") or ""
        thinking = msg.get("thinking", "") or ""
        if "<think>" in content:
            content = THINK_RE.sub("", content).strip()
        tool_calls_raw = msg.get("tool_calls") or []

        normalized: list[dict] = []
        for tc in tool_calls_raw:
            if isinstance(tc, dict):
                norm = _norm_tool_call(tc)
                if norm:
                    normalized.append(norm)

        if not normalized and content:
            normalized = _try_extract_tool_calls_from_text(content)

        return LLMResponse(content=content, tool_calls=normalized, raw=data, tier=tier, thinking=thinking)

    def _post_chat_with_fallback(self, model: str, payload: dict[str, Any]) -> dict:
        initial_error: httpx.HTTPStatusError | None = None
        try:
            r = self.client.post(f"{self.base_url}/api/chat", json=payload)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code not in (400, 422):
                raise
            initial_error = e

        variants: list[tuple[str, dict[str, Any]]] = []
        if "think" in payload:
            no_think = copy.deepcopy(payload)
            no_think.pop("think", None)
            variants.append(("think", no_think))
        if "tools" in payload:
            no_tools = copy.deepcopy(payload)
            no_tools.pop("tools", None)
            variants.append(("tools", no_tools))
        if "think" in payload and "tools" in payload:
            no_both = copy.deepcopy(payload)
            no_both.pop("think", None)
            no_both.pop("tools", None)
            variants.append(("think,tools", no_both))

        last_error: httpx.HTTPStatusError | None = None
        for disabled, candidate in variants:
            try:
                r = self.client.post(f"{self.base_url}/api/chat", json=candidate)
                r.raise_for_status()
                self._unsupported.setdefault(model, set()).update(disabled.split(","))
                data = r.json()
                data["_rasp_disabled_features"] = disabled
                return data
            except httpx.HTTPStatusError as e:
                last_error = e
                continue
        if last_error is not None:
            raise last_error
        if initial_error is not None:
            raise initial_error
        raise RuntimeError("unreachable chat fallback state")

    def stream_chat(
        self,
        model: str,
        messages: list[dict],
        ctx: int,
        temperature: float = 0.4,
        think: bool | str | None = None,
    ) -> Iterator[str]:
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "keep_alive": self.keep_alive,
            "options": {"num_ctx": ctx, "temperature": temperature},
        }
        if think is not None:
            payload["think"] = think
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
                 num_thread: int | None = None,
                 think_fast: bool | str | None = False,
                 think_reasoner: bool | str | None = None):
        self.client = client
        self.models = {TIER_FAST: model_fast, TIER_REASONER: model_reasoner}
        self.ctx = {TIER_FAST: ctx_fast, TIER_REASONER: ctx_reasoner}
        self.num_predict = {TIER_FAST: num_predict_fast, TIER_REASONER: num_predict_reasoner}
        self.num_thread = num_thread
        self.think = {TIER_FAST: think_fast, TIER_REASONER: think_reasoner}

    def chat(self, messages: list[dict], tools: list[dict] | None, tier: str) -> LLMResponse:
        return self.client.chat(
            model=self.models[tier],
            messages=messages,
            tools=tools,
            ctx=self.ctx[tier],
            tier=tier,
            num_predict=self.num_predict[tier],
            num_thread=self.num_thread,
            think=self.think[tier],
        )

    def supports_tools(self, tier: str) -> bool:
        return self.client.supports_tools(self.models[tier])

    def stream(self, messages: list[dict], tier: str) -> Iterator[str]:
        return self.client.stream_chat(
            model=self.models[tier],
            messages=messages,
            ctx=self.ctx[tier],
            think=self.think[tier],
        )

    def warm(self, tier: str) -> None:
        self.client.warm(self.models[tier])
