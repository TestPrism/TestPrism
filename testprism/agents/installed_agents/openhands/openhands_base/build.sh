#!/usr/bin/env bash
# Build a reusable OpenHands runtime image for TestPrism task images.
set -euo pipefail
cd "$(dirname "$0")"

IMAGE="${1:-testprism-openhands-base:pypi-0.51.1-orjson-v1}"
OPENHANDS_VERSION="${2:-0.51.1}"
OPENHANDS_GIT_VERSION="${3:-}"
RUNTIME_KEY="${4:-pypi-${OPENHANDS_VERSION}-orjson-v1}"

docker build \
  --build-arg "OPENHANDS_VERSION=${OPENHANDS_VERSION}" \
  --build-arg "OPENHANDS_GIT_VERSION=${OPENHANDS_GIT_VERSION}" \
  --build-arg "RUNTIME_KEY=${RUNTIME_KEY}" \
  -t "$IMAGE" \
  .

echo "Built $IMAGE"
docker run --rm "$IMAGE" /opt/openhands-venv/bin/python -c "import openhands, orjson"
docker run --rm "$IMAGE" test -f /opt/testprism-openhands-runtime.json
