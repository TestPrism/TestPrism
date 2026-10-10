import os
import shlex
from pathlib import Path

from testprism.agents.agent_name import AgentName
from testprism.agents.installed_agents.abstract_installed_agent import (
    AbstractInstalledAgent,
)
from testprism.terminal.models import TerminalCommand


class GrokCliAgent(AbstractInstalledAgent):
    @staticmethod
    def name() -> str:
        return AgentName.GROK_CLI.value

    def __init__(self, model_name: str, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._model_name = model_name
        self._version = kwargs.get("version", "latest")

    @property
    def _env(self) -> dict[str, str]:
        return {"GROK_API_KEY": os.environ["GROK_API_KEY"]}

    @property
    def _install_agent_script_path(self) -> Path:
        return self._get_templated_script_path("grok-cli-setup.sh.j2")

    def _run_agent_commands(self, instruction: str) -> list[TerminalCommand]:
        escaped_instruction = shlex.quote(instruction)
        log_dir = self.CONTAINER_AGENT_LOGS_PATH
        return [
            TerminalCommand(
                command=(
                    f"mkdir -p {log_dir} && "
                    f"grok -m {self._model_name} -p {escaped_instruction}"
                    f" 2>&1 | tee {log_dir}/grok-cli.log"
                ),
                min_timeout_sec=0.0,
                max_timeout_sec=float("inf"),
                block=True,
                append_enter=True,
            ),
        ]
