"""Tiny stdio MCP server with one tool: add(a, b)."""
import json
import sys

for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    method = msg["method"]
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fake", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "add", "description": "Add two numbers",
                             "inputSchema": {"type": "object", "properties": {
                                 "a": {"type": "number"}, "b": {"type": "number"}},
                                 "required": ["a", "b"]}}]}
    elif method == "tools/call":
        args = msg["params"]["arguments"]
        result = {"content": [{"type": "text", "text": str(args["a"] + args["b"])}]}
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "no"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
