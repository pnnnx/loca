"""Ollama /api/chat client with streaming and a fallback tool-call parser.

We use the native Ollama API (not the OpenAI-compatible one) because only it
lets us set num_ctx / keep_alive / think per request.
"""
from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Iterable


class LLMError(Exception):
    pass


@dataclass
class ToolCall:
    name: str
    arguments: dict


@dataclass
class LLMResponse:
    content: str = ""
    thinking: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    output_tokens: int = 0
    seconds: float = 0.0
    parsed_from_text: bool = False


_TAGGED = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)
_FENCED = re.compile(r"```(?:json|tool_call)?\s*(\{.*?\})\s*```", re.S)


def _coerce_args(args) -> dict:
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            value = json.loads(args)
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def parse_text_tool_calls(text: str, known: Iterable[str]) -> tuple[list[ToolCall], str]:
    """Small models sometimes print the call instead of using native tool calling."""
    known = set(known)
    calls: list[ToolCall] = []
    cleaned = text
    for pattern in (_TAGGED, _FENCED):
        for m in pattern.finditer(text):
            try:
                obj = json.loads(m.group(1))
            except json.JSONDecodeError:
                continue
            name = obj.get("name") or obj.get("tool") or (obj.get("function") or {}).get("name")
            args = obj.get("arguments", obj.get("parameters", obj.get("args", {})))
            if name in known:
                calls.append(ToolCall(name, _coerce_args(args)))
                cleaned = cleaned.replace(m.group(0), "")
        if calls:
            return calls, cleaned.strip()
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            obj = json.loads(stripped)
            if obj.get("name") in known:
                return [ToolCall(obj["name"], _coerce_args(obj.get("arguments", {})))], ""
        except json.JSONDecodeError:
            pass
    return [], text


class OllamaClient:
    def __init__(self, cfg: dict):
        self.host = cfg["host"].rstrip("/")
        if not self.host.startswith("http"):
            self.host = "http://" + self.host
        self.model = cfg["model"]
        self.num_ctx = int(cfg["num_ctx"])
        self.temperature = float(cfg["temperature"])
        self.think = cfg.get("think")
        self.keep_alive = cfg.get("keep_alive", "30m")
        self.timeout = int(cfg.get("request_timeout", 600))

    # ------------------------------------------------------------------
    def _post(self, path: str, body: dict, timeout: int | None = None):
        req = urllib.request.Request(
            self.host + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        try:
            return urllib.request.urlopen(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            if "does not support tools" in detail:
                raise LLMError(f"Модель {self.model} не поддерживает tools в Ollama. "
                               "Выберите другую (/model qwen3.5:9b).") from e
            if e.code == 404 and "not found" in detail:
                raise LLMError(f"Модель не найдена. Выполните: ollama pull {self.model}") from e
            raise LLMError(f"Ollama HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, ConnectionError, socket.timeout) as e:
            raise LLMError(f"Не удалось подключиться к Ollama на {self.host} ({e}). "
                           "Запущен ли `ollama serve`?") from e

    def list_models(self) -> list[str]:
        req = urllib.request.Request(self.host + "/api/tags")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return [m["name"] for m in json.load(r).get("models", [])]
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"Ollama недоступна на {self.host}: {e}") from e

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             on_token: Callable[[str], None] | None = None,
             on_think: Callable[[str], None] | None = None,
             think: bool | None = None) -> LLMResponse:
        body: dict = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "keep_alive": self.keep_alive,
            "options": {"num_ctx": self.num_ctx, "temperature": self.temperature},
        }
        if tools:
            body["tools"] = tools
        use_think = self.think if think is None else think
        if use_think is not None:
            body["think"] = bool(use_think)

        resp = LLMResponse()
        content, thinking = [], []
        with self._post("/api/chat", body) as r:
            for raw in r:
                if not raw.strip():
                    continue
                chunk = json.loads(raw)
                if chunk.get("error"):
                    raise LLMError(chunk["error"])
                msg = chunk.get("message") or {}
                if msg.get("thinking"):
                    thinking.append(msg["thinking"])
                    if on_think:
                        on_think(msg["thinking"])
                if msg.get("content"):
                    content.append(msg["content"])
                    if on_token:
                        on_token(msg["content"])
                for tc in msg.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        resp.tool_calls.append(ToolCall(fn["name"], _coerce_args(fn.get("arguments"))))
                if chunk.get("done"):
                    resp.prompt_tokens = chunk.get("prompt_eval_count", 0) or 0
                    resp.output_tokens = chunk.get("eval_count", 0) or 0
                    resp.seconds = (chunk.get("total_duration", 0) or 0) / 1e9
        resp.content = "".join(content)
        resp.thinking = "".join(thinking)
        # qwen3 may leak <think> blocks into content when think is off
        resp.content = re.sub(r"<think>.*?</think>\s*", "", resp.content, flags=re.S)
        if not resp.tool_calls and tools:
            names = [t["function"]["name"] for t in tools]
            calls, cleaned = parse_text_tool_calls(resp.content, names)
            if calls:
                resp.tool_calls, resp.content, resp.parsed_from_text = calls, cleaned, True
        return resp
