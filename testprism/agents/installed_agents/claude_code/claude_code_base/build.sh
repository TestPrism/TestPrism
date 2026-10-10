#!/usr/bin/env bash
# Build a reusable Claude Code runtime image. Run this once on a networked host,
# then task containers can receive the CLI without contacting external services.
set -euo pipefail
cd "$(dirname "$0")"

IMAGE="${1:-testprism-claude-code-base:latest-node22-v1}"
CLAUDE_CODE_VERSION="${2:-latest}"
RUNTIME_KEY="${3:-${CLAUDE_CODE_VERSION}-node22-v1}"

docker build \
  --build-arg "CLAUDE_CODE_VERSION=${CLAUDE_CODE_VERSION}" \
  --build-arg "RUNTIME_KEY=${RUNTIME_KEY}" \
  -t "$IMAGE" \
  .

docker run --rm "$IMAGE" sh -c 'claude --version && test -f /opt/testprism-claude-code-runtime.json'
echo "Built $IMAGE"
