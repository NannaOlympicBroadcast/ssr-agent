"""Assembles and runs the SSR coding agent.

Primary runtime is Google ADK (``LlmAgent`` + ``Runner``). When ADK is not
importable in the current environment the agent transparently falls back to a
``google-genai`` automatic-function-calling chat loop so it still runs end to
end. Both paths share the same :class:`ToolKit`.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..config import Settings
from ..context_pool.retrieval import Retriever
from ..integrations.mcp_client import MCPManager, MCPTool, is_mcp_tool_name
from .memory import MemoryStore
from .tools import ToolKit

SYSTEM_PROMPT = """You are SSR Agent, a meticulous command-line coding agent.
支持 snh48 宋昕冉 谢谢喵.

Operating principles:
1. PLAN before acting on non-trivial tasks: call `update_plan` with concrete steps.
2. Use the filesystem and `run_command` tools to inspect and change the project.
3. Use `search_context` to pull relevant configurations, skills, memory and tools
   from the context pool before asking the user or guessing. Prefer embedding mode
   for fuzzy questions, classic (grep) mode for exact strings.
4. Persist durable facts/preferences with `remember`.
5. Use `web_search` (Tavily) for up-to-date external information.
6. Delegate self-contained subtasks to `spawn_sub_agent`.
Be concise. Show your reasoning through the plan and tool calls, not verbosity.
"""


class SSRAgent:
    def __init__(self, settings: Settings, on_event=None):
        self.settings = settings
        self.on_event = on_event  # optional callback(event: dict) for TUI streaming
        # Spawn the MCP servers declared in ~/.ssr/mcp.json and enumerate their
        # tools. Failures are isolated per-server (the agent still runs).
        self.mcp_manager = _start_mcp_manager(settings.mcp_config)
        self.mcp_tools = self.mcp_manager.tools
        # Per-tool specs seed the 'tools' context category (so MCP tools are
        # retrievable); fall back to server-level specs if nothing started.
        self.mcp_tool_specs = (
            [_mcp_tool_spec(t) for t in self.mcp_tools]
            or _load_mcp_specs(settings.mcp_config)
        )
        self.retriever = Retriever(settings, tool_specs=None)
        self.memory = MemoryStore(settings)
        self.toolkit = ToolKit(
            settings,
            self.retriever,
            self.memory,
            sub_agent_runner=self._run_sub_agent,
        )
        # Seed the 'tools' context category with builtin + MCP tool specs.
        self.retriever.tool_specs = self.toolkit.specs() + self.mcp_tool_specs
        self._client = None  # retained so its HTTP transport isn't GC-closed
        self._history = []  # running conversation Contents (multi-turn memory)
        self._mcp_tools_param = None  # cached genai Tool for the MCP declarations

    def close(self) -> None:
        """Tear down all managed MCP server subprocesses."""
        manager = getattr(self, "mcp_manager", None)
        if manager is not None:
            manager.shutdown()

    def __del__(self):  # best-effort; atexit in MCPManager is the real safety net
        try:
            self.close()
        except Exception:
            pass


    # ----------------------------------------------------------- instructions
    def system_instruction(self) -> str:
        parts = [SYSTEM_PROMPT]
        configs = self.retriever.pool().by_category("configurations")
        if configs:
            parts.append("\n# Loaded configurations (claude.md / soul.md / profile.md)")
            for item in configs:
                parts.append(f"\n## {item.title}\n{item.text[:6000]}")
        return "\n".join(parts)

    def _retrieved_context_text(self, user_message: str) -> str:
        """Return a ``<retrieved_context>`` block for the request, or ''."""
        try:
            results = self.retriever.search(user_message, mode="embedding", top_k=5)
        except Exception:
            results = []
        if not results:
            return ""
        ctx = "\n".join(f"- ({r.category}) {r.title}: {r.snippet}" for r in results)
        return f"<retrieved_context>\n{ctx}\n</retrieved_context>"

    def augment_with_context(self, user_message: str) -> str:
        """Retrieve top context for the request and prepend it to the message."""
        ctx = self._retrieved_context_text(user_message)
        return f"{ctx}\n\n{user_message}" if ctx else user_message

    # -------------------------------------------------------------------- run
    def run(self, user_message: str) -> str:
        """Run a single text turn (convenience wrapper over :meth:`run_parts`)."""
        return self.run_parts([{"type": "text", "text": user_message}])

    def run_parts(self, content_parts: list[dict]) -> str:
        """Run one turn from mixed input parts (text / image / audio).

        Each part is a dict:
          * ``{"type": "text", "text": str}``
          * ``{"type": "image", "mime_type": str, "data": bytes}``
          * ``{"type": "audio", "mime_type": str, "data": bytes}``

        Images and audio are passed to the (multimodal) Gemini model inline.
        """
        from google.genai import types

        text_blob = " ".join(
            p.get("text", "") for p in content_parts if p.get("type") == "text"
        ).strip()
        n_media = sum(1 for p in content_parts if p.get("type") in ("image", "audio"))
        summary = text_blob or "[no text]"
        if n_media:
            summary += f"  (+{n_media} media attachment(s))"
        self.memory.log_turn("user", summary)

        gp: list = []
        ctx = self._retrieved_context_text(text_blob) if text_blob else ""
        if ctx:
            gp.append(types.Part(text=ctx))
        for p in content_parts:
            t = p.get("type")
            if t == "text" and p.get("text"):
                gp.append(types.Part(text=p["text"]))
            elif t in ("image", "audio") and p.get("data"):
                mime = p.get("mime_type") or ("image/png" if t == "image" else "audio/wav")
                gp.append(types.Part.from_bytes(data=p["data"], mime_type=mime))
        if not gp:
            gp.append(types.Part(text=text_blob))

        self._history.append(types.Content(role="user", parts=gp))
        try:
            reply = self._complete(
                self._history, self.system_instruction(), self.toolkit, max_iters=24, tag="ssr"
            )
        except Exception as e:
            reply = f"[agent error] {e}"
        self._history.append(types.Content(role="model", parts=[types.Part(text=reply)]))
        self.memory.log_turn("assistant", reply)
        return reply

    def _ensure_client(self):
        if self._client is None:
            from google import genai

            self._client = genai.Client(api_key=self.settings.gemini_api_key)
        return self._client

    def _emit(self, kind: str, tag: str = "ssr", **fields) -> None:
        """Push a streaming event to the TUI callback, if one is registered."""
        if self.on_event is None:
            return
        try:
            self.on_event({"type": kind, "agent": tag, **fields})
        except Exception:
            pass  # never let UI rendering break the agent loop

    def _mcp_tools_parameter(self):
        """Build (once) a genai ``Tool`` of function declarations for MCP tools.

        Returns ``None`` when there are no MCP tools, so the config is unchanged
        for users without any configured servers.
        """
        if not self.mcp_tools:
            return None
        if self._mcp_tools_param is not None:
            return self._mcp_tools_param
        from google.genai import types

        declarations = []
        for tool in self.mcp_tools:
            schema = tool.input_schema if isinstance(tool.input_schema, dict) else {}
            if schema.get("type") != "object":
                schema = {"type": "object", "properties": {}}
            try:
                declarations.append(
                    types.FunctionDeclaration(
                        name=tool.qualified_name,
                        description=tool.description or f"MCP tool {tool.name}",
                        parameters_json_schema=schema,
                    )
                )
            except Exception:  # a malformed schema must not break the whole loop
                declarations.append(
                    types.FunctionDeclaration(
                        name=tool.qualified_name,
                        description=tool.description or f"MCP tool {tool.name}",
                        parameters_json_schema={"type": "object", "properties": {}},
                    )
                )
        self._mcp_tools_param = types.Tool(function_declarations=declarations)
        return self._mcp_tools_param

    def _complete(
        self, contents, system_instruction, toolkit: ToolKit, max_iters: int, tag: str = "ssr"
    ) -> str:
        """Manual function-calling loop.

        google-genai's *automatic* function calling does not invoke bound
        methods, so we drive the tool loop ourselves: the SDK still auto-builds
        the function schemas from the typed callables, and we dispatch each
        ``function_call`` to the matching :class:`ToolKit` method.

        Intermediate reasoning text and every tool call/result are streamed to
        the TUI via :meth:`_emit` so the user sees the agent think and act.
        """
        from google.genai import types

        client = self._ensure_client()
        dispatch = {fn.__name__: fn for fn in toolkit.callables()}
        # Builtin Python callables + a Tool carrying the MCP function declarations.
        tools = list(toolkit.callables())
        mcp_param = self._mcp_tools_parameter()
        if mcp_param is not None:
            tools.append(mcp_param)
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            tools=tools,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

        for _ in range(max_iters):
            resp = client.models.generate_content(
                model=self.settings.default_model, contents=contents, config=config
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
            # Reasoning the model emitted alongside its tool calls.
            if text:
                self._emit("thinking", tag=tag, text=text)
            fr_parts = []
            for call in calls:
                fn = dispatch.get(call.name)
                args = dict(call.args or {})
                self._emit("tool_call", tag=tag, name=call.name, args=args)
                if is_mcp_tool_name(call.name):
                    # Route MCP tool calls to the managed server subprocess.
                    try:
                        result = self.mcp_manager.call_tool(call.name, args)
                    except Exception as e:
                        result = f"ERROR: {type(e).__name__}: {e}"
                elif fn is None:
                    result = f"ERROR: unknown tool {call.name}"
                else:
                    try:
                        result = fn(**args)
                    except Exception as e:  # surface tool errors back to the model
                        result = f"ERROR: {type(e).__name__}: {e}"
                self._emit("tool_result", tag=tag, name=call.name, result=str(result))
                fr_parts.append(
                    types.Part.from_function_response(
                        name=call.name, response={"result": str(result)}
                    )
                )
            contents.append(types.Content(role="tool", parts=fr_parts))
        return "(reached tool-call limit without a final answer)"

    # ------------------------------------------------------------- sub agents
    def _run_sub_agent(self, task: str, context: str = "") -> str:
        """Run a fresh, isolated sub-agent for a focused subtask."""
        from google.genai import types

        try:
            sub_toolkit = ToolKit(self.settings, self.retriever, self.memory, sub_agent_runner=None)
            prompt = task if not context else f"Context:\n{context}\n\nTask:\n{task}"
            contents = [types.Content(role="user", parts=[types.Part(text=prompt)])]
            self._emit("sub_agent", tag="sub", task=task)
            return self._complete(
                contents,
                "You are an SSR sub-agent handling one focused subtask. "
                "Complete it fully and report a concise result.",
                sub_toolkit,
                tag="sub",
                max_iters=12,
            )
        except Exception as e:
            return f"[sub-agent error] {e}"

    # --------------------------------------------------------------- ADK view
    def build_adk_agent(self):
        """Best-effort construction of a Google ADK ``LlmAgent`` (compliance/ACP)."""
        from google.adk.agents import LlmAgent  # type: ignore

        return LlmAgent(
            model=self.settings.default_model,
            name="ssr_agent",
            description="SSR command-line coding agent",
            instruction=self.system_instruction(),
            tools=list(self.toolkit.callables()),
        )


def _start_mcp_manager(mcp_path: Path) -> MCPManager:
    """Build an :class:`MCPManager` from the config and start its servers.

    Never raises: if anything goes wrong an empty (no-op) manager is returned so
    the agent keeps working without MCP.
    """
    try:
        manager = MCPManager.from_config(mcp_path)
        manager.start_all()
        return manager
    except Exception:  # pragma: no cover - defensive
        return MCPManager([])


def _mcp_tool_spec(tool: MCPTool) -> dict:
    """Context-pool spec for a single MCP tool (seeds the 'tools' category)."""
    return {
        "name": tool.qualified_name,
        "description": tool.description or f"MCP tool {tool.name}",
        "origin": "mcp",
        "server": tool.server,
    }


def _load_mcp_specs(mcp_path: Path) -> list[dict]:
    """Read ~/.ssr/mcp.json and return server-level specs for the context pool.

    Used as a fallback for the context pool when no MCP server tools could be
    enumerated (e.g. servers disabled or failed to start).
    """
    if not mcp_path.exists():
        return []
    try:
        data = json.loads(mcp_path.read_text("utf-8"))
    except Exception:
        return []
    specs: list[dict] = []
    servers = data.get("mcpServers", data.get("servers", {}))
    for name, cfg in servers.items():
        desc = cfg.get("description", f"MCP server '{name}'")
        specs.append({"name": f"mcp:{name}", "description": desc, "origin": "mcp"})
    return specs
