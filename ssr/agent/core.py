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
        self.mcp_tool_specs = _load_mcp_specs(settings.mcp_config)
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

    # ----------------------------------------------------------- instructions
    def system_instruction(self) -> str:
        parts = [SYSTEM_PROMPT]
        configs = self.retriever.pool().by_category("configurations")
        if configs:
            parts.append("\n# Loaded configurations (claude.md / soul.md / profile.md)")
            for item in configs:
                parts.append(f"\n## {item.title}\n{item.text[:6000]}")
        return "\n".join(parts)

    def augment_with_context(self, user_message: str) -> str:
        """Retrieve top context for the request and prepend it to the message."""
        try:
            results = self.retriever.search(user_message, mode="embedding", top_k=5)
        except Exception:
            results = []
        if not results:
            return user_message
        ctx = "\n".join(f"- ({r.category}) {r.title}: {r.snippet}" for r in results)
        return f"<retrieved_context>\n{ctx}\n</retrieved_context>\n\n{user_message}"

    # -------------------------------------------------------------------- run
    def run(self, user_message: str) -> str:
        self.memory.log_turn("user", user_message)
        message = self.augment_with_context(user_message)
        try:
            reply = self._run_genai(message)
        except Exception as e:
            reply = f"[agent error] {e}"
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

    def _run_genai(self, message: str) -> str:
        from google.genai import types

        self._history.append(types.Content(role="user", parts=[types.Part(text=message)]))
        reply = self._complete(
            self._history,
            self.system_instruction(),
            self.toolkit,
            max_iters=24,
            tag="ssr",
        )
        self._history.append(types.Content(role="model", parts=[types.Part(text=reply)]))
        return reply

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
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            tools=toolkit.callables(),
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
                if fn is None:
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


def _load_mcp_specs(mcp_path: Path) -> list[dict]:
    """Read ~/.ssr/mcp.json and return tool specs for the context pool."""
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
