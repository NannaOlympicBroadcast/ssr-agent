"""Built-in agent tools: filesystem, shell, memory, web search, retrieval,
planning and sub-agent spawning.

Each tool is a plain Python function with a typed signature and docstring so it
can be auto-wrapped by Google ADK's ``FunctionTool``. A :class:`ToolKit` binds
the tools to runtime context (settings, retriever, memory).
"""

from __future__ import annotations

import locale
import os
import subprocess
from pathlib import Path

from ..config import Settings
from ..context_pool.retrieval import Retriever
from ..permissions import PermissionManager, PermissionResult
from ..approval import AutoApprovalHandler, TUIApprovalHandler, ApprovalDecision
from .memory import MemoryStore

_MAX_OUTPUT = 12_000
_MAX_READ = 60_000
_MAX_FILE_SEND = 16 * 1024 * 1024  # cap a single file transfer at ~16 MB (ws max_size)


def _auto_approve_enabled() -> bool:
    """Whether SSR_AUTO_APPROVE asks the toolkit to allow every command."""
    return os.environ.get("SSR_AUTO_APPROVE", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def decode_output(data: bytes | None) -> str:
    """Decode subprocess output bytes without ever raising.

    Tries UTF-8, then the platform's preferred encoding (e.g. GBK on a Chinese
    Windows console), then falls back to UTF-8 with replacement. This avoids the
    ``UnicodeDecodeError`` that ``subprocess`` raises in text mode when command
    output is not valid in the locale codec.
    """
    if not data:
        return ""
    encodings = ["utf-8"]
    try:
        preferred = locale.getpreferredencoding(False)
        if preferred and preferred.lower() not in encodings:
            encodings.append(preferred)
    except Exception:
        pass
    for enc in (*encodings, "gbk"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def _coerce_bool(v, default: bool = True) -> bool:
    """Coerce a tool argument to bool (it may arrive as a string like 'false')."""
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    return str(v).strip().lower() in ("true", "1", "yes", "y", "t", "on")


class ToolKit:
    """Holds runtime context and exposes bound tool callables."""

    def __init__(
        self,
        settings: Settings,
        retriever: Retriever,
        memory: MemoryStore,
        sub_agent_runner=None,
    ):
        self.settings = settings
        self.retriever = retriever
        self.memory = memory
        self._plan: list[str] = []
        self._sub_agent_runner = sub_agent_runner
        from .async_terminal import AsyncTerminal
        self.terminal = AsyncTerminal()
        self.permission_manager = PermissionManager(settings)
        # SSR_AUTO_APPROVE lets a headless entry point (e.g. the whole `ssr arm`
        # command group — see cmd_arm) run every command without an approval
        # prompt, the same way the xiaomi speaker channel does. Default off, so the
        # interactive TUI still asks.
        if _auto_approve_enabled():
            self.approval_handler = AutoApprovalHandler()
        else:
            self.approval_handler = TUIApprovalHandler()
        self.approval_handler.toolkit = self


    # -------------------------------------------------------------- approvals
    def _approval_context(self) -> dict:
        """Context passed to the approval handler (IM/RC routing target)."""
        agent = getattr(self, "agent_instance", None)
        ctx = getattr(agent, "active_im_context", None) if agent is not None else None
        if ctx:
            return {"channel": ctx[0], "target": ctx[1]}
        return {}

    def _denied_message(self) -> str:
        """Build the tool result for a denied command, including any user reason.

        When the user supplies a reason on denial we hand it back to the model as
        guidance so it can change course rather than blindly retrying the command.
        """
        reason = getattr(self.approval_handler, "denial_reason", "") or ""
        if reason.strip():
            return (
                "Command execution DENIED by the user.\n"
                f"User's reason / instruction: {reason.strip()}\n"
                "Do not retry the same command. Follow the user's reason and adapt your approach."
            )
        return "ERROR: Command execution denied by user."

    # ------------------------------------------------------------------ paths
    def _resolve(self, path: str) -> Path:
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = self.settings.project_dir / p
        return p

    # ============================================================== tool impls
    def read_file(self, path: str) -> str:
        """Read a UTF-8 text file and return its contents.

        Args:
            path: File path, absolute or relative to the project directory.
        """
        p = self._resolve(path)
        if not p.exists():
            return f"ERROR: no such file: {p}"
        try:
            return p.read_text(encoding="utf-8", errors="replace")[:_MAX_READ]
        except OSError as e:
            return f"ERROR: {e}"

    def write_file(self, path: str, content: str) -> str:
        """Create or overwrite a text file with the given content.

        Args:
            path: Destination path, absolute or relative to the project directory.
            content: Full file content to write.
        """
        p = self._resolve(path)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            return f"Wrote {len(content)} bytes to {p}"
        except OSError as e:
            return f"ERROR: {e}"

    def list_dir(self, path: str = ".") -> str:
        """List the entries of a directory.

        Args:
            path: Directory path, absolute or relative to the project directory.
        """
        p = self._resolve(path)
        if not p.is_dir():
            return f"ERROR: not a directory: {p}"
        entries = []
        for e in sorted(p.iterdir()):
            entries.append(("📁 " if e.is_dir() else "📄 ") + e.name)
        return "\n".join(entries) or "(empty)"

    def run_command(self, command: str, timeout: int = 120, cwd: str = "") -> str:
        """Run a shell command and return stdout/stderr.

        Args:
            command: The shell command line to execute.
            timeout: Maximum seconds to wait before aborting.
            cwd: Optional working directory. Defaults to the project directory,
                but may be any absolute or relative path so commands are not
                restricted to the default directory (useful in channel mode).
        """
        res = self.permission_manager.check_permission(command)
        if res == PermissionResult.NEEDS_APPROVAL:
            decision = self.approval_handler.request_approval(command, self._approval_context())
            if decision == ApprovalDecision.ALWAYS_ALLOW:
                self.permission_manager.add_always_allow(command)
            elif decision == ApprovalDecision.DENY:
                return self._denied_message()

        workdir = self._resolve(cwd) if cwd else self.settings.project_dir
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=str(workdir),
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return f"ERROR: command timed out after {timeout}s"
        stdout = decode_output(proc.stdout)
        stderr = decode_output(proc.stderr)
        out = stdout + (("\n[stderr]\n" + stderr) if stderr else "")
        out = out[:_MAX_OUTPUT]
        return f"exit={proc.returncode}\n{out}".strip()

    def spawn_terminal(self, command: str, timeout: int = 300) -> str:
        """Launch a shell command in the background and return its terminal ID.

        Args:
            command: The command line to execute.
            timeout: Maximum seconds before process is killed.
        """
        res = self.permission_manager.check_permission(command)
        if res == PermissionResult.NEEDS_APPROVAL:
            decision = self.approval_handler.request_approval(command, self._approval_context())
            if decision == ApprovalDecision.ALWAYS_ALLOW:
                self.permission_manager.add_always_allow(command)
            elif decision == ApprovalDecision.DENY:
                return self._denied_message()

        def on_finished(tid, code):
            agent = getattr(self, "agent_instance", None)
            if agent is not None and hasattr(agent, "on_terminal_finished"):
                agent.on_terminal_finished(tid, code)

        tid = self.terminal.spawn(command, self.settings.project_dir, timeout, on_finished=on_finished)
        agent = getattr(self, "agent_instance", None)
        if agent is not None:
            active_ctx = getattr(agent, "active_im_context", None)
            if active_ctx:
                agent.terminal_contexts[tid] = active_ctx
        return tid

    def check_terminal(self, terminal_id: str) -> str:
        """Read newly buffered output from a running background terminal.

        Args:
            terminal_id: The ID of the terminal returned by spawn_terminal.
        """
        return self.terminal.check_output(terminal_id)

    def send_to_terminal(self, terminal_id: str, text: str) -> str:
        """Send keys/input followed by a newline to a running background terminal.

        Args:
            terminal_id: The ID of the terminal.
            text: The text/input to send.
        """
        return self.terminal.send_input(terminal_id, text)
    def kill_terminal(self, terminal_id: str) -> str:
        """Kill a running background terminal process.

        Args:
            terminal_id: The ID of the terminal.
        """
        return self.terminal.kill(terminal_id)

    def send_file_to_user(self, path: str, caption: str = "") -> str:
        """Send a local file to the user so they can download/receive it.

        In an IM/channel session (feishu/wechat) the file is delivered through
        that channel. Use this to deliver generated artifacts (reports, images,
        archives, build outputs) to the user.

        Args:
            path: Path to the local file, absolute or relative to the project dir.
            caption: Optional short note shown alongside the file.
        """
        import mimetypes

        p = self._resolve(path)
        if not p.is_file():
            return f"ERROR: no such file: {p}"
        data = p.read_bytes()
        if len(data) > _MAX_FILE_SEND:
            return f"ERROR: file too large to send ({len(data)} bytes; limit {_MAX_FILE_SEND})."
        agent = getattr(self, "agent_instance", None)
        ctx = getattr(agent, "active_im_context", None) if agent is not None else None
        mime = mimetypes.guess_type(p.name)[0] or "application/octet-stream"

        # Channels with native file support (feishu/wechat).
        if ctx and ctx[0] in ("feishu", "wechat"):
            try:
                import ssr.channels  # ensure channels are registered
                from ssr.channels.registry import registry

                channel = registry.get(ctx[0])
                if channel is not None:
                    channel.send_file(ctx[1], str(p), mime)
                    return f"Sent file '{p.name}' to {ctx[0]}."
            except Exception as e:
                return f"ERROR: could not send file via {ctx[0]}: {e}"

        return (
            f"File is ready at {p} ({len(data)} bytes). "
            "Direct file delivery is only available in IM / channel sessions."
        )

    def push_notification(self, channel: str, target: str, message: str) -> str:
        """Send a message or notification to a specific channel and recipient.

        Args:
            channel: Channel name — 'feishu', 'wechat', or 'xiaomi' (the XiaoAI
                speaker, which speaks the message via TTS; voice-only, no media).
            target: The target user or chat identifier (ignored for 'xiaomi').
            message: The message body to send.
        """
        from .tools_push import push_notification_impl
        return push_notification_impl(self.settings, channel, target, message)

    # NOTE: the miloco_* (Mi Home) tools used to live here as ToolKit methods. They
    # are now contributed in-process by the bundled `miloco` plugin
    # (agent_tools -> ssr.agent.tools_miloco:MilocoTools), like the openarm arm_*
    # tools — so Mi Home control is a toggleable plugin, not core.

    def update_plan(self, steps: list[str]) -> str:
        """Record the current step-by-step plan for the task.

        Args:
            steps: Ordered list of planned steps.
        """
        self._plan = list(steps)
        body = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(self._plan))
        return f"Plan updated:\n{body}"

    def get_plan(self) -> str:
        """Return the current task plan."""
        if not self._plan:
            return "(no plan yet)"
        return "\n".join(f"{i + 1}. {s}" for i, s in enumerate(self._plan))

    def remember(self, note: str, scope: str = "project") -> str:
        """Store a durable note into MEMORY (memory.md).

        Args:
            note: The fact or preference to remember.
            scope: 'project' (default) or 'global'.
        """
        return self.memory.remember(note, scope=scope)

    def recall_memory(self, scope: str = "project") -> str:
        """Return the contents of MEMORY (memory.md).

        Args:
            scope: 'project' (default) or 'global'.
        """
        return self.memory.recall(scope=scope) or "(memory empty)"

    def search_context(
        self,
        query: str,
        category: str = "all",
        mode: str = "embedding",
        top_k: int = 5,
    ) -> str:
        """Retrieve relevant context from the context pool.

        This is the core context-retrieval tool. It searches the four context
        categories (tools, configurations, skills, memory).

        Args:
            query: What to look for.
            category: One of 'tools', 'configurations', 'skills', 'memory', or 'all'.
            mode: 'embedding' (model2vec vector search) or 'classic' (grep/regex).
            top_k: Number of results to return.
        """
        cats = None if category in ("all", "", None) else [category]
        results = self.retriever.search(query, categories=cats, mode=mode, top_k=top_k)
        if not results:
            return "(no matching context)"
        return "\n\n".join(r.render() for r in results)

    def web_search(self, query: str, max_results: int = 5) -> str:
        """Search the web with Tavily.

        Args:
            query: The search query.
            max_results: Maximum number of results.
        """
        key = self.settings.tavily_api_key
        if not key:
            return "ERROR: TAVILY_API_KEY not configured in ~/.ssr/.env"
        try:
            from tavily import TavilyClient

            client = TavilyClient(api_key=key)
            resp = client.search(query=query, max_results=max_results)
            lines = []
            for r in resp.get("results", []):
                lines.append(f"- {r.get('title')}\n  {r.get('url')}\n  {r.get('content', '')[:300]}")
            answer = resp.get("answer")
            head = f"Answer: {answer}\n\n" if answer else ""
            return head + "\n".join(lines)
        except Exception as e:  # pragma: no cover - network dependent
            return f"ERROR: tavily search failed: {e}"

    def spawn_sub_agent(self, task: str, context: str = "") -> str:
        """Spawn a focused sub-agent to handle a self-contained subtask.

        Args:
            task: A complete description of the subtask for the sub-agent.
            context: Optional extra context to hand to the sub-agent.
        """
        if self._sub_agent_runner is None:
            return "ERROR: sub-agent runner not available"
        return self._sub_agent_runner(task, context)

    # ----------------------------------------------------------------- bus
    def _bus(self):
        """Return the running agent's built-in MessageBus, or None."""
        agent = getattr(self, "agent_instance", None)
        return getattr(agent, "bus", None) if agent is not None else None

    def bus_publish(self, topic: str, payload_json: str = "", source: str = "") -> str:
        """Publish a structured event onto the bus to communicate asynchronously.

        Events propagate to local listeners and, if the bus is bridged, to the
        remote bus server (and from there to other agents/programs).

        Args:
            topic: Dotted topic name, e.g. 'task.completed' or 'agent.alice.ping'.
            payload_json: Optional JSON object string carried with the event.
            source: Optional override for the event source label.
        """
        import json

        bus = self._bus()
        if bus is None:
            return "ERROR: bus not available in this context"
        try:
            payload = json.loads(payload_json) if payload_json.strip() else {}
            if not isinstance(payload, dict):
                payload = {"value": payload}
        except json.JSONDecodeError as e:
            return f"ERROR: payload_json is not valid JSON: {e}"
        event = bus.publish(topic, payload, source=source or None)
        return f"Published event {event.id} on '{topic}'."

    def bus_create_handler(
        self,
        event: str,
        handler_prompt: str = "",
        type: str = "every",
        inherit_session: bool = True,
    ) -> str:
        """Register a *bus event handler agent*: each matching event triggers an agent turn.

        Use this instead of blocking — the handler fires a fresh agent turn every
        time a matching event arrives, so the same source can trigger you many
        times and no event is missed on a timeout. If you genuinely need to wait
        for one event before continuing, register a handler and end your turn;
        the event will start a new turn (and the user can `/stop` to abort).

        Args:
            event: Topic pattern to match — '*' = one segment, '**' = the rest
                (e.g. 'task.*', 'cat.detected', 'agent.**').
            handler_prompt: Instructions for the handler agent on how to react to
                a matching event. The event topic/source/payload are appended.
            type: 'every' (fire on every match) or 'once' (fire once, then the
                handler auto-removes itself).
            inherit_session: true = handle within the current conversation
                (continues this session's context); false = isolated sub-agent.
        """
        agent = getattr(self, "agent_instance", None)
        if agent is None or not hasattr(agent, "create_bus_handler"):
            return "ERROR: bus not available in this context"
        once = str(type).strip().lower() in ("once", "one", "oneshot", "one-shot", "1")
        inherit = _coerce_bool(inherit_session, default=True)
        hid = agent.create_bus_handler(
            event, handler_prompt, once=once, inherit_session=inherit,
            description=(handler_prompt[:60] if handler_prompt else ""),
        )
        kind = "once" if once else "every match"
        ctx = "current session" if inherit else "isolated sub-agent"
        return (
            f"Created bus handler {hid} on '{event}' ({kind}, {ctx}). "
            f"Remove it with bus_remove_handler('{hid}')."
        )

    def bus_create_mcp_handler(self, event: str, tool: str, args_json: str = "",
                               type: str = "every") -> str:
        """Register a bus handler that CALLS AN MCP TOOL on each matching event.

        No LLM turn runs — the active MCP tool is invoked directly. Use this to wire
        an event straight to a tool (e.g. turn on a light when a sensor fires).

        Args:
            event: Topic pattern to match (e.g. 'sensor.motion', 'task.*').
            tool: Qualified MCP tool name, exactly as advertised: 'mcp__<server>__<tool>'.
            args_json: JSON object of the tool's arguments (static).
            type: 'every' or 'once'.
        """
        import json
        agent = getattr(self, "agent_instance", None)
        if agent is None or not hasattr(agent, "create_bus_handler"):
            return "ERROR: bus not available in this context"
        try:
            args = json.loads(args_json) if args_json.strip() else {}
            if not isinstance(args, dict):
                return "ERROR: args_json must be a JSON object"
        except json.JSONDecodeError as e:
            return f"ERROR: args_json is not valid JSON: {e}"
        once = str(type).strip().lower() in ("once", "one", "oneshot", "one-shot", "1")
        hid = agent.create_bus_handler(
            event, once=once, description=f"mcp_tool {tool}",
            action={"kind": "mcp_tool", "tool": tool, "args": args},
        )
        return (f"Created mcp_tool handler {hid} on '{event}' -> {tool} "
                f"({'once' if once else 'every match'}).")

    def bus_create_shell_handler(self, event: str, command: str,
                                 type: str = "every") -> str:
        """Register a bus handler that RUNS A SHELL COMMAND on each matching event.

        No LLM turn runs. The event is exposed to the command via the env vars
        SSR_EVENT_TOPIC, SSR_EVENT_SOURCE and SSR_EVENT_PAYLOAD (JSON).

        Args:
            event: Topic pattern to match.
            command: Shell command line to execute.
            type: 'every' or 'once'.
        """
        agent = getattr(self, "agent_instance", None)
        if agent is None or not hasattr(agent, "create_bus_handler"):
            return "ERROR: bus not available in this context"
        once = str(type).strip().lower() in ("once", "one", "oneshot", "one-shot", "1")
        hid = agent.create_bus_handler(
            event, once=once, description=f"shell {command[:40]}",
            action={"kind": "shell", "command": command},
        )
        return (f"Created shell handler {hid} on '{event}' "
                f"({'once' if once else 'every match'}).")

    def bus_create_python_handler(self, event: str, code: str,
                                  type: str = "every") -> str:
        """Register a bus handler that EXECUTES A PYTHON SNIPPET on each matching event.

        No LLM turn runs. The snippet runs with these names in scope: event, topic,
        source, payload (the event payload dict), agent, bus, settings, and helpers
        mcp(tool, **args) and shell(cmd).

        Args:
            event: Topic pattern to match.
            code: Python source to exec on each matching event.
            type: 'every' or 'once'.
        """
        agent = getattr(self, "agent_instance", None)
        if agent is None or not hasattr(agent, "create_bus_handler"):
            return "ERROR: bus not available in this context"
        once = str(type).strip().lower() in ("once", "one", "oneshot", "one-shot", "1")
        hid = agent.create_bus_handler(
            event, once=once, description="python handler",
            action={"kind": "python", "code": code},
        )
        return (f"Created python handler {hid} on '{event}' "
                f"({'once' if once else 'every match'}).")

    def bus_remove_handler(self, handler_id: str) -> str:
        """Destroy a bus handler created by bus_create_handler.

        Args:
            handler_id: The id returned when the handler was created.
        """
        agent = getattr(self, "agent_instance", None)
        if agent is None or not hasattr(agent, "remove_bus_handler"):
            return "ERROR: bus not available in this context"
        return "Removed bus handler." if agent.remove_bus_handler(handler_id) else "No such handler."

    def bus_listeners(self) -> str:
        """List the active bus event handlers on this agent."""
        agent = getattr(self, "agent_instance", None)
        if agent is None or not hasattr(agent, "bus_handlers"):
            return "ERROR: bus not available in this context"
        handlers = agent.bus_handlers()
        if not handlers:
            return "(no active bus handlers)"
        return "\n".join(
            f"- {h['id']}  pattern='{h.get('pattern')}'  "
            f"kind={h.get('kind', 'subagent')}  "
            f"{'once' if h.get('once') else 'every'}"
            for h in handlers
        )

    def bus_history(self, pattern: str = "**", limit: int = 10) -> str:
        """Show recent events seen on the bus (most recent last).

        Args:
            pattern: Topic pattern to filter by (defaults to everything).
            limit: Maximum number of events to return.
        """
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 10
        bus = self._bus()
        if bus is None:
            return "ERROR: bus not available in this context"
        events = bus.history(pattern, limit)
        if not events:
            return "(no events in bus history)"
        return "\n".join(f"- {e.topic} ({e.source}): {e.payload}" for e in events)

    def reindex_context(self, category: str = "all") -> str:
        """Rebuild the model2vec embedding index for the context pool.

        Args:
            category: 'all' or a specific category to reindex.
        """
        cats = None if category in ("all", "", None) else [category]
        counts = self.retriever.reindex(cats)
        return "Reindexed: " + ", ".join(f"{k}={v}" for k, v in counts.items())

    # --------------------------------------------------------- plugin tools
    def _plugin_tool_callables(self) -> list:
        """In-process agent tools contributed by enabled plugins.

        A plugin declares ``"agent_tools": ["pkg.module:ClassName", ...]`` in its
        manifest; each class is constructed with this ToolKit and must expose a
        ``callables()`` returning its tool functions. This is how e.g. the bundled
        ``openarm`` plugin contributes the arm-control tools without the core
        hardcoding them. Failures are skipped so one bad plugin can't break tools.
        """
        import importlib

        from .. import plugins

        out: list = []
        try:
            specs = plugins.plugin_agent_tools(self.settings)
        except Exception:
            return out
        for spec in specs:
            try:
                mod_name, _, attr = spec.partition(":")
                obj = getattr(importlib.import_module(mod_name), attr)
                out.extend(obj(self).callables())
            except Exception:
                continue
        return out

    # ------------------------------------------------------------- collection
    def callables(self) -> list:
        return [
            self.read_file,
            self.write_file,
            self.list_dir,
            self.run_command,
            self.spawn_terminal,
            self.check_terminal,
            self.send_to_terminal,
            self.kill_terminal,
            self.update_plan,
            self.get_plan,
            self.remember,
            self.recall_memory,
            self.search_context,
            self.web_search,
            self.spawn_sub_agent,
            self.reindex_context,
            self.push_notification,
            self.send_file_to_user,
            self.bus_publish,
            self.bus_create_handler,
            self.bus_create_mcp_handler,
            self.bus_create_shell_handler,
            self.bus_create_python_handler,
            self.bus_remove_handler,
            self.bus_listeners,
            self.bus_history,
            # In-process tools contributed by enabled plugins: openarm's arm_* and
            # miloco's miloco_* (Mi Home) both arrive here, not hardcoded.
            *self._plugin_tool_callables(),
        ]

    def specs(self) -> list[dict]:
        """Tool metadata used to seed the 'tools' context category."""
        out = []
        for fn in self.callables():
            doc = (fn.__doc__ or "").strip().split("\n")[0]
            out.append({"name": fn.__name__, "description": doc, "origin": "builtin"})
        return out
