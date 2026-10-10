FROM node:22-bookworm

ARG CLAUDE_CODE_VERSION=latest
ARG RUNTIME_KEY=latest-node22-v1

RUN set -eux; \
    npm install --global "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"; \
    claude --version; \
    mkdir -p /opt; \
    RUNTIME_KEY="$RUNTIME_KEY" CLAUDE_CODE_VERSION="$CLAUDE_CODE_VERSION" \
      node -e 'console.log(JSON.stringify({runtime_key: process.env.RUNTIME_KEY, version: process.env.CLAUDE_CODE_VERSION, node: process.version}))' \
      > /opt/testprism-claude-code-runtime.json; \
    npm cache clean --force
