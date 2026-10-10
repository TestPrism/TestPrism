FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV MINI_SWE_VENV=/opt/mini-swe-agent-venv
ENV PATH="/opt/mini-swe-agent-venv/bin:/usr/local/bin:${PATH}"

ARG MINI_SWE_AGENT_VERSION=2.4.6
ARG RUNTIME_KEY=pypi-2.4.6-py311-v1

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends build-essential ca-certificates git; \
    python -m pip install --no-cache-dir --upgrade pip; \
    python -m venv "$MINI_SWE_VENV"; \
    "$MINI_SWE_VENV/bin/pip" install --no-cache-dir "mini-swe-agent==${MINI_SWE_AGENT_VERSION}"; \
    "$MINI_SWE_VENV/bin/mini" --help >/dev/null; \
    mkdir -p /opt; \
    RUNTIME_KEY="$RUNTIME_KEY" MINI_SWE_AGENT_VERSION="$MINI_SWE_AGENT_VERSION" \
      "$MINI_SWE_VENV/bin/python" -c 'import importlib.metadata, json, os; print(json.dumps({"python": "3.11", "resolved_version": importlib.metadata.version("mini-swe-agent"), "runtime_key": os.environ.get("RUNTIME_KEY", ""), "version_spec": os.environ.get("MINI_SWE_AGENT_VERSION", "2.4.6")}, sort_keys=True))' \
      > /opt/testprism-mini-swe-agent-runtime.json; \
    rm -rf /root/.cache /var/lib/apt/lists/*
