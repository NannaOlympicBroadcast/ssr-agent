"""A tiny, real MCP server over stdio used by the test-suite (no mocks).

Implements just enough of the protocol — ``initialize``, ``tools/list`` and
``tools/call`` for two tools (``echo`` and ``add``) — using newline-delimited
JSON-RPC, exactly as the MCP stdio transport specifies.
"""

import json
import sys

# Force output to utf-8
sys.stdin.reconfigure(encoding='utf-8')
sys.stdout.reconfigure(encoding='utf-8')

TOOLS = [
    {
        "name": "echo",
        "description": "Echo the given text back to the caller.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "text to echo"}},
            "required": ["text"],
        },
    },
    {
        "name": "add",
        "description": "Add two numbers and return the sum.",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
        },
    },
]


def _send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _text_result(mid, text, is_error=False):
    _send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}], "isError": is_error}})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        mid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}

        if method == "initialize":
            _send({
                "jsonrpc": "2.0",
                "id": mid,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "echo-test-server", "version": "1.0"},
                },
            })
        elif method == "notifications/initialized":
            continue  # notification: no response
        elif method == "tools/list":
            _send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            if name == "echo":
                _text_result(mid, str(args.get("text", "")))
            elif name == "add":
                try:
                    _text_result(mid, str(args["a"] + args["b"]))
                except Exception as e:  # report through MCP's isError channel
                    _text_result(mid, f"add failed: {e}", is_error=True)
            else:
                _send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown tool {name}"}})
        elif mid is not None:
            _send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}})


if __name__ == "__main__":
    main()
