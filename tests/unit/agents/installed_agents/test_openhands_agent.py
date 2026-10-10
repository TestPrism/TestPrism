"""Unit tests for OpenHandsAgent template rendering with git_version and version
variables."""

import inspect
import json
from pathlib import Path

import pytest
from jinja2 import Template

from testprism.agents.installed_agents.openhands.openhands_agent import (
    OpenHandsAgent,
)
from testprism.utils.template_utils import (
    get_template_path,
    render_setup_script,
)


class TestOpenHandsAgentTemplate:
    """Test OpenHandsAgent template rendering with complex conditional logic."""

    @property
    def template_path(self) -> Path:
        """Get the path to the openhands template."""
        agent_file = inspect.getfile(OpenHandsAgent)
        agent_dir = Path(agent_file).parent
        return get_template_path(agent_dir, "openhands-setup.sh.j2")

    def test_template_rendering_with_version(self):
        """Test template rendering when version is provided (PyPI install)."""
        if not self.template_path.exists():
            pytest.skip(f"Template not found: {self.template_path}")

        context = {"version": "0.8.2"}
        result = render_setup_script(self.template_path, context)

        # Version-specific checks (PyPI install)
        assert "uv_pip_install_with_fallback openhands-ai==0.8.2" in result
        assert "Installing from git version:" not in result
        assert "git+https://github.com/All-Hands-AI/OpenHands.git@" not in result

        # No unrendered jinja syntax
        assert "{{" not in result
        assert "}}" not in result
        assert "{%" not in result
        assert "%}" not in result

    def test_template_rendering_with_git_version(self):
        """Test template rendering when git_version is provided (git install)."""
        if not self.template_path.exists():
            pytest.skip(f"Template not found: {self.template_path}")

        context = {"git_version": "a6301075ecb4bde5507df6d6af3854b8a3cd46f0"}
        result = render_setup_script(self.template_path, context)

        # Basic structure checks
        assert result.startswith("#!/bin/bash")
        assert "apt-get update" in result
        assert "apt-get install -y curl git" in result

        # Git version-specific checks
        assert (
            "Installing from git version: a6301075ecb4bde5507df6d6af3854b8a3cd46f0"
            in result
        )
        assert (
            "uv_pip_install_with_fallback "
            "git+https://github.com/All-Hands-AI/OpenHands.git@"
            "a6301075ecb4bde5507df6d6af3854b8a3cd46f0" in result
        )
        assert "Installing from PyPI version:" not in result
        assert "uv_pip_install_with_fallback openhands-ai==" not in result

        # No unrendered jinja syntax
        assert "{{" not in result
        assert "}}" not in result
        assert "{%" not in result
        assert "%}" not in result

    def test_template_rendering_with_both_version_and_git_version(self):
        """
        Test template rendering when both version and git_version are provided
        (git_version takes precedence).
        """
        if not self.template_path.exists():
            pytest.skip(f"Template not found: {self.template_path}")

        context = {
            "version": "0.8.2",
            "git_version": "a6301075ecb4bde5507df6d6af3854b8a3cd46f0",
        }
        result = render_setup_script(self.template_path, context)

        # git_version should take precedence
        assert (
            "Installing from git version: a6301075ecb4bde5507df6d6af3854b8a3cd46f0"
            in result
        )
        assert (
            "uv_pip_install_with_fallback "
            "git+https://github.com/All-Hands-AI/OpenHands.git@"
            "a6301075ecb4bde5507df6d6af3854b8a3cd46f0" in result
        )
        assert "Installing from PyPI version:" not in result
        assert "uv_pip_install_with_fallback openhands-ai==" not in result

    def test_template_rendering_with_neither_version_nor_git_version(self):
        """Test template rendering when neither version nor git_version is provided."""
        if not self.template_path.exists():
            pytest.skip(f"Template not found: {self.template_path}")

        context = {}
        result = render_setup_script(self.template_path, context)

        # Should fall back to a pinned PyPI default.
        assert "uv_pip_install_with_fallback openhands-ai==0.51.1" in result
        assert "Installing from git version:" not in result
        assert "if ! command -v python >/dev/null 2>&1; then" in result
        assert 'ln -s "$OPENHANDS_VENV/bin/python" /usr/local/bin/python' in result

    def test_template_checks_preinstalled_runtime_before_network_install(self):
        if not self.template_path.exists():
            pytest.skip(f"Template not found: {self.template_path}")

        result = render_setup_script(self.template_path, {})

        preinstall_check = result.index("Using preinstalled OpenHands runtime")
        apt_update = result.index("apt-get update")

        assert preinstall_check < apt_update
        assert (
            'OPENHANDS_RUNTIME_MARKER="/opt/testprism-openhands-runtime.json"'
            in result
        )
        assert 'EXPECTED_OPENHANDS_VERSION="0.51.1"' in result
        assert (
            'EXPECTED_OPENHANDS_RUNTIME_KEY='
            '"pypi-0.51.1-orjson-v1"' in result
        )
        assert "import openhands" in result
        assert "import orjson" in result


class TestOpenHandsAgentEnvCompatibility:
    def test_profile_environment_keeps_endpoint_isolated_and_adds_model(self) -> None:
        agent = OpenHandsAgent(
            model_name="azure/gpt-4.1-mini-20250414",
            profile_env={
                "LLM_API_KEY": "dummy",
                "LLM_BASE_URL": "http://172.17.0.1:23437",
                "LLM_API_VERSION": "2025-04-01-preview",
            },
        )

        setup = agent._create_env_setup_file()

        assert "export LLM_MODEL=azure/gpt-4.1-mini-20250414" in setup
        assert "export LLM_BASE_URL=http://172.17.0.1:23437" in setup
        assert "export LLM_API_KEY=" not in setup
        assert "export AZURE_OPENAI_AD_TOKEN=dummy" in setup
        assert "export RUNTIME=local" in setup

    def test_system_prompt_uses_native_agent_template_config(self) -> None:
        agent = OpenHandsAgent(
            model_name="openai/gpt-4.1",
            profile_env={"LLM_API_KEY": "test-key"},
            system_prompt="authoritative {{ policy }}",
        )

        setup = agent._create_env_setup_file()

        assert (
            "export AGENT_SYSTEM_PROMPT_FILENAME="
            "/installed-agent/openhands-system-prompt.j2" in setup
        )

        copied: dict[str, str] = {}

        class _CopySession:
            def copy_to_container(self, source: Path, **kwargs: str) -> None:
                copied[kwargs["container_filename"]] = source.read_text(
                    encoding="utf-8"
                )

        agent._copy_system_prompt_to_container(_CopySession())

        template = copied["openhands-system-prompt.j2"]
        assert Template(template).render() == "authoritative {{ policy }}"

    def test_uses_readme_openai_base_url_fallback(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
        monkeypatch.setenv("OPENAI_BASE_URL", "https://example.openai-proxy/v1")
        monkeypatch.setenv("LLM_MODEL", "openai/gpt-4o")

        env = OpenHandsAgent()._env

        assert env["LLM_API_KEY"] == "sk-openai"
        assert env["LLM_MODEL"] == "openai/gpt-4o"
        assert env["LLM_BASE_URL"] == "https://example.openai-proxy/v1"

    def test_uses_readme_azure_fallbacks(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
        monkeypatch.setenv("DEFAULT_MODEL", "azure/gpt-4o-20241120")
        monkeypatch.setenv("AZURE_API_BASE", "https://azure.example.net")
        monkeypatch.setenv("AZURE_API_VERSION", "2025-04-01-preview")

        env = OpenHandsAgent()._env

        assert env["LLM_API_KEY"] == "sk-openai"
        assert env["LLM_MODEL"] == "azure/gpt-4o-20241120"
        assert env["LLM_BASE_URL"] == "https://azure.example.net"
        assert env["LLM_API_VERSION"] == "2025-04-01-preview"

    def test_prefers_anthropic_key_for_anthropic_model(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")

        env = OpenHandsAgent(model_name="anthropic/claude-4-sonnet")._env

        assert env["LLM_API_KEY"] == "sk-ant"

    def test_agent_config_json_supplies_llm_credentials(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for key in (
            "LLM_MODEL",
            "LLM_API_KEY",
            "ANTHROPIC_MODEL",
            "DEFAULT_MODEL",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
        ):
            monkeypatch.delenv(key, raising=False)

        cfg = tmp_path / "openhands.json"
        cfg.write_text(
            json.dumps(
                {
                    "model": "openai/gpt-4o-mini",
                    "env": {
                        "LLM_API_KEY": "from-config",
                        "LLM_BASE_URL": "https://example.llm/v1",
                    },
                }
            ),
            encoding="utf-8",
        )

        env = OpenHandsAgent(config=str(cfg))._env

        assert env["LLM_MODEL"] == "openai/gpt-4o-mini"
        assert env["LLM_API_KEY"] == "from-config"
        assert env["LLM_BASE_URL"] == "https://example.llm/v1"

    def test_cli_model_overrides_config_model(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for key in (
            "LLM_MODEL",
            "LLM_API_KEY",
            "OPENAI_API_KEY",
        ):
            monkeypatch.delenv(key, raising=False)

        cfg = tmp_path / "openhands.json"
        cfg.write_text(
            json.dumps(
                {
                    "model": "openai/gpt-4o-mini",
                    "env": {"LLM_API_KEY": "k"},
                }
            ),
            encoding="utf-8",
        )

        env = OpenHandsAgent(
            model_name="anthropic/claude-4-sonnet",
            config=str(cfg),
        )._env

        assert env["LLM_MODEL"] == "anthropic/claude-4-sonnet"
        assert env["LLM_API_KEY"] == "k"

    def test_run_command_mounts_agent_tests_for_testbench(self) -> None:
        """OpenHands sandbox must bind the same path as ``{workdir}/agent-tests``."""
        agent = OpenHandsAgent()
        cmds = agent._run_agent_commands("do something")
        assert len(cmds) == 1
        assert '${_tw}/agent-tests:${_tw}/agent-tests:rw' in cmds[0].command
        assert (
            "/tmp/testprism-agent-output:/tmp/testprism-agent-output:rw"
            in cmds[0].command
        )
        assert "/tmp/testprism-review:/tmp/testprism-review:ro" in cmds[0].command
        assert 'export SANDBOX_VOLUMES="${_tw}:/workspace:rw,' in cmds[0].command
        assert "TASK_WORKDIR" in cmds[0].command
