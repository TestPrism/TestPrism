#!/usr/bin/env bash
set -u

python - <<'PY'
from app import add, identity

checks = {
    "fix_positive": add(2, 3) == 5,
    "fix_negative": add(-2, 1) == -1,
    "stable_identity": identity(7) == 7,
}
for test_id, passed in checks.items():
    print(f"RESULT {test_id} {'passed' if passed else 'failed'}")
raise SystemExit(0 if all(checks.values()) else 1)
PY
