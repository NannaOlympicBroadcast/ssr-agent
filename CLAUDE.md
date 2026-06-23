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
- `ssr/bus/` — the **event bus**: asynchronous, structured (JSON-RPC 2.0)
  pub/sub for managing agents and tasks. `core.MessageBus` is the in-process bus
  every running `SSRAgent` owns (topic wildcards `*`/`**`, listeners, `wait_for`
  to suspend a session until an event arrives, de-dup by event id).
  `server.BusServer` (`ssr bus serve`) brokers events between peers over
  WebSocket; `client.BusClient` is the synchronous programmatic client for
  external programs / other agents, and `client.RemoteBusBridge` bridges an
  agent's built-in bus to a remote server. Every `ssr` main process **auto-starts
  a non-blocking embedded bus server** (`SSR_BUS_SERVE`, default on; reuses an
  existing one on port conflict) and bridges to it; set `SSR_BUS_API_KEY` to
  require auth (handshake `bus.auth`), plus `SSR_BUS_HOST`/`SSR_BUS_PORT`/
  `SSR_BUS_URL`. Agent tools: `bus_publish`, `bus_create_handler` (register a
  *handler agent* that fires a fresh turn on every matching event — `type`
  once/every, `inherit_session` to continue the current conversation or run an
  isolated sub-agent; never blocks, so no missed-on-timeout events),
  `bus_remove_handler`, `bus_listeners`, `bus_history`. `push_notification` can
  target the `xiaomi` speaker (voice-only TTS). REPL `/bus`, CLI
  `ssr bus <serve|send|listen|status>` (all accept `--api-key`).
- `ssr/plugins.py` + `ssr/builtin_plugins/` — bundled *plugins* (Claude-Code
  `.claude-plugin/plugin.json` + `.mcp.json` format) that contribute MCP servers,
  merged with `~/.ssr/mcp.json`. Ships `chrome-devtools`; supports shared
  credentials via `${namespace.key}` → `~/.ssr/<namespace>.json` (e.g. the `miot`
  plugin shares the `xiaomi` channel's credentials).
- `ssr/channels/` — IM/voice channels: `feishu`, `wechat`, and `xiaomi` (XiaoAI
  speaker: polls the Mi cloud **conversation-history API** for speech and replies
  via TTS — pausing playback first, and using the MiIO `play-text` action where
  MiNA `text_to_speech` silently no-ops). `ssr channel login xiaomi` does a
  one-time interactive login that caches the passToken (so a headless gateway can
  log in without re-verification): `--browser` opens a real Chrome and harvests
  the token via the DevTools Protocol; `--pass-token/--user-id` import it from
  browser cookies. Channel slash commands stay in sync with the CLI; channels are
  not pinned to a default dir.
- `ssr/integrations/` — `pm2` background tasks, `feishu` (Lark) bot, `acp`
  (Agent Client Protocol) server, `mcp_client` (spawns the MCP servers in
  `~/.ssr/mcp.json` and speaks JSON-RPC over stdio; tools are exposed to the
  model as `mcp__<server>__<tool>` and routed by `SSRAgent`; failed/timed-out MCP
  servers are killed (no orphan leak) and children are tied to the parent via a
  Windows Job object), `gateway` (`ssr gateway` — installs a channel-bound
  instance as a **system service** per OS: systemd user unit / launchd plist /
  **Windows pm2** (ecosystem file + restart guards + `pm2-windows-startup`;
  falls back to a scheduled task if pm2 is absent); records in
  `~/.ssr/gateways.json`, the service runs `ssr gateway run <name>`).
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
- `ssr bus serve` — run a remote bus server; `ssr bus send <topic> '<json>'` /
  `ssr bus listen '<pattern>'` / `ssr bus status` — talk to it from the CLI
- While the agent runs: `/stop` interrupts the task; `/btw <q>` answers a side
  question concurrently (isolated toolkit, no races). On an approval prompt,
  `/disallow <reason>` (or a typed reason in the TUI) is fed back to the model.
- `python -m pytest` — run tests
