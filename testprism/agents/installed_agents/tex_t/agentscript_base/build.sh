#!/usr/bin/env bash
# Build testprism-agentscript-base:latest (Node + ~/.agentscript/node_modules).
set -euo pipefail
cd "$(dirname "$0")"
IMAGE="${1:-testprism-agentscript-base:latest}"
docker build -t "$IMAGE" .
echo "Built $IMAGE"
docker run --rm "$IMAGE" node -v
docker run --rm "$IMAGE" test -d /root/.agentscript/node_modules && echo "node_modules OK"
