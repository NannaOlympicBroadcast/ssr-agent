# SSR Agent ✨

A beautiful command-line **coding agent**, built on [Google ADK](https://adk.dev)
and Gemini. `claude.md`-compatible, with a four-category context pool and
model2vec vector retrieval at its core.

```
ssr
┌────────────────────────── ✨ SSR Agent ✨ ──────────────────────────┐
│  ██╗    ██╗███████╗██╗      ██████╗ ██████╗ ███╗   ███╗███████╗     │
│  ...   Welcome To SSR   ...                                        │
└────────────────────── powered by Google ADK · gemini ───────────────┘
```

## Features

1. **Polished TUI** — `ssr` prints an ASCII-art *Welcome To SSR* banner, then drops into an interactive REPL. Features non-blocking concurrent stdin polling on both Windows and POSIX, allowing you to run slash commands (like `/status`, `/help`, `/bypass-permissions`), ask side questions with `/btw`, request termination with `/stop`, or approve/disallow pending commands (via `1`/`2`/`3` or `/approve`/`/alwaysallow`/`/disallow`) while the agent loop is executing (including in `/goal` mode).
2. **Coding-agent core** — step planning, filesystem access, command execution,
   durable **MEMORY**, **sub-agent** spawning, and **Tavily** web search.
3. **`claude.md`-compatible config** + skills discovered from `~/.agent/skills`,
   `~/.codex/skills`, `~/.gemini/skills`, `~/.claude/skills`, `~/.ssr/skills`.
   The **context pool** has four categories:
   - **tools** — built-in + MCP tools (`~/.ssr/mcp.json`)
   - **configurations** — `claude.md` / `soul.md` / `profile.md` (global + project)
   - **skills** — user skills
   - **memory** — `memory.md` (global + project) + `.ssr/past_chats.jsonl`

   On every request, context is retrieved by **vector search**
   ([model2vec](https://github.com/MinishLab/model2vec), indexes in
   `~/.ssr/indexes`). Build the index on a schedule, manually via `/index`, or let
   the agent call `reindex_context`. The agent's `search_context` tool chooses the
   **category**, **keyword**, and **mode** (`classic` grep / `embedding`).
4. **Background tasks via pm2** — schedule recurring agent jobs at runtime; or
   trigger from a **Feishu/Lark bot** over a **WebSocket long connection**
   (`lark-oapi` `lark.ws.Client`, the same WS-only transport as the
   [Vercel Chat SDK Lark adapter](https://chat-sdk.dev/adapters/vendor-official/lark)) —
   no public webhook URL needed; interactive setup of working dir, session id,
   ak & sk.
5. **ACP interface** — `ssr --experimental-acp` speaks the
   [Agent Client Protocol](https://agentclientprotocol.com).
6. **Built-in skills** installed to `~/.ssr/skills`:
   [larksuite](https://github.com/larksuite/cli) and
   [agent-browser](https://github.com/vercel-labs/agent-browser).
7. **Gateway & deployment** — `ssr gateway` installs a channel-bound instance as
   a cross-platform **system service** (systemd / launchd / Windows scheduled
   task) for always-on messaging channels, and a `Dockerfile` /
   `docker-compose.yml` provide a containerised deployment.
8. **Extensible via plugins** — a plugin can contribute **MCP servers**,
   **in-process agent tools** (`agent_tools`), and **declarative bus event
   handlers** (`handlers`), all toggleable from the CLI (`ssr plugin
   list|enable|disable|info`). Bundles `chrome-devtools`, `openarm` (robot-arm
   control) and `miloco` (Xiaomi Mi Home). Bus handlers come in four kinds —
   subagent, MCP-tool call, shell command, and python snippet — so events wire to
   actions with or without an LLM turn.

## Install

```bash
./install.sh          # or: pip install -e . && ssr init
```

Configure `~/.ssr/.env`:

```dotenv
GEMINI_API_KEY=...
TAVILY_API_KEY=...
DEFAULT_MODEL=gemini-3.1-flash-lite
```

## Usage

```bash
ssr                      # launch the TUI
ssr ask "fix the failing test in app.py"
ssr index                # rebuild the model2vec context index
ssr --experimental-acp   # run as an ACP server

# background tasks (pm2)
ssr task create nightly "summarise today's git log" --cron "0 22 * * *"

# Unified Channels (Feishu / WeChat / Xiaomi / etc.) - Note: `ssr feishu` is deprecated
ssr channel config feishu  # configure Feishu channel (alias: configure)
ssr channel on feishu      # start listening on Feishu channel (alias: serve)
ssr channel config wechat  # configure WeChat channel (scanning QR code to log in; network-resilient status checks handling wait, scanned, expired, canceled, timeout, and customized error message responses; press Ctrl+C to cancel)
ssr channel on wechat      # start listening on WeChat channel (with dynamic X-WECHAT-UIN headers, full base_info / client_id payload alignment, incoming image message decryption support, auto-exit on session timeout, robust media/file send support, and verbose logging for API calls)
ssr channel config xiaomi          # configure Xiaomi speaker channel (XiaoAI speaker, Mi passport login)
ssr channel login xiaomi --browser # open a real Chrome, log in, auto-harvest the token via DevTools (handles Mi safety verification)
ssr channel login xiaomi --pass-token <PT> --user-id <UID>  # or import passToken+userId from a logged-in browser (i.mi.com cookies)
ssr channel on xiaomi              # start listening on Xiaomi speaker channel (polls cloud conversation history; replies via TTS, pausing playback first so the reply is audible)

# Plugins (MCP servers, in-process agent tools, bus handlers)
ssr plugin list          # list plugins + status (chrome-devtools, openarm, miloco, …)
ssr plugin disable miloco  # turn a plugin's tools/handlers off (enable to re-add)

# Mi Home (Xiaomi Miloco) integration
ssr miloco status        # is the local Miloco service reachable / Mi account bound?
ssr miloco sync          # snapshot the home (devices/family/events) into context
ssr miloco bridge        # stream home activities onto the bus

# Multi-Model configurations
ssr models config        # interactively configure LLM models (Gemini, Anthropic, OpenAI)

# OpenAI-compatible API server
ssr serve --port 8000    # start a local OpenAI-compatible HTTP server

# Gateway: install a channel-bound instance as a system service (Win/macOS/Linux)
ssr gateway install voicebox --channel xiaomi   # create + start a background service
ssr gateway status                              # show all gateways and their state
ssr gateway stop voicebox                       # stop / start / restart
ssr gateway uninstall voicebox                  # remove the service

# Run EvalScope benchmark with custom runner
python run_benchmark.py
```

### Multi-Model Configuration (`ssr models config`)

Configure multiple LLM models (Gemini, Anthropic, and OpenAI format) interactively. Configured models are saved to `~/.ssr/models.json` and are used for agent execution with automatic retry and model fallback support.

You can set up API credentials in two ways:
1. **Environment Variables**: Provide the name of the environment variable containing your API key (e.g., `GEMINI_API_KEY`).
2. **Direct API Keys**: Input the actual API key directly. This key will be saved directly into the config file. If you accidentally paste your actual API key (e.g., starting with `sk-` or `AIza`) into the environment variable field, the configuration tool will automatically detect it, save it as a direct key, and set the environment variable name to its provider's default name.

### OpenAI-Compatible HTTP Server (`ssr serve`)

Expose the SSR Agent as a local OpenAI-compatible API server using FastAPI and Uvicorn. Exposes standard endpoints:
- `GET /v1/models`: Retrieve the list of configured models.
- `POST /v1/chat/completions`: Query the SSR Agent using OpenAI-compatible payloads.

Supports both standard JSON response and server-sent event (SSE) streaming (`stream: true`).

**Example Query:**
```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "default-gemini",
    "messages": [{"role": "user", "content": "Write a hello world script in Python"}],
    "stream": false
  }'
```

### Gateway — channel as a system service (`ssr gateway`)

A **gateway** turns a channel-bound SSR instance into a long-running background
**system service** that survives logout/reboot and restarts on failure. The same
command works on all three platforms, using each one's native service manager:

| Platform | Backend | Where it lives |
| --- | --- | --- |
| Linux | systemd **user** unit (`systemctl --user`) | `~/.config/systemd/user/ssr-gateway-<name>.service` |
| macOS | launchd LaunchAgent (`launchctl`) | `~/Library/LaunchAgents/com.ssr.gateway.<name>.plist` |
| Windows | **Docker** container (`--restart unless-stopped`) | container `ssr-gateway-<name>` |

On **Windows the native service backend is deprecated**: because the Miloco Mi
Home integration (and a clean POSIX runtime for the channels) can't run natively
on Windows, the gateway runs in a **Docker container** by default — auto-started
on boot and auto-restarted on failure by the Docker daemon, with the host
`~/.ssr` bind-mounted in. If Docker is unavailable it falls back to the legacy
**nssm** service (or a Scheduled Task at logon when nssm is absent). Force the
Docker backend on any platform with `SSR_GATEWAY_BACKEND=docker`.

```bash
# Configure the channel once, then install it as a service:
ssr channel config xiaomi
ssr gateway install voicebox --channel xiaomi   # or feishu | wechat | all
ssr gateway install voicebox --channel xiaomi --cwd /srv/app --no-start

ssr gateway list                  # show configured gateways
ssr gateway status [name]         # show service state + memory (RSS) per gateway
ssr gateway stats [name]          # live resource usage: PID / RSS / threads / source
ssr gateway stats name --watch    # refresh continuously (Ctrl-C to stop); --cpu adds CPU%
ssr gateway start|stop|restart name
ssr gateway uninstall name        # stop, remove the unit, drop the record
```

Memory is read per platform: systemd's cgroup `MemoryCurrent` on Linux, `ps`
RSS on macOS, and `docker stats` for the container on Windows (the deprecated
nssm/Scheduled-Task fallback reads the process `WorkingSetSize`). Install
[`psutil`](https://pypi.org/project/psutil/) for thread counts and `--cpu`
sampling (optional — it degrades gracefully without it).

Gateway definitions are stored in `~/.ssr/gateways.json`; the installed service
simply runs `ssr gateway run <name>`, which loads the record and serves its
channel. The service runs `python -m ssr` from the **same interpreter** you
installed with, and pins `SSR_HOME` so it finds your config and tokens.

Notes:
- **Linux:** user services stop when you log out unless lingering is enabled —
  run `loginctl enable-linger $USER` for always-on. Logs: `journalctl --user -u ssr-gateway-<name> -f`.
- **Windows (Docker, default):** the gateway runs as a container
  `ssr-gateway-<name>` from the `ssr-agent` image, `--restart unless-stopped`
  (auto-start on boot, auto-restart on crash), with the host `~/.ssr`
  bind-mounted at `/data/.ssr` and `MILOCO_BASE_URL` pointing at
  `host.docker.internal:1810` so it reaches a Miloco service on the host. Build
  the image first (`docker build -t ssr-agent:latest .`) or set
  `SSR_DOCKER_IMAGE`. Logs: `docker logs -f ssr-gateway-<name>`. The legacy
  **nssm** service is used only when Docker is absent.
- **macOS:** stdout/stderr are written to `~/.ssr/logs/gateway-<name>.log`.
- If no manager is available (e.g. a minimal container without Docker), the
  gateway is still saved and run-instructions are printed.

### Docker deployment

The repo ships a `Dockerfile`, `docker-compose.yml` and `.dockerignore` for a
containerised deployment. All mutable state (config, tokens, sessions, indexes)
lives under `SSR_HOME=/data/.ssr`, mounted as the `ssr-data` volume so it
persists across container recreation.

```bash
cp .env.example .env        # fill in GEMINI_API_KEY / TAVILY_API_KEY

# OpenAI-compatible API server (default service) on :8000
docker compose up -d ssr

# Or run a messaging channel instead:
docker compose run --rm channel channel config xiaomi   # one-time interactive setup
docker compose --profile channel up -d channel
```

The image entrypoint scaffolds `~/.ssr` (idempotent `ssr init`) and then execs
`ssr <command>`. Override `command:` in compose (or `docker run … ssr <args>`)
to serve a different channel (`channel on feishu|wechat|all`) or a gateway.
Build with `--build-arg INSTALL_NODE=true` to also bundle Node.js for the
chrome-devtools MCP plugin.

### Remote control (`ssr rc`) — removed

`ssr rc` and the remote dispatch integration have been **removed**. Running
`ssr rc` now prints: *"rc(dispatch) is deprecated, the new agent bus protocol
will be implemented in next version."* The replacement agent-bus protocol is
planned for a future release.

### EvalScope Benchmark (`run_benchmark.py`)

SSR Agent integrates with the **EvalScope External Agent Bridge** framework. A custom runner (`SSRAgentRunner` inside [run_benchmark.py](file:///e:/ssr-agent/run_benchmark.py)) is registered using EvalScope's `@register_runner("ssr-agent")` decorator.

When running under EvalScope:
1. An OpenAI-compatible completion loop fallback (`_complete_openai` in `ssr/agent/core.py`) is triggered by the environment variable `SSR_USE_OPENAI=1`.
2. The agent forwards its LLM completions, tool definitions, and tool execution results back to the EvalScope bridge endpoint.
3. EvalScope intercepts and records the interaction trajectory as an `agent_trace`, which can be replayed and evaluated against benchmarks (such as GSM8K).

To execute the benchmark locally:
```bash
python run_benchmark.py
```

To execute the benchmark inside a Docker container (recommended for SWE-bench to avoid OS-level limitations and isolate dependencies):
```bash
docker run --rm \
  -v $(pwd):/workspace \
  -v ~/.ssr:/root/.ssr \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -w /workspace \
  python:3.12-slim bash run_in_docker.sh
```


### System Rules

The agent dynamically loads custom behavior rules from `~/.ssr/rules.md` (global) and `.ssr/rules.md` (project-level). The agent is strictly instructed to:
- Avoid placeholders, mocks, or other fake fallback forms.
- Report any command failures, package import issues, or image download errors directly to the user to handle.

### Asynchronous Terminal (Background Commands)

When the agent executes a background command via the `spawn_terminal` tool, it is monitored asynchronously. Upon completion of the process:
- The agent is automatically woken up for a single turn with the terminal exit status.
- The agent's generated response is pushed back to the corresponding channel (such as Feishu, WeChat, or XiaoAI) from which the command was initiated.

### Event Bus (async agent & task coordination)

Every running agent owns a built-in **event bus** for asynchronous coordination
between agents, tasks, and external programs. Events are **structured** and travel
over **JSON-RPC 2.0**; topics are dotted names with wildcards (`*` = one segment,
`**` = the rest, e.g. `task.*`, `agent.**`).

**Bus event handlers — 4 action kinds.** A handler reacts to each matching event
by running one of four actions, so wiring an event to a side effect no longer
always needs an LLM turn:
- **`subagent`** — fire a fresh agent turn following a prompt (the classic
  handler; `inherit_session=true` continues this conversation, `false` runs an
  isolated sub-agent).
- **`mcp_tool`** — call an active MCP tool directly (no LLM turn).
- **`shell`** — run a terminal command (the event is exposed via the
  `SSR_EVENT_TOPIC` / `SSR_EVENT_SOURCE` / `SSR_EVENT_PAYLOAD` env vars).
- **`python`** — exec a code snippet with `event` / `topic` / `source` /
  `payload` / `agent` / `bus` and `mcp(tool, **args)` / `shell(cmd)` helpers in
  scope.

The non-`subagent` kinds run their side effect directly on a daemon thread. This
replaces blocking — handlers never freeze the main loop and never miss an event on
a timeout. (To wait for one event, register a handler and end the turn; the event
starts a new turn, and the user can `/stop` to abort.)

**Agent tools** (the model can call these):
- `bus_publish(topic, payload_json)` — emit an event to communicate.
- `bus_create_handler(event, handler_prompt, type, inherit_session)` — register a
  **subagent** handler (fires an agent turn per matching event).
- `bus_create_mcp_handler(event, tool, args_json, type)` — handler that calls an
  MCP tool.
- `bus_create_shell_handler(event, command, type)` — handler that runs a shell
  command.
- `bus_create_python_handler(event, code, type)` — handler that execs python.
- `bus_remove_handler(handler_id)`, `bus_listeners()`, `bus_history(pattern)`.

Plugins can also declare handlers of any kind in their manifest (`handlers`), so
an event → action wiring can ship purely as config (see **Plugins** below).

**Embedded server (on by default).** Every `ssr` main process starts a
**non-blocking** bus server (so external scripts / other agents can connect) and
bridges its own bus to it. If the port is already taken (another `ssr` process
owns it), it transparently reuses that one. Set an **API key** to require
authentication — peers that don't present it are rejected:
```bash
# ~/.ssr/.env
SSR_BUS_API_KEY=your-secret   # require auth on the bus
SSR_BUS_HOST=127.0.0.1        # default
SSR_BUS_PORT=8765             # default
SSR_BUS_SERVE=1               # set 0 to disable the embedded server
SSR_BUS_URL=                  # set to bridge to an external server instead
```

**Remote bus server.** Or run a standalone broker that connects many peers:
```bash
ssr bus serve --host 0.0.0.0 --port 8765 --api-key SECRET   # start the broker
ssr bus send task.done '{"id": 42}' --api-key SECRET        # publish from the CLI
ssr bus listen 'task.*' --api-key SECRET                     # stream matching events
ssr bus status --api-key SECRET                              # ping + recent events
```
Point an agent at a broker with `SSR_BUS_URL=ws://host:8765` (or `/bus connect
ws://host:8765` in the TUI); its built-in bus is then bridged so local and remote
events flow both ways (de-duplicated by event id, so there are no echo loops).

**TUI slash command:** `/bus send|listen|wait|ls|history|connect|status`.

**External programmatic API.** Any external program or other AI agent can drive
the bus in code via the synchronous client — no `async`/`await` needed:
```python
from ssr.bus import BusClient

client = BusClient("ws://localhost:8765", source="my-script", api_key="your-secret").connect()
client.publish("task.started", {"id": 42})
client.subscribe("task.*", lambda ev: print("event:", ev.topic, ev.payload))
event = client.wait_for("task.done", timeout=30)   # block for one event
client.close()
```

### Hooks Mechanism

Allows executing arbitrary shell commands configured in global `~/.ssr/hooks.json` or project-level `.ssr/hooks.json` on key agent events (`UserPromptSubmit` at the start of a prompt and `Stop` at the end of a reply). The event payload is passed to the command's standard input as a JSON string.

Example `hooks.json` format:
```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "command": "python -c \"import sys, json; payload = json.load(sys.stdin); print(f'Prompt: {payload[\"prompt\"]}')\"",
        "timeout": 10
      }
    ],
    "Stop": [
      {
        "command": "echo 'Done!'"
      }
    ]
  }
}
```

### Plugins Mechanism

Plugins are discovered from `~/.ssr/plugins` (bundled ones are installed there on
`ssr init`) and the repo's `ssr/builtin_plugins/`. A plugin (Claude-Code
`.claude-plugin/plugin.json` manifest) can contribute **three** kinds of things:

*   **MCP servers** — inline in `plugin.json` (`mcpServers` key) or a sibling
    `.mcp.json`, merged with `~/.ssr/mcp.json`. Path placeholders (`${__dirname}`)
    and shared credentials (`${namespace.key}` → `~/.ssr/<namespace>.json`) are
    resolved at runtime.
*   **In-process agent tools** — `"agent_tools": ["pkg.module:ClassName", …]`,
    where the class is constructed with the ToolKit and exposes `callables()`.
    This is how `openarm` and `miloco` ship their tools without the core
    hardcoding them.
*   **Bus event handlers** — `"handlers": [{event, kind, …}]` (kind ∈
    `subagent` / `mcp_tool` / `shell` / `python`), registered on agent startup, so
    a plugin can wire a custom event → action declaratively.

Enable/disable plugins from the CLI; the state is recorded in `~/.ssr/plugins.json`:

```bash
ssr plugin list                 # all plugins + enabled/disabled + what each contributes
ssr plugin enable <name>
ssr plugin disable <name>
ssr plugin info <name>
```

(Separately, plugin **manifests** are also surfaced into the `skills` context-pool
category as `{"kind": "plugin"}` knowledge items.)

### Built-in Plugins

*   **`chrome-devtools`** — wraps [`chrome-devtools-mcp`](https://github.com/ChromeDevTools/chrome-devtools-mcp)
    for browser automation, debugging and performance analysis (an MCP-server plugin).
*   **`openarm`** — the OpenArm / Isaac-Lab robot-arm control tools (`arm_*`):
    capability-driven skills (pick / place / move / raw) driven over the bus with a
    suspend → completion-handler → wake loop. An `agent_tools` plugin; it replaces
    the old `ssr arm` command, so the arm is now controllable from any session.
*   **`miloco`** — the Xiaomi Mi Home tools (`miloco_*`), an `agent_tools` plugin
    backed by the native **Miloco** integration (see below). Disable with
    `ssr plugin disable miloco`.

> **Note:** the former `miot` MCP plugin has been **removed**. Xiaomi Mi Home
> device control, family/identity, home events and automations are now provided
> by the **Miloco** integration / plugin — see [Mi Home via Miloco](#mi-home-via-miloco) below.

### Mi Home via Miloco

SSR integrates with [Xiaomi Miloco](https://github.com/XiaoMi/xiaomi-miloco), the
official open-source "perceptive home" gateway, instead of a third-party MIoT MCP
server. Run Miloco locally (it binds your Mi account and exposes an HTTP API on
`http://127.0.0.1:1810`), then:

```bash
ssr miloco config        # set the Miloco base_url / API key (~/.ssr/miloco.json)
ssr miloco status        # is Miloco reachable? is the Mi account bound?
ssr miloco sync          # snapshot devices/family/events/automations into context
ssr miloco devices       # list Mi Home devices
ssr miloco activities    # recent home events
ssr miloco bridge        # stream home activities onto the SSR bus (foreground)
```

The `miloco_*` tools (`miloco_devices`, `miloco_device_control`, `miloco_family`,
`miloco_activities`, `miloco_automations`, `miloco_sync`, …) are contributed by the
bundled **`miloco` plugin** (`ssr plugin disable miloco` to turn them off). Home
**activities** become `miloco.activity.<type>` bus events (so a handler — of any of
the 4 kinds — can react to a person arriving, a sensor tripping, a hazard being
detected), and a synced **snapshot** of devices / family members / events /
automations is surfaced as persistent context.

> Miloco runs natively on **macOS / Linux only**. On **Windows it must run in
> Docker** — which is also why the SSR gateway defaults to a Docker backend on
> Windows. SSR raises a clear "use Docker" message rather than failing silently.

### HTTP/SSE MCP

The agent supports two transport types for external Model Context Protocol (MCP) servers:
- **Stdio Transport**: Spawns a local subprocess and communicates via standard input/output (`command` and `args` in the configuration).
- **HTTP+SSE Transport**: Connects to a remote MCP server using Server-Sent Events (SSE) for receiving messages and HTTP POST for sending requests (`url` key in the configuration).

#### Configuration Example

In `~/.ssr/mcp.json` or plugin configurations, use `url` instead of `command` to declare an SSE server:

```json
{
  "mcpServers": {
    "my-remote-server": {
      "url": "https://mcp.example.com/mcp",
      "headers": {
        "Authorization": "Bearer my-secret-token"
      },
      "query_params": {
        "version": "1.0"
      }
    }
  }
}
```

### Session recording

Every conversation turn — REPL, one-shot, or channel agent run — is
recorded as an append-only `~/.ssr/sessions/<id>.jsonl` transcript. List them in
the TUI with `/sessions`; `/clear` starts a fresh session.

### srdb — agent debugger (`ssr srdb`)

Every running main-agent process opens a small **srdb** debug server (TCP,
newline-delimited JSON-RPC 2.0, key-authenticated, bound to `127.0.0.1`). Each
live agent prints a debug link to **stderr** on startup (and exposes it as
`agent.srdb_link`):

```
[srdb] debug this agent: tcp://127.0.0.1:52017?key=…&agent=agent:0860fea3
```

Connect to it to inspect and steer the live process — across **all** agents and
sub-agents running in it:

```bash
ssr srdb agents  tcp://127.0.0.1:52017?key=…      # list running agents
ssr srdb call    tcp://…  srdb.sessions            # sessions / subagents / channels / bus
ssr srdb call    tcp://…  srdb.session.edit '{"id":"…","title":"x","turns":[…]}'
ssr srdb call    tcp://…  srdb.bus.emit '{"topic":"demo.ping","payload":{}}'
ssr srdb send    tcp://…  feishu <chat_id> "hi"    # message a channel directly
ssr srdb eval    tcp://…  'agent.run("status report")'   # run Python in the runtime
ssr srdb watch   tcp://…  [agent|all]              # stream live activity in real time
```

`ssr srdb watch` tails an agent's **real-time** activity — `turn_start`, the
final `reply`, and every `thinking` / `tool_call` / `tool_result` / `sub_agent` /
`bus_event` in between — so you can see exactly what a main agent (and its
sub-agents, tagged `sub`) is doing right now, even in a headless gateway. Events
are pushed live from the moment you subscribe; trigger the agent (send it a
message) to see a turn flow through.

Methods: `srdb.agents` / `srdb.agent`, `srdb.sessions` / `srdb.session.get` /
`srdb.session.edit`, `srdb.subagents` / `srdb.subagent.edit`, `srdb.channels` /
`srdb.channel.send`, `srdb.bus` / `srdb.bus.history` / `srdb.bus.emit`,
`srdb.watch` / `srdb.unwatch` (live event stream), and `srdb.eval`. Because `srdb.eval` runs arbitrary in-process Python, the key is a
password — keep the server on loopback. Disable with `SSR_SRDB=0`; override with
`SSR_SRDB_HOST` / `SSR_SRDB_PORT` / `SSR_SRDB_KEY`.

### Slash commands (inside the TUI)
`/help` `/index [category]` `/status` `/context <mode> <query>`
`/attach <path> [prompt]` (image/audio input; aliases `/image` `/audio`)
`/skills` `/memory` `/plan` `/bus <send|listen|wait|ls|history|connect|status>`
`/clear` `/quit`

### Multimodal input (image + voice)
Gemini is multimodal, so SSR accepts images and audio:
- **TUI**: `/attach ./diagram.png explain this chart` or `/audio ./note.ogg`
- **ACP**: `session/prompt` accepts `image` / `audio` content blocks
  (advertised via `promptCapabilities`).
- **Feishu/Lark**: image and voice messages are downloaded and passed to the model.

## Design

- **Model**: Gemini `gemini-3.1-flash-lite` (override with `DEFAULT_MODEL`).
- **Framework**: Google ADK `LlmAgent` + tools; a `google-genai`
  automatic-function-calling loop is used as a runtime fallback.
- See [`CLAUDE.md`](./CLAUDE.md) for the module map.

## Development

```bash
pip install -e ".[dev]"
python -m pytest
```
