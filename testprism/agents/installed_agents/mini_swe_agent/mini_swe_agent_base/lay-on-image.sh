#!/usr/bin/env bash
# Overlay the prebuilt mini-swe-agent runtime onto an existing TestPrism client image.
set -euo pipefail
cd "$(dirname "$0")"

BASE_IMAGE="${TESTPRISM_MINI_SWE_AGENT_BASE:-testprism-mini-swe-agent-base:pypi-2.4.6-py311-v1}"
SOURCE="${1:?source image required}"
TARGET="${2:?target tag required}"
RUNTIME_KEY="${3:-}"

if ! docker image inspect "$BASE_IMAGE" >/dev/null 2>&1; then
  echo "Missing $BASE_IMAGE; run ./build.sh first" >&2
  exit 1
fi

if [ -n "$RUNTIME_KEY" ] && docker run --rm "$SOURCE" sh -c \
  "test -f /opt/testprism-mini-swe-agent-runtime.json && \
   grep -F '\"runtime_key\": \"${RUNTIME_KEY}\"' /opt/testprism-mini-swe-agent-runtime.json >/dev/null && \
   /opt/mini-swe-agent-venv/bin/mini --help >/dev/null 2>&1"; then
  echo "$SOURCE already has mini-swe-agent runtime ${RUNTIME_KEY}"
  exit 0
fi

BUILD_CONTEXT="$(mktemp -d)"
trap 'rm -rf "$BUILD_CONTEXT"' EXIT

tee "$BUILD_CONTEXT/Dockerfile" >/dev/null <<EOF
FROM ${SOURCE}
COPY --from=${BASE_IMAGE} /usr/local/ /usr/local/
COPY --from=${BASE_IMAGE} /opt/mini-swe-agent-venv/ /opt/mini-swe-agent-venv/
COPY --from=${BASE_IMAGE} /opt/testprism-mini-swe-agent-runtime.json /opt/testprism-mini-swe-agent-runtime.json
ENV PATH="/opt/mini-swe-agent-venv/bin:/usr/local/bin:\${PATH}"
RUN /opt/mini-swe-agent-venv/bin/mini --help >/dev/null && \\
    test -f /opt/testprism-mini-swe-agent-runtime.json
EOF

docker build -t "$TARGET" "$BUILD_CONTEXT"

echo "Created $TARGET (mini-swe-agent runtime layer from $BASE_IMAGE)"
