import json
import os
from pathlib import Path

from testprism.agents._cloudgpt import (
    is_cloudgpt_model,
    strip_cloudgpt_prefix,
)
from testprism.agents._cloudgpt_proxy import CloudGPTAnthropicProxy
from testprism.agents.agent_name import AgentName
from testprism.agents.base_agent import AgentResult, SystemPromptMode
from testprism.agents.installed_agents.abstract_installed_agent import (
    AbstractInstalledAgent,
)
from testprism.terminal.models import TerminalCommand
from testprism.terminal.tmux_session import TmuxSession
from testprism.utils.logger import logger


class ClaudeCodeAgent(AbstractInstalledAgent):
    native_system_prompt_modes = frozenset(SystemPromptMode)

    @staticmethod
    def name() -> str:
        return AgentName.CLAUDE_CODE.value

    ALLOWED_TOOLS = [
        "Bash",
        "Edit",
        "Write",
        "Read",
        "Glob",
        "Grep",
        "LS",
        "WebFetch",
        "NotebookEdit",
        "NotebookRead",
        "TodoRead",
        "TodoWrite",
        "Agent",
    ]

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
        # Three routes, mutually exclusive:
        #   - explicit Anthropic-compatible gateway (PPAPI, OneAPI, ...): direct
        #   - CloudGPT (no native Anthropic endpoint): litellm-proxy bridge
        #   - classic Anthropic key: direct
        self._use_anthropic_gateway = bool(api_key and base_url)
        self._use_cloudgpt = (
            is_cloudgpt_model(model_name) and not self._use_anthropic_gateway
        )
        self._proxy: CloudGPTAnthropicProxy | None = None

    @staticmethod
    def _load_config(config_path: str | None) -> dict | None:
        if config_path is None:
            return None
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")
        with open(path) as f:
            config = json.load(f)
        logger.info(f"Loaded claude-code config from {config_path}")
        return config

    @property
    def _env(self) -> dict[str, str]:
        env: dict[str, str] = {
            "FORCE_AUTO_BACKGROUND_TASKS": "1",
            "ENABLE_BACKGROUND_TASKS": "1",
        }

        if self._use_anthropic_gateway:
            # PPAPI-style: Claude Code talks Anthropic natively to the
            # gateway; no proxy needed.
            env["ANTHROPIC_BASE_URL"] = self._explicit_base
            env["ANTHROPIC_AUTH_TOKEN"] = self._explicit_key
            env["ANTHROPIC_API_KEY"] = self._explicit_key
            if self._model_name:
                env["ANTHROPIC_MODEL"] = self._model_name
            return env

        if self._use_cloudgpt:
            assert self._proxy is not None, (
                "CloudGPT proxy must be started before _env is read."
            )
            env["ANTHROPIC_BASE_URL"] = self._proxy.base_url_for_container
            env["ANTHROPIC_AUTH_TOKEN"] = "sk-testprism-cloudgpt"
            env["ANTHROPIC_MODEL"] = self._proxy.model_alias
            env["ANTHROPIC_API_KEY"] = "sk-testprism-cloudgpt"
            return env

        if self._config and "env" in self._config:
            env.update(self._config["env"])
            if not self._model_name and "model" in self._config:
                env.setdefault(
                    "ANTHROPIC_MODEL", self._config["model"]
                )
        else:
            env["ANTHROPIC_AUTH_TOKEN"] = os.environ["ANTHROPIC_AUTH_TOKEN"]
            if self._model_name:
                env["ANTHROPIC_MODEL"] = self._model_name.removeprefix(
                    "anthropic/"
                )
            elif "ANTHROPIC_MODEL" in os.environ:
                env["ANTHROPIC_MODEL"] = os.environ["ANTHROPIC_MODEL"]

        if self._model_name:
            env["ANTHROPIC_MODEL"] = self._model_name.removeprefix("anthropic/")

        return env

    @property
    def _install_agent_script_path(self) -> Path:
        return self._get_templated_script_path("claude-code-setup.sh.j2")

    def _run_agent_commands(self, instruction: str) -> list[TerminalCommand]:
        log_dir = self.CONTAINER_AGENT_LOGS_PATH
        extra = ""
        if self._use_cloudgpt and self._proxy is not None:
            extra = f"--model {self._proxy.model_alias} "
        elif self._model_name:
            # Profile environments are injected directly into the container and
            # intentionally bypass ``_env``.  Pass the configured model on the
            # CLI so a profile's ``model`` field remains authoritative without
            # requiring a second, duplicated ANTHROPIC_MODEL env reference.
            extra = f"--model {self._model_name.removeprefix('anthropic/')} "
        if self.system_prompt:
            self._validate_system_prompt_delivery()
            flag = (
                "--system-prompt-file"
                if self.system_prompt_mode is SystemPromptMode.REPLACE
                else "--append-system-prompt-file"
            )
            extra += f"{flag} {self.CONTAINER_SYSTEM_PROMPT_PATH} "
        return [
            TerminalCommand(
                command=(
                    f"mkdir -p {log_dir} && "
                    f"claude --verbose --output-format stream-json "
                    f"{extra}"
                    f'-p "$(cat {self.CONTAINER_INSTRUCTION_PATH})" --allowedTools '
                    f"{' '.join(self.ALLOWED_TOOLS)}"
                    f" 2>&1 | tee {log_dir}/claude-code.jsonl"
                ),
                min_timeout_sec=0.0,
                max_timeout_sec=float("inf"),
                block=True,
                append_enter=True,
            ),
        ]

    def perform_task(
        self,
        instruction: str,
        session: TmuxSession,
        logging_dir: Path | None = None,
    ) -> AgentResult:
        if self._use_cloudgpt:
            deployment = strip_cloudgpt_prefix(self._model_name)
            self._proxy = CloudGPTAnthropicProxy(deployment=deployment)
            self._proxy.start()
            try:
                return super().perform_task(instruction, session, logging_dir)
            finally:
                self._proxy.stop()
                self._proxy = None
        return super().perform_task(instruction, session, logging_dir)
