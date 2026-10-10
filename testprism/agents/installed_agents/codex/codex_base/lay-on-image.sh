#!/usr/bin/env bash
# Overlay a prebuilt Codex runtime onto an existing task client image.
set -euo pipefail
cd "$(dirname "$0")"

BASE_IMAGE="${TESTPRISM_CODEX_BASE:-testprism-codex-base:latest-node22-v1}"
SOURCE="${1:?source image required}"
TARGET="${2:?target tag required}"
RUNTIME_KEY="${3:-}"

if ! docker image inspect "$BASE_IMAGE" >/dev/null 2>&1; then
  echo "Missing $BASE_IMAGE; run ./build.sh first" >&2
  exit 1
fi

if [ -n "$RUNTIME_KEY" ] && docker run --rm "$SOURCE" sh -c \
  "test -f /opt/testprism-codex-runtime.json && \
   grep -F '\"runtime_key\":\"${RUNTIME_KEY}\"' /opt/testprism-codex-runtime.json >/dev/null && \
   command -v codex >/dev/null" >/dev/null 2>&1; then
  echo "$SOURCE already has Codex runtime ${RUNTIME_KEY}"
  exit 0
fi

BUILD_CONTEXT="$(mktemp -d)"
trap 'rm -rf "$BUILD_CONTEXT"' EXIT

tee "$BUILD_CONTEXT/Dockerfile" >/dev/null <<EOF
FROM ${SOURCE}
COPY --from=${BASE_IMAGE} /usr/local/ /usr/local/
COPY --from=${BASE_IMAGE} /opt/testprism-codex-runtime.json /opt/testprism-codex-runtime.json
ENV PATH="/usr/local/bin:\${PATH}"
RUN codex --version && test -f /opt/testprism-codex-runtime.json
EOF

docker build -t "$TARGET" "$BUILD_CONTEXT"
echo "Created $TARGET (Codex runtime layer from $BASE_IMAGE)"
