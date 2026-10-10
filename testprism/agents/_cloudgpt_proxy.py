"""Host-side LiteLLM proxy wrapper for Claude Code against CloudGPT.

Claude Code only speaks the Anthropic Messages API. CloudGPT is Azure OpenAI.
LiteLLM's proxy server exposes `/v1/messages` (Anthropic-shaped) and can route
to any litellm backend, including `azure/<deployment>`. We spawn it as a
subprocess bound to a free localhost port, hand its URL to Claude Code via
ANTHROPIC_BASE_URL, and tear it down when the agent is done.

The proxy runs on the host (not in the task container). The container reaches
it via host.docker.internal. That hostname is already routable in our
`AbstractInstalledAgent` setup since other agents (e.g. agentscript) rely on
it for proxy URLs.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

from testprism.agents._cloudgpt import (
    CLOUDGPT_ENDPOINT,
    get_cloudgpt_api_version,
    get_cloudgpt_token,
)
from testprism.utils.logger import logger


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_ready(port: int, timeout_sec: float = 30.0) -> None:
    url = f"http://127.0.0.1:{port}/health/liveness"
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            pass
        time.sleep(0.3)
    raise RuntimeError(
        f"LiteLLM proxy at :{port} did not come up within {timeout_sec}s"
    )


class CloudGPTAnthropicProxy:
    """Spawn a litellm proxy that translates Anthropic Messages → CloudGPT."""

    def __init__(self, deployment: str, model_alias: str | None = None):
        self._deployment = deployment
        # The alias is what Claude Code sends in the `model` field; litellm
        # maps it back to `azure/<deployment>` via our config.
        self._model_alias = model_alias or f"claude-via-{deployment}"
        self._port = _pick_free_port()
        self._proc: subprocess.Popen | None = None
        self._config_path: Path | None = None

    @property
    def port(self) -> int:
        return self._port

    @property
    def model_alias(self) -> str:
        return self._model_alias

    @property
    def base_url_for_container(self) -> str:
        # host.docker.internal resolves from inside the task container on
        # docker-for-linux when the --add-host flag is set (standard in our
        # docker compose setup). On platforms where it's not resolvable, the
        # user can override via CLOUDGPT_PROXY_HOST.
        host = os.environ.get("CLOUDGPT_PROXY_HOST", "host.docker.internal")
        return f"http://{host}:{self._port}"

    def _write_config(self, token: str) -> Path:
        config = {
            "model_list": [
                {
                    "model_name": self._model_alias,
                    "litellm_params": {
                        "model": f"azure/{self._deployment}",
                        "api_base": CLOUDGPT_ENDPOINT,
                        "api_version": get_cloudgpt_api_version(),
                        "api_key": token,
                    },
                }
            ],
            "general_settings": {
                # Accept any bearer from Claude Code — we don't enforce auth
                # on a loopback proxy spawned per task.
                "master_key": "sk-testprism-cloudgpt",
            },
        }
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix="-cloudgpt-proxy.yaml", delete=False
        )
        yaml.safe_dump(config, tmp)
        tmp.close()
        return Path(tmp.name)

    def start(self) -> None:
        if self._proc is not None:
            return
        token = get_cloudgpt_token()
        self._config_path = self._write_config(token)
        cmd = [
            sys.executable,
            "-m",
            "litellm",
            "--config",
            str(self._config_path),
            "--port",
            str(self._port),
            "--host",
            "0.0.0.0",
        ]
        log_path = Path(tempfile.gettempdir()) / f"cloudgpt-proxy-{self._port}.log"
        self._log_fh = open(log_path, "w")
        logger.info(
            f"Starting CloudGPT anthropic proxy on :{self._port} "
            f"(deployment={self._deployment}, log={log_path})"
        )
        self._proc = subprocess.Popen(
            cmd,
            stdout=self._log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        atexit.register(self.stop)
        try:
            _wait_ready(self._port)
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        if self._proc is None:
            return
        proc = self._proc
        self._proc = None
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        if getattr(self, "_log_fh", None):
            self._log_fh.close()
        if self._config_path and self._config_path.exists():
            with contextlib.suppress(OSError):
                self._config_path.unlink()

    def __enter__(self) -> "CloudGPTAnthropicProxy":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()
