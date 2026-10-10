import os

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


class QwenCodeAgent(AbstractInstalledAgent):
    native_system_prompt_modes = frozenset(SystemPromptMode)

    @staticmethod
    def name() -> str:
        return AgentName.QWEN_CODER.value

    def __init__(self, model_name: str | None = None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._use_cloudgpt = is_cloudgpt_model(model_name)
        # Qwen's OpenAI client sends the raw model name through; strip any
        # `provider/` prefix so PPAPI etc. see `gpt-4o-mini` not
        # `openai/gpt-4o-mini`.
        if model_name and "/" in model_name:
            self._model_name = strip_cloudgpt_prefix(model_name)
            if "/" in self._model_name:
                self._model_name = self._model_name.split("/", 1)[1]
        else:
            self._model_name = model_name
        self._version = kwargs.get("version", "latest")

        # Configurable API settings through agent_kwargs
        self._api_key = kwargs.get("api_key")
        self._base_url = kwargs.get("base_url")

    @property
    def _env(self) -> dict[str, str]:
        env = {}

        if self._use_cloudgpt and not self._api_key:
            env["OPENAI_API_KEY"] = get_cloudgpt_token()
            env["OPENAI_BASE_URL"] = self._base_url or CLOUDGPT_V1_ENDPOINT
            if self._model_name:
                env["OPENAI_MODEL"] = self._model_name
            return env

        # Explicit key/base_url from kwargs — the zero-config PPAPI / OneAPI /
        # self-hosted path. Qwen Code's OpenAI client honours OPENAI_BASE_URL.
        if self._api_key:
            env["OPENAI_API_KEY"] = self._api_key
        elif "OPENAI_API_KEY" in os.environ:
            env["OPENAI_API_KEY"] = os.environ["OPENAI_API_KEY"]
        else:
            raise ValueError(
                "OPENAI_API_KEY must be provided either via environment variable "
                "or --agent-kwarg api_key=your_key"
            )

        if self._model_name:
            env["OPENAI_MODEL"] = self._model_name
        elif "OPENAI_MODEL" in os.environ:
            env["OPENAI_MODEL"] = os.environ["OPENAI_MODEL"]
        else:
            env["OPENAI_MODEL"] = "qwen3-coder-plus"

        if self._base_url:
            env["OPENAI_BASE_URL"] = self._base_url
        elif "OPENAI_BASE_URL" in os.environ:
            env["OPENAI_BASE_URL"] = os.environ["OPENAI_BASE_URL"]
        else:
            env["OPENAI_BASE_URL"] = (
                "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
            )

        return env

    @property
    def _install_agent_script_path(self) -> os.PathLike:
        return self._get_templated_script_path("qwen_code-setup.sh.j2")

    def _run_agent_commands(self, task_description: str) -> list[TerminalCommand]:
        log_dir = self.CONTAINER_AGENT_LOGS_PATH
        system_prompt_flag = ""
        if self.system_prompt:
            self._validate_system_prompt_delivery()
            flag = (
                "--system-prompt"
                if self.system_prompt_mode is SystemPromptMode.REPLACE
                else "--append-system-prompt"
            )
            system_prompt_flag = (
                f' {flag} "$(cat {self.CONTAINER_SYSTEM_PROMPT_PATH})"'
            )
        return [
            TerminalCommand(
                command=(
                    f"mkdir -p {log_dir} && "
                    f"cat {self.CONTAINER_INSTRUCTION_PATH} | qwen --approval-mode yolo"
                    f"{system_prompt_flag}"
                    f" 2>&1 | tee {log_dir}/qwen-code.log"
                ),
                max_timeout_sec=float("inf"),
                block=True,
            )
        ]
