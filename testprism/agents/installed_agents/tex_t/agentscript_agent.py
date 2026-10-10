import base64
import json
import os
from pathlib import Path

from testprism.agents._cloudgpt import (
    CLOUDGPT_V1_ENDPOINT,
    get_cloudgpt_token,
    is_cloudgpt_model,
    strip_cloudgpt_prefix,
)
from testprism.agents.agent_name import AgentName
from testprism.agents.base_agent import SystemPromptMode
from testprism.agents.installed_agents.abstract_installed_agent import (
    AbstractInstalledAgent,
)
from testprism.terminal.models import TerminalCommand
from testprism.utils.logger import logger


class AgentScriptAgent(AbstractInstalledAgent):
    native_system_prompt_modes = frozenset(SystemPromptMode)

    @staticmethod
    def name() -> str:
        return AgentName.AGENTSCRIPT.value

    def __init__(
        self,
        model_name: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._model_name = model_name
        self._explicit_key = api_key
        self._explicit_base = base_url
        self._version = kwargs.get("version", "latest")
        self._config = self._load_config(kwargs.get("config"))

    @staticmethod
    def _load_config(config_path: str | None) -> dict | None:
        if config_path is None:
            return None
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")
        with open(path) as f:
            config = json.load(f)
        logger.info(f"Loaded agentscript config from {config_path}")
        return config

    @staticmethod
    def _split_model_name(model_name: str) -> tuple[str | None, str]:
        if "/" not in model_name:
            return None, model_name
        provider, model = model_name.split("/", 1)
        return provider, model

    @property
    def _env(self) -> dict[str, str]:
        env: dict[str, str] = {}

        if self._config and "env" in self._config:
            env.update(self._config["env"])

        if self._config and "provider" in self._config:
            env.setdefault("AGENTSCRIPT_PROVIDER", self._config["provider"])
        if self._config and "model" in self._config:
            env.setdefault("AGENTSCRIPT_MODEL", self._config["model"])
        config_has_model = bool(
            self._config and "provider" in self._config and "model" in self._config
        )

        if self._model_name and not config_has_model:
            if self._explicit_key and self._explicit_base:
                # Any OpenAI-compatible gateway. runner.mjs reads
                # OPENAI_API_KEY + OPENAI_BASE_URL.
                env["AGENTSCRIPT_PROVIDER"] = "openai"
                provider, model = self._split_model_name(self._model_name)
                env["AGENTSCRIPT_MODEL"] = model or self._model_name
                env["OPENAI_API_KEY"] = self._explicit_key
                env["OPENAI_BASE_URL"] = self._explicit_base
                return env

            if is_cloudgpt_model(self._model_name):
                # Route through the generic OpenAI-compatible branch in
                # runner.mjs and point it at CloudGPT's /openai/v1 gateway.
                env["AGENTSCRIPT_PROVIDER"] = "openai"
                env["AGENTSCRIPT_MODEL"] = strip_cloudgpt_prefix(self._model_name)
                env["OPENAI_API_KEY"] = get_cloudgpt_token()
                env["OPENAI_BASE_URL"] = CLOUDGPT_V1_ENDPOINT
                return env

            provider, model = self._split_model_name(self._model_name)
            if provider:
                env["AGENTSCRIPT_PROVIDER"] = provider
            env["AGENTSCRIPT_MODEL"] = model

        passthrough_keys = [
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "DEEPSEEK_API_KEY",
            "DEEPSEEK_BASE_URL",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_BASE_URL",
            "GOOGLE_GENERATIVE_AI_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "XAI_API_KEY",
            "GROQ_API_KEY",
            "MISTRAL_API_KEY",
            "OPENROUTER_API_KEY",
        ]

        for key in passthrough_keys:
            if key in os.environ:
                env.setdefault(key, os.environ[key])

        return env

    @property
    def _install_agent_script_path(self) -> Path:
        return self._get_templated_script_path("agentscript-setup.sh.j2")

    def _run_agent_commands(self, instruction: str) -> list[TerminalCommand]:
        instruction_b64 = base64.b64encode(instruction.encode("utf-8")).decode("ascii")
        log_dir = self.CONTAINER_AGENT_LOGS_PATH
        system_prompt_env = ""
        if self.system_prompt:
            self._validate_system_prompt_delivery()
            system_prompt_env = (
                f"TESTPRISM_SYSTEM_PROMPT_PATH={self.CONTAINER_SYSTEM_PROMPT_PATH} "
                f"TESTPRISM_SYSTEM_PROMPT_MODE={self.system_prompt_mode.value} "
            )
        return [
            TerminalCommand(
                command=(
                    f"mkdir -p {log_dir} && "
                    f"AGENTSCRIPT_TRAJECTORY_PATH={log_dir}/agentscript-trajectory.jsonl "
                    f"{system_prompt_env}"
                    f"node $HOME/.agentscript/runner.mjs --instruction-b64 "
                    f"{instruction_b64} 2>&1 | tee {log_dir}/agentscript.log"
                ),
                min_timeout_sec=0.0,
                max_timeout_sec=float("inf"),
                block=True,
                append_enter=True,
            ),
        ]
