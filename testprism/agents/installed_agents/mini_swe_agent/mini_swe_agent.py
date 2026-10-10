import json
import os
import shlex
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlparse

from testprism.agents._cloudgpt import (
    CLOUDGPT_ENDPOINT,
    ResolvedEndpoint,
    get_cloudgpt_api_version,
    is_cloudgpt_model,
    resolve_endpoint,
)
from testprism.agents.agent_name import AgentName
from testprism.agents.base_agent import SystemPromptMode
from testprism.agents.installed_agents.abstract_installed_agent import (
    AbstractInstalledAgent,
)
from testprism.terminal.docker_compose_manager import DockerComposeManager
from testprism.terminal.models import TerminalCommand
from testprism.terminal.tmux_session import TmuxSession
from testprism.utils.logger import logger
from testprism.utils.mini_swe_agent_runtime import (
    DEFAULT_MINI_SWE_AGENT_VERSION,
    mini_swe_agent_runtime_key,
    resolve_mini_swe_agent_version,
)


class MiniSweAgent(AbstractInstalledAgent):
    native_system_prompt_modes = frozenset({SystemPromptMode.REPLACE})

    MINI_BINARY_PATH = "/opt/mini-swe-agent-venv/bin/mini"
    TESTBENCH_CONFIG_PATH = Path(__file__).with_name("mini-testbench.yaml")
    CONTAINER_TESTBENCH_CONFIG_PATH = Path(
        "/installed-agent/mini-swe-testbench.yaml"
    )
    _LOCAL_HOST_MARKERS = (
        "localhost",
        "127.0.0.1",
        "0.0.0.0",
        "host.docker.internal",
        "gateway.docker.internal",
        "172.17.0.1",
    )
    _CONTAINER_REACHABLE_URL_ENV_NAMES = {
        "AZURE_API_BASE",
        "AZURE_OPENAI_ENDPOINT",
        "OPENAI_API_BASE",
        "OPENAI_BASE_URL",
    }

    @staticmethod
    def name() -> str:
        return AgentName.MINI_SWE_AGENT.value

    def __init__(
        self,
        model_name: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        api_version: str | None = None,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        if not self.version:
            self._version = DEFAULT_MINI_SWE_AGENT_VERSION
        self._config = self._load_config(kwargs.get("config"))
        self._model_name = model_name or (
            self._config.get("model") if self._config else None
        )
        if self._model_name is None:
            raise ValueError(
                "MiniSweAgent requires a model name via --model or agent config."
            )

        if "/" not in self._model_name:
            raise ValueError(
                "MiniSweAgent model name must be in provider/model format."
            )
        self._provider, _ = self._model_name.split("/", 1)
        self._use_cloudgpt = is_cloudgpt_model(self._model_name)
        self._explicit_key = api_key
        self._explicit_base = base_url
        self._explicit_version = api_version
        self._resolved = None
        self._use_cloudgpt_sidecar = False
        self._use_testbench_config = False

        if self._use_cloudgpt:
            deployment = self._model_name.split("/", 1)[1]
            sidecar_base = self._normalize_azure_api_base(
                base_url or os.environ.get("AZURE_API_BASE")
            )
            if sidecar_base and self._looks_like_local_sidecar_endpoint(sidecar_base):
                self._use_cloudgpt_sidecar = True
                self._provider = "openai"
                self._model_name = "openai/" + deployment
                self._resolved = ResolvedEndpoint(
                    api_key=api_key or "dummy",
                    base_url=sidecar_base.rstrip("/") + "/openai/v1",
                    api_version=None,
                    mode="openai-compat",
                    is_cloudgpt=True,
                )
            else:
                # litellm understands the `azure/` prefix natively.
                self._model_name = "azure/" + deployment
                self._provider = "azure"
                self._resolved = resolve_endpoint(
                    "cloudgpt/" + deployment,
                    {
                        "api_key": api_key,
                        "base_url": base_url,
                        "api_version": api_version,
                    },
                )
        elif api_key or base_url:
            # Generic OpenAI-compatible gateway (PPAPI, OneAPI, vLLM, ...).
            # Force the litellm `openai/` prefix so it uses the compat path.
            _, bare = self._model_name.split("/", 1)
            self._model_name = f"openai/{bare}"
            self._provider = "openai"
            self._resolved = resolve_endpoint(
                self._model_name,
                {
                    "api_key": api_key,
                    "base_url": base_url,
                    "api_version": api_version,
                },
            )

    @classmethod
    def _looks_like_local_sidecar_endpoint(cls, endpoint: str) -> bool:
        hostname = urlparse(endpoint).hostname
        return hostname in cls._LOCAL_HOST_MARKERS

    @staticmethod
    def _normalize_azure_api_base(base_url: str | None) -> str | None:
        if not base_url:
            return None
        normalized = base_url.rstrip("/")
        if normalized.endswith("/openai/v1"):
            normalized = normalized[: -len("/openai/v1")]
        elif normalized.endswith("/openai"):
            normalized = normalized[: -len("/openai")]
        return normalized

    @staticmethod
    def _load_config(config_path: str | None) -> dict | None:
        if config_path is None:
            return None
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")
        with open(path) as f:
            config = json.load(f)
        logger.info(f"Loaded mini-swe-agent config from {config_path}")
        return config

    @property
    def _env(self) -> dict[str, str]:
        env: dict[str, str] = {}

        # Set MSWEA_CONFIGURED to true to disable interactive setup
        env["MSWEA_CONFIGURED"] = "true"
        if self._config and "env" in self._config:
            env.update(self._config["env"])

        if self._use_cloudgpt:
            env.setdefault("MSWEA_COST_TRACKING", "ignore_errors")
            if self._use_cloudgpt_sidecar and self._provider == "openai":
                env["OPENAI_API_KEY"] = self._resolved.api_key or "dummy"
                env.setdefault("OPENAI_BASE_URL", self._resolved.base_url)
            elif self._use_cloudgpt_sidecar:
                # Route B sidecar injects Authorization on the proxy side.
                # Keep a dummy API key so Azure clients don't fail local checks.
                env["AZURE_API_KEY"] = self._resolved.api_key or "dummy"
                env.setdefault("AZURE_OPENAI_API_KEY", env["AZURE_API_KEY"])
                env.setdefault("AZURE_API_BASE", self._resolved.base_url)
            else:
                env["AZURE_OPENAI_AD_TOKEN"] = self._resolved.api_key
                env.setdefault("AZURE_API_BASE", CLOUDGPT_ENDPOINT)
            env.setdefault(
                "AZURE_API_VERSION",
                self._resolved.api_version or get_cloudgpt_api_version(),
            )
            return env

        if self._resolved is not None:
            # OpenAI-compatible gateway. litellm's openai provider reads
            # OPENAI_API_KEY and OPENAI_API_BASE.
            env.setdefault("MSWEA_COST_TRACKING", "ignore_errors")
            env["OPENAI_API_KEY"] = self._resolved.api_key
            if self._resolved.base_url:
                env["OPENAI_API_BASE"] = self._resolved.base_url
            return env

        # Handle API key with fallback logic
        if "MSWEA_API_KEY" in env:
            return env

        if "MSWEA_API_KEY" in os.environ:
            env["MSWEA_API_KEY"] = os.environ["MSWEA_API_KEY"]
            return env

        if self._provider == "anthropic":
            if "ANTHROPIC_API_KEY" in env:
                return env
            if "ANTHROPIC_API_KEY" in os.environ:
                env["ANTHROPIC_API_KEY"] = os.environ["ANTHROPIC_API_KEY"]
                return env
        elif self._provider == "openai":
            if "OPENAI_API_KEY" in env:
                return env
            if "OPENAI_API_KEY" in os.environ:
                env["OPENAI_API_KEY"] = os.environ["OPENAI_API_KEY"]
                return env
        else:
            raise ValueError(
                f"No API key found for provider {self._provider}. "
                "Please set MSWEA_API_KEY, ANTHROPIC_API_KEY, or OPENAI_API_KEY "
                "environment variable"
            )

        raise ValueError(
            f"No API key found for provider {self._provider}. "
            "Please set MSWEA_API_KEY, ANTHROPIC_API_KEY, or OPENAI_API_KEY "
            "environment variable"
        )

    _ESSENTIAL_ENV: Mapping[str, str] = {
        "MSWEA_CONFIGURED": "true",
        "MSWEA_COST_TRACKING": "ignore_errors",
    }

    def _runtime_env(self) -> dict[str, str]:
        env = {
            "TESTPRISM_MINI_SWE_MODEL": self._model_name,
            "TESTPRISM_TASK_INSTRUCTION_PATH": str(self.CONTAINER_INSTRUCTION_PATH),
            "TESTPRISM_AGENT_LOG_DIR": self.CONTAINER_AGENT_LOGS_PATH,
            "TESTPRISM_MINI_SWE_VERSION_SPEC": resolve_mini_swe_agent_version(
                self.version
            ),
            "TESTPRISM_MINI_SWE_RUNTIME_KEY": mini_swe_agent_runtime_key(
                self.version
            ),
        }
        if self.system_prompt:
            self._validate_system_prompt_delivery()
            env["TESTPRISM_SYSTEM_PROMPT_PATH"] = str(
                self.CONTAINER_SYSTEM_PROMPT_PATH
            )
        if self._use_testbench_config:
            env["TESTPRISM_MINI_SWE_BASE_CONFIG"] = str(
                self.CONTAINER_TESTBENCH_CONFIG_PATH
            )
        return env

    def enable_testbench_prompt_config(self) -> None:
        """Use the neutral mini-SWE-agent workflow for TestBench tasks."""

        self._use_testbench_config = True

    def _copy_install_assets_to_container(self, session: TmuxSession) -> None:
        if not self._use_testbench_config:
            return
        session.copy_to_container(
            self.TESTBENCH_CONFIG_PATH,
            container_dir=str(self.CONTAINER_TESTBENCH_CONFIG_PATH.parent),
            container_filename=self.CONTAINER_TESTBENCH_CONFIG_PATH.name,
        )

    @classmethod
    def _should_rewrite_container_url(cls, key: str, value: str) -> bool:
        if key not in cls._CONTAINER_REACHABLE_URL_ENV_NAMES:
            return False
        parsed = urlparse(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname)

    @classmethod
    def _container_reachable_env(cls, env: Mapping[str, str]) -> dict[str, str]:
        reachable = dict(env)
        for key, value in list(reachable.items()):
            if cls._should_rewrite_container_url(key, value):
                reachable[key] = DockerComposeManager.container_reachable_loopback_url(
                    value
                )
        return reachable

    def _create_env_setup_file(self) -> str:
        if self.profile_env:
            env = dict(self._ESSENTIAL_ENV)
            env.update(self.profile_env)
        else:
            env = self._env
        env = self._container_reachable_env(env)
        env.update(self._runtime_env())
        return "\n".join(
            f"export {key}={shlex.quote(value)}" for key, value in env.items()
        )

    @property
    def _install_agent_script_path(self) -> Path:
        return self._get_templated_script_path("mini-swe-setup.sh.j2")

    def _run_agent_commands(self, instruction: str) -> list[TerminalCommand]:
        return [
            TerminalCommand(
                command="bash /installed-agent/run-mini-swe-agent.sh",
                min_timeout_sec=0.0,
                max_timeout_sec=float("inf"),
                block=True,
                append_enter=True,
            ),
        ]
