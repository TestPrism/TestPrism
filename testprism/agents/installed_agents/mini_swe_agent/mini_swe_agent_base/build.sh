#!/usr/bin/env bash
# Build a reusable mini-swe-agent runtime image for TestPrism task images.
set -euo pipefail
cd "$(dirname "$0")"

IMAGE="${1:-testprism-mini-swe-agent-base:pypi-2.4.6-py311-v1}"
MINI_SWE_AGENT_VERSION="${2:-2.4.6}"
RUNTIME_KEY="${3:-pypi-${MINI_SWE_AGENT_VERSION}-py311-v1}"

docker build \
  --build-arg "MINI_SWE_AGENT_VERSION=${MINI_SWE_AGENT_VERSION}" \
  --build-arg "RUNTIME_KEY=${RUNTIME_KEY}" \
  -t "$IMAGE" \
  .

echo "Built $IMAGE"
docker run --rm "$IMAGE" /opt/mini-swe-agent-venv/bin/mini --help >/dev/null
docker run --rm "$IMAGE" test -f /opt/testprism-mini-swe-agent-runtime.json
