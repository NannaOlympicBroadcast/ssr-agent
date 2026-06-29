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
  `bus_remove_handler`, `bus_listeners`, `bus_history`. A handler runs one of **4
  action kinds**: `subagent` (fire an agent turn — the classic handler), `mcp_tool`
  (call an active MCP tool directly), `shell` (run a terminal command, with the
  event in `SSR_EVENT_TOPIC`/`SSR_EVENT_SOURCE`/`SSR_EVENT_PAYLOAD`), and `python`
  (exec a snippet with `event`/`payload`/`agent`/`bus` + `mcp()`/`shell()` helpers).
  The non-`subagent` kinds run their side effect directly (no LLM turn). The agent
  creates them via `bus_create_handler` (subagent), `bus_create_mcp_handler`,
  `bus_create_shell_handler`, `bus_create_python_handler`; plugins can declare the
  same handlers in their manifest (`handlers`). `push_notification` can target the
  `xiaomi` speaker (voice-only TTS). REPL `/bus`, CLI
  `ssr bus <serve|send|listen|status>` (all accept `--api-key`).
- `ssr/srdb/` — the **agent debug server**. Every main-agent process opens one
  `SrdbServer` (a TCP server speaking newline-delimited JSON-RPC 2.0 on an
  OS-allocated port, key-authenticated). Each live `SSRAgent` self-registers
  (`registry`) and advertises a `tcp://host:port?key=…&agent=<id>` link (printed
  to **stderr** on creation, also `agent.srdb_link`). Lets a debugger inspect all
  running agents / sub-agents (bus-handler agents) / sessions / channels / bus
  state; **edit** sessions (`SessionStore.replace`), sub-agent config and bus
  events; send a message straight to a channel; **`srdb.watch`** to stream an
  agent's (and its sub-agents') real-time events (`turn_start`/`reply`/`thinking`/
  `tool_call`/`tool_result`/`sub_agent`/`bus_event`) via a tap on `SSRAgent._emit`
  (works even when the TUI `on_event` is unset, e.g. headless channels; `run_parts`
  emits `turn_start`+`reply` so even a no-tool turn is visible); and **`srdb.eval`**
  arbitrary Python in the live runtime (`agent`/`settings`/`bus` in scope).
  `client.SrdbClient` is the sync client; CLI `ssr srdb
  <agents|call|eval|send|watch> <tcp-link> …`. Bind is `127.0.0.1` only; disable
  with `SSR_SRDB=0`, override `SSR_SRDB_HOST`/`_PORT`/`_KEY`.
- `ssr/plugins.py` + `ssr/builtin_plugins/` — bundled *plugins* (Claude-Code
  `.claude-plugin/plugin.json` + `.mcp.json` format). A plugin can contribute three
  things: **MCP servers** (`mcpServers` / `.mcp.json`), **in-process agent tools**
  (`"agent_tools": ["pkg.module:ClassName", …]` — a class built with the ToolKit
  exposing `callables()`), and **bus event handlers** (`"handlers": [{event, kind,
  …}]`, kind ∈ subagent/mcp_tool/shell/python — registered on agent startup).
  Enable/disable from the CLI (`ssr plugin list|enable|disable|info`), recorded in
  `~/.ssr/plugins.json`; MCP servers also merge with `~/.ssr/mcp.json`. Shared
  credentials via `${namespace.key}` → `~/.ssr/<namespace>.json` (e.g. the `miot`
  plugin shares the `xiaomi` channel's credentials). Ships `chrome-devtools`,
  `miot`, and `openarm` (the OpenArm/Isaac-Lab arm-control tools — `arm_*` — which
  used to be the removed `ssr arm` command; they are now just an agent-tools plugin
  driven from any session over the bus).
- `ssr/channels/` — IM/voice channels: `feishu`, `wechat`, and `xiaomi` (XiaoAI
  speaker: polls the Mi cloud **conversation-history API** for speech and replies
  via TTS — pausing playback first, and using the MiIO `play-text` action where
  MiNA `text_to_speech` silently no-ops; markdown is stripped before TTS and the
  reply is kept short. A speaker has no usable approval UX, so xiaomi
  **auto-approves all commands by default** (`AutoApprovalHandler`); set
  `"auto_approve": false` in `xiaomi.json` to restore TTS `/approve` prompts). `ssr channel login xiaomi` does a
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
  **Windows nssm** (a real Windows service via `nssm install`/`set` —
  `SERVICE_AUTO_START` + restart throttle; falls back to a scheduled task if
  nssm is absent); records in
  `~/.ssr/gateways.json`, the service runs `ssr gateway run <name>`).
- Container deployment: `Dockerfile` + `docker-compose.yml` (`SSR_HOME=/data/.ssr`
  on the `ssr-data` volume; entrypoint runs `ssr init` then `ssr <command>`).

## Conventions
- Keep tools as plain typed functions with docstrings (ADK auto-wraps them).
- Degrade gracefully when optional deps / network are missing.
- Persist durable facts via the `remember` tool → `memory.md`.

## Debugging agents with srdb (do this first)
When a bug involves a **running** agent — a channel not replying, a stuck turn, a
sub-agent / bus handler misbehaving, wrong model/proxy in a gateway service — use
**srdb** to inspect the *live* process instead of guessing from code or restarting.
It beats `print`-debugging because the agent is already running with its real
config, sessions, channels and bus.

1. **Get the link.** Every agent prints `[srdb] debug this agent: tcp://127.0.0.1:<port>?key=…&agent=<id>`
   to **stderr** at startup. For a gateway service it's in `~/.ssr/logs/gateway-<name>.log`
   (grep `srdb`). No link in scope? Reproduce locally with `ssr ask "<prompt>"` or a
   tiny script that builds `SSRAgent(load_settings())` and reads `agent.srdb_link`.
2. **See what's live, in real time:**
   - `ssr srdb agents <link>` — every agent/sub-agent in the process (busy, model,
     session, handler/MCP counts).
   - `ssr srdb watch <link> all` — **stream the turn as it happens**: `turn_start`
     → `thinking` → `tool_call`/`tool_result` → `reply` (sub-agent events tagged
     `sub`). Run this, then trigger the agent (send the channel a message) to watch
     where a turn stalls or errors.
   - `ssr srdb call <link> srdb.bus` / `srdb.channels` / `srdb.sessions` — bus
     listeners & bridge, channel state, recorded sessions.
3. **Poke the live runtime** with `ssr srdb eval <link> '<python>'` (`agent`,
   `settings`, `bus` in scope). This is the fastest way to confirm a hypothesis —
   e.g. check the real model chain / proxy the *service* sees:
   `ssr srdb eval <link> 'agent.models_config.get_primary().id'`,
   `ssr srdb eval <link> 'import os; (os.environ.get("HTTPS_PROXY"), os.environ.get("GEMINI_API_KEY")[:6])'`,
   or drive a turn directly: `ssr srdb eval <link> 'agent.run("ping")'`.
4. **Edit/inject to reproduce:** `srdb.session.edit` rewrites a transcript,
   `srdb.bus.emit` injects an event to fire a handler, `ssr srdb send <link>
   <channel> <target> <text>` posts straight to a channel.

Rule of thumb: reproduce with `watch` + `eval` against the live agent, confirm the
exact failing call/config there, *then* fix the code. srdb binds to loopback and
needs the key; `srdb.eval` is full in-process Python, so it's debug-only.

## Useful commands
- `ssr` — launch TUI
- `ssr ask "<prompt>"` — one-shot
- `ssr index [category]` / `/index` — rebuild the embedding index
- `ssr --experimental-acp` — ACP server over stdio
- `ssr task create <name> "<prompt>" --cron "*/30 * * * *"` — pm2 task
- `ssr plugin list` / `ssr plugin enable <name>` / `ssr plugin disable <name>` /
  `ssr plugin info <name>` — manage plugins (MCP servers, agent tools, bus handlers)
- `ssr feishu configure` — set up the Lark bot
- `ssr channel config xiaomi` / `ssr channel on xiaomi` — XiaoAI speaker channel
- `ssr bus serve` — run a remote bus server; `ssr bus send <topic> '<json>'` /
  `ssr bus listen '<pattern>'` / `ssr bus status` — talk to it from the CLI
- `ssr srdb agents <tcp-link>` / `ssr srdb eval <tcp-link> '<python>'` /
  `ssr srdb call <tcp-link> <method> '<json>'` / `ssr srdb send <tcp-link>
  <channel> <target> <text>` — debug a running agent (link is printed on stderr
  at agent startup, e.g. in the gateway log)
- While the agent runs: `/stop` interrupts the task; `/btw <q>` answers a side
  question concurrently (isolated toolkit, no races). On an approval prompt,
  `/disallow <reason>` (or a typed reason in the TUI) is fed back to the model.
- `python -m pytest` — run tests
