#!/usr/bin/env bash
# Overlay a generic prebuilt installed-agent runtime onto a task client image.
set -euo pipefail
cd "$(dirname "$0")"

BASE_IMAGE="${1:?base image required}"
SOURCE="${2:?source image required}"
TARGET="${3:?target tag required}"
RUNTIME_KEY="${4:?runtime key required}"
AGENT_NAME="${5:?agent name required}"

case "$AGENT_NAME" in
  aider) AGENT_BIN=aider ;;
  cursor-cli) AGENT_BIN=cursor-agent ;;
  gemini-cli) AGENT_BIN=gemini ;;
  goose) AGENT_BIN=goose ;;
  grok-cli) AGENT_BIN=grok ;;
  kimi-code) AGENT_BIN=kimi ;;
  opencode) AGENT_BIN=opencode ;;
  qwen-code) AGENT_BIN=qwen ;;
  *) echo "Unsupported generic layer-on agent: $AGENT_NAME" >&2; exit 2 ;;
esac

if ! docker image inspect "$BASE_IMAGE" >/dev/null 2>&1; then
  echo "Missing $BASE_IMAGE; build it first" >&2
  exit 1
fi

RUNTIME_ROOT=/opt/testprism-agent-runtime
RUNTIME_PATH="$RUNTIME_ROOT/bin:$RUNTIME_ROOT/home/.local/bin"
if docker run --rm "$SOURCE" sh -c \
  "test -f '$RUNTIME_ROOT/runtime.json' && \
   grep -F '\"runtime_key\":\"${RUNTIME_KEY}\"' '$RUNTIME_ROOT/runtime.json' >/dev/null && \
   PATH='$RUNTIME_PATH':\"\$PATH\" command -v '$AGENT_BIN' >/dev/null" \
  >/dev/null 2>&1; then
  echo "$SOURCE already has $AGENT_NAME runtime $RUNTIME_KEY"
  exit 0
fi

BUILD_CONTEXT="$(mktemp -d)"
trap 'rm -rf "$BUILD_CONTEXT"' EXIT

tee "$BUILD_CONTEXT/Dockerfile" >/dev/null <<EOF
FROM ${SOURCE}
COPY --from=${BASE_IMAGE} /opt/testprism-agent-runtime/ /opt/testprism-agent-runtime/
ENV PATH="/opt/testprism-agent-runtime/bin:/opt/testprism-agent-runtime/home/.local/bin:\${PATH}"
ENV LD_LIBRARY_PATH="/opt/testprism-agent-runtime/lib"
RUN command -v ${AGENT_BIN} >/dev/null && \\
    ${AGENT_BIN} --version >/dev/null && \\
    grep -F '"runtime_key":"${RUNTIME_KEY}"' /opt/testprism-agent-runtime/runtime.json >/dev/null
EOF

docker build -t "$TARGET" "$BUILD_CONTEXT"
echo "Created $TARGET ($AGENT_NAME runtime layer from $BASE_IMAGE)"
