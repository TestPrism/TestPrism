#!/usr/bin/env bash
# Overlay the prebuilt OpenHands runtime onto an existing TestPrism client image.
set -euo pipefail
cd "$(dirname "$0")"

BASE_IMAGE="${TESTPRISM_OPENHANDS_BASE:-testprism-openhands-base:pypi-0.51.1-orjson-v1}"
SOURCE="${1:?source image required}"
TARGET="${2:?target tag required}"
RUNTIME_KEY="${3:-}"

if ! docker image inspect "$BASE_IMAGE" >/dev/null 2>&1; then
  echo "Missing $BASE_IMAGE; run ./build.sh first" >&2
  exit 1
fi

if [ -n "$RUNTIME_KEY" ] && docker run --rm "$SOURCE" sh -c \
  "test -f /opt/testprism-openhands-runtime.json && \
   grep -F '\"runtime_key\": \"${RUNTIME_KEY}\"' /opt/testprism-openhands-runtime.json >/dev/null && \
   /opt/openhands-venv/bin/python -c 'import openhands, orjson'" >/dev/null 2>&1; then
  echo "$SOURCE already has OpenHands runtime ${RUNTIME_KEY}"
  exit 0
fi

BUILD_CONTEXT="$(mktemp -d)"
trap 'rm -rf "$BUILD_CONTEXT"' EXIT

tee "$BUILD_CONTEXT/Dockerfile" >/dev/null <<EOF
FROM ${SOURCE}
COPY --from=${BASE_IMAGE} /usr/local/ /usr/local/
COPY --from=${BASE_IMAGE} /opt/openhands-venv/ /opt/openhands-venv/
COPY --from=${BASE_IMAGE} /opt/testprism-openhands-runtime.json /opt/testprism-openhands-runtime.json
ENV PATH="/opt/openhands-venv/bin:/usr/local/bin:\${PATH}"
RUN /opt/openhands-venv/bin/python -c "import openhands, orjson" && \\
    test -f /opt/testprism-openhands-runtime.json
EOF

docker build -t "$TARGET" "$BUILD_CONTEXT"

echo "Created $TARGET (OpenHands runtime layer from $BASE_IMAGE)"
