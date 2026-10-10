FROM python:3.13-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple/
ENV UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple/
ENV SKIP_VSCODE_BUILD=true
ENV OPENHANDS_VENV=/opt/openhands-venv
ENV PATH="/opt/openhands-venv/bin:/usr/local/bin:${PATH}"

ARG OPENHANDS_VERSION=0.51.1
ARG OPENHANDS_GIT_VERSION=
ARG RUNTIME_KEY=pypi-0.51.1-orjson-v1

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates curl git build-essential; \
    python -m pip install --no-cache-dir uv; \
    uv venv "$OPENHANDS_VENV" --python /usr/local/bin/python3.13; \
    if [ -n "$OPENHANDS_GIT_VERSION" ]; then \
      uv pip install --python "$OPENHANDS_VENV/bin/python" \
        "git+https://github.com/All-Hands-AI/OpenHands.git@${OPENHANDS_GIT_VERSION}"; \
    else \
      uv pip install --python "$OPENHANDS_VENV/bin/python" \
        "openhands-ai==${OPENHANDS_VERSION}"; \
    fi; \
    uv pip install --python "$OPENHANDS_VENV/bin/python" deprecated orjson; \
    "$OPENHANDS_VENV/bin/python" -c "import openhands, orjson"; \
    mkdir -p /opt; \
    RUNTIME_KEY="$RUNTIME_KEY" OPENHANDS_VERSION="$OPENHANDS_VERSION" OPENHANDS_GIT_VERSION="$OPENHANDS_GIT_VERSION" \
      python -c 'import json, os; print(json.dumps({"runtime_key": os.environ.get("RUNTIME_KEY", ""), "version": os.environ.get("OPENHANDS_VERSION", ""), "git_version": os.environ.get("OPENHANDS_GIT_VERSION", ""), "python": "3.13"}, sort_keys=True))' \
      > /opt/testprism-openhands-runtime.json; \
    rm -rf /root/.cache /var/lib/apt/lists/*
