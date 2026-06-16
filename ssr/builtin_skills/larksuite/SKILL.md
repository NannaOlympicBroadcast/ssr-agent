---
name: larksuite
description: Drive Feishu / Lark from the command line via the official larksuite CLI (https://github.com/larksuite/cli) — manage bots, messages, docs, bitable and approvals.
---

# Larksuite (Feishu / Lark) CLI Skill

Use the official Lark CLI to interact with Feishu/Lark from the terminal.

## Install
```bash
npm install -g @larksuiteoapi/lark-cli
# or
go install github.com/larksuite/cli/cmd/lark@latest
```

## Authenticate
Configure your app credentials (App ID = ak, App Secret = sk):
```bash
lark config set app_id <APP_ID>
lark config set app_secret <APP_SECRET>
```
SSR stores these in `~/.ssr/feishu.json` (run `ssr feishu configure`).

## Common operations
- Send a message to a chat:
  ```bash
  lark im message create --receive-id <chat_id> --msg-type text --content '{"text":"hi"}'
  ```
- List chats the bot is in:
  ```bash
  lark im chat list
  ```
- Read a Docs / Bitable record, manage approvals, upload files — see `lark --help`.

## When to use
Use this skill whenever the task involves Feishu/Lark: notifying a channel,
posting build results, reading a bitable, or triggering an approval. Pair it with
`ssr feishu configure` so the agent and CLI share one set of credentials.
