#!/usr/bin/env bash
set -euo pipefail
export TESTHELIX_CONFIG=${TESTHELIX_CONFIG:-configs/testprism.quickstart.json}
exec bash "$(dirname "$0")/run_model_diagnostic.sh" "$@"
