#!/usr/bin/env bash
# SSR Agent installer.
# - installs the `ssr` package (editable)
# - scaffolds ~/.ssr (.env, mcp.json, indexes/) and the built-in skills
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Installing ssr-agent (Python)…"
if command -v uv >/dev/null 2>&1; then
  uv pip install -e "$here"
else
  python3 -m pip install -e "$here"
fi

echo "==> Initialising ~/.ssr…"
ssr init || python3 -m ssr init

cat <<'EOF'

✓ SSR Agent installed.

Next steps:
  1. Edit ~/.ssr/.env and set GEMINI_API_KEY, TAVILY_API_KEY, DEFAULT_MODEL
  2. Run `ssr` to launch the TUI
  3. Optional: `ssr feishu configure` to wire up a Feishu/Lark bot
  4. Optional: `npm i -g pm2` for background agent tasks

Built-in skills installed to ~/.ssr/skills: larksuite, agent-browser
EOF
