import json
import re
from pathlib import Path

import pytest

from testprism.agents.base_agent import (
    AgentResult,
    SystemPromptMode,
    UnsupportedSystemPromptError,
)
from testprism.agents.installed_agents.abstract_installed_agent import (
    AbstractInstalledAgent,
)
from testprism.terminal.models import TerminalCommand


class _FakeContainer:
    def exec_run(self, command):
        return None


class _FakeSession:
    def __init__(
        self,
        *,
        fail_instruction_copy: bool = False,
        install_exit_code: int = 0,
        command_exit_code: int = 0,
        echo_install_command: bool = False,
        echo_agent_command: bool = False,
    ):
        self.container = _FakeContainer()
        self.fail_instruction_copy = fail_instruction_copy
        self.install_exit_code = install_exit_code
        self.command_exit_code = command_exit_code
        self.echo_install_command = echo_install_command
        self.echo_agent_command = echo_agent_command
        self.pane_output = ""
        self.copy_calls = 0
        self.copied_text: dict[str, str] = {}
        self.commands = []
        self.sent_key_commands = []

    def copy_to_container(self, *args, **kwargs):
        self.copy_calls += 1
        if self.fail_instruction_copy and self.copy_calls == 2:
            raise RuntimeError("instruction copy failed")
        source = Path(args[0])
        filename = kwargs.get("container_filename")
        if filename and source.suffix == ".txt":
            self.copied_text[filename] = source.read_text(encoding="utf-8")

    def send_keys(self, *args, **kwargs):
        keys = args[0] if args else kwargs.get("keys", [])
        command = keys[0] if keys else ""
        self.sent_key_commands.append(command)
        if "/installed-agent/install-agent.sh" in command:
            marker = re.search(r"(__TESTPRISM_INSTALL_EXIT_[0-9a-f]+__)", command)
            lines = []
            if self.echo_install_command:
                lines.append(f"root@box# {command}")
            lines.append("codex-cli 0.146.0")
            if marker:
                lines.append(f"{marker.group(1)}{self.install_exit_code}")
            self.pane_output = "\n".join(lines)
        return None

    def capture_pane(self, *args, **kwargs):
        return self.pane_output

    def send_command(self, command):
        self.commands.append(command)
        marker = re.search(r"(__TESTPRISM_AGENT_EXIT_[0-9a-f]+__)", command.command)
        lines = []
        if self.echo_agent_command:
            lines.append(f"root@box# {command.command}")
        lines.append("agent output")
        if marker:
            lines.append(f"{marker.group(1)}{self.command_exit_code}")
        self.pane_output = "\n".join(lines)


class _FakeInstalledAgent(AbstractInstalledAgent):
    def __init__(self, install_script: Path, **kwargs: object):
        super().__init__(**kwargs)
        self.install_script = install_script

    @staticmethod
    def name() -> str:
        return "fake-installed-agent"

    @property
    def _env(self) -> dict[str, str]:
        return {}

    @property
    def _install_agent_script_path(self) -> Path:
        return self.install_script

    def _run_agent_commands(self, instruction: str) -> list[TerminalCommand]:
        return [TerminalCommand(command="run-fake-agent")]


class _NativeFakeInstalledAgent(_FakeInstalledAgent):
    native_system_prompt_modes = frozenset({SystemPromptMode.REPLACE})


def _events(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_exit_marker_wrapper_waits_for_marker() -> None:
    command = TerminalCommand(
        command="run-agent",
        block=False,
        min_timeout_sec=1.0,
        max_timeout_sec=321.0,
    )

    wrapped = _FakeInstalledAgent._wrap_command_with_exit_marker(command, "MARKER")

    assert wrapped.block is True
    assert wrapped.min_timeout_sec == 0.0
    assert wrapped.max_timeout_sec == 321.0
    assert "MARKER" in wrapped.command


def test_exit_marker_wrapper_preserves_heredoc_terminator_line() -> None:
    command = TerminalCommand(command="cat <<'EOF'\nbody\nEOF")

    wrapped = _FakeInstalledAgent._wrap_command_with_exit_marker(command, "MARKER")

    assert "\nEOF\n}\n" in wrapped.command
    assert "EOF;" not in wrapped.command


def test_perform_task_records_secret_free_lifecycle_events(tmp_path):
    install_script = tmp_path / "install.sh"
    install_script.write_text("#!/bin/sh\n")
    log_dir = tmp_path / "logs"
    session = _FakeSession()

    result = _FakeInstalledAgent(install_script).perform_task(
        "test instruction", session, logging_dir=log_dir
    )

    assert result == AgentResult()
    assert len(session.commands) == 1
    assert "run-fake-agent" in session.commands[0].command
    assert "__TESTPRISM_AGENT_EXIT_" in session.commands[0].command
    events = _events(log_dir / "agent-execution.jsonl")
    assert [event["event"] for event in events] == [
        "install_script_copy_started",
        "install_script_copy_completed",
        "install_asset_copy_started",
        "install_asset_copy_completed",
        "environment_setup_started",
        "environment_setup_completed",
        "environment_source_started",
        "environment_source_completed",
        "agent_install_started",
        "agent_install_completed",
        "instruction_render_started",
        "instruction_render_completed",
        "instruction_copy_started",
        "instruction_copy_completed",
        "agent_command_prepare_started",
        "agent_command_prepare_completed",
        "agent_command_dispatch_started",
        "agent_command_dispatch_completed",
    ]
    assert events[-1]["command_failed"] is False
    assert events[-1]["command_exit_code"] == 0
    assert events[-1]["command_index"] == 0
    assert all("rendered_instruction" not in event for event in events)


def test_perform_task_records_failing_stage(tmp_path):
    install_script = tmp_path / "install.sh"
    install_script.write_text("#!/bin/sh\n")
    log_dir = tmp_path / "logs"
    session = _FakeSession(fail_instruction_copy=True)

    with pytest.raises(RuntimeError, match="instruction copy failed"):
        _FakeInstalledAgent(install_script).perform_task(
            "test instruction", session, logging_dir=log_dir
        )

    events = _events(log_dir / "agent-execution.jsonl")
    assert events[-1]["event"] == "agent_execution_failed"
    assert events[-1]["stage"] == "instruction_copy"
    assert events[-1]["error_type"] == "RuntimeError"


def test_install_command_echo_does_not_cause_false_failure(tmp_path):
    install_script = tmp_path / "install.sh"
    install_script.write_text("#!/bin/sh\n")
    session = _FakeSession(install_exit_code=0, echo_install_command=True)

    result = _FakeInstalledAgent(install_script).perform_task(
        "test instruction", session
    )

    assert result == AgentResult()
    assert len(session.commands) == 1
    assert "run-fake-agent" in session.commands[0].command


def test_install_script_runs_in_subshell_so_nonzero_source_can_be_captured(tmp_path):
    install_script = tmp_path / "install.sh"
    install_script.write_text("#!/bin/sh\n")
    session = _FakeSession()

    _FakeInstalledAgent(install_script).perform_task("test instruction", session)

    install_command = next(
        command
        for command in session.sent_key_commands
        if "/installed-agent/install-agent.sh" in command
    )
    assert "( source /installed-agent/install-agent.sh );" in install_command


def test_install_command_restores_common_cli_paths_in_parent_shell(tmp_path):
    install_script = tmp_path / "install.sh"
    install_script.write_text("#!/bin/sh\n")
    session = _FakeSession()

    _FakeInstalledAgent(install_script).perform_task("test instruction", session)

    install_command = next(
        command
        for command in session.sent_key_commands
        if "/installed-agent/install-agent.sh" in command
    )
    assert 'export PATH="$HOME/.local/bin:$PATH"' in install_command
    assert 'source "$HOME/.nvm/nvm.sh"' in install_command
    assert "hash -r" in install_command


def test_install_nonzero_exit_is_detected(tmp_path):
    install_script = tmp_path / "install.sh"
    install_script.write_text("#!/bin/sh\n")
    session = _FakeSession(install_exit_code=7)

    result = _FakeInstalledAgent(install_script).perform_task(
        "test instruction", session
    )

    assert result.failure_mode.name == "AGENT_INSTALLATION_FAILED"
    assert session.commands == []


def test_agent_command_nonzero_exit_is_detected(tmp_path):
    install_script = tmp_path / "install.sh"
    install_script.write_text("#!/bin/sh\n")
    log_dir = tmp_path / "logs"
    session = _FakeSession(command_exit_code=429)

    result = _FakeInstalledAgent(install_script).perform_task(
        "test instruction", session, logging_dir=log_dir
    )

    assert result.failure_mode.name == "UNKNOWN_AGENT_ERROR"
    events = _events(log_dir / "agent-execution.jsonl")
    assert events[-1]["event"] == "agent_command_dispatch_completed"
    assert events[-1]["command_failed"] is True
    assert events[-1]["command_exit_code"] == 429


def test_agent_command_echo_does_not_cause_false_failure(tmp_path):
    install_script = tmp_path / "install.sh"
    install_script.write_text("#!/bin/sh\n")
    session = _FakeSession(command_exit_code=0, echo_agent_command=True)

    result = _FakeInstalledAgent(install_script).perform_task(
        "test instruction", session
    )

    assert result == AgentResult()


def test_native_system_prompt_is_copied_separately_from_user_instruction(tmp_path):
    install_script = tmp_path / "install.sh"
    install_script.write_text("#!/bin/sh\n")
    session = _FakeSession()
    agent = _NativeFakeInstalledAgent(
        install_script,
        system_prompt="authoritative policy",
    )

    result = agent.perform_task("task body", session)

    assert result == AgentResult()
    assert session.copied_text == {
        "system-prompt.txt": "authoritative policy",
        "task-instruction.txt": "task body",
    }


def test_unsupported_system_prompt_fails_closed(tmp_path):
    install_script = tmp_path / "install.sh"
    install_script.write_text("#!/bin/sh\n")
    session = _FakeSession()
    agent = _FakeInstalledAgent(install_script)

    with pytest.raises(UnsupportedSystemPromptError, match="supported modes: none"):
        agent.system_prompt = "must be authoritative"
    assert session.copy_calls == 0


def test_user_prompt_fallback_requires_explicit_opt_in(tmp_path):
    install_script = tmp_path / "install.sh"
    install_script.write_text("#!/bin/sh\n")
    session = _FakeSession()
    agent = _FakeInstalledAgent(
        install_script,
        allow_system_prompt_user_fallback="true",
    )
    agent.system_prompt = "legacy policy"

    result = agent.perform_task("task body", session)

    assert result == AgentResult()
    assert session.copied_text["task-instruction.txt"] == (
        "<system>\nlegacy policy\n</system>\n\ntask body"
    )


def test_false_string_does_not_enable_user_prompt_fallback(tmp_path):
    install_script = tmp_path / "install.sh"
    install_script.write_text("#!/bin/sh\n")

    with pytest.raises(UnsupportedSystemPromptError):
        _FakeInstalledAgent(
            install_script,
            system_prompt="must stay authoritative",
            allow_system_prompt_user_fallback="false",
        )
