#!/usr/bin/env sh
# SSR Agent container entrypoint.
#
# Ensures SSR_HOME exists and is scaffolded (idempotent), then execs `ssr` with
# whatever command was passed (defaults to the API server via the Dockerfile
# CMD). Channel credentials are expected to be provided via the mounted
# /data/.ssr volume or environment variables.
set -e

: "${SSR_HOME:=/data/.ssr}"
export SSR_HOME

mkdir -p "$SSR_HOME"

# First-run scaffolding (.env template, mcp.json, built-in skills/plugins).
# Safe to run on every boot; it only fills in what's missing.
ssr init >/dev/null 2>&1 || true

# No args → fall back to the API server bound to all interfaces.
if [ "$#" -eq 0 ]; then
    set -- serve --host 0.0.0.0 --port 8000
fi

exec ssr "$@"
