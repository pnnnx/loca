"""Клиенты моделей: Ollama (/api/chat) и llama.cpp (llama-server, OpenAI API) + разбор вызовов из текста.

Ollama /api/chat client with streaming and a fallback tool-call parser.

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
    backend = "ollama"

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


# ---------------------------------------------------------------- llama.cpp
def to_openai_messages(messages: list[dict]) -> list[dict]:
    """Ollama-формат истории → OpenAI-формат, который понимает llama-server.

    Отличия: у вызова инструмента нужен id, arguments — JSON-строка, а ответ
    инструмента ссылается на вызов через tool_call_id (у Ollama — tool_name по порядку).
    """
    out: list[dict] = []
    pending: list[str] = []        # id вызовов последнего ответа модели, ещё без результата
    counter = 0
    for m in messages:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            calls = []
            pending = []
            for tc in m["tool_calls"]:
                fn = tc.get("function") or {}
                counter += 1
                call_id = tc.get("id") or f"call_{counter}"
                args = fn.get("arguments", {})
                calls.append({"id": call_id, "type": "function",
                              "function": {"name": fn.get("name", ""),
                                           "arguments": args if isinstance(args, str)
                                           else json.dumps(args, ensure_ascii=False)}})
                pending.append(call_id)
            out.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": calls})
        elif role == "tool":
            call_id = pending.pop(0) if pending else f"call_orphan_{counter}"
            out.append({"role": "tool", "tool_call_id": call_id, "content": m.get("content", "")})
        else:
            out.append({"role": role, "content": m.get("content", "")})
    return out


class LlamaCppClient:
    """Клиент llama-server (llama.cpp) через его OpenAI-совместимый API.

    Отличия от Ollama, которые важны агенту:
    * контекст задаётся при запуске сервера (`-c`), а не в запросе — при первом
      обращении читаем его из /props и подставляем в num_ctx, чтобы сжатие
      срабатывало по реальному размеру;
    * модель одна — та, с которой запущен сервер; `-m` в loca здесь только подпись;
    * вызовы инструментов работают при запуске сервера с `--jinja`;
    * префикс промпта сервер кэширует сам (cache_prompt), поэтому длинная история
      агента не пересчитывается целиком на каждом шаге.
    """

    backend = "llamacpp"

    def __init__(self, cfg: dict):
        host = cfg.get("host") or ""
        if not host or "11434" in host:          # адрес Ollama по умолчанию — не наш
            host = cfg.get("llamacpp_host", "http://127.0.0.1:8080")
        self.host = host.rstrip("/")
        if not self.host.startswith("http"):
            self.host = "http://" + self.host
        self.model = cfg.get("model") or "local"
        self.num_ctx = int(cfg["num_ctx"])
        self.temperature = float(cfg["temperature"])
        self.think = cfg.get("think")
        self.timeout = int(cfg.get("request_timeout", 600))
        self.max_tokens = int(cfg.get("max_output_tokens", 4096))
        self._cfg = cfg
        self._synced = False

    # ------------------------------------------------------------------
    def _open(self, req, timeout=None):
        try:
            return urllib.request.urlopen(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            if "tools" in detail and ("jinja" in detail or "not supported" in detail):
                raise LLMError("llama-server не принимает tools: перезапустите его с флагом --jinja") from e
            if "exceed" in detail and "context" in detail:
                raise LLMError(f"Запрос не влез в контекст сервера ({self.num_ctx}): "
                               "/compact или перезапустите llama-server с большим -c") from e
            raise LLMError(f"llama-server HTTP {e.code}: {detail[:500]}") from e
        except (urllib.error.URLError, ConnectionError, socket.timeout) as e:
            raise LLMError(
                f"Не удалось подключиться к llama-server на {self.host} ({e}). Запустите его, например:\n"
                "  llama-server -m модель.gguf --jinja -c 16384 -fa on -ctk q8_0 -ctv q8_0 --port 8080\n"
                "(или bin/llama.sh)") from e

    def _get(self, path: str, timeout: int = 5):
        with self._open(urllib.request.Request(self.host + path), timeout) as r:
            return json.load(r)

    def sync(self) -> None:
        """Подтягивает реальный размер контекста и имя модели с сервера (один раз)."""
        if self._synced:
            return
        self._synced = True
        try:
            props = self._get("/props")
        except LLMError:
            return
        settings = props.get("default_generation_settings") or {}
        n_ctx = settings.get("n_ctx") or props.get("n_ctx")
        if isinstance(n_ctx, int) and n_ctx > 0:
            self.num_ctx = n_ctx
            self._cfg["num_ctx"] = n_ctx
        path = props.get("model_path") or settings.get("model") or ""
        if path and self.model in ("local", "", None):
            self.model = path.rsplit("/", 1)[-1]

    def list_models(self) -> list[str]:
        try:
            data = self._get("/v1/models")
        except LLMError as e:
            raise LLMError(f"llama-server недоступен на {self.host}: {e}") from e
        return [m.get("id", "") for m in data.get("data", [])]

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             on_token: Callable[[str], None] | None = None,
             on_think: Callable[[str], None] | None = None,
             think: bool | None = None) -> LLMResponse:
        self.sync()
        body: dict = {
            "model": self.model,
            "messages": to_openai_messages(messages),
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": self.temperature,
            "cache_prompt": True,
            # без лимита сервер генерирует до конца контекста: маленькая модель
            # может зациклиться на повторе и съесть весь ход
            "max_tokens": self.max_tokens,
        }
        if tools:
            body["tools"] = tools
        use_think = self.think if think is None else think
        if use_think is not None:      # qwen3/3.5: гибридный режим размышлений через шаблон
            body["chat_template_kwargs"] = {"enable_thinking": bool(use_think)}

        req = urllib.request.Request(self.host + "/v1/chat/completions",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        resp = LLMResponse()
        content, thinking = [], []
        calls: dict[int, dict] = {}
        started = __import__("time").time()
        with self._open(req) as r:
            for raw in r:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if chunk.get("error"):
                    err = chunk["error"]
                    raise LLMError(err.get("message") if isinstance(err, dict) else str(err))
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("reasoning_content"):
                        thinking.append(delta["reasoning_content"])
                        if on_think:
                            on_think(delta["reasoning_content"])
                    if delta.get("content"):
                        content.append(delta["content"])
                        if on_token:
                            on_token(delta["content"])
                    for tc in delta.get("tool_calls") or []:
                        slot = calls.setdefault(tc.get("index", len(calls)), {"name": "", "args": ""})
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] += fn["name"]
                        if fn.get("arguments"):
                            slot["args"] += fn["arguments"] if isinstance(fn["arguments"], str) \
                                else json.dumps(fn["arguments"])
                usage = chunk.get("usage")
                if usage:
                    resp.prompt_tokens = usage.get("prompt_tokens", 0) or 0
                    resp.output_tokens = usage.get("completion_tokens", 0) or 0
                timings = chunk.get("timings")
                if timings and not resp.prompt_tokens:
                    resp.prompt_tokens = (timings.get("prompt_n", 0) or 0) + (timings.get("cache_n", 0) or 0)
                    resp.output_tokens = timings.get("predicted_n", 0) or 0
        resp.seconds = __import__("time").time() - started
        for _i, slot in sorted(calls.items()):
            if slot["name"]:
                resp.tool_calls.append(ToolCall(slot["name"], _coerce_args(slot["args"] or "{}")))
        resp.content = "".join(content)
        resp.thinking = "".join(thinking)
        resp.content = re.sub(r"<think>.*?</think>\s*", "", resp.content, flags=re.S)
        if not resp.tool_calls and tools:
            names = [t["function"]["name"] for t in tools]
            found, cleaned = parse_text_tool_calls(resp.content, names)
            if found:
                resp.tool_calls, resp.content, resp.parsed_from_text = found, cleaned, True
        return resp


def make_client(cfg: dict):
    backend = str(cfg.get("backend", "ollama")).lower().replace(".", "").replace("-", "")
    if backend in ("llamacpp", "llama", "llamaserver"):
        return LlamaCppClient(cfg)
    return OllamaClient(cfg)
