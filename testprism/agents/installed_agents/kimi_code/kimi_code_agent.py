import json
import os
import shlex
from pathlib import Path

from testprism.agents.agent_name import AgentName
from testprism.agents.base_agent import SystemPromptMode
from testprism.agents.installed_agents.abstract_installed_agent import (
    AbstractInstalledAgent,
)
from testprism.terminal.docker_compose_manager import DockerComposeManager
from testprism.terminal.models import TerminalCommand


def _normalize_optional_string(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _toml_string(value: str) -> str:
    return json.dumps(value)


def _toml_string_array(values: list[str]) -> str:
    return "[ " + ", ".join(_toml_string(value) for value in values) + " ]"


class KimiCodeAgent(AbstractInstalledAgent):
    native_system_prompt_modes = frozenset(SystemPromptMode)

    CONTAINER_KIMI_HOME = Path("/installed-agent/kimi-home")
    CONTAINER_SYSTEM_AGENT_PATH = Path("/installed-agent/kimi-system-agent.md")
    MODEL_ALIAS = "testprism/default"
    DEFAULT_MAX_CONTEXT_SIZE = 262144

    @staticmethod
    def name() -> str:
        return AgentName.KIMI_CODE.value

    def __init__(
        self,
        model_name: str,
        api_key: str | None = None,
        base_url: str | None = None,
        reasoning_level: str | None = None,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)

        resolved_model_name = _normalize_optional_string(model_name)
        if resolved_model_name is None:
            raise ValueError("model_name must be provided for kimi-code")

        self._model_name = resolved_model_name.split("/")[-1]
        self._resolved_api_key = self._resolve_api_key(api_key)
        self._resolved_base_url = self._resolve_base_url(base_url)
        self._reasoning_level = _normalize_optional_string(reasoning_level)
        self._version = kwargs.get("version", "latest")
        self._config_toml_body = self._build_config_toml_body()

    def _resolve_api_key(self, api_key: str | None) -> str:
        resolved_api_key = _normalize_optional_string(api_key)
        if resolved_api_key is None:
            resolved_api_key = _normalize_optional_string(self.profile_env.get("KIMI_API_KEY"))
        if resolved_api_key is None:
            resolved_api_key = _normalize_optional_string(os.environ.get("KIMI_API_KEY"))
        if resolved_api_key is None:
            raise ValueError(
                "KIMI_API_KEY must be provided either via environment variable "
                "or --agent-kwarg api_key=your_key"
            )
        return resolved_api_key

    def _resolve_base_url(self, base_url: str | None) -> str | None:
        resolved_base_url = _normalize_optional_string(base_url)
        if resolved_base_url is None:
            resolved_base_url = _normalize_optional_string(
                self.profile_env.get("KIMI_BASE_URL")
            )
        if resolved_base_url is None:
            resolved_base_url = _normalize_optional_string(os.environ.get("KIMI_BASE_URL"))
        if resolved_base_url is None:
            return None
        return DockerComposeManager.container_reachable_loopback_url(resolved_base_url)

    @property
    def _env(self) -> dict[str, str]:
        return {"KIMI_API_KEY": self._resolved_api_key}

    @property
    def _install_agent_script_path(self) -> Path:
        return self._get_templated_script_path("kimi-code-setup.sh.j2")

    def _get_template_variables(self) -> dict[str, str]:
        variables = super()._get_template_variables()
        variables["config_toml_body"] = self._config_toml_body
        return variables

    def _create_env_setup_file(self) -> str:
        environment = dict(self.profile_env) if self.profile_env else dict(self._env)
        environment.setdefault("KIMI_API_KEY", self._resolved_api_key)
        return "\n".join(
            f"export {key}={shlex.quote(value)}"
            for key, value in environment.items()
        )

    def _build_config_toml_body(self) -> str:
        lines = [
            f"default_model = {_toml_string(self.MODEL_ALIAS)}",
            'default_permission_mode = "auto"',
            "telemetry = false",
            "",
            "[providers.testprism]",
            'type = "kimi"',
            'api_key = "${KIMI_API_KEY}"',
        ]
        if self._resolved_base_url is not None:
            lines.append(f"base_url = {_toml_string(self._resolved_base_url)}")

        lines.extend(
            [
                "",
                f'[models."{self.MODEL_ALIAS}"]',
                'provider = "testprism"',
                f"model = {_toml_string(self._model_name)}",
                f"max_context_size = {self.DEFAULT_MAX_CONTEXT_SIZE}",
                f"capabilities = {_toml_string_array(['thinking', 'tool_use'])}",
            ]
        )

        if self._reasoning_level is not None:
            lines.extend(
                [
                    "",
                    "[thinking]",
                    "enabled = true",
                    f"effort = {_toml_string(self._reasoning_level)}",
                ]
            )

        return "\n".join(lines)

    def _render_system_agent_file(self) -> str:
        if not self.system_prompt:
            raise ValueError("system_prompt must be set before rendering agent file")

        prompt_body = self.system_prompt.strip()
        if self.system_prompt_mode is SystemPromptMode.APPEND:
            prompt_body = f"${{base_prompt}}\n\n{prompt_body}"

        return (
            "---\n"
            "name: testprism-system\n"
            "description: TestPrism system prompt bridge for Kimi Code\n"
            "---\n\n"
            f"{prompt_body}\n"
        )

    def _copy_system_prompt_to_container(self, session) -> None:
        if not self.system_prompt:
            return
        self._copy_text_to_container(
            self._render_system_agent_file(),
            self.CONTAINER_SYSTEM_AGENT_PATH,
            session,
        )

    def _run_agent_commands(self, instruction: str) -> list[TerminalCommand]:
        del instruction

        log_dir = self.CONTAINER_AGENT_LOGS_PATH
        system_prompt_flag = ""
        if self.system_prompt:
            self._validate_system_prompt_delivery()
            system_prompt_flag = (
                f" --agent-file {self.CONTAINER_SYSTEM_AGENT_PATH}"
            )

        return [
            TerminalCommand(
                command=(
                    f"mkdir -p {log_dir} && "
                    f"prompt_text=\"$(cat {self.CONTAINER_INSTRUCTION_PATH})\" && "
                    "set -o pipefail && "
                    f"KIMI_CODE_HOME={self.CONTAINER_KIMI_HOME} "
                    f"kimi --model {self.MODEL_ALIAS}{system_prompt_flag} "
                    '-p "$prompt_text"'
                    f" 2>&1 | tee {log_dir}/kimi-code.log"
                ),
                min_timeout_sec=0.0,
                max_timeout_sec=float("inf"),
                block=True,
                append_enter=True,
            )
        ]
