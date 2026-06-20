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

1. **Polished TUI** — `ssr` prints an ASCII-art *Welcome To SSR* banner, then drops into an interactive REPL.
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
5b. **Remote control** — `ssr rc` connects this instance to an
   [SSR Dispatch Server](https://github.com/NannaOlympicBroadcast/ssr-dispatch-server)
   as a *node*. From the dispatch web UI you can browse this machine's files,
   open a live terminal, and transfer files both ways; an MCP-over-SSE endpoint
   lets any AI dispatch agents to your nodes by name or tag.
6. **Built-in skills** installed to `~/.ssr/skills`:
   [larksuite](https://github.com/larksuite/cli) and
   [agent-browser](https://github.com/vercel-labs/agent-browser).

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

# Unified Channels (Feishu / WeChat / etc.) - Note: `ssr feishu` is deprecated
ssr channel config feishu  # configure Feishu channel (alias: configure)
ssr channel on feishu      # start listening on Feishu channel (alias: serve)
ssr channel config wechat  # configure WeChat channel (scanning QR code to log in; network-resilient status checks handling wait, scanned, expired, canceled, timeout, and customized error message responses; press Ctrl+C to cancel)
ssr channel on wechat      # start listening on WeChat channel (with dynamic X-WECHAT-UIN headers, full base_info / client_id payload alignment, incoming image message decryption support, auto-exit on session timeout, robust media/file send support, and verbose logging for API calls)

# Multi-Model configurations
ssr models config        # interactively configure LLM models (Gemini, Anthropic, OpenAI)

# OpenAI-compatible API server
ssr serve --port 8000    # start a local OpenAI-compatible HTTP server

# Remote control (connect to a dispatch server as a node)
ssr rc                   # first run prompts for endpoint + token, then connects
ssr rc status            # show the saved remote-control config
ssr rc tags prod,gpu     # set this node's tags (applied on next connect)

# Run EvalScope benchmark with custom runner
python run_benchmark.py
```

### Multi-Model Configuration (`ssr models config`)

Configure multiple LLM models (Gemini, Anthropic, and OpenAI format) interactively. Configured models are saved to `~/.ssr/models.json` and are used for agent execution with automatic retry and model fallback support.

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

### Remote control (`ssr rc`)

Run `ssr rc` to register this machine as a **node** on a dispatch server. The
node dials out over a single WebSocket (no inbound port needed) and serves:

- **Filesystem** browsing and **file/image transfer** both ways.
- An interactive **terminal** (a real PTY) driven from the dispatch web UI.
- One-shot shell commands and full **`agent.run`** tasks dispatched over MCP.

First run is interactive (dispatch endpoint, API token, node name, tags); the
config is saved to `~/.ssr/remote.json`. See the
[ssr-dispatch-server](https://github.com/NannaOlympicBroadcast/ssr-dispatch-server)
project for the server, web UI, and the `<baseurl>/mcp?key=<token>` endpoint.

From the dispatch web UI you can **chat** with the agent on a node (with file &
image attachments and a live preview of the agent's thinking and tool calls),
and browse each node's **past sessions**.

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
- The agent's generated response is pushed back to the corresponding channel (such as Feishu, WeChat, or Remote Control/RC client) from which the command was initiated.

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

Automatically discovers and loads Claude-Code/Codex-style plugins (which have `.claude-plugin/plugin.json` or `.codex-plugin/plugin.json` manifests) from global `~/.ssr/plugins` and project-level `.ssr/plugins` directories.

*   **Skill Integration**: Loaded manifests are registered into the skill context pool under the `ContextCategory.SKILLS` category with metadata `{"kind": "plugin"}`.
*   **Dynamic MCP Registration**: Plugins can define MCP servers either inline in `plugin.json` (under the `mcpServers` key) or in a separate `.mcp.json` or `mcp.json` file in the plugin's root directory. The agent automatically loads these configurations when initialized.
*   **Path Resolution**: To support portable installations, path placeholders like `${__dirname}`, `__dirname`, and `${CLAUDE_PLUGIN_ROOT}` in the plugin's MCP server configuration are resolved to the absolute path of the plugin root directory at runtime.

### Built-in Plugins

*   **`miot`**: A built-in Xiaomi Home (MIoT) device control plugin. It is automatically installed into `~/.ssr/plugins/miot` during initialization. It provides comprehensive tools for discovering, querying, and controlling your Xiaomi smart devices (like lights, outlets, sensors, etc.).

### HTTP/SSE MCP and Remote Dispatch

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

#### Auto-registered Dispatch Server

If you have configured remote control using `ssr rc` (which stores credentials in `~/.ssr/remote.json`), the agent automatically detects this configuration and registers a default SSE MCP server named `"dispatch"`. Once registered, all tools exposed by the dispatch server (e.g. `list_nodes`, `run_command`, `run_agent`) become immediately available to the agent as `mcp__dispatch__list_nodes`, `mcp__dispatch__run_command`, etc.

### Session recording


Every conversation turn — REPL, one-shot, dispatched agent run, or web chat — is
recorded as an append-only `~/.ssr/sessions/<id>.jsonl` transcript. List them in
the TUI with `/sessions`; `/clear` starts a fresh session.

### Slash commands (inside the TUI)
`/help` `/index [category]` `/status` `/context <mode> <query>`
`/attach <path> [prompt]` (image/audio input; aliases `/image` `/audio`)
`/skills` `/memory` `/plan` `/clear` `/quit`

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
