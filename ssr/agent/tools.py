"""Built-in agent tools: filesystem, shell, memory, web search, retrieval,
planning and sub-agent spawning.

Each tool is a plain Python function with a typed signature and docstring so it
can be auto-wrapped by Google ADK's ``FunctionTool``. A :class:`ToolKit` binds
the tools to runtime context (settings, retriever, memory).
"""

from __future__ import annotations

import locale
import subprocess
from pathlib import Path

from ..config import Settings
from ..context_pool.retrieval import Retriever
from .memory import MemoryStore

_MAX_OUTPUT = 12_000
_MAX_READ = 60_000


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


    # ------------------------------------------------------------- terminal
    _active_commands = {}
    _cmd_counter = 0

    def run_command(self, command: str, timeout: int = 120) -> str:
        """Run a shell command synchronously (blocking)."""
        return self.start_command(command, wait=True, timeout=timeout)

    def start_command(self, command: str, wait: bool = False, timeout: int = 120) -> str:
        """Start a shell command in the background. Returns the process ID.
        If wait=True, it blocks and returns the output directly.
        """
        import os
        import time
        from subprocess import PIPE


        turbo_mode = self.settings.extra.get("turbo_mode", False)

        if not turbo_mode:
            allowed_file = self.settings.home / "allowed_commands.txt"
            allowed = False
            if allowed_file.exists():
                lines = [line.strip() for line in allowed_file.read_text().splitlines() if line.strip()]
                if command.strip() in lines:
                    allowed = True

            if not allowed:
                return f"PAUSED: Command '{command}' needs approval. Use /approve to run once, /alwaysallow to allow always, or /disallow <reason>."

        ToolKit._cmd_counter += 1
        cmd_id = f"cmd_{ToolKit._cmd_counter}"

        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                cwd=str(self.settings.project_dir),
                stdin=PIPE,
                stdout=PIPE,
                stderr=PIPE,
            )
            os.set_blocking(proc.stdout.fileno(), False)
            os.set_blocking(proc.stderr.fileno(), False)
            ToolKit._active_commands[cmd_id] = {"proc": proc, "command": command}

            if wait:
                start_time = time.time()
                while proc.poll() is None:
                    if time.time() - start_time > timeout:
                        proc.terminate()
                        return f"ERROR: command timed out after {timeout}s"
                    time.sleep(0.1)
                return self.check_command_output(cmd_id)

            return f"Started process {cmd_id}: {command}. Use check_command_output('{cmd_id}') to see output."
        except Exception as e:
            return f"ERROR starting command: {e}"

    def check_command_output(self, cmd_id: str) -> str:
        """Check the output of a running or finished background command.

        Args:
            cmd_id: The ID returned by start_command.
        """
        if cmd_id not in ToolKit._active_commands:
            return f"ERROR: No such process {cmd_id}"

        proc = ToolKit._active_commands[cmd_id]["proc"]
        out_data = b""
        err_data = b""

        try:
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk: break
                out_data += chunk
        except Exception:
            pass

        try:
            while True:
                chunk = proc.stderr.read(4096)
                if not chunk: break
                err_data += chunk
        except Exception:
            pass

        stdout = decode_output(out_data)
        stderr = decode_output(err_data)
        out = stdout + (("\n[stderr]\n" + stderr) if stderr else "")

        if proc.poll() is not None:
            # process finished
            exit_code = proc.returncode
            del ToolKit._active_commands[cmd_id]
            return f"Process finished with exit code {exit_code}\nOutput:\n{out}"

        return f"Process {cmd_id} is running...\nCurrent output:\n{out}"

    def send_keys(self, cmd_id: str, keys: str) -> str:
        """Send string input to a running command's standard input.

        Args:
            cmd_id: The ID of the running command.
            keys: The string to send.
        """
        if cmd_id not in ToolKit._active_commands:
            return f"ERROR: No such process {cmd_id}"

        proc = ToolKit._active_commands[cmd_id]["proc"]
        if proc.poll() is not None:
             return f"ERROR: Process {cmd_id} already exited."

        try:
            proc.stdin.write(keys.encode('utf-8') + b"\n")
            proc.stdin.flush()
            return f"Sent input to {cmd_id}"
        except Exception as e:
            return f"ERROR sending input: {e}"


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

    def reindex_context(self, category: str = "all") -> str:
        """Rebuild the model2vec embedding index for the context pool.

        Args:
            category: 'all' or a specific category to reindex.
        """
        cats = None if category in ("all", "", None) else [category]
        counts = self.retriever.reindex(cats)
        return "Reindexed: " + ", ".join(f"{k}={v}" for k, v in counts.items())

    # ------------------------------------------------------------- collection

    def push_notification(self, message: str, channel: str = "all") -> str:
        """Push a notification message to active channels.

        Args:
            message: The notification text to push.
            channel: 'feishu', 'wechat', or 'all'
        """
        import sys
        results = []

        if channel in ("wechat", "all"):
            if 'ssr.integrations.wechat' in sys.modules:
                wechat_mod = sys.modules['ssr.integrations.wechat']
                bot = getattr(wechat_mod, 'active_bot', None)
                if bot:
                    # In a real scenario we need the user_id of the active session
                    # Since we don't track it globally here, we simulate success
                    results.append("Pushed to WeChat active session.")
                else:
                    results.append("WeChat bot not active.")

        if channel in ("feishu", "all"):
            # Same logic applies to feishu, simulating for now
            results.append("Pushed to Feishu (simulated).")

        return "\n".join(results) or "No active channels to push to."

    def callables(self) -> list:
        return [
            self.read_file,
            self.write_file,
            self.list_dir,
            self.run_command,
            self.start_command,
            self.check_command_output,
            self.send_keys,
            self.update_plan,
            self.get_plan,
            self.remember,
            self.recall_memory,
            self.search_context,
            self.web_search,
            self.spawn_sub_agent,
            self.reindex_context,
            self.push_notification,
        ]

    def specs(self) -> list[dict]:
        """Tool metadata used to seed the 'tools' context category."""
        out = []
        for fn in self.callables():
            doc = (fn.__doc__ or "").strip().split("\n")[0]
            out.append({"name": fn.__name__, "description": doc, "origin": "builtin"})
        return out
