# SSR Agent — container image.
#
# Builds the `ssr` CLI and runs it as a long-lived service (a messaging
# channel, a gateway, or the OpenAI-compatible HTTP API). All mutable state
# (config, tokens, sessions, indexes) lives under SSR_HOME=/data/.ssr, which is
# a volume so it survives container recreation.
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SSR_HOME=/data/.ssr

# Optional: set INSTALL_NODE=true to also install Node.js so the bundled
# chrome-devtools / miot MCP plugins (run via npx) are available in-container.
ARG INSTALL_NODE=false

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && if [ "$INSTALL_NODE" = "true" ]; then apt-get install -y --no-install-recommends nodejs npm; fi \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies first for better layer caching, then the package itself.
COPY pyproject.toml README.md ./
COPY ssr ./ssr
RUN pip install .

# Persisted SSR home (config / tokens / sessions / indexes).
VOLUME ["/data"]

COPY docker/entrypoint.sh /usr/local/bin/ssr-entrypoint
RUN chmod +x /usr/local/bin/ssr-entrypoint

# The API server is a self-contained default; docker-compose overrides `command`
# to run a channel or a gateway instead.
EXPOSE 8000
ENTRYPOINT ["ssr-entrypoint"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
