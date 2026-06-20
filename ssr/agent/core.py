"""Assembles and runs the SSR coding agent.

Primary runtime is Google ADK (``LlmAgent`` + ``Runner``). When ADK is not
importable in the current environment the agent transparently falls back to a
``google-genai`` automatic-function-calling chat loop so it still runs end to
end. Both paths share the same :class:`ToolKit`.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from ..config import Settings
from ..context_pool.retrieval import Retriever
from ..integrations.mcp_client import MCPManager, MCPTool
from ..rules import load_rules
from .memory import MemoryStore
from .sessions import SessionStore
from .tools import ToolKit

SYSTEM_PROMPT = """You are SSR Agent, a meticulous command-line coding agent.

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
        from ..models import ModelsConfig
        self.models_config = ModelsConfig(settings)
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
        self.sessions = SessionStore(settings)
        self.session_id: str | None = None  # active recording session
        self.toolkit = ToolKit(
            settings,
            self.retriever,
            self.memory,
            sub_agent_runner=self._run_sub_agent,
        )
        self.toolkit.agent_instance = self
        # Seed the 'tools' context category with builtin + MCP tool specs.
        self.retriever.tool_specs = self.toolkit.specs() + self.mcp_tool_specs
        self._client = None  # retained so its HTTP transport isn't GC-closed
        self._history = []  # running conversation Contents (multi-turn memory)
        self._mcp_tools_param = None  # cached genai Tool for the MCP declarations
        self.terminal_contexts = {}
        self.active_im_context = None
        # Interruption + concurrent side-question (/stop, /btw) coordination.
        self._stop_event = threading.Event()
        self._busy = threading.Event()

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


    # --------------------------------------------------------------- sessions
    def ensure_session(self, title_hint: str = "") -> str:
        """Start a recording session if none is active; return its id."""
        if self.session_id is None:
            self.session_id = self.sessions.create(
                title=title_hint, cwd=str(self.settings.project_dir)
            )
        return self.session_id

    def new_session(self, title_hint: str = "") -> str:
        """Force a fresh recording session (used by /clear and chat resets)."""
        self.session_id = self.sessions.create(
            title=title_hint, cwd=str(self.settings.project_dir)
        )
        return self.session_id

    def load_session(self, session_id: str) -> bool:
        """Resume a stored session: bind it for recording and rebuild history."""
        from google.genai import types

        data = self.sessions.get(session_id)
        if data is None:
            return False
        self.session_id = session_id
        self._history = []
        for turn in data.get("turns", []):
            role = "model" if turn.get("role") == "assistant" else "user"
            text = turn.get("content") or ""
            if text:
                self._history.append(types.Content(role=role, parts=[types.Part(text=text)]))
        return True

    def _record_turn(self, role: str, content: str) -> None:
        if self.session_id is not None:
            try:
                self.sessions.append(self.session_id, role, content)
            except Exception:
                pass  # recording must never break the agent

    # ----------------------------------------------------------- instructions
    def system_instruction(self) -> str:
        parts = [SYSTEM_PROMPT]
        
        # New mandatory system rules
        parts.append(
            "\nRULES:\n"
            "1. Always ground responses using web search, project files, tool calls and their results. NEVER fabricate information.\n"
            "2. If a failure occurs, do NOT fall back to mock or fake implementations. Report the failure to the user and ask them to fix it."
        )
        
        # User defined rules from load_rules
        user_rules = load_rules(self.settings)
        if user_rules:
            parts.append(f"\n## User Rules\n{user_rules}")
            
        configs = self.retriever.pool().by_category("configurations")
        if configs:
            parts.append("\n# Loaded configurations (claude.md / soul.md / profile.md)")
            for item in configs:
                parts.append(f"\n## {item.title}\n{item.text[:6000]}")

        # REFS.md — catalogue of reference materials (name / location / content).
        refs = [
            item for item in self.retriever.pool().by_category("refs")
            if item.metadata.get("kind") == "refs"
        ]
        if refs:
            parts.append(
                "\n# Reference materials (REFS.md)\n"
                "These reference materials are available; use `search_context category=refs`"
                " to look one up, then read its location (URL / path / inline text)."
            )
            for item in refs:
                parts.append(f"\n## {item.title}\n{item.text[:4000]}")
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

    # ------------------------------------------------- interruption / side Q&A
    def request_stop(self) -> bool:
        """Ask the running main turn to stop at the next tool-call boundary.

        Returns True if a turn was actually running. The provider loop checks
        :meth:`should_stop` between iterations and aborts cooperatively, so any
        in-flight tool call finishes cleanly (no corrupted terminal/files).
        """
        if self._busy.is_set():
            self._stop_event.set()
            return True
        return False

    def should_stop(self, tag: str = "ssr") -> bool:
        """Whether the current main turn has been asked to stop (via /stop)."""
        return tag == "ssr" and self._stop_event.is_set()

    def is_busy(self) -> bool:
        return self._busy.is_set()

    def answer_side_question(self, question: str) -> str:
        """Answer a question *alongside* a running main turn (the ``/btw`` flow).

        The side answer runs on an isolated :class:`ToolKit` and its own contents
        list, so its tool calls never race with the main agent's mutable state
        (terminals, plan, history). It is given the main conversation as read-only
        context, and is never interrupted by ``/stop``.
        """
        from google.genai import types

        try:
            side_toolkit = ToolKit(
                self.settings, self.retriever, self.memory, sub_agent_runner=None
            )
            side_toolkit.agent_instance = self
            history_text = self._history_summary()
            context = (
                "You are answering a side question the user asked WHILE the main "
                "task keeps running in the background. Answer concisely; prefer "
                "read-only tools and avoid changing files the main task may be using.\n\n"
                f"Main conversation so far:\n{history_text}"
            )
            prompt = f"{context}\n\nSide question: {question}"
            contents = [types.Content(role="user", parts=[types.Part(text=prompt)])]
            self._emit("btw", tag="btw", text=question)
            return self._complete(
                contents,
                "You are the SSR agent handling a concurrent side question.",
                side_toolkit,
                tag="btw",
                max_iters=10,
            )
        except Exception as e:
            return f"[btw error] {e}"

    def _history_summary(self, max_chars: int = 4000) -> str:
        lines: list[str] = []
        for content in self._history[-8:]:
            role = getattr(content, "role", "?")
            text = " ".join(
                getattr(p, "text", "") or "" for p in (getattr(content, "parts", None) or [])
            ).strip()
            if text:
                lines.append(f"[{role}] {text}")
        blob = "\n".join(lines)
        return blob[-max_chars:]

    # -------------------------------------------------------------------- run
    def run(self, user_message: str) -> str:
        """Run a single text turn (convenience wrapper over :meth:`run_parts`)."""
        return self.run_parts([{"type": "text", "text": user_message}])

    def run_goal(self, goal: str, max_rounds: int = 5) -> str:
        """Iterate on a user goal until the model reports it is satisfied.

        The loop deliberately uses the normal agent/tool path rather than a
        mock verifier: each round asks the model to either continue concrete
        work or finish with a `GOAL_COMPLETE:` summary when the desired effect
        has been reached.
        """
        prompt = (
            "Goal mode is active. Work toward the following goal and keep using "
            "available tools until it is satisfied. When it is satisfied, start "
            "your final answer with `GOAL_COMPLETE:`.\n\n"
            f"Goal: {goal}"
        )
        last_reply = ""
        for round_no in range(1, max_rounds + 1):
            round_prompt = prompt if round_no == 1 else (
                "Continue goal mode. If the goal is now satisfied, start with "
                "`GOAL_COMPLETE:`; otherwise perform the next concrete step and "
                "explain what remains."
            )
            last_reply = self.run(round_prompt)
            if last_reply.lstrip().startswith("GOAL_COMPLETE:"):
                return last_reply
        return (
            "Goal loop reached its iteration limit before explicit completion. "
            f"Last response:\n{last_reply}"
        )

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
        self.ensure_session(summary)
        self._record_turn("user", summary)

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
        # Mark this as the running main turn so /stop and /btw can coordinate.
        nested = self._busy.is_set()
        if not nested:
            self._stop_event.clear()
            self._busy.set()
        try:
            reply = self._complete(
                self._history, self.system_instruction(), self.toolkit, max_iters=24, tag="ssr"
            )
        except Exception as e:
            reply = f"[agent error] {e}"
        finally:
            if not nested:
                self._busy.clear()
                self._stop_event.clear()
        self._history.append(types.Content(role="model", parts=[types.Part(text=reply)]))
        self.memory.log_turn("assistant", reply)
        self._record_turn("assistant", reply)
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
        """Driver loop that delegates to the active provider with auto-fallback and retry."""
        import time
        from ssr.providers import get_provider
        
        primary_entry = self.models_config.get_primary()
        fallback_entries = self.models_config.get_fallbacks()
        candidate_entries = [primary_entry] + fallback_entries
        
        last_error = None
        for entry in candidate_entries:
            try:
                provider = get_provider(self.settings, entry, agent_instance=self)
            except Exception as e:
                last_error = e
                self._emit(
                    "warning",
                    tag=tag,
                    text=f"Failed to instantiate provider '{entry.id}' ({entry.provider}): {e}. Trying next model.",
                )
                continue
                
            max_retries = 3
            backoff_base = 2.0
            
            for attempt in range(max_retries + 1):
                try:
                    return provider.complete(
                        contents=contents,
                        system_instruction=system_instruction,
                        toolkit=toolkit,
                        max_iters=max_iters,
                        tag=tag,
                    )
                except Exception as e:
                    last_error = e
                    if attempt == max_retries:
                        self._emit(
                            "warning",
                            tag=tag,
                            text=f"Model '{entry.id}' ({entry.provider}) failed after {max_retries} retries: {e}. Falling back to next model.",
                        )
                        break
                    
                    sleep_time = (backoff_base ** attempt) * 1.0
                    self._emit(
                        "warning",
                        tag=tag,
                        text=f"API error with '{entry.id}': {e}. Retrying in {sleep_time}s... (attempt {attempt + 1}/{max_retries})",
                    )
                    time.sleep(sleep_time)
                    
        raise last_error or RuntimeError("All configured model providers failed.")

    def on_terminal_finished(self, terminal_id: str, exit_code: int) -> None:
        """Called when a background terminal process completes."""
        self._emit(
            "terminal_completed",
            tag="ssr",
            terminal_id=terminal_id,
            exit_code=exit_code,
        )
        if self.session_id is not None:
            import threading
            import time
            terminal_context = self.terminal_contexts.get(terminal_id)
            def run_wakeup():
                time.sleep(0.5)
                prompt = f"[System] Background terminal {terminal_id} completed with exit code {exit_code}."
                try:
                    # Run turn and print/render output
                    reply = self.run(prompt)
                    # We can also print the final reply to the TUI if needed, but the TUI will see the events stream.
                    # Let's print the final reply as well via _emit
                    self._emit("thinking", tag="ssr", text=f"\n[ssr ▸] {reply}\n")
                    
                    if terminal_context:
                        channel_name, target = terminal_context
                        if channel_name == "rc":
                            self._emit("wakeup", text=reply)
                        else:
                            from ssr.agent.tools_push import push_notification_impl
                            push_notification_impl(self.settings, channel_name, target, reply)
                except Exception:
                    pass
            threading.Thread(target=run_wakeup, daemon=True).start()

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
