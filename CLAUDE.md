# SSR Agent — project guide

SSR Agent (`ssr`) is a command-line coding agent built on **Google ADK** with
**Gemini** (`gemini-3.1-flash-lite` by default). This file is loaded into the
`configurations` context category at startup (it is `claude.md`-compatible).

## Architecture

- `ssr/cli.py` — entrypoint, TUI banner, interactive REPL, subcommands.
- `ssr/banner.py` — ASCII-art "Welcome To SSR" splash + subtitle.
- `ssr/config.py` — `~/.ssr` home, `.env` loading, `Settings`.
- `ssr/context_pool/` — the core. Four categories (tools / configurations /
  skills / memory), model2vec embedding index in `~/.ssr/indexes`, and the
  unified `Retriever` (classic grep + embedding search).
- `ssr/agent/` — `SSRAgent` (ADK agent + genai fallback), `ToolKit`
  (filesystem, run_command, memory, web_search, search_context, sub-agents,
  planning), `MemoryStore`.
- `ssr/skills/` — skill discovery across `~/.agent`, `~/.codex`, `~/.gemini`,
  `~/.claude`, `~/.ssr` + built-in skill installer.
- `ssr/integrations/` — `pm2` background tasks, `feishu` (Lark) bot, `acp`
  (Agent Client Protocol) server, `mcp_client` (spawns the MCP servers in
  `~/.ssr/mcp.json` and speaks JSON-RPC over stdio; tools are exposed to the
  model as `mcp__<server>__<tool>` and routed by `SSRAgent`).

## Conventions
- Keep tools as plain typed functions with docstrings (ADK auto-wraps them).
- Degrade gracefully when optional deps / network are missing.
- Persist durable facts via the `remember` tool → `memory.md`.

## Useful commands
- `ssr` — launch TUI
- `ssr ask "<prompt>"` — one-shot
- `ssr index [category]` / `/index` — rebuild the embedding index
- `ssr --experimental-acp` — ACP server over stdio
- `ssr task create <name> "<prompt>" --cron "*/30 * * * *"` — pm2 task
- `ssr feishu configure` — set up the Lark bot
- `python -m pytest` — run tests
