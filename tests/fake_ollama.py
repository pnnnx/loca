"""A scripted stand-in for the Ollama HTTP API used by the tests."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeOllama:
    def __init__(self, script):
        """script: list of dicts {content?, tool_calls?: [(name, args)]} or callables(messages)->dict."""
        self.script = list(script)
        self.requests: list[dict] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = json.dumps({"models": [{"name": "fake:1b"}]}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(req)
                step = outer.script.pop(0) if outer.script else {"content": "done"}
                if callable(step):
                    step = step(req["messages"])
                self.send_response(200)
                self.end_headers()
                content = step.get("content", "")
                for piece in [content[i:i + 7] for i in range(0, len(content), 7)] or [""]:
                    self.wfile.write((json.dumps({"message": {"role": "assistant", "content": piece}}) + "\n").encode())
                calls = [{"function": {"name": n, "arguments": a}} for n, a in step.get("tool_calls", [])]
                final = {"message": {"role": "assistant", "content": "", "tool_calls": calls} if calls else
                         {"role": "assistant", "content": ""},
                         "done": True, "prompt_eval_count": 1000, "eval_count": 50, "total_duration": 10**9}
                self.wfile.write((json.dumps(final) + "\n").encode())

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
