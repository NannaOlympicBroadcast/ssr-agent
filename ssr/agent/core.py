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
    def __init__(self, settings: Settings):
        self.settings = settings
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
        self._chat = None  # lazy genai chat session

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

    def _ensure_chat(self):
        if self._chat is not None:
            return self._chat
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=self.settings.gemini_api_key)
        config = types.GenerateContentConfig(
            system_instruction=self.system_instruction(),
            tools=self.toolkit.callables(),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                maximum_remote_calls=24
            ),
        )
        self._chat = client.chats.create(model=self.settings.default_model, config=config)
        return self._chat

    def _run_genai(self, message: str) -> str:
        chat = self._ensure_chat()
        resp = chat.send_message(message)
        return (getattr(resp, "text", None) or "").strip() or "(no response)"

    # ------------------------------------------------------------- sub agents
    def _run_sub_agent(self, task: str, context: str = "") -> str:
        """Run a fresh, isolated sub-agent for a focused subtask."""
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=self.settings.gemini_api_key)
            sub_toolkit = ToolKit(self.settings, self.retriever, self.memory, sub_agent_runner=None)
            config = types.GenerateContentConfig(
                system_instruction=(
                    "You are an SSR sub-agent handling one focused subtask. "
                    "Complete it fully and report a concise result."
                ),
                tools=sub_toolkit.callables(),
                automatic_function_calling=types.AutomaticFunctionCallingConfig(
                    maximum_remote_calls=12
                ),
            )
            chat = client.chats.create(model=self.settings.default_model, config=config)
            prompt = task if not context else f"Context:\n{context}\n\nTask:\n{task}"
            resp = chat.send_message(prompt)
            return (getattr(resp, "text", None) or "(sub-agent produced no output)").strip()
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
