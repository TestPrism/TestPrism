#!/usr/bin/env bash
set -eu

cat > /app/app.py <<'PY'
def add(a, b):
    return a + b


def identity(value):
    return value
PY
