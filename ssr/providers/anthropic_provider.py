"""Anthropic provider implementation."""

from __future__ import annotations

import os
import json
import base64
import inspect
from google.genai import types
from ssr.providers.base import AbstractProvider
from ssr.agent.tools import ToolKit
from ssr.integrations.mcp_client import is_mcp_tool_name

class AnthropicProvider(AbstractProvider):
    def __init__(self, settings, entry, agent_instance=None):
        super().__init__(settings, entry, agent_instance)
        api_key = getattr(entry, "api_key", None) or os.environ.get(entry.api_key_env) or os.environ.get("ANTHROPIC_API_KEY") or "fake-key"
        
        import anthropic
        self.client = anthropic.Anthropic(api_key=api_key)

    def complete(
        self,
        contents: list,
        system_instruction: str,
        toolkit: ToolKit,
        max_iters: int,
        tag: str = "ssr",
    ) -> str:
        dispatch = {fn.__name__: fn for fn in toolkit.callables()}

        def fn_to_tool(fn):
            name = fn.__name__
            doc = fn.__doc__ or ""
            description = doc.strip().split("\n")[0] if doc else f"Call {name}"
            
            sig = inspect.signature(fn)
            properties = {}
            required = []
            param_docs = {}
            
            lines = doc.strip().split("\n")
            current_section = "desc"
            for line in lines:
                line_strip = line.strip()
                if not line_strip:
                    continue
                if line_strip.lower().startswith("args:"):
                    current_section = "args"
                    continue
                if current_section == "args":
                    if ":" in line_strip:
                        p_name, p_doc = line_strip.split(":", 1)
                        param_docs[p_name.strip()] = p_doc.strip()
            
            for param_name, param in sig.parameters.items():
                if param_name == "self":
                    continue
                param_type = "string"
                if param.annotation == int:
                    param_type = "integer"
                elif param.annotation == bool:
                    param_type = "boolean"
                elif param.annotation == float:
                    param_type = "number"
                elif param.annotation == list or getattr(param.annotation, "__origin__", None) == list:
                    param_type = "array"
                
                param_desc = param_docs.get(param_name, f"Parameter {param_name}")
                p_schema = {"type": param_type, "description": param_desc}
                if param_type == "array":
                    p_schema["items"] = {"type": "string"}
                
                properties[param_name] = p_schema
                if param.default == inspect.Parameter.empty:
                    required.append(param_name)
                    
            return {
                "name": name,
                "description": description[:1024],
                "input_schema": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                }
            }

        # Translate tools
        tools = [fn_to_tool(fn) for fn in toolkit.callables()]
        mcp_param = None
        if self.agent_instance is not None:
            mcp_param = self.agent_instance._mcp_tools_parameter()
            
        if mcp_param is not None:
            for dec in mcp_param.function_declarations:
                tools.append({
                    "name": dec.name,
                    "description": (dec.description or "")[:1024],
                    "input_schema": dec.parameters_json_schema or {"type": "object", "properties": {}}
                })

        for _ in range(max_iters):
            if self.agent_instance is not None and self.agent_instance.should_stop(tag):
                return "⏹ Task stopped by user (/stop)."
            # Translate Gemini contents to Anthropic messages
            messages = []
            
            for content in contents:
                role = content.role
                if role == "model":
                    role = "assistant"
                
                text_content = ""
                tool_calls = []
                image_blocks = []
                tool_result_blocks = []

                parts = content.parts or []
                for part in parts:
                    if getattr(part, "text", None):
                        text_content += part.text
                    elif getattr(part, "function_call", None):
                        tool_calls.append({
                            "type": "tool_use",
                            "id": f"call_{getattr(part, 'function_call').name}",
                            "name": getattr(part, "function_call").name,
                            "input": getattr(part, "function_call").args or {}
                        })
                    elif getattr(part, "function_response", None):
                        resp = getattr(part, "function_response")
                        tool_result_blocks.append({
                            "type": "tool_result",
                            "tool_use_id": f"call_{resp.name}",
                            "content": str(resp.response.get("result", "") if resp.response else ""),
                        })
                    elif getattr(part, "inline_data", None) is not None:
                        # Raw image bytes queued by a tool (e.g. arm_get_camera) or
                        # attached to the initial turn (run_parts) — without this
                        # branch these silently vanish and the model never sees them.
                        blob = part.inline_data
                        image_blocks.append({
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": blob.mime_type or "image/png",
                                "data": base64.b64encode(blob.data).decode("ascii"),
                            },
                        })

                if tool_result_blocks:
                    # ALL tool_result blocks for this round (+ any images the tools
                    # produced) must ride in a SINGLE user message: Anthropic
                    # requires strict user/assistant alternation, so one tool_result
                    # per message would either violate that (multiple tool_use
                    # blocks need their results batched together) or silently drop
                    # any image collected alongside a "tool"-role content, since the
                    # generic branch below is skipped for role == "tool".
                    messages.append({"role": "user", "content": tool_result_blocks + image_blocks})
                elif role != "tool":
                    content_list = []
                    if text_content:
                        content_list.append({"type": "text", "text": text_content})
                    for tc in tool_calls:
                        content_list.append(tc)
                    content_list.extend(image_blocks)

                    if content_list:
                        messages.append({"role": role, "content": content_list})

            # Call Anthropic completion
            resp = self.client.messages.create(
                model=self.model_name,
                system=system_instruction,
                messages=messages,
                tools=tools if tools else None,
                max_tokens=4000,
            )

            text = ""
            anthropic_calls = []
            for block in resp.content:
                if block.type == "text":
                    text += block.text
                elif block.type == "tool_use":
                    anthropic_calls.append(block)

            # Append assistant message to contents
            gp_parts = []
            if text:
                gp_parts.append(types.Part(text=text))
                if self.agent_instance is not None:
                    self.agent_instance._emit("thinking", tag=tag, text=text)
                
            for tc in anthropic_calls:
                call_args = dict(tc.input or {})
                gp_parts.append(
                    types.Part(
                        function_call=types.FunctionCall(
                            name=tc.name,
                            args=call_args
                        )
                    )
                )
                if self.agent_instance is not None:
                    self.agent_instance._emit("tool_call", tag=tag, name=tc.name, args=call_args)
                
            contents.append(types.Content(role="model", parts=gp_parts))

            if not anthropic_calls:
                if text:
                    return text
                return "(no response)"

            # Execute tool calls
            fr_parts = []
            image_parts = []
            for tc in anthropic_calls:
                name = tc.name
                args = dict(tc.input or {})
                fn = dispatch.get(name)

                if is_mcp_tool_name(name) and self.agent_instance is not None:
                    try:
                        result = self.agent_instance.mcp_manager.call_tool(
                            name, args, agent=self.agent_instance)
                    except Exception as e:
                        result = f"ERROR: {type(e).__name__}: {e}"
                elif fn is None:
                    result = f"ERROR: unknown tool {name}"
                else:
                    try:
                        result = fn(**args)
                    except Exception as e:
                        result = f"ERROR: {type(e).__name__}: {e}"

                if self.agent_instance is not None:
                    self.agent_instance._emit("tool_result", tag=tag, name=name, result=str(result))

                fr_parts.append(
                    types.Part.from_function_response(
                        name=name, response={"result": str(result)}
                    )
                )
                # See gemini.py for why: a tool's return value is text-only, so a
                # tool that produced an image (e.g. arm_get_camera) must queue the
                # raw bytes for the model to actually see, not just read a caption.
                if self.agent_instance is not None:
                    for img in self.agent_instance.take_tool_images():
                        image_parts.append(
                            types.Part.from_bytes(data=img["data"], mime_type=img.get("mime_type") or "image/png")
                        )
            # Image parts ride in the SAME Content as the function_response(s), not
            # a separate trailing message — Anthropic requires strict user/assistant
            # alternation, so a lone extra "user" turn right after the tool_result
            # "user" turn would be rejected. The translation loop above merges them
            # into that same tool_result message (see the elif inline_data branch).
            contents.append(types.Content(role="tool", parts=fr_parts + image_parts))

        return "(reached tool-call limit without a final answer)"
