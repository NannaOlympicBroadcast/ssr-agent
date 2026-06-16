"""Built-in agent tools: filesystem, shell, memory, web search, retrieval,
planning and sub-agent spawning.

Each tool is a plain Python function with a typed signature and docstring so it
can be auto-wrapped by Google ADK's ``FunctionTool``. A :class:`ToolKit` binds
the tools to runtime context (settings, retriever, memory).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..config import Settings
from ..context_pool.retrieval import Retriever
from .memory import MemoryStore

_MAX_OUTPUT = 12_000
_MAX_READ = 60_000


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

    def run_command(self, command: str, timeout: int = 120) -> str:
        """Run a shell command in the project directory and return stdout/stderr.

        Args:
            command: The shell command line to execute.
            timeout: Maximum seconds to wait before aborting.
        """
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=str(self.settings.project_dir),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return f"ERROR: command timed out after {timeout}s"
        out = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
        out = out[:_MAX_OUTPUT]
        return f"exit={proc.returncode}\n{out}".strip()

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
    def callables(self) -> list:
        return [
            self.read_file,
            self.write_file,
            self.list_dir,
            self.run_command,
            self.update_plan,
            self.get_plan,
            self.remember,
            self.recall_memory,
            self.search_context,
            self.web_search,
            self.spawn_sub_agent,
            self.reindex_context,
        ]

    def specs(self) -> list[dict]:
        """Tool metadata used to seed the 'tools' context category."""
        out = []
        for fn in self.callables():
            doc = (fn.__doc__ or "").strip().split("\n")[0]
            out.append({"name": fn.__name__, "description": doc, "origin": "builtin"})
        return out
