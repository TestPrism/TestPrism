#!/usr/bin/env bash
# Overlay agentscript base onto an existing image (e.g. prepull or tb__ task client).
#
# Usage:
#   ./build.sh
#   ./layer-on-image.sh <source-image> <target-tag>
#
# Example:
#   ./layer-on-image.sh \
#     testprism-prepull-from-quixbugs-quixbugs-java-hanoi:latest \
#     tb__quixbugs-java-hanoi__client:latest
set -euo pipefail
cd "$(dirname "$0")"

BASE_IMAGE="${TESTPRISM_AGENTSCRIPT_BASE:-testprism-agentscript-base:latest}"
SOURCE="${1:?source image required}"
TARGET="${2:?target tag required}"

if ! docker image inspect "$BASE_IMAGE" >/dev/null 2>&1; then
  echo "Missing $BASE_IMAGE — run ./build.sh first" >&2
  exit 1
fi

BUILD_CONTEXT="$(mktemp -d)"
trap 'rm -rf "$BUILD_CONTEXT"' EXIT

cat >"$BUILD_CONTEXT/Dockerfile" <<EOF
FROM ${SOURCE}
COPY --from=${BASE_IMAGE} /usr/local/ /usr/local/
COPY --from=${BASE_IMAGE} /root/.agentscript/ /root/.agentscript/
ENV PATH="/usr/local/bin:\${PATH}"
EOF

docker build -t "$TARGET" "$BUILD_CONTEXT"

echo "Created $TARGET (agentscript layer from $BASE_IMAGE)"
