"""Unit tests for MiniSweAgent template rendering with conditional logic."""

import inspect
import os
import shlex
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from testprism.agents.installed_agents.mini_swe_agent.mini_swe_agent import (
    MiniSweAgent,
)
from testprism.terminal.docker_compose_manager import DockerComposeManager
from testprism.utils.template_utils import (
    get_template_path,
    render_setup_script,
)


class TestMiniSweAgentTemplate:
    """Test MiniSweAgent template rendering with conditional version logic."""

    @property
    def template_path(self) -> Path:
        """Get the path to the mini_swe_agent template."""
        agent_file = inspect.getfile(MiniSweAgent)
        agent_dir = Path(agent_file).parent
        return get_template_path(agent_dir, "mini-swe-setup.sh.j2")

    def test_template_rendering_with_version(self):
        """Test template rendering when version is provided."""
        if not self.template_path.exists():
            pytest.skip(f"Template not found: {self.template_path}")

        context = {"version": "2.1.0"}
        result = render_setup_script(self.template_path, context)

        # Basic structure checks
        assert result.startswith("#!/bin/bash")
        assert "set -euo pipefail" in result
        assert 'SYSTEM_PYTHON="/usr/bin/python3"' in result
        assert 'SYSTEM_PYTHON="$(command -v python3)"' in result
        assert "apt-get update" in result
        assert "apt-get install -y python3 python3-pip python3-venv" in result
        assert '"${SYSTEM_PYTHON}" - <<\'PY\'' in result
        assert '"${SYSTEM_PYTHON}" -m venv "${VENV_DIR}"' in result
        assert '"${VENV_DIR}/bin/pip" install --upgrade pip' in result
        assert 'RUNTIME_MARKER="/opt/testprism-mini-swe-agent-runtime.json"' in result
        assert 'EXPECTED_MINI_SWE_VERSION="${TESTPRISM_MINI_SWE_VERSION_SPEC:-2.4.6}"' in result
        assert "Using preinstalled mini-swe-agent runtime" in result

        # Version-specific checks
        assert '"${VENV_DIR}/bin/pip" install "mini-swe-agent==2.1.0"' in result
        assert '"${MINI_BIN}" --help >/dev/null' in result
        assert "mini-swe-agent-runtime.tar.gz" not in result
        assert "TESTPRISM_MINI_SWE_AGENT_BUNDLE" not in result
        assert "mini-swe-agent requires Python >=3.10" in result
        assert "INSTALL_SUCCESS" in result

        # No unrendered jinja syntax
        assert "{{" not in result
        assert "}}" not in result
        assert "{%" not in result
        assert "%}" not in result

    def test_template_rendering_without_version(self):
        """Test template rendering when version is not provided."""
        if not self.template_path.exists():
            pytest.skip(f"Template not found: {self.template_path}")

        context = {}
        result = render_setup_script(self.template_path, context)

        # Basic structure checks
        assert result.startswith("#!/bin/bash")
        assert "set -euo pipefail" in result
        assert 'SYSTEM_PYTHON="/usr/bin/python3"' in result
        assert 'SYSTEM_PYTHON="$(command -v python3)"' in result
        assert "apt-get update" in result
        assert "apt-get install -y python3 python3-pip python3-venv" in result
        assert '"${SYSTEM_PYTHON}" - <<\'PY\'' in result
        assert '"${SYSTEM_PYTHON}" -m venv "${VENV_DIR}"' in result
        assert '"${VENV_DIR}/bin/pip" install --upgrade pip' in result
        assert 'RUNTIME_MARKER="/opt/testprism-mini-swe-agent-runtime.json"' in result
        assert 'EXPECTED_MINI_SWE_VERSION="${TESTPRISM_MINI_SWE_VERSION_SPEC:-2.4.6}"' in result
        assert "Using preinstalled mini-swe-agent runtime" in result

        # No-version-specific checks
        assert '"${VENV_DIR}/bin/pip" install "mini-swe-agent==2.4.6"' in result
        assert '"${MINI_BIN}" --help >/dev/null' in result
        assert "INSTALL_SUCCESS" in result

        # No unrendered jinja syntax
        assert "{{" not in result
        assert "}}" not in result
        assert "{%" not in result
        assert "%}" not in result

    def test_template_rendering_with_empty_version(self):
        """Test template rendering when version is empty string."""
        if not self.template_path.exists():
            pytest.skip(f"Template not found: {self.template_path}")

        context = {"version": ""}
        result = render_setup_script(self.template_path, context)

        # Empty string should be falsy in jinja2, so should use fallback
        assert '"${VENV_DIR}/bin/pip" install "mini-swe-agent==2.4.6"' in result

    def test_template_rendering_with_none_version(self):
        """Test template rendering when version is None."""
        if not self.template_path.exists():
            pytest.skip(f"Template not found: {self.template_path}")

        context = {"version": None}
        result = render_setup_script(self.template_path, context)

        # None should be falsy in jinja2, so should use fallback
        assert '"${VENV_DIR}/bin/pip" install "mini-swe-agent==2.4.6"' in result


def test_default_version_is_pinned_to_2_4_6():
    agent = MiniSweAgent(
        model_name="openai/demo-model",
        api_key="test-key",
        base_url="https://example.test/v1",
    )

    assert agent.version == "2.4.6"


def test_profile_openai_base_url_is_rewritten_for_container_reachability():
    with patch.object(
        DockerComposeManager,
        "container_reachable_loopback_url",
        classmethod(lambda cls, url: "http://172.17.0.1:19142/v1"),
    ):
        agent = MiniSweAgent(
            model_name="openai/gpt-5.4-20260305",
            profile_env={
                "OPENAI_API_KEY": "dummy",
                "OPENAI_API_BASE": "http://127.0.0.1:9846/v1",
            },
        )
        env_setup = agent._create_env_setup_file()

    assert "export OPENAI_API_BASE=http://172.17.0.1:19142/v1" in env_setup
    assert "export OPENAI_API_BASE=http://127.0.0.1:9846/v1" not in env_setup
    assert "export TESTPRISM_MINI_SWE_MODEL=openai/gpt-5.4-20260305" in env_setup
    assert "export TESTPRISM_MINI_SWE_VERSION_SPEC=2.4.6" in env_setup
    assert "export TESTPRISM_MINI_SWE_RUNTIME_KEY=pypi-2.4.6-py311-v1" in env_setup


def test_system_prompt_uses_native_agent_config_file():
    agent = MiniSweAgent(
        model_name="openai/demo-model",
        api_key="test-key",
        base_url="https://example.test/v1",
        system_prompt="authoritative policy",
    )

    env_setup = agent._create_env_setup_file()
    install_script = agent._install_agent_script_path.read_text(encoding="utf-8")

    assert (
        "export TESTPRISM_SYSTEM_PROMPT_PATH=/installed-agent/system-prompt.txt"
        in env_setup
    )
    assert 'PYTHON_BIN="${VENV_DIR}/bin/python"' in install_script
    assert r'"\${PYTHON_BIN}" - "\${SYSTEM_PROMPT_PATH}"' in install_script
    assert r'"\${VENV_DIR}/bin/python"' not in install_script
    assert 'CONFIG_ARGS=(-c mini.yaml)' in install_script
    assert 'CONFIG_ARGS+=(-c "\\${SYSTEM_PROMPT_CONFIG}")' in install_script
    assert "authoritative policy" not in env_setup + install_script


def test_testbench_config_keeps_workflow_without_solution_direction():
    config = yaml.safe_load(MiniSweAgent.TESTBENCH_CONFIG_PATH.read_text())
    instance_template = config["agent"]["instance_template"]

    assert instance_template.lstrip().startswith("{{task}}")
    assert "Recommended Workflow" in instance_template
    assert "Command Execution Rules" in instance_template
    assert "Please solve this issue" not in instance_template
    assert "resolve the issue" not in instance_template
    assert "Edit the source code" not in instance_template


def test_testbench_config_is_selected_without_changing_default_config():
    agent = MiniSweAgent(
        model_name="openai/demo-model",
        api_key="test-key",
        base_url="https://example.test/v1",
        system_prompt="testbench policy",
    )

    assert "TESTPRISM_MINI_SWE_BASE_CONFIG" not in agent._runtime_env()

    agent.enable_testbench_prompt_config()

    assert agent._runtime_env()["TESTPRISM_MINI_SWE_BASE_CONFIG"] == (
        "/installed-agent/mini-swe-testbench.yaml"
    )


def test_testbench_config_is_copied_as_an_install_asset():
    copied: list[tuple[Path, str, str]] = []

    class CopySession:
        def copy_to_container(
            self,
            path: Path,
            container_dir: str,
            container_filename: str,
        ) -> None:
            copied.append((path, container_dir, container_filename))

    agent = MiniSweAgent(
        model_name="openai/demo-model",
        api_key="test-key",
        base_url="https://example.test/v1",
    )
    agent.enable_testbench_prompt_config()

    agent._copy_install_assets_to_container(CopySession())

    assert copied == [
        (
            MiniSweAgent.TESTBENCH_CONFIG_PATH,
            "/installed-agent",
            "mini-swe-testbench.yaml",
        )
    ]


def test_generated_runner_resolves_python_path_with_system_prompt(tmp_path: Path):
    template_path = TestMiniSweAgentTemplate().template_path
    setup_script = render_setup_script(template_path, {"version": "2.4.6"})
    heredoc_start = "cat > /installed-agent/run-mini-swe-agent.sh <<SH\n"
    body_start = setup_script.index(heredoc_start) + len(heredoc_start)
    body_end = setup_script.index("\nSH\n", body_start)
    runner_body = setup_script[body_start:body_end]

    venv_dir = tmp_path / "venv"
    python_bin = venv_dir / "bin" / "python"
    python_bin.parent.mkdir(parents=True)
    python_bin.write_text("#!/bin/sh\ncat >/dev/null\n", encoding="utf-8")
    python_bin.chmod(0o755)

    mini_bin = tmp_path / "mini"
    mini_bin.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$TEST_CAPTURE"\n',
        encoding="utf-8",
    )
    mini_bin.chmod(0o755)

    runner = tmp_path / "run-mini-swe-agent.sh"
    generate_runner = "\n".join(
        (
            "set -eu",
            f"VENV_DIR={shlex.quote(str(venv_dir))}",
            f"MINI_BIN={shlex.quote(str(mini_bin))}",
            f"cat > {shlex.quote(str(runner))} <<SH",
            runner_body,
            "SH",
        )
    )
    subprocess.run(["bash", "-c", generate_runner], check=True)

    task_instruction = tmp_path / "task-instruction.txt"
    task_instruction.write_text("task body", encoding="utf-8")
    system_prompt = tmp_path / "system-prompt.txt"
    system_prompt.write_text("authoritative policy", encoding="utf-8")
    capture_path = tmp_path / "mini-args.txt"
    completed = subprocess.run(
        ["bash", str(runner)],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "TESTPRISM_AGENT_LOG_DIR": str(tmp_path / "logs"),
            "TESTPRISM_TASK_INSTRUCTION_PATH": str(task_instruction),
            "TESTPRISM_MINI_SWE_MODEL": "openai/demo-model",
            "TESTPRISM_SYSTEM_PROMPT_PATH": str(system_prompt),
            "TEST_CAPTURE": str(capture_path),
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert "unbound variable" not in completed.stderr
    assert f'PYTHON_BIN="{python_bin}"' in runner.read_text(encoding="utf-8")
    assert capture_path.read_text(encoding="utf-8").splitlines()[:4] == [
        "-c",
        "mini.yaml",
        "-c",
        "/installed-agent/mini-swe-system-prompt.yaml",
    ]

    completed = subprocess.run(
        ["bash", str(runner)],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "TESTPRISM_AGENT_LOG_DIR": str(tmp_path / "logs"),
            "TESTPRISM_TASK_INSTRUCTION_PATH": str(task_instruction),
            "TESTPRISM_MINI_SWE_MODEL": "openai/demo-model",
            "TESTPRISM_SYSTEM_PROMPT_PATH": str(system_prompt),
            "TESTPRISM_MINI_SWE_BASE_CONFIG": (
                "/installed-agent/mini-swe-testbench.yaml"
            ),
            "TEST_CAPTURE": str(capture_path),
        },
    )

    assert completed.returncode == 0, completed.stderr
    assert capture_path.read_text(encoding="utf-8").splitlines()[:4] == [
        "-c",
        "/installed-agent/mini-swe-testbench.yaml",
        "-c",
        "/installed-agent/mini-swe-system-prompt.yaml",
    ]
