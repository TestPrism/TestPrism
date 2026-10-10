FROM node:22.19.0-bookworm

ARG AGENT_NAME
ARG AGENT_VERSION=latest
ARG RUNTIME_ID

ENV DEBIAN_FRONTEND=noninteractive
ENV AGENT_RUNTIME_ROOT=/opt/testprism-agent-runtime
ENV PATH="/opt/testprism-agent-runtime/bin:/opt/testprism-agent-runtime/home/.local/bin:${PATH}"
ENV LD_LIBRARY_PATH="/opt/testprism-agent-runtime/lib"
ENV NPM_CONFIG_PREFIX=/opt/testprism-agent-runtime

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      bzip2 ca-certificates curl libgomp1 libxcb1; \
    mkdir -p "$AGENT_RUNTIME_ROOT/bin" "$AGENT_RUNTIME_ROOT/home"; \
    case "$AGENT_NAME" in \
      aider) \
        UV_INSTALL_DIR="$AGENT_RUNTIME_ROOT/bin" \
          sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'; \
        export UV_TOOL_DIR="$AGENT_RUNTIME_ROOT/tools"; \
        export UV_TOOL_BIN_DIR="$AGENT_RUNTIME_ROOT/bin"; \
        export UV_PYTHON_INSTALL_DIR="$AGENT_RUNTIME_ROOT/python"; \
        uv python install 3.12; \
        if [ "$AGENT_VERSION" = latest ]; then \
          uv tool install --python 3.12 aider-chat; \
        else \
          uv tool install --python 3.12 "aider-chat==$AGENT_VERSION"; \
        fi; \
        ;; \
      cursor-cli) \
        HOME="$AGENT_RUNTIME_ROOT/home" \
          sh -c 'curl https://cursor.com/install -fsS | bash'; \
        ln -s "$AGENT_RUNTIME_ROOT/home/.local/bin/cursor-agent" \
          "$AGENT_RUNTIME_ROOT/bin/cursor-agent"; \
        ;; \
      gemini-cli) npm install --global "@google/gemini-cli@$AGENT_VERSION" ;; \
      goose) \
        HOME="$AGENT_RUNTIME_ROOT/home" CONFIGURE=false GOOSE_DISABLE_KEYRING=true \
          sh -c 'curl -fsSL https://github.com/block/goose/releases/download/stable/download_cli.sh | bash'; \
        ln -s "$AGENT_RUNTIME_ROOT/home/.local/bin/goose" \
          "$AGENT_RUNTIME_ROOT/bin/goose"; \
        ;; \
      grok-cli) npm install --global "@vibe-kit/grok-cli@$AGENT_VERSION" ;; \
      kimi-code) npm install --global "@moonshot-ai/kimi-code@$AGENT_VERSION" ;; \
      opencode) npm install --global "opencode-ai@$AGENT_VERSION" ;; \
      qwen-code) npm install --global "@qwen-code/qwen-code@$AGENT_VERSION" ;; \
      *) echo "Unsupported generic layer-on agent: $AGENT_NAME" >&2; exit 2 ;; \
    esac; \
    case "$AGENT_NAME" in \
      gemini-cli|grok-cli|kimi-code|opencode|qwen-code) \
        cp -L /usr/local/bin/node "$AGENT_RUNTIME_ROOT/bin/node"; \
        ;; \
    esac; \
    case "$AGENT_NAME" in \
      aider) AGENT_BIN=aider ;; \
      cursor-cli) AGENT_BIN=cursor-agent ;; \
      gemini-cli) AGENT_BIN=gemini ;; \
      goose) AGENT_BIN=goose ;; \
      grok-cli) AGENT_BIN=grok ;; \
      kimi-code) AGENT_BIN=kimi ;; \
      opencode) AGENT_BIN=opencode ;; \
      qwen-code) AGENT_BIN=qwen ;; \
    esac; \
    command -v "$AGENT_BIN"; \
    "$AGENT_BIN" --version; \
    mkdir -p "$AGENT_RUNTIME_ROOT/lib"; \
    if [ "$AGENT_NAME" = goose ] && ldd "$(command -v "$AGENT_BIN")" >/dev/null 2>&1; then \
      ldd "$(command -v "$AGENT_BIN")" \
        | awk '/ => \/[^ ]+/ {print $3}' \
        | grep -Ev '/(libc|libm|libpthread|libdl|librt|ld-linux)[.-]' \
        | xargs -r -I '{}' cp -L --no-clobber '{}' "$AGENT_RUNTIME_ROOT/lib/"; \
    fi; \
    AGENT_NAME="$AGENT_NAME" AGENT_VERSION="$AGENT_VERSION" RUNTIME_KEY="$RUNTIME_ID" \
      node -e 'const fs=require("fs"); fs.writeFileSync(process.env.AGENT_RUNTIME_ROOT + "/runtime.json", JSON.stringify({agent:process.env.AGENT_NAME,version:process.env.AGENT_VERSION,runtime_key:process.env.RUNTIME_KEY,node:process.version}))'; \
    npm cache clean --force; \
    rm -rf /root/.cache /var/lib/apt/lists/*
