"""Поддельный llama-server (OpenAI-совместимый /v1/chat/completions со стримингом SSE)."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeLlamaCpp:
    def __init__(self, script, n_ctx: int = 8192, model_path: str = "/models/qwen-test.gguf"):
        """script: list of dicts {content?, reasoning?, tool_calls?: [(name, args)]} or callables(messages)->dict."""
        self.script = list(script)
        self.requests: list[dict] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, obj):
                body = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.startswith("/props"):
                    self._json({"default_generation_settings": {"n_ctx": n_ctx}, "model_path": model_path})
                elif self.path.startswith("/v1/models"):
                    self._json({"data": [{"id": model_path.rsplit('/', 1)[-1]}]})
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(req)
                step = outer.script.pop(0) if outer.script else {"content": "done"}
                if callable(step):
                    step = step(req["messages"])
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()

                def send(delta, **extra):
                    chunk = {"choices": [{"index": 0, "delta": delta}], **extra}
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())

                if step.get("reasoning"):
                    send({"reasoning_content": step["reasoning"]})
                content = step.get("content", "")
                for piece in [content[i:i + 7] for i in range(0, len(content), 7)]:
                    send({"content": piece})
                for i, (name, args) in enumerate(step.get("tool_calls", [])):
                    raw = json.dumps(args, ensure_ascii=False)
                    half = len(raw) // 2
                    # как настоящий сервер: имя и id в первом куске, аргументы частями
                    send({"tool_calls": [{"index": i, "id": f"srv{i}", "type": "function",
                                          "function": {"name": name, "arguments": raw[:half]}}]})
                    send({"tool_calls": [{"index": i, "function": {"arguments": raw[half:]}}]})
                self.wfile.write(("data: " + json.dumps({
                    "choices": [], "usage": {"prompt_tokens": 900, "completion_tokens": 40}}) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
