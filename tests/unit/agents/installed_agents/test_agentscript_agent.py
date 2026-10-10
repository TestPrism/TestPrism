import json
from pathlib import Path

import pytest

from testprism.agents.installed_agents.tex_t.agentscript_agent import (
    AgentScriptAgent,
)


def test_config_model_overrides_cloudgpt_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = tmp_path / "agentscript.json"
    cfg.write_text(
        json.dumps(
            {
                "provider": "openai",
                "model": "gpt-5.4-20260305",
                "env": {
                    "OPENAI_API_KEY": "dummy",
                    "OPENAI_BASE_URL": "http://127.0.0.1:23437/openai/v1",
                },
            }
        ),
        encoding="utf-8",
    )

    def fail_get_cloudgpt_token() -> str:
        raise AssertionError("config-backed agentscript should not request CloudGPT")

    monkeypatch.setattr(
        "testprism.agents.installed_agents.tex_t.agentscript_agent.get_cloudgpt_token",
        fail_get_cloudgpt_token,
    )

    env = AgentScriptAgent(
        model_name="azure/gpt-5.4-20260305",
        config=str(cfg),
    )._env

    assert env["AGENTSCRIPT_PROVIDER"] == "openai"
    assert env["AGENTSCRIPT_MODEL"] == "gpt-5.4-20260305"
    assert env["OPENAI_API_KEY"] == "dummy"
    assert env["OPENAI_BASE_URL"] == "http://127.0.0.1:23437/openai/v1"


def test_system_prompt_is_passed_to_runner_as_a_separate_native_input() -> None:
    agent = AgentScriptAgent(
        model_name="openai/demo",
        api_key="test-key",
        base_url="https://example.test/v1",
        system_prompt="authoritative policy",
        system_prompt_mode="append",
    )

    command = agent._run_agent_commands("task body")[0].command

    assert "TESTPRISM_SYSTEM_PROMPT_PATH=/installed-agent/system-prompt.txt" in command
    assert "TESTPRISM_SYSTEM_PROMPT_MODE=append" in command
    assert "authoritative policy" not in command
