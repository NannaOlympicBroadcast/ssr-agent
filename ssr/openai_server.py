"""OpenAI-compatible HTTP server for SSR Agent."""
from __future__ import annotations

import time, uuid
from typing import Any

from .api import SSRAgent, Config


def create_app(cwd: str | None = None):
    try:
        from fastapi import FastAPI
    except Exception as e:  # pragma: no cover
        raise RuntimeError("Install fastapi and uvicorn to expose the OpenAI-compatible server") from e
    app = FastAPI(title="SSR Agent OpenAI-compatible API")
    agent = SSRAgent(Config(cwd=cwd))

    @app.post("/v1/chat/completions")
    def chat_completions(req: dict[str, Any]):
        result = agent.invoke({"messages": req.get("messages", [])})
        content = result["content"]
        return {
            "id": "chatcmpl-" + uuid.uuid4().hex,
            "object": "chat.completion",
            "created": int(time.time()),
            "model": req.get("model", "ssr-agent"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        }

    return app
