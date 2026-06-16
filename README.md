# SSR Agent ✨

A beautiful command-line **coding agent**, built on [Google ADK](https://adk.dev)
and Gemini. `claude.md`-compatible, with a four-category context pool and
model2vec vector retrieval at its core.

> 支持 snh48 宋昕冉 谢谢喵

```
ssr
┌────────────────────────── ✨ SSR Agent ✨ ──────────────────────────┐
│  ██╗    ██╗███████╗██╗      ██████╗ ██████╗ ███╗   ███╗███████╗     │
│  ...   Welcome To SSR   ...                                        │
│                  支持 snh48 宋昕冉 谢谢喵                             │
└────────────────────── powered by Google ADK · gemini ───────────────┘
```

## Features

1. **Polished TUI** — `ssr` prints an ASCII-art *Welcome To SSR* banner with the
   SNH48 subtitle, then drops into an interactive REPL.
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

# Feishu / Lark bot (WebSocket long connection — no webhook URL)
ssr feishu configure     # set working dir, session id, app id (ak), app secret (sk)
ssr feishu serve         # opens an outbound WS to Feishu and serves the agent
```

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
