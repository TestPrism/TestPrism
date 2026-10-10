FROM node:22-bookworm

ARG CODEX_VERSION=latest
ARG RUNTIME_KEY=latest-node22-v1

RUN set -eux; \
    npm install --global "@openai/codex@${CODEX_VERSION}"; \
    codex --version; \
    mkdir -p /opt; \
    RUNTIME_KEY="$RUNTIME_KEY" CODEX_VERSION="$CODEX_VERSION" \
      node -e 'console.log(JSON.stringify({runtime_key: process.env.RUNTIME_KEY, version: process.env.CODEX_VERSION, node: process.version}))' \
      > /opt/testprism-codex-runtime.json; \
    npm cache clean --force
