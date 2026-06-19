"""Gemini provider implementation."""

from __future__ import annotations

import os
from google import genai
from google.genai import types
from ssr.providers.base import AbstractProvider
from ssr.agent.tools import ToolKit
from ssr.integrations.mcp_client import is_mcp_tool_name

class GeminiProvider(AbstractProvider):
    def __init__(self, settings, entry, agent_instance=None):
        super().__init__(settings, entry, agent_instance)
        api_key = os.environ.get(entry.api_key_env) or settings.gemini_api_key
        self.client = genai.Client(api_key=api_key)

    def complete(
        self,
        contents: list,
        system_instruction: str,
        toolkit: ToolKit,
        max_iters: int,
        tag: str = "ssr",
    ) -> str:
        dispatch = {fn.__name__: fn for fn in toolkit.callables()}
        tools = list(toolkit.callables())
        
        mcp_param = None
        if self.agent_instance is not None:
            mcp_param = self.agent_instance._mcp_tools_parameter()
            
        if mcp_param is not None:
            tools.append(mcp_param)
            
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            tools=tools,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

        for _ in range(max_iters):
            resp = self.client.models.generate_content(
                model=self.model_name, contents=contents, config=config
            )
            candidate = (resp.candidates or [None])[0]
            if candidate is None or candidate.content is None:
                return (getattr(resp, "text", None) or "(no response)").strip()
            parts = candidate.content.parts or []
            calls = [p.function_call for p in parts if getattr(p, "function_call", None)]
            text = " ".join(p.text for p in parts if getattr(p, "text", None)).strip()
            contents.append(candidate.content)
            if not calls:
                if text:
                    return text
                return "(no response)"
                
            if text and self.agent_instance is not None:
                self.agent_instance._emit("thinking", tag=tag, text=text)
                
            fr_parts = []
            for call in calls:
                fn = dispatch.get(call.name)
                args = dict(call.args or {})
                if self.agent_instance is not None:
                    self.agent_instance._emit("tool_call", tag=tag, name=call.name, args=args)
                
                if is_mcp_tool_name(call.name) and self.agent_instance is not None:
                    try:
                        result = self.agent_instance.mcp_manager.call_tool(call.name, args)
                    except Exception as e:
                        result = f"ERROR: {type(e).__name__}: {e}"
                elif fn is None:
                    result = f"ERROR: unknown tool {call.name}"
                else:
                    try:
                        result = fn(**args)
                    except Exception as e:
                        result = f"ERROR: {type(e).__name__}: {e}"
                        
                if self.agent_instance is not None:
                    self.agent_instance._emit("tool_result", tag=tag, name=call.name, result=str(result))
                    
                fr_parts.append(
                    types.Part.from_function_response(
                        name=call.name, response={"result": str(result)}
                    )
                )
            contents.append(types.Content(role="tool", parts=fr_parts))
            
        return "(reached tool-call limit without a final answer)"
