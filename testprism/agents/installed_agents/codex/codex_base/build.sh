#!/usr/bin/env bash
# Build a reusable Codex runtime image. Run this once on a networked host,
# then task containers can receive the CLI without contacting npm at startup.
set -euo pipefail
cd "$(dirname "$0")"

IMAGE="${1:-testprism-codex-base:latest-node22-v1}"
CODEX_VERSION="${2:-latest}"
RUNTIME_KEY="${3:-${CODEX_VERSION}-node22-v1}"

docker build \
  --build-arg "CODEX_VERSION=${CODEX_VERSION}" \
  --build-arg "RUNTIME_KEY=${RUNTIME_KEY}" \
  -t "$IMAGE" \
  .

docker run --rm "$IMAGE" sh -c 'codex --version && test -f /opt/testprism-codex-runtime.json'
echo "Built $IMAGE"
