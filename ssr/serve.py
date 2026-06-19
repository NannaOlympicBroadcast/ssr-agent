import asyncio
import json
import logging
import time
import uuid
from typing import List, Dict, Any, Optional
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from ssr.config import Settings
from ssr.agent.core import SSRAgent
from ssr.models import ModelsConfig

logger = logging.getLogger(__name__)

app = FastAPI(title="SSR Agent OpenAI-compatible Server")

# Global reference to agent
agent_instance: Optional[SSRAgent] = None

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatCompletionRequest(BaseModel):
    model: Optional[str] = None
    messages: List[ChatMessage]
    stream: Optional[bool] = False
    temperature: Optional[float] = 1.0
    top_p: Optional[float] = 1.0
    n: Optional[int] = 1
    max_tokens: Optional[int] = None
    presence_penalty: Optional[float] = 0.0
    frequency_penalty: Optional[float] = 0.0
    user: Optional[str] = None

@app.get("/v1/models")
async def list_models():
    global agent_instance
    if not agent_instance:
        raise HTTPException(status_code=500, detail="Agent not initialized")
    
    models = agent_instance.models_config.list_models()
    return {
        "object": "list",
        "data": [
            {
                "id": m.id,
                "object": "model",
                "created": int(time.time()),
                "owned_by": m.provider
            }
            for m in models
        ]
    }

@app.post("/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest):
    global agent_instance
    if not agent_instance:
        raise HTTPException(status_code=500, detail="Agent not initialized")
        
    # Extract the last user message
    user_msg = None
    for msg in reversed(request.messages):
        if msg.role == "user":
            user_msg = msg.content
            break
            
    if not user_msg:
        raise HTTPException(status_code=400, detail="No user message found in the messages history")

    completion_id = f"chatcmpl-{uuid.uuid4()}"
    created_time = int(time.time())
    resolved_model = request.model or agent_instance.models_config.get_primary().id

    # If stream is requested
    if request.stream:
        async def stream_generator():
            try:
                # Run the blocking agent task in a threadpool to avoid blocking event loop
                reply = await run_in_threadpool(agent_instance.run, user_msg)
            except Exception as e:
                reply = f"[agent error] {e}"

            # Stream the reply in chunks to mimic a real streaming response
            chunk_size = 12
            for i in range(0, len(reply), chunk_size):
                chunk_text = reply[i:i+chunk_size]
                chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created_time,
                    "model": resolved_model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "content": chunk_text
                            },
                            "finish_reason": None
                        }
                    ]
                }
                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                await asyncio.sleep(0.01)

            # Send stop finish_reason
            stop_chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created_time,
                "model": resolved_model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop"
                    }
                ]
            }
            yield f"data: {json.dumps(stop_chunk, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream_generator(), media_type="text/event-stream")
    else:
        # Non-streaming response
        try:
            reply = await run_in_threadpool(agent_instance.run, user_msg)
        except Exception as e:
            reply = f"[agent error] {e}"

        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": created_time,
            "model": resolved_model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": reply
                    },
                    "finish_reason": "stop"
                }
            ],
            "usage": {
                "prompt_tokens": len(user_msg.split()) if user_msg else 0,
                "completion_tokens": len(reply.split()),
                "total_tokens": (len(user_msg.split()) if user_msg else 0) + len(reply.split())
            }
        }

def run_server(settings: Settings, host: str, port: int):
    global agent_instance
    agent_instance = SSRAgent(settings)
    
    import uvicorn
    uvicorn.run(app, host=host, port=port)
