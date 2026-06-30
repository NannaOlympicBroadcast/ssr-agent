"""Tests for the HTTP+SSE MCP client transport."""

from __future__ import annotations

import json
import queue
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import pytest

from ssr.config import Settings
from ssr.integrations.mcp_client import (
    MCPManager,
    MCPSSEServer,
    MCPServer,
)


class SSETestServer(BaseHTTPRequestHandler):
    response_queue = queue.Queue()
    requests_received = []
    get_paths = []

    def log_message(self, format, *args):
        pass  # suppress logging to stderr

    def do_GET(self):
        if self.path.startswith("/mcp"):
            self.get_paths.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(b"event: endpoint\ndata: /mcp/messages?session=123\n\n")
            self.wfile.flush()
            
            while True:
                try:
                    msg = self.response_queue.get(timeout=1.0)
                    if msg is None:  # sentinel to shutdown
                        break
                    event_str = f"event: message\ndata: {json.dumps(msg)}\n\n"
                    self.wfile.write(event_str.encode('utf-8'))
                    self.wfile.flush()
                except queue.Empty:
                    try:
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                    except Exception:
                        break
                except Exception:
                    break

    def do_POST(self):
        if self.path.startswith("/mcp/messages"):
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            msg = json.loads(post_data.decode('utf-8'))
            self.requests_received.append(msg)
            
            msg_id = msg.get("id")
            method = msg.get("method")
            
            response = {"jsonrpc": "2.0", "id": msg_id}
            if method == "initialize":
                response["result"] = {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "serverInfo": {"name": "test-mock-server", "version": "1.0.0"}
                }
            elif method == "tools/list":
                response["result"] = {
                    "tools": [
                        {
                            "name": "hello_sse",
                            "description": "A test SSE tool",
                            "inputSchema": {"type": "object"}
                        }
                    ]
                }
            elif method == "tools/call":
                tool_name = msg.get("params", {}).get("name")
                response["result"] = {
                    "content": [{"type": "text", "text": f"Called {tool_name}"}]
                }
            
            self.response_queue.put(response)
            
            self.send_response(202)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')
            self.wfile.flush()


@pytest.fixture()
def sse_server():
    SSETestServer.response_queue = queue.Queue()
    SSETestServer.requests_received = []
    SSETestServer.get_paths = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), SSETestServer)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
            
    yield f"http://127.0.0.1:{port}"
    
    SSETestServer.response_queue.put(None)
    server.shutdown()
    server.server_close()
    thread.join()



@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    home.mkdir()
    proj.mkdir()
    s = Settings(home=home, project_dir=proj, default_model="gemini-3.1-flash-lite")
    s.ensure_dirs()
    return s


def test_sse_client_handshake_and_call(sse_server):
    # Connect client
    server = MCPSSEServer(name="test_sse", url=f"{sse_server}/mcp")
    try:
        tools = server.start()
        names = {t.name for t in tools}
        assert names == {"hello_sse"}
        
        # Test call tool
        res = server.call("hello_sse", {})
        assert res == "Called hello_sse"
        
        # Verify messages sent
        methods_sent = [m.get("method") for m in SSETestServer.requests_received]
        assert "initialize" in methods_sent
        assert "tools/list" in methods_sent
        assert "tools/call" in methods_sent
    finally:
        server.stop()


def test_sse_client_preserves_url_query_key(sse_server):
    # A key embedded in the URL (the Hosted Tools pattern
    # ``/sse/integrations?key=<mcp_key>``) must reach the server on the GET.
    # Regression for httpx replacing the URL query whenever ``params`` is passed
    # (even an empty dict), which stripped the key and produced a 401.
    server = MCPSSEServer(name="keyed_sse", url=f"{sse_server}/mcp?key=SECRET123")
    try:
        server.start()
        assert SSETestServer.get_paths, "server never received the GET"
        assert any("key=SECRET123" in p for p in SSETestServer.get_paths), (
            f"key was stripped from the SSE GET: {SSETestServer.get_paths}"
        )
    finally:
        server.stop()


def test_from_config_sse_parsing(settings):
    cfg = {
        "mcpServers": {
            "my_sse_server": {
                "url": "http://example.com/mcp",
                "headers": {"Authorization": "Bearer token"},
                "query_params": {"foo": "bar"},
            },
            "my_stdio_server": {
                "command": "python",
                "args": ["-m", "http.server"],
            }
        }
    }
    path = settings.home / "mcp.json"
    path.write_text(json.dumps(cfg), "utf-8")
    
    mgr = MCPManager.from_config(path)
    assert len(mgr._servers) == 2
    
    sse = mgr._servers["my_sse_server"]
    assert isinstance(sse, MCPSSEServer)
    assert sse.url == "http://example.com/mcp"
    assert sse.headers == {"Authorization": "Bearer token"}
    assert sse.query_params == {"foo": "bar"}
    
    stdio = mgr._servers["my_stdio_server"]
    assert isinstance(stdio, MCPServer)
    assert stdio.command == "python"
