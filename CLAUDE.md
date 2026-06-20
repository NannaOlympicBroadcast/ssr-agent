# SSR Agent — project guide

SSR Agent (`ssr`) is a command-line coding agent built on **Google ADK** with
**Gemini** (`gemini-3.1-flash-lite` by default). This file is loaded into the
`configurations` context category at startup (it is `claude.md`-compatible).

## Architecture

- `ssr/cli.py` — entrypoint, TUI banner, interactive REPL, subcommands.
- `ssr/banner.py` — ASCII-art "Welcome To SSR" splash + subtitle.
- `ssr/config.py` — `~/.ssr` home, `.env` loading, `Settings`.
- `ssr/context_pool/` — the core. Five categories (tools / configurations /
  skills / memory / **refs**), model2vec embedding index in `~/.ssr/indexes`, and
  the unified `Retriever` (classic grep + embedding search). The `refs` category
  is backed by `REFS.md` (a markdown table of reference materials:
  name / location / content) in the project root and/or `~/.ssr`.
- `ssr/agent/` — `SSRAgent` (ADK agent + genai fallback), `ToolKit`
  (filesystem, run_command, memory, web_search, search_context, sub-agents,
  planning), `MemoryStore`, `SessionStore` (append-only conversation transcripts
  in `~/.ssr/sessions/<id>.jsonl`; every REPL / one-shot / channel turn is
  recorded and is browsable via `/sessions`).
- `ssr/skills/` — skill discovery across `~/.agent`, `~/.codex`, `~/.gemini`,
  `~/.claude`, `~/.ssr` + built-in skill installer (bundled skills in
  `ssr/builtin_skills/`, incl. `review-mode`).
- `ssr/plugins.py` + `ssr/builtin_plugins/` — bundled *plugins* (Claude-Code
  `.claude-plugin/plugin.json` + `.mcp.json` format) that contribute MCP servers,
  merged with `~/.ssr/mcp.json`. Ships `chrome-devtools`; supports shared
  credentials via `${namespace.key}` → `~/.ssr/<namespace>.json` (e.g. the `miot`
  plugin shares the `xiaomi` channel's credentials).
- `ssr/channels/` — IM/voice channels: `feishu`, `wechat`, and `xiaomi` (XiaoAI
  speaker: polls the Mi cloud for speech and replies via TTS). Channel slash
  commands stay in sync with the CLI; channels are not pinned to a default dir.
- `ssr/integrations/` — `pm2` background tasks, `feishu` (Lark) bot, `acp`
  (Agent Client Protocol) server, `mcp_client` (spawns the MCP servers in
  `~/.ssr/mcp.json` and speaks JSON-RPC over stdio; tools are exposed to the
  model as `mcp__<server>__<tool>` and routed by `SSRAgent`), `gateway` (`ssr gateway` —
  installs a channel-bound instance as a **system service** via the native
  manager per OS: systemd user unit / launchd plist / Windows scheduled task;
  records in `~/.ssr/gateways.json`, the service runs `ssr gateway run <name>`).
- Container deployment: `Dockerfile` + `docker-compose.yml` (`SSR_HOME=/data/.ssr`
  on the `ssr-data` volume; entrypoint runs `ssr init` then `ssr <command>`).

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
- `ssr channel config xiaomi` / `ssr channel on xiaomi` — XiaoAI speaker channel
- While the agent runs: `/stop` interrupts the task; `/btw <q>` answers a side
  question concurrently (isolated toolkit, no races). On an approval prompt,
  `/disallow <reason>` (or a typed reason in the TUI) is fed back to the model.
- `python -m pytest` — run tests
