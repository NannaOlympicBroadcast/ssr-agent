#!/usr/bin/env bash
# SSR Agent installer.
# - installs the `ssr` package (editable)
# - scaffolds ~/.ssr (.env, mcp.json, indexes/) and the built-in skills
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> Installing ssr-agent (Python)…"
if command -v uv >/dev/null 2>&1; then
  uv pip install -e "$here" --system
else
  python3 -m pip install -e "$here"
fi

echo "==> Initialising ~/.ssr…"
ssr init || python3 -m ssr init

npm install -g chrome-devtools-mcp@latest

# --- Windows: ensure Chocolatey + nssm (the gateway's Windows service backend).
# nssm registers `ssr gateway run <name>` as a real auto-start Windows service.
# This is optional — without it the gateway falls back to a logon Scheduled Task —
# so every step degrades gracefully instead of aborting the installer.
install_windows_service_deps() {
  case "$(uname -s 2>/dev/null)" in
    MINGW*|MSYS*|CYGWIN*) ;;            # running under Git Bash / MSYS on Windows
    *) return 0 ;;                       # not Windows → nothing to do
  esac

  if command -v nssm >/dev/null 2>&1; then
    echo "==> nssm already installed; skipping."
    return 0
  fi

  echo "==> Installing nssm (Windows gateway service backend)…"
  if ! command -v choco >/dev/null 2>&1; then
    echo "    Chocolatey not found — installing it (needs an Administrator shell)…"
    powershell -NoProfile -ExecutionPolicy Bypass -Command \
      "Set-ExecutionPolicy Bypass -Scope Process -Force; \
       [System.Net.ServicePointManager]::SecurityProtocol = \
         [System.Net.ServicePointManager]::SecurityProtocol -bor 3072; \
       iex ((New-Object System.Net.WebClient).DownloadString('https://community.chocolatey.org/install.ps1'))" \
      || { echo "    [!] Chocolatey install failed (run this shell as Administrator). Install nssm manually: https://nssm.cc/"; return 0; }
    # choco puts itself in the machine PATH; expose it to this session.
    export PATH="$PATH:/c/ProgramData/chocolatey/bin"
  fi

  if command -v choco >/dev/null 2>&1; then
    choco install -y nssm \
      || echo "    [!] 'choco install nssm' failed (try an Administrator shell, or 'scoop install nssm')."
  else
    echo "    [!] Chocolatey still unavailable; install nssm manually from https://nssm.cc/"
  fi
}
install_windows_service_deps

cat <<'EOF'

✓ SSR Agent installed.

Next steps:
  1. Edit ~/.ssr/.env and set GEMINI_API_KEY, TAVILY_API_KEY, DEFAULT_MODEL
  2. Run `ssr` to launch the TUI
  3. Optional: `ssr feishu configure` to wire up a Feishu/Lark bot
  4. Optional: `npm i -g pm2` for background agent tasks
  5. Windows gateway: `choco install nssm` (or `scoop install nssm`) so
     `ssr gateway install` registers a real auto-start Windows service

Built-in skills installed to ~/.ssr/skills: larksuite, agent-browser
EOF
