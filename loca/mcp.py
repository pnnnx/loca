"""Minimal MCP client over stdio (JSON-RPC 2.0, newline-delimited)."""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading

from .tools.base import Tool, ToolError, truncate

PROTOCOL_VERSION = "2025-06-18"


class MCPError(Exception):
    pass


class MCPServer:
    def __init__(self, name: str, spec: dict):
        self.name = name
        self.spec = spec
        self.proc: subprocess.Popen | None = None
        self._id = 0
        self._responses: "queue.Queue[dict]" = queue.Queue()
        self._lock = threading.Lock()
        self.tools: list[dict] = []

    def start(self, timeout: float = 30) -> None:
        command = self.spec["command"]
        if not shutil.which(command):
            raise MCPError(f"команда не найдена: {command}")
        env = {**os.environ, **self.spec.get("env", {})}
        self.proc = subprocess.Popen(
            [command, *self.spec.get("args", [])], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=env, text=True, bufsize=1, cwd=self.spec.get("cwd"))
        threading.Thread(target=self._reader, daemon=True).start()
        self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "loca", "version": "0.1"}}, timeout)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools = self.request("tools/list", {}, timeout).get("tools", [])
        allowed = self.spec.get("tools")
        self.tools = [t for t in tools if not allowed or t["name"] in allowed]

    def _reader(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                self._responses.put(msg)
            elif "id" in msg and "method" in msg:
                # server->client request (ping, sampling...): answer minimal
                reply = {"jsonrpc": "2.0", "id": msg["id"]}
                if msg["method"] == "ping":
                    reply["result"] = {}
                else:
                    reply["error"] = {"code": -32601, "message": "not supported"}
                self._send(reply)
        self._responses.put({"id": None, "error": {"message": "server exited"}})

    def _send(self, msg: dict) -> None:
        if not self.proc or self.proc.poll() is not None or not self.proc.stdin:
            raise MCPError(f"MCP сервер {self.name} не запущен")
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict, timeout: float = 120) -> dict:
        with self._lock:
            self._id += 1
            rid = self._id
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            while True:
                try:
                    msg = self._responses.get(timeout=timeout)
                except queue.Empty:
                    raise MCPError(f"{self.name}: timeout on {method}")
                if msg.get("id") is None and "error" in msg:
                    raise MCPError(f"{self.name}: {msg['error']['message']}")
                if msg.get("id") != rid:
                    continue
                if "error" in msg:
                    raise MCPError(f"{self.name}: {msg['error'].get('message')}")
                return msg.get("result") or {}

    def call(self, tool: str, args: dict) -> tuple[str, bool]:
        result = self.request("tools/call", {"name": tool, "arguments": args})
        parts = []
        for item in result.get("content", []):
            if item.get("type") == "text":
                parts.append(item["text"])
            elif item.get("type") == "resource":
                parts.append(str(item.get("resource", {}).get("text", "")))
            else:
                parts.append(f"[{item.get('type')} content]")
        if not parts and result.get("structuredContent") is not None:
            parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
        return "\n".join(parts), bool(result.get("isError"))

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class MCPTool(Tool):
    read_only = False
    kind = "other"

    def __init__(self, server: MCPServer, spec: dict):
        self.server = server
        self.remote_name = spec["name"]
        self.name = f"mcp__{server.name}__{spec['name']}"[:64]
        # keep descriptions short: small models have small contexts
        self.description = (spec.get("description") or "").strip()[:300]
        schema = spec.get("inputSchema") or {"type": "object", "properties": {}}
        schema.setdefault("properties", {})
        self.parameters = schema

    def run(self, args, ctx):
        try:
            text, is_error = self.server.call(self.remote_name, args)
        except MCPError as e:
            raise ToolError(str(e))
        if is_error:
            raise ToolError(text or "MCP tool error")
        return truncate(text or "(empty result)", ctx.cfg["max_tool_output"])


class MCPManager:
    def __init__(self, specs: dict, ui):
        self.servers: dict[str, MCPServer] = {}
        self.errors: dict[str, str] = {}
        for name, spec in specs.items():
            if spec.get("disabled"):
                continue
            server = MCPServer(name, spec)
            try:
                server.start()
                self.servers[name] = server
                ui.info(f"MCP {name}: {len(server.tools)} инструментов")
            except Exception as e:  # noqa: BLE001
                server.stop()
                self.errors[name] = str(e)
                ui.warn(f"MCP {name} не запустился: {e}")

    def tools(self) -> list[Tool]:
        return [MCPTool(s, t) for s in self.servers.values() for t in s.tools]

    def stop(self) -> None:
        for s in self.servers.values():
            s.stop()
