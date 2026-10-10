# Universal agentscript runtime layer (Node 20 + npm deps).
# Task images copy from this tag; install-agent.sh still refreshes runner.mjs.
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV NPM_CONFIG_REGISTRY=https://registry.npmmirror.com
ENV PATH="/usr/local/bin:${PATH}"

ARG NODE_VERSION=20.20.2

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl xz-utils; \
    curl -fsSL "https://npmmirror.com/mirrors/node/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" \
      | tar -xJ -C /usr/local --strip-components=1; \
    npm config set registry https://registry.npmmirror.com; \
    node -v; \
    npm -v; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /root/.agentscript
COPY package.json ./
RUN npm install --network-timeout=120000 --no-audit --no-fund
