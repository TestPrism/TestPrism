#!/usr/bin/env bash
# Build one reusable runtime image for an installed agent.
set -euo pipefail
cd "$(dirname "$0")"

IMAGE="${1:?target base image required}"
AGENT_NAME="${2:?agent name required}"
AGENT_VERSION="${3:-latest}"
RUNTIME_KEY="${4:-${AGENT_VERSION}-runtime-v1}"

docker build \
  --build-arg "AGENT_NAME=${AGENT_NAME}" \
  --build-arg "AGENT_VERSION=${AGENT_VERSION}" \
  --build-arg "RUNTIME_ID=${RUNTIME_KEY}" \
  -t "$IMAGE" \
  .

docker run --rm "$IMAGE" test -f /opt/testprism-agent-runtime/runtime.json
echo "Built $IMAGE"
