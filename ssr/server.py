"""OpenAI compatible server."""

import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from ssr.config import load_settings
from ssr.agent.core import SSRAgent

class OpenAIApiHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == '/v1/chat/completions':
            content_length = int(self.headers['Content-Length'])
            post_data = self.rfile.read(content_length)
            req = json.loads(post_data.decode('utf-8'))

            settings = load_settings()
            agent = SSRAgent(settings)
            reply = agent.invoke(req)

            resp = {
                "id": "chatcmpl-123",
                "object": "chat.completion",
                "created": 1677652288,
                "model": req.get("model", settings.default_model),
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": reply,
                    },
                    "finish_reason": "stop"
                }],
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0
                }
            }

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(resp).encode('utf-8'))
        else:
            self.send_response(404)
            self.end_headers()

def serve(port=8000):
    server_address = ('', port)
    httpd = HTTPServer(server_address, OpenAIApiHandler)
    print(f"SSR OpenAI compatible API starting on port {port}...")
    httpd.serve_forever()
