#!/usr/bin/env bash
# Overlay a prebuilt Claude Code runtime onto an existing task client image.
set -euo pipefail
cd "$(dirname "$0")"

BASE_IMAGE="${TESTPRISM_CLAUDE_CODE_BASE:-testprism-claude-code-base:latest-node22-v1}"
SOURCE="${1:?source image required}"
TARGET="${2:?target tag required}"
RUNTIME_KEY="${3:-}"

if ! docker image inspect "$BASE_IMAGE" >/dev/null 2>&1; then
  echo "Missing $BASE_IMAGE; run ./build.sh first" >&2
  exit 1
fi

if [ -n "$RUNTIME_KEY" ] && docker run --rm "$SOURCE" sh -c \
  "test -f /opt/testprism-claude-code-runtime.json && \
   grep -F '\"runtime_key\":\"${RUNTIME_KEY}\"' /opt/testprism-claude-code-runtime.json >/dev/null && \
   command -v claude >/dev/null" >/dev/null 2>&1; then
  echo "$SOURCE already has Claude Code runtime ${RUNTIME_KEY}"
  exit 0
fi

BUILD_CONTEXT="$(mktemp -d)"
trap 'rm -rf "$BUILD_CONTEXT"' EXIT

tee "$BUILD_CONTEXT/Dockerfile" >/dev/null <<EOF
FROM ${SOURCE}
COPY --from=${BASE_IMAGE} /usr/local/ /usr/local/
COPY --from=${BASE_IMAGE} /opt/testprism-claude-code-runtime.json /opt/testprism-claude-code-runtime.json
ENV PATH="/usr/local/bin:\${PATH}"
RUN claude --version && test -f /opt/testprism-claude-code-runtime.json
EOF

docker build -t "$TARGET" "$BUILD_CONTEXT"
echo "Created $TARGET (Claude Code runtime layer from $BASE_IMAGE)"
