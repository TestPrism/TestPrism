"""
Any agent that implements this abstract class will be installed into the task container
and executed using a shell command.

This is useful for agents that are not compatible with the tools we provide to interact
with the task container externally.

This should be used as a last resort, because it adds unnecessary dependencies to the
task container and may fail due to properties of the task container rather than the
agent's inability to perform the task (e.g. volume constraints, broken networking).
"""

import inspect
import json
import os
import re
import shlex
import tempfile
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from testprism.agents.base_agent import AgentResult, BaseAgent
from testprism.agents.failure_mode import FailureMode
from testprism.terminal.docker_compose_manager import DockerComposeManager
from testprism.terminal.models import TerminalCommand
from testprism.terminal.tmux_session import TmuxSession
from testprism.utils.template_utils import render_setup_script


class AbstractInstalledAgent(BaseAgent, ABC):
    """
    The container agent logs path is mounted to the host. Your agent can log to it if
    you want to view trajectories after the task finishes.
    """

    CONTAINER_AGENT_LOGS_PATH = DockerComposeManager.CONTAINER_AGENT_LOGS_PATH
    CONTAINER_INSTRUCTION_PATH = Path("/installed-agent/task-instruction.txt")
    CONTAINER_SYSTEM_PROMPT_PATH = Path("/installed-agent/system-prompt.txt")
    EXECUTION_DIAGNOSTIC_FILENAME = "agent-execution.jsonl"
    supports_profile_environment = True

    @property
    @abstractmethod
    def _env(self) -> dict[str, str]:
        """
        Environment variables to use when running the agent (e.g. ANTHROPIC_API_KEY).
        """
        ...

    @property
    @abstractmethod
    def _install_agent_script_path(self) -> Path:
        """
        Script to install the agent in the container.
        """
        ...

    def _get_template_variables(self) -> dict[str, str]:
        """
        Get template variables for rendering setup scripts.

        Override this method to provide custom template variables.
        The default implementation returns the agent version if defined.

        Returns:
            Dictionary of template variables
        """
        return {"version": self.version} if self.version else {}

    def _get_templated_script_path(self, template_name: str = "setup.sh.j2") -> Path:
        """
        Get the path to a templated setup script.

        Args:
            template_name: Name of the template file

        Returns:
            Path to the rendered setup script
        """
        # Get the agent's directory by using the agent's own file location
        agent_file = inspect.getfile(self.__class__)
        agent_dir = Path(agent_file).parent

        # Template path
        template_path = agent_dir / template_name

        # Render the template with template variables
        script_content = render_setup_script(
            template_path, self._get_template_variables()
        )

        # Create temporary file with rendered content
        temp_file = tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False)
        temp_file.write(script_content)
        temp_file.close()

        # Make the file executable
        os.chmod(temp_file.name, 0o755)

        return Path(temp_file.name)

    @abstractmethod
    def _run_agent_commands(self, instruction: str) -> list[TerminalCommand]:
        """
        Commands to run the agent with the given task instruction.
        """
        pass

    @staticmethod
    def _wrap_command_with_exit_marker(
        command: TerminalCommand,
        marker: str,
    ) -> TerminalCommand:
        wrapped = command.model_copy()
        # The caller parses the marker immediately after send_command returns,
        # so every wrapped command must wait for the marker even when a legacy
        # adapter declared the original command non-blocking.
        wrapped.block = True
        wrapped.min_timeout_sec = 0.0
        wrapped.command = (
            "set -o pipefail\n"
            "{\n"
            f"{command.command}\n"
            "}\n"
            "__testprism_agent_status=$?; "
            "printf '\n"
            f"{marker}"
            "%s\n' \"$__testprism_agent_status\""
        )
        return wrapped

    def _create_env_setup_file(self) -> str:
        # A profile environment is an authoritative, per-instance container
        # mapping. Do not evaluate ``_env`` in this case: several legacy Agents
        # read process-global credentials there, which is unsafe under concurrency.
        environment = self.profile_env if self.profile_env else self._env
        return "\n".join(
            [f"export {key}={shlex.quote(value)}" for key, value in environment.items()]
        )

    def _copy_text_to_container(
        self,
        content: str,
        destination: Path,
        session: TmuxSession,
    ) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".txt",
            delete=False,
            encoding="utf-8",
        ) as temp_file:
            temp_file.write(content)
            temp_path = Path(temp_file.name)

        try:
            session.copy_to_container(
                temp_path,
                container_dir=str(destination.parent),
                container_filename=destination.name,
            )
        finally:
            temp_path.unlink(missing_ok=True)

    def _copy_instruction_to_container(
        self,
        instruction: str,
        session: TmuxSession,
    ) -> None:
        self._copy_text_to_container(
            instruction,
            self.CONTAINER_INSTRUCTION_PATH,
            session,
        )

    def _copy_system_prompt_to_container(self, session: TmuxSession) -> None:
        if not self.system_prompt:
            return
        self._copy_text_to_container(
            self.system_prompt.strip(),
            self.CONTAINER_SYSTEM_PROMPT_PATH,
            session,
        )

    def _copy_install_assets_to_container(self, session: TmuxSession) -> None:
        """Copy optional install-time assets before running install-agent.sh."""

        return

    def _record_execution_event(
        self,
        logging_dir: Path | None,
        event: str,
        **details: object,
    ) -> None:
        """Persist secret-free lifecycle markers for installed CLI Agents.

        Terminal recordings prove what reached tmux, but not whether a failure
        happened immediately before a file copy or command dispatch. Keep a
        host-side event log so strategy failures can be localized without
        recording prompts, environment variables, or commands containing them.
        """

        if logging_dir is None:
            return
        try:
            logging_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "timestamp": datetime.now(UTC).isoformat(),
                "event": event,
                **details,
            }
            with (logging_dir / self.EXECUTION_DIAGNOSTIC_FILENAME).open(
                "a", encoding="utf-8"
            ) as stream:
                stream.write(json.dumps(payload, sort_keys=True) + "\n")
        except OSError:
            # Diagnostics must never turn an otherwise runnable Agent into a
            # failure. The terminal/session logs remain the fallback evidence.
            return

    def perform_task(
        self,
        instruction: str,
        session: TmuxSession,
        logging_dir: Path | None = None,
    ) -> AgentResult:
        # Reject unsupported delivery before spending time installing the CLI.
        self._validate_system_prompt_delivery()
        stage = "install_script_copy"
        try:
            self._record_execution_event(logging_dir, f"{stage}_started")
            session.copy_to_container(
                self._install_agent_script_path,
                container_dir="/installed-agent",
                container_filename="install-agent.sh",
            )
            self._record_execution_event(logging_dir, f"{stage}_completed")

            stage = "install_asset_copy"
            self._record_execution_event(logging_dir, f"{stage}_started")
            self._copy_install_assets_to_container(session)
            self._record_execution_event(logging_dir, f"{stage}_completed")

            # Execute outside the session to avoid exposing the env variables.
            stage = "environment_setup"
            self._record_execution_event(logging_dir, f"{stage}_started")
            env_setup_content = self._create_env_setup_file()
            session.container.exec_run(
                [
                    "sh",
                    "-c",
                    (
                        f"echo {shlex.quote(env_setup_content)} > "
                        "/installed-agent/setup-env.sh"
                    ),
                ]
            )
            self._record_execution_event(logging_dir, f"{stage}_completed")

            stage = "environment_source"
            self._record_execution_event(logging_dir, f"{stage}_started")
            session.send_keys(
                [
                    "source /installed-agent/setup-env.sh",
                    "Enter",
                ],
                block=True,
                max_timeout_sec=float("inf"),
            )
            self._record_execution_event(logging_dir, f"{stage}_completed")

            stage = "agent_install"
            self._record_execution_event(logging_dir, f"{stage}_started")
            install_status_marker = f"__TESTPRISM_INSTALL_EXIT_{uuid4().hex}__"
            session.send_keys(
                [
                    (
                        "( source /installed-agent/install-agent.sh ); "
                        "__testprism_install_status=$?; "
                        "if [ \"$__testprism_install_status\" -eq 0 ]; then "
                        # Installers commonly put binaries in ~/.local/bin or an
                        # nvm-managed Node directory. The installer itself runs in
                        # a subshell so an accidental `exit` cannot kill the tmux
                        # session; restore those PATH changes in the parent shell
                        # before dispatching the agent command.
                        "export PATH=\"$HOME/.local/bin:$PATH\"; "
                        "if [ -s \"$HOME/.nvm/nvm.sh\" ]; then "
                        "source \"$HOME/.nvm/nvm.sh\"; "
                        "fi; "
                        "hash -r; "
                        "fi; "
                        "printf '\\n"
                        f"{install_status_marker}"
                        "%s\\n' \"$__testprism_install_status\""
                    ),
                    "Enter",
                ],
                block=True,
                max_timeout_sec=float("inf"),
            )

            # Parse only the marker emitted by printf; command echoes contain
            # the marker but not the resolved numeric status.
            installation_output = session.capture_pane(capture_entire=True)
            install_status_matches = re.findall(
                rf"^{re.escape(install_status_marker)}(\d+)$",
                installation_output,
                flags=re.MULTILINE,
            )
            installation_exit_code = (
                int(install_status_matches[-1]) if install_status_matches else None
            )
            installation_failed = installation_exit_code != 0
            self._record_execution_event(
                logging_dir,
                f"{stage}_completed",
                installation_failed=installation_failed,
                installation_exit_code=installation_exit_code,
            )
            if installation_failed:
                return AgentResult(
                    total_input_tokens=0,
                    total_output_tokens=0,
                    failure_mode=FailureMode.AGENT_INSTALLATION_FAILED,
                )

            # Render the task separately from the system prompt. Native CLI
            # adapters consume CONTAINER_SYSTEM_PROMPT_PATH through their own
            # system/developer configuration instead of smuggling it into user text.
            stage = "instruction_render"
            self._record_execution_event(logging_dir, f"{stage}_started")
            rendered_instruction = self._render_instruction(instruction)
            if self.system_prompt and not self.supports_configured_system_prompt:
                rendered_instruction = self._apply_system_prompt(rendered_instruction)
            self._record_execution_event(logging_dir, f"{stage}_completed")

            if self.system_prompt and self.supports_configured_system_prompt:
                stage = "system_prompt_copy"
                self._record_execution_event(logging_dir, f"{stage}_started")
                self._copy_system_prompt_to_container(session)
                self._record_execution_event(logging_dir, f"{stage}_completed")

            stage = "instruction_copy"
            self._record_execution_event(logging_dir, f"{stage}_started")
            self._copy_instruction_to_container(rendered_instruction, session)
            self._record_execution_event(logging_dir, f"{stage}_completed")

            stage = "agent_command_prepare"
            self._record_execution_event(logging_dir, f"{stage}_started")
            run_agent_commands = self._run_agent_commands(rendered_instruction)
            self._record_execution_event(
                logging_dir,
                f"{stage}_completed",
                command_count=len(run_agent_commands),
            )
            for index, command in enumerate(run_agent_commands):
                stage = "agent_command_dispatch"
                command_status_marker = f"__TESTPRISM_AGENT_EXIT_{uuid4().hex}__"
                self._record_execution_event(
                    logging_dir, f"{stage}_started", command_index=index
                )
                session.send_command(
                    self._wrap_command_with_exit_marker(command, command_status_marker)
                )
                command_output = session.capture_pane(capture_entire=True)
                command_status_matches = re.findall(
                    rf"^{re.escape(command_status_marker)}(\d+)$",
                    command_output,
                    flags=re.MULTILINE,
                )
                command_exit_code = (
                    int(command_status_matches[-1])
                    if command_status_matches
                    else None
                )
                command_failed = command_exit_code != 0
                self._record_execution_event(
                    logging_dir,
                    f"{stage}_completed",
                    command_failed=command_failed,
                    command_exit_code=command_exit_code,
                    command_index=index,
                )
                if command_failed:
                    return AgentResult(
                        total_input_tokens=0,
                        total_output_tokens=0,
                        failure_mode=FailureMode.UNKNOWN_AGENT_ERROR,
                    )

            return AgentResult(
                total_input_tokens=0,
                total_output_tokens=0,
            )
        except Exception as error:
            self._record_execution_event(
                logging_dir,
                "agent_execution_failed",
                stage=stage,
                error_type=type(error).__name__,
            )
            raise
