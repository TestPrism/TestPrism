from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from testprism.agents.base_agent import (
    SystemPromptMode,
    UnsupportedSystemPromptError,
)
from testprism.agents.installed_agents.aider.aider_agent import AiderAgent
from testprism.agents.installed_agents.cursor_cli.cursor_cli_agent import (
    CursorCliAgent,
)
from testprism.agents.installed_agents.grok_cli.grok_cli_agent import GrokCliAgent
from testprism.agents.mcp_agents.goose_mcp_agent import GooseMCPAgent
from testprism.agents.mcp_agents.mcp_terminus import MCPTerminus
from testprism.agents.naive_agent import NaiveAgent
from testprism.agents.null_agent import NopAgent
from testprism.agents.oracle_agent import OracleAgent
from testprism.agents.terminus_1 import Terminus
from testprism.agents.terminus_2 import Terminus2

SYSTEM_PROMPT = "authoritative system policy"
USER_PROMPT = "untrusted user task"


@pytest.mark.parametrize(
    "factory",
    [
        lambda: AiderAgent(system_prompt=SYSTEM_PROMPT),
        lambda: CursorCliAgent(model_name="probe", system_prompt=SYSTEM_PROMPT),
        lambda: GrokCliAgent(model_name="probe", system_prompt=SYSTEM_PROMPT),
        lambda: GooseMCPAgent(
            model_name="openai/probe",
            docker_image_name="unused",
            docker_compose_path=Path("unused"),
            system_prompt=SYSTEM_PROMPT,
        ),
    ],
)
def test_agents_without_native_system_channel_fail_before_runtime(factory) -> None:
    with pytest.raises(UnsupportedSystemPromptError):
        factory()


def test_non_prompting_validation_agents_are_explicit() -> None:
    assert NopAgent.consumes_task_instruction is False
    assert OracleAgent.consumes_task_instruction is False


def test_naive_sends_system_and_user_as_distinct_messages() -> None:
    llm = MagicMock()
    llm.call.return_value = '{"commands": [], "explanation": "done"}'
    llm.count_tokens.return_value = 1
    session = MagicMock()
    agent = NaiveAgent(llm=llm, system_prompt=SYSTEM_PROMPT)

    agent.perform_task(USER_PROMPT, session)

    call = llm.call.call_args
    assert call.kwargs["message_history"] == [
        {"role": "system", "content": SYSTEM_PROMPT}
    ]
    assert USER_PROMPT in call.args[0]
    assert SYSTEM_PROMPT not in call.args[0]


@pytest.mark.parametrize(
    ("agent_class", "chat_import", "terminal_method"),
    [
        (Terminus, "testprism.agents.terminus_1.Chat", "capture_pane"),
        (
            Terminus2,
            "testprism.agents.terminus_2.terminus_2.Chat",
            "get_incremental_output",
        ),
    ],
)
def test_terminus_variants_seed_chat_with_native_system_message(
    agent_class, chat_import: str, terminal_method: str
) -> None:
    agent = agent_class(
        model_name="openai/probe",
        max_episodes=1,
        system_prompt=SYSTEM_PROMPT,
    )
    session = MagicMock()
    getattr(session, terminal_method).return_value = "terminal state"
    fake_chat = MagicMock(total_input_tokens=0, total_output_tokens=0)

    with (
        patch(chat_import, return_value=fake_chat) as chat_class,
        patch.object(agent, "_run_agent_loop") as run_loop,
    ):
        agent.perform_task(USER_PROMPT, session)

    chat_class.assert_called_once_with(
        agent._llm,
        initial_messages=[{"role": "system", "content": SYSTEM_PROMPT}],
    )
    initial_prompt = run_loop.call_args.args[0]
    assert USER_PROMPT in initial_prompt
    assert SYSTEM_PROMPT not in initial_prompt


def test_mcp_terminus_declares_native_role_system_delivery() -> None:
    agent = MCPTerminus(model_name="openai/probe", system_prompt=SYSTEM_PROMPT)

    assert agent.native_system_prompt_modes == frozenset(SystemPromptMode)
    assert agent._system_prompt_messages() == [
        {"role": "system", "content": SYSTEM_PROMPT}
    ]
