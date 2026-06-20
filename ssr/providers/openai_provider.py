"""OpenAI-compatible provider implementation."""

from __future__ import annotations

import os
import json
import inspect
from google.genai import types
from ssr.providers.base import AbstractProvider
from ssr.agent.tools import ToolKit
from ssr.integrations.mcp_client import is_mcp_tool_name

class OpenAIProvider(AbstractProvider):
    def __init__(self, settings, entry, agent_instance=None):
        super().__init__(settings, entry, agent_instance)
        api_key = getattr(entry, "api_key", None) or os.environ.get(entry.api_key_env) or os.environ.get("OPENAI_API_KEY") or "fake-key"
        base_url = entry.base_url or os.environ.get("OPENAI_BASE_URL")
        
        import openai
        self.client = openai.OpenAI(base_url=base_url, api_key=api_key)

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
            for idx, line in enumerate(lines):
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
                "type": "function",
                "function": {
                    "name": name,
                    "description": description[:1024],
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    }
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
                    "type": "function",
                    "function": {
                        "name": dec.name,
                        "description": (dec.description or "")[:1024],
                        "parameters": dec.parameters_json_schema or {"type": "object", "properties": {}}
                    }
                })

        for _ in range(max_iters):
            if self.agent_instance is not None and self.agent_instance.should_stop(tag):
                return "⏹ Task stopped by user (/stop)."
            # Translate Gemini contents to OpenAI messages
            messages = []
            if system_instruction:
                messages.append({"role": "system", "content": system_instruction})
            
            for content in contents:
                role = content.role
                if role == "model":
                    role = "assistant"
                
                text_content = ""
                tool_calls = []
                
                parts = content.parts or []
                for part in parts:
                    if getattr(part, "text", None):
                        text_content += part.text
                    elif getattr(part, "function_call", None):
                        tool_calls.append({
                            "id": getattr(part, "function_call").name,
                            "type": "function",
                            "function": {
                                "name": getattr(part, "function_call").name,
                                "arguments": json.dumps(getattr(part, "function_call").args or {}),
                            }
                        })
                    elif getattr(part, "function_response", None):
                        resp = getattr(part, "function_response")
                        messages.append({
                            "role": "tool",
                            "tool_call_id": resp.name,
                            "name": resp.name,
                            "content": str(resp.response.get("result", "") if resp.response else ""),
                        })
                
                if role != "tool":
                    msg = {"role": role}
                    if text_content:
                        msg["content"] = text_content
                    if tool_calls:
                        msg["tool_calls"] = tool_calls
                    messages.append(msg)

            # Call OpenAI completion
            resp = self.client.chat.completions.create(
                model=self.model_name,
                messages=messages,
                tools=tools if tools else None,
                tool_choice="auto" if tools else None,
            )

            message = resp.choices[0].message
            text = message.content or ""
            openai_calls = message.tool_calls or []

            # Append assistant message to contents
            gp_parts = []
            if text:
                gp_parts.append(types.Part(text=text))
                if self.agent_instance is not None:
                    self.agent_instance._emit("thinking", tag=tag, text=text)
                
            for tc in openai_calls:
                call_args = json.loads(tc.function.arguments)
                gp_parts.append(
                    types.Part(
                        function_call=types.FunctionCall(
                            name=tc.function.name,
                            args=call_args
                        )
                    )
                )
                if self.agent_instance is not None:
                    self.agent_instance._emit("tool_call", tag=tag, name=tc.function.name, args=call_args)
                
            contents.append(types.Content(role="model", parts=gp_parts))

            if not openai_calls:
                if text:
                    return text
                return "(no response)"

            # Execute tool calls
            fr_parts = []
            for tc in openai_calls:
                name = tc.function.name
                args = json.loads(tc.function.arguments)
                fn = dispatch.get(name)
                
                if is_mcp_tool_name(name) and self.agent_instance is not None:
                    try:
                        result = self.agent_instance.mcp_manager.call_tool(name, args)
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
            contents.append(types.Content(role="tool", parts=fr_parts))

        return "(reached tool-call limit without a final answer)"
