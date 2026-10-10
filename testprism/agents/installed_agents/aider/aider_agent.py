"""
The Aider agent installs https://aider.chat/ into the task container and attempts to
solve the task using Aider's scripting mode.

Currently, Aider does not support autonomous bash execution, and therefore the Aider
agent fails almost all tasks -- not due to inability to solve the task, but rather
inability to execute the bash commands it generates.

As soon as Aider adds support for autonomous bash execution, we will update this agent.
"""

import os
import shlex
from pathlib import Path

from testprism.agents._cloudgpt import (
    CLOUDGPT_ENDPOINT,
    get_cloudgpt_api_version,
    is_cloudgpt_model,
    resolve_endpoint,
    strip_cloudgpt_prefix,
)
from testprism.agents.agent_name import AgentName
from testprism.agents.installed_agents.abstract_installed_agent import (
    AbstractInstalledAgent,
)
from testprism.terminal.models import TerminalCommand


class AiderAgent(AbstractInstalledAgent):
    @staticmethod
    def name() -> str:
        return AgentName.AIDER.value

    def __init__(
        self,
        model_name: str = "openai/o3-mini",
        api_key: str | None = None,
        base_url: str | None = None,
        api_version: str | None = None,
        **kwargs,
    ):
        """
        Args:
            model_name: provider/model, e.g. `openai/gpt-4o-mini`,
                `anthropic/claude-sonnet-4-6`, `cloudgpt/gpt-4o`, or
                `azure/<deployment>`.
            api_key: direct override; when present together with base_url,
                the agent talks to that gateway without any further lookup.
            base_url: OpenAI-compatible base URL (e.g. `https://app.ppapi.ai/v1`).
            api_version: optional Azure API version.
        """
        super().__init__(**kwargs)

        if "/" not in model_name:
            raise ValueError("Model name must be in the format provider/model_name")

        self._provider, bare_model = model_name.split("/", 1)
        self._use_cloudgpt = is_cloudgpt_model(model_name)

        resolver_kwargs = {
            "api_key": api_key,
            "base_url": base_url,
            "api_version": api_version,
        }
        # Aider gateway path: delegate to resolver whenever the user passed
        # explicit kwargs. Only fall back to provider-specific env lookup
        # (OPENAI_API_KEY / ANTHROPIC_API_KEY) if none of those are given.
        self._resolved = None
        if api_key or base_url or self._use_cloudgpt:
            self._resolved = resolve_endpoint(model_name, resolver_kwargs)

        if self._use_cloudgpt:
            self._provider = "azure"
            self._model_name = strip_cloudgpt_prefix(model_name)
        elif self._resolved is not None:
            # Explicit gateway: aider's `openai/<model>` + OPENAI_API_BASE
            # override works for any OpenAI-compatible endpoint.
            self._model_name = bare_model
        elif api_key:
            self._model_name = bare_model
        elif self._provider == "openai":
            self._model_name = bare_model
            # legacy: pick up env
            os.environ["OPENAI_API_KEY"]  # raise if missing
        elif self._provider == "anthropic":
            self._model_name = bare_model
            os.environ["ANTHROPIC_API_KEY"]
        else:
            raise ValueError(f"No API key provided for provider: {self._provider}")

    @property
    def _env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        if self._use_cloudgpt:
            # Aider's native Azure mode: AZURE_API_KEY/BASE/VERSION + model
            # `azure/<deployment>`.
            env["AZURE_API_KEY"] = self._resolved.api_key
            env["AZURE_API_BASE"] = CLOUDGPT_ENDPOINT
            env["AZURE_API_VERSION"] = (
                self._resolved.api_version or get_cloudgpt_api_version()
            )
            return env

        if self._resolved is not None:
            # Any OpenAI-compatible gateway (PPAPI, OneAPI, self-hosted).
            # Aider honours OPENAI_API_BASE for the openai provider.
            env["OPENAI_API_KEY"] = self._resolved.api_key
            if self._resolved.base_url:
                env["OPENAI_API_BASE"] = self._resolved.base_url
            return env

        # Legacy: provider-from-model + env fallback.
        if self._provider == "openai":
            env["OPENAI_API_KEY"] = os.environ["OPENAI_API_KEY"]
        elif self._provider == "anthropic":
            env["ANTHROPIC_API_KEY"] = os.environ["ANTHROPIC_API_KEY"]
        return env

    @property
    def _install_agent_script_path(self) -> os.PathLike:
        return Path(__file__).parent / "aider-setup.sh"

    def _run_agent_commands(self, instruction: str) -> list[TerminalCommand]:
        escaped_instruction = shlex.quote(instruction)
        log_dir = self.CONTAINER_AGENT_LOGS_PATH
        if self._use_cloudgpt:
            model_arg = f"azure/{self._model_name}"
        elif self._resolved is not None:
            # Force the openai dialect; Aider will hit OPENAI_API_BASE for
            # routing. Works for PPAPI, OneAPI, vLLM, etc.
            model_arg = f"openai/{self._model_name}"
        else:
            model_arg = f"{self._provider}/{self._model_name}"
        return [
            TerminalCommand(
                command=(
                    f"mkdir -p {log_dir} && "
                    f"aider --message {escaped_instruction} --yes --model"
                    f" {model_arg}"
                    f" --chat-history-file {log_dir}/aider-chat-history.md"
                    f" --input-history-file {log_dir}/aider-input-history"
                    f" --llm-history-file {log_dir}/aider-llm-history.jsonl"
                    f" 2>&1 | tee {log_dir}/aider.log"
                ),
                min_timeout_sec=0.0,
                max_timeout_sec=float("inf"),
                block=False,
                append_enter=True,
            )
        ]
