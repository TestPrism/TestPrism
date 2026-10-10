"""Unit tests for simple agents that use standard {{ version }} templates."""

import inspect
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from testprism.agents.agent_factory import AgentFactory
from testprism.agents.agent_name import AgentName
from testprism.agents.installed_agents import KimiCodeAgent as ExportedKimiCodeAgent
from testprism.agents.installed_agents.claude_code.claude_code_agent import (
    ClaudeCodeAgent,
)
from testprism.agents.installed_agents.codex.codex_agent import CodexAgent
from testprism.agents.installed_agents.cursor_cli.cursor_cli_agent import (
    CursorCliAgent,
)
from testprism.agents.installed_agents.gemini_cli.gemini_cli_agent import (
    GeminiCliAgent,
)
from testprism.agents.installed_agents.goose.goose_agent import GooseAgent
from testprism.agents.installed_agents.grok_cli.grok_cli_agent import GrokCliAgent
from testprism.agents.installed_agents.kimi_code.kimi_code_agent import KimiCodeAgent
from testprism.agents.installed_agents.opencode.opencode_agent import OpenCodeAgent
from testprism.agents.installed_agents.qwen_code.qwen_code import QwenCodeAgent
from testprism.terminal.docker_compose_manager import DockerComposeManager
from testprism.utils.template_utils import (
    get_template_path,
    render_prompt_template,
    render_setup_script,
)


class TestSimpleAgentTemplates:
    """Test agents that use simple {{ version }} templates."""

    @pytest.mark.parametrize(
        "agent_class,template_name",
        [
            (CodexAgent, "codex-setup.sh.j2"),
            (ClaudeCodeAgent, "claude-code-setup.sh.j2"),
            (GeminiCliAgent, "gemini-cli-setup.sh.j2"),
            (GrokCliAgent, "grok-cli-setup.sh.j2"),
            (KimiCodeAgent, "kimi-code-setup.sh.j2"),
            (OpenCodeAgent, "opencode-setup.sh.j2"),
        ],
    )
    def test_template_rendering_with_version(self, agent_class, template_name):
        """Test that templates render correctly with version."""
        # Get the template path using the same logic as the agent classes
        agent_file = inspect.getfile(agent_class)
        agent_dir = Path(agent_file).parent
        template_path = get_template_path(agent_dir, template_name)

        if not template_path.exists():
            pytest.skip(f"Template not found: {template_path}")

        # Test with version
        context = {"version": "1.2.3"}
        result = render_setup_script(template_path, context)

        # Common assertions
        assert result.startswith("#!/bin/bash")
        assert "apt-get update" in result
        assert "@1.2.3" in result

        # Ensure no unrendered jinja variables remain
        assert "{{" not in result
        assert "}}" not in result

    @pytest.mark.parametrize(
        "agent_class",
        [
            CodexAgent,
            ClaudeCodeAgent,
            GeminiCliAgent,
            GrokCliAgent,
            OpenCodeAgent,
        ],
    )
    def test_agent_default_template_variables(self, agent_class):
        """Test these agents default to latest version when no version is specified."""
        if agent_class == CodexAgent:
            with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
                agent = agent_class(model_name="gpt-4")
        elif agent_class == ClaudeCodeAgent:
            with patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "test-key"}):
                agent = agent_class()
        elif agent_class == GeminiCliAgent:
            with patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}):
                agent = agent_class(model_name="gemini-pro")
        elif agent_class == GrokCliAgent:
            with patch.dict(os.environ, {"GROK_API_KEY": "test-key"}):
                agent = agent_class(model_name="grok-beta")
        elif agent_class == OpenCodeAgent:
            with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
                agent = agent_class(model_name="openai/gpt-4")

        variables = agent._get_template_variables()
        assert variables["version"] == "latest"
        if agent_class == CodexAgent:
            assert "config_toml_body" in variables
            assert "auth_json_body" in variables

    def test_codex_renders_custom_config_toml_from_agent_kwargs(self):
        with patch.object(
            DockerComposeManager,
            "container_reachable_loopback_url",
            classmethod(lambda cls, url: "http://172.17.0.1:19142/v1"),
        ):
            agent = CodexAgent(
                model_name="gpt-5.4-20260305",
                profile_env={
                    "CLOUDGPT_BASE_URL": "http://127.0.0.1:4142/v1",
                    "CLOUDGPT_API_KEY": "test-token",
                },
                codex_config={
                    "model_provider": "cloudgpt",
                    "model_reasoning_effort": "high",
                    "model_reasoning_summary": "detailed",
                    "approval_policy": "never",
                    "sandbox_mode": "danger-full-access",
                    "disable_response_storage": True,
                    "features": {"goals": True},
                    "model_providers": {
                        "cloudgpt": {
                            "name": "CloudGPT via forwarded LiteLLM",
                            "base_url_env": "CLOUDGPT_BASE_URL",
                            "wire_api": "responses",
                            "experimental_bearer_token_env": "CLOUDGPT_API_KEY",
                        }
                    }
                },
            )

        install_script = agent._install_agent_script_path.read_text(encoding="utf-8")
        assert 'model_provider = "cloudgpt"' in install_script
        assert 'model = "gpt-5.4-20260305"' in install_script
        assert 'model_reasoning_effort = "high"' in install_script
        assert 'model_reasoning_summary = "detailed"' in install_script
        assert 'approval_policy = "never"' in install_script
        assert 'sandbox_mode = "danger-full-access"' in install_script
        assert "disable_response_storage = true" in install_script
        assert "[features]" in install_script
        assert "goals = true" in install_script
        assert "[model_providers.cloudgpt]" in install_script
        assert 'base_url = "http://172.17.0.1:19142/v1"' in install_script
        assert 'experimental_bearer_token = "${CLOUDGPT_API_KEY}"' in install_script

        command = agent._run_agent_commands("instruction")[0].command
        assert "--config model_provider=cloudgpt " in command
        assert "--sandbox danger-full-access " in command
        assert "set -o pipefail && codex exec" in command
        assert '"$(cat /installed-agent/task-instruction.txt)"' not in command
        assert "-- - < /installed-agent/task-instruction.txt 2>&1" in command

    def test_codex_reads_instruction_from_stdin_file(self):
        agent = CodexAgent(
            model_name="gpt-5.4-20260305",
            profile_env={"OPENAI_API_KEY": "test-key"},
        )

        command = agent._run_agent_commands("instruction")[0].command

        assert "set -o pipefail && codex exec" in command
        assert '"$(cat /installed-agent/task-instruction.txt)"' not in command
        assert "-- - < /installed-agent/task-instruction.txt 2>&1" in command

    def test_codex_keeps_auth_json_when_only_top_level_settings_are_customized(self):
        agent = CodexAgent(
            model_name="gpt-5.4-20260305",
            profile_env={"OPENAI_API_KEY": "test-key"},
            codex_config={
                "approval_policy": "never",
                "disable_response_storage": True,
            },
        )

        install_script = agent._install_agent_script_path.read_text(encoding="utf-8")
        assert 'approval_policy = "never"' in install_script
        assert "disable_response_storage = true" in install_script
        assert 'cat <<EOF >"$HOME/.codex/auth.json"' in install_script
        assert '"OPENAI_API_KEY": "${OPENAI_API_KEY}"' in install_script

    def test_kimi_registration_surfaces(self):
        agent = AgentFactory.get_agent(
            agent_name=AgentName.KIMI_CODE,
            model_name="kimi-for-coding",
            api_key="test-key",
        )

        assert AgentName.KIMI_CODE.value == "kimi-code"
        assert AgentFactory.get_agent_class(AgentName.KIMI_CODE) is KimiCodeAgent
        assert isinstance(agent, KimiCodeAgent)
        assert ExportedKimiCodeAgent is KimiCodeAgent

    def test_kimi_renders_config_toml_from_agent_kwargs(self):
        with patch.object(
            DockerComposeManager,
            "container_reachable_loopback_url",
            classmethod(lambda cls, url: "http://172.17.0.1:19142/v1"),
        ):
            agent = KimiCodeAgent(
                model_name="kimi-for-coding",
                api_key="test-token",
                base_url="http://127.0.0.1:4142/v1",
                reasoning_level="high",
            )

        install_script = agent._install_agent_script_path.read_text(encoding="utf-8")
        assert 'default_model = "testprism/default"' in install_script
        assert 'default_permission_mode = "auto"' in install_script
        assert "telemetry = false" in install_script
        assert "[providers.testprism]" in install_script
        assert 'type = "kimi"' in install_script
        assert 'api_key = "${KIMI_API_KEY}"' in install_script
        assert 'base_url = "http://172.17.0.1:19142/v1"' in install_script
        assert '[models."testprism/default"]' in install_script
        assert 'provider = "testprism"' in install_script
        assert 'model = "kimi-for-coding"' in install_script
        assert "max_context_size = 262144" in install_script
        assert 'capabilities = [ "thinking", "tool_use" ]' in install_script
        assert "[thinking]" in install_script
        assert 'effort = "high"' in install_script

    def test_kimi_merges_explicit_api_key_with_profile_environment(self):
        agent = KimiCodeAgent(
            model_name="kimi-for-coding",
            api_key="test-token",
            profile_env={"PROBE_SECRET": "container-only-sentinel"},
        )

        env_setup = agent._create_env_setup_file()

        assert "export PROBE_SECRET=" in env_setup
        assert "container-only-sentinel" in env_setup
        assert "export KIMI_API_KEY=" in env_setup
        assert "test-token" in env_setup

    def test_claude_preinstalled_fast_path_returns_when_sourced(self, tmp_path):
        """The prebuilt runtime path must not terminate the tmux shell."""

        template_path = (
            Path(inspect.getfile(ClaudeCodeAgent)).parent / "claude-code-setup.sh.j2"
        )
        install_script = tmp_path / "install-agent.sh"
        install_script.write_text(
            render_setup_script(template_path, {"version": "2.1.100"}),
            encoding="utf-8",
        )
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        claude = fake_bin / "claude"
        claude.write_text("#!/bin/sh\necho '2.1.100 (Claude Code)'\n", encoding="utf-8")
        claude.chmod(0o755)

        result = subprocess.run(
            [
                "bash",
                "-c",
                f"source {install_script}; printf '__SHELL_STILL_ALIVE__\\n'",
            ],
            env={**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"},
            capture_output=True,
            text=True,
            check=False,
        )

        assert result.returncode == 0
        assert "2.1.100 (Claude Code)" in result.stdout
        assert "__SHELL_STILL_ALIVE__" in result.stdout

    @pytest.mark.parametrize(
        ("relative_path", "binary", "message"),
        [
            ("aider/aider-setup.sh", "aider", "preinstalled Aider"),
            (
                "cursor_cli/cursor-cli-setup.sh",
                "cursor-agent",
                "preinstalled Cursor CLI",
            ),
            ("gemini_cli/gemini-cli-setup.sh.j2", "gemini", "preinstalled Gemini"),
            ("goose/goose-setup.sh", "goose", "preinstalled Goose"),
            ("grok_cli/grok-cli-setup.sh.j2", "grok", "preinstalled Grok"),
            (
                "kimi_code/kimi-code-setup.sh.j2",
                "kimi",
                "preinstalled Kimi Code runtime",
            ),
            ("opencode/opencode-setup.sh.j2", "opencode", "preinstalled OpenCode"),
            ("qwen_code/qwen_code-setup.sh.j2", "qwen", "preinstalled Qwen Code"),
        ],
    )
    def test_generic_layer_agents_prefer_preinstalled_runtime(
        self, relative_path, binary, message
    ):
        installed_agents_dir = Path(__file__).resolve().parents[4] / (
            "testprism/agents/installed_agents"
        )
        setup_script = (installed_agents_dir / relative_path).read_text(
            encoding="utf-8"
        )

        check = f"command -v {binary}"
        assert check in setup_script
        assert message in setup_script
        assert setup_script.index(check) < setup_script.index("apt-get update")


class TestPromptTemplateRendering:
    """Test prompt template rendering functionality."""

    def test_basic_prompt_template_rendering(self):
        """Test basic prompt template rendering with instruction variable."""

        template_content = """You are a helpful assistant.

Task: {{ instruction }}

Please complete the task carefully."""

        with tempfile.NamedTemporaryFile(mode="w", suffix=".j2", delete=False) as f:
            f.write(template_content)
            template_path = Path(f.name)

        try:
            instruction = "Create a simple hello world program"
            result = render_prompt_template(template_path, instruction)

            # Check that the instruction was properly rendered
            assert "Create a simple hello world program" in result
            assert "You are a helpful assistant." in result
            assert "Please complete the task carefully." in result

            # Ensure no unrendered jinja variables remain
            assert "{{" not in result
            assert "}}" not in result
        finally:
            template_path.unlink()

    def test_missing_instruction_variable_raises_error(self):
        """Test that template missing instruction variable raises ValueError."""
        import tempfile

        template_content = """You are a helpful assistant.

Task: {{ task_description }}

Please complete the task carefully.

Note: This template is missing the required instruction variable."""

        with tempfile.NamedTemporaryFile(mode="w", suffix=".j2", delete=False) as f:
            f.write(template_content)
            template_path = Path(f.name)

        try:
            instruction = "Some task instruction"

            with pytest.raises(ValueError) as exc_info:
                render_prompt_template(template_path, instruction)

            # Check that the error message explains the missing variable
            assert "must include an 'instruction' variable" in str(exc_info.value)
        finally:
            template_path.unlink()

    def test_nonexistent_template_raises_error(self):
        """Test that nonexistent template file raises FileNotFoundError."""
        template_path = Path("/nonexistent/path/template.j2")
        instruction = "Some task instruction"

        with pytest.raises(FileNotFoundError) as exc_info:
            render_prompt_template(template_path, instruction)

        assert "Template file not found" in str(exc_info.value)

    def test_agent_render_instruction_with_template(self):
        """Test that BaseAgent._render_instruction works with prompt templates."""
        template_content = """You are a helpful assistant.

Task: {{ instruction }}

Please complete the task carefully."""

        with tempfile.NamedTemporaryFile(mode="w", suffix=".j2", delete=False) as f:
            f.write(template_content)
            template_path = Path(f.name)

        try:
            with patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "test-key"}):
                agent = ClaudeCodeAgent(prompt_template=str(template_path))

            instruction = "Write a Python function to calculate factorial"
            result = agent._render_instruction(instruction)

            # Check that the instruction was properly rendered using the agent's method
            assert "Write a Python function to calculate factorial" in result
            assert "You are a helpful assistant." in result
            assert "Please complete the task carefully." in result

            # Ensure no unrendered jinja variables remain
            assert "{{" not in result
            assert "}}" not in result
        finally:
            template_path.unlink()

    def test_agent_render_instruction_without_template(self):
        """Test that original instruction is returned when no template is provided."""
        with patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "test-key"}):
            agent = ClaudeCodeAgent()  # No prompt_template provided

        instruction = "Write a Python function to calculate factorial"
        result = agent._render_instruction(instruction)

        # Should return the original instruction unchanged
        assert result == instruction

    def test_instruction_variable_edge_cases(self):
        """Test various edge cases for instruction variable validation."""

        test_cases = [
            # (template_content, should_pass, description)
            ("Hello {{instruction}} world!", True, "instruction without spaces"),
            ("Hello {{ instruction|upper }} world!", True, "instruction with filter"),
            (
                "{% if instruction %}{{ instruction }}{% endif %}",
                True,
                "instruction in conditional",
            ),
            (
                "{{ instruction ~ ' extra text' }}",
                True,
                "instruction with concatenation",
            ),
            (
                "{% set my_instruction = instruction %}{{ my_instruction }}",
                True,
                "instruction assigned to variable",
            ),
            ("<!-- {{ instruction }} -->", True, "instruction in comment-like syntax"),
            ("Hello world! No variables here.", False, "no instruction variable"),
            ("Hello {{ other_var }} world!", False, "different variable name"),
        ]

        for template_content, should_pass, description in test_cases:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".j2", delete=False) as f:
                f.write(template_content)
                template_path = Path(f.name)

            try:
                if should_pass:
                    # Should render successfully
                    result = render_prompt_template(template_path, "test instruction")
                    assert "{{" not in result, f"Failed for case: {description}"
                    assert "}}" not in result, f"Failed for case: {description}"
                else:
                    # Should raise ValueError
                    with pytest.raises(ValueError) as exc_info:
                        render_prompt_template(template_path, "test instruction")
                    assert "must include an 'instruction' variable" in str(
                        exc_info.value
                    )
            finally:
                template_path.unlink()

    def test_undefined_variables_caught_by_strict_undefined(self):
        """Test that StrictUndefined catches undefined variables during rendering."""

        template_content = """You are a helpful assistant.

Task: {{ instruction }}

Additional context: {{ undefined_var }}

Please complete the task carefully."""

        with tempfile.NamedTemporaryFile(mode="w", suffix=".j2", delete=False) as f:
            f.write(template_content)
            template_path = Path(f.name)

        try:
            instruction = "Some task instruction"

            with pytest.raises(ValueError) as exc_info:
                render_prompt_template(template_path, instruction)

            # Check that the error message mentions undefined variables
            assert "undefined variables" in str(exc_info.value)
            assert "undefined_var" in str(exc_info.value)
        finally:
            template_path.unlink()

    def test_complex_template_with_multiple_instruction_uses(self):
        """Test template that uses instruction variable in multiple ways."""

        template_content = """System: You are a helpful assistant.

Primary Task: {{ instruction }}

Task Summary: {{ instruction|truncate(50) }}

{% if instruction %}
Instructions provided: Yes
{% else %}
Instructions provided: No
{% endif %}

Formatted instruction: "{{ instruction|upper }}"
"""

        with tempfile.NamedTemporaryFile(mode="w", suffix=".j2", delete=False) as f:
            f.write(template_content)
            template_path = Path(f.name)

        try:
            instruction = "Create a Python script to process CSV files"
            result = render_prompt_template(template_path, instruction)

            # Check that all uses of instruction were rendered
            assert "Create a Python script to process CSV files" in result
            assert "CREATE A PYTHON SCRIPT TO PROCESS CSV FILES" in result
            assert "Instructions provided: Yes" in result

            # Ensure no unrendered jinja variables remain
            assert "{{" not in result
            assert "}}" not in result
        finally:
            template_path.unlink()


class TestInstructionFileCommands:
    @pytest.mark.parametrize(
        "agent_factory",
        [
            lambda: ClaudeCodeAgent(),
            lambda: CodexAgent(model_name="gpt-4"),
            lambda: CursorCliAgent(model_name="gpt-4"),
            lambda: KimiCodeAgent(model_name="kimi-for-coding", api_key="test-key"),
            lambda: QwenCodeAgent(model_name="qwen3-coder-plus"),
        ],
    )
    def test_long_instructions_are_not_inlined_in_commands(self, agent_factory):
        long_instruction = "long-instruction-" * 3000

        with patch.dict(
            os.environ,
            {
                "ANTHROPIC_AUTH_TOKEN": "test-key",
                "OPENAI_API_KEY": "test-key",
            },
            clear=False,
        ):
            agent = agent_factory()

        command = agent._run_agent_commands(long_instruction)[0].command

        assert long_instruction not in command
        assert str(agent.CONTAINER_INSTRUCTION_PATH) in command


class TestNativeSystemPromptCommands:
    def test_claude_code_uses_native_replace_and_append_file_flags(self):
        with patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "test-key"}):
            replace_agent = ClaudeCodeAgent(system_prompt="policy")
            append_agent = ClaudeCodeAgent(
                system_prompt="policy",
                system_prompt_mode="append",
            )

        replace_command = replace_agent._run_agent_commands("task")[0].command
        append_command = append_agent._run_agent_commands("task")[0].command

        assert (
            "--system-prompt-file /installed-agent/system-prompt.txt"
            in replace_command
        )
        assert (
            "--append-system-prompt-file /installed-agent/system-prompt.txt"
            in append_command
        )
        assert "policy" not in replace_command + append_command

    def test_codex_uses_model_instructions_file(self):
        agent = CodexAgent(
            model_name="gpt-5.4-20260305",
            profile_env={"OPENAI_API_KEY": "test-key"},
            system_prompt="policy",
        )

        command = agent._run_agent_commands("task")[0].command

        assert (
            "model_instructions_file=\"/installed-agent/system-prompt.txt\""
            in command
        )
        assert "policy" not in command

    def test_kimi_uses_agent_file_for_replace_and_append(self):
        replace_agent = KimiCodeAgent(
            model_name="kimi-for-coding",
            api_key="test-key",
            system_prompt="policy",
        )
        append_agent = KimiCodeAgent(
            model_name="kimi-for-coding",
            api_key="test-key",
            system_prompt="policy",
            system_prompt_mode="append",
        )

        replace_prompt = replace_agent._render_system_agent_file()
        append_prompt = append_agent._render_system_agent_file()
        replace_command = replace_agent._run_agent_commands("task")[0].command
        append_command = append_agent._run_agent_commands("task")[0].command

        assert "${base_prompt}" not in replace_prompt
        assert append_prompt.count("${base_prompt}") == 1
        assert append_prompt.index("${base_prompt}") < append_prompt.index("policy")
        assert "policy" in replace_prompt
        assert "policy" in append_prompt
        assert "--agent-file /installed-agent/kimi-system-agent.md" in replace_command
        assert "--agent-file /installed-agent/kimi-system-agent.md" in append_command
        assert "--model testprism/default" in replace_command
        assert "policy" not in replace_command + append_command

    def test_gemini_qwen_and_opencode_use_native_prompt_surfaces(self):
        gemini = GeminiCliAgent(model_name="gemini-pro", system_prompt="policy")
        qwen = QwenCodeAgent(model_name="qwen3-coder-plus", system_prompt="policy")
        opencode = OpenCodeAgent(
            model_name="openai/gpt-4",
            api_key="test-key",
            base_url="https://example.test/v1",
            system_prompt="policy",
        )

        gemini_command = gemini._run_agent_commands("task")[0].command
        qwen_command = qwen._run_agent_commands("task")[0].command
        opencode_command = opencode._run_agent_commands("task")[0].command

        assert "GEMINI_SYSTEM_MD=/installed-agent/system-prompt.txt" in gemini_command
        assert (
            '--system-prompt "$(cat /installed-agent/system-prompt.txt)"'
            in qwen_command
        )
        assert "OPENCODE_CONFIG_CONTENT=" in opencode_command
        assert (
            '"prompt": "{file:/installed-agent/system-prompt.txt}"'
            in opencode_command
        )
        assert "--agent testprism" in opencode_command

    def test_goose_recipe_keeps_system_and_user_channels_separate(self):
        agent = GooseAgent(
            model_name="openai/demo",
            api_key="test-key",
            base_url="https://example.test/v1",
            system_prompt="authoritative policy",
        )

        recipe = agent._create_recipe_yaml("task body")

        assert "instructions: authoritative policy" in recipe
        assert "prompt: task body" in recipe
