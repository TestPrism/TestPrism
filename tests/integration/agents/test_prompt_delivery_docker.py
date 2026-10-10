from __future__ import annotations

import base64
import json
import uuid
from pathlib import Path

import docker
import pytest
import yaml

from testprism.agents.base_agent import AgentResult
from testprism.agents.installed_agents.claude_code.claude_code_agent import (
    ClaudeCodeAgent,
)
from testprism.agents.installed_agents.codex.codex_agent import CodexAgent
from testprism.agents.installed_agents.gemini_cli.gemini_cli_agent import (
    GeminiCliAgent,
)
from testprism.agents.installed_agents.goose.goose_agent import GooseAgent
from testprism.agents.installed_agents.kimi_code.kimi_code_agent import (
    KimiCodeAgent,
)
from testprism.agents.installed_agents.mini_swe_agent.mini_swe_agent import (
    MiniSweAgent,
)
from testprism.agents.installed_agents.opencode.opencode_agent import OpenCodeAgent
from testprism.agents.installed_agents.openhands.openhands_agent import OpenHandsAgent
from testprism.agents.installed_agents.qwen_code.qwen_code import QwenCodeAgent
from testprism.agents.installed_agents.tex_t.agentscript_agent import (
    AgentScriptAgent,
)
from testprism.terminal.tmux_session import TmuxSession

IMAGE = "ghcr.io/laude-institute/t-bench/python-3-13:latest"
SYSTEM_PROMPT = "SYSTEM_SENTINEL: authoritative policy {{ literal }}"
USER_PROMPT = "USER_SENTINEL: inspect input; $(touch /tmp/prompt-injection-pwned)"


def _docker_prerequisite() -> tuple[bool, str]:
    try:
        client = docker.from_env()
        client.ping()
        # This integration test must never pull an image implicitly.
        client.images.get(IMAGE)
    except Exception as error:
        return False, f"real Docker prompt fixture unavailable: {type(error).__name__}"
    return True, ""


DOCKER_AVAILABLE, DOCKER_SKIP_REASON = _docker_prerequisite()
pytestmark = pytest.mark.skipif(
    not DOCKER_AVAILABLE,
    reason=DOCKER_SKIP_REASON,
)


PROBE_INSTALL_SCRIPT = r'''#!/bin/bash
set -euo pipefail
mkdir -p /agent-logs /installed-agent /opt/openhands-venv/bin \
  /opt/mini-swe-agent-venv/bin "$HOME/.agentscript" "$HOME/.local/bin"

cat > /usr/local/bin/testprism-agent-probe <<'PY'
#!/usr/bin/python3
import json
import os
import sys
from pathlib import Path

name = Path(sys.argv[0]).name
if name == "python" and "-c" in sys.argv:
    raise SystemExit(0)

keys = (
    "AGENT_SYSTEM_PROMPT_FILENAME",
    "TESTPRISM_SYSTEM_PROMPT_MODE",
    "TESTPRISM_SYSTEM_PROMPT_PATH",
    "GEMINI_SYSTEM_MD",
    "OPENCODE_CONFIG_CONTENT",
    "PROBE_SECRET",
)
paths = (
    "/installed-agent/task-instruction.txt",
    "/installed-agent/kimi-system-agent.md",
    "/installed-agent/system-prompt.txt",
    "/installed-agent/openhands-system-prompt.j2",
    "/installed-agent/mini-swe-system-prompt.yaml",
    "/agent-logs/goose-recipe.yaml",
)
record = {
    "argv": sys.argv[1:],
    "env": {key: os.environ[key] for key in keys if key in os.environ},
    "files": {
        path: Path(path).read_text(encoding="utf-8")
        for path in paths
        if Path(path).is_file()
    },
    "stdin": None if sys.stdin.isatty() else sys.stdin.read(),
}
Path(f"/agent-logs/probe-{name}.json").write_text(
    json.dumps(record, ensure_ascii=False),
    encoding="utf-8",
)
PY
chmod +x /usr/local/bin/testprism-agent-probe

for name in claude codex gemini goose kimi opencode qwen node; do
  # Match the installers that expose their CLI only through ~/.local/bin.
  # AbstractInstalledAgent must restore that PATH in the parent tmux shell.
  ln -sf /usr/local/bin/testprism-agent-probe "$HOME/.local/bin/$name"
done
ln -sf /usr/local/bin/testprism-agent-probe /opt/openhands-venv/bin/python
ln -sf /usr/local/bin/testprism-agent-probe /opt/mini-swe-agent-venv/bin/mini

cat > /installed-agent/run-mini-swe-agent.sh <<'SH'
#!/bin/bash
set -euo pipefail
python3 - "$TESTPRISM_SYSTEM_PROMPT_PATH" <<'PY'
import json
import sys
from pathlib import Path

prompt = Path(sys.argv[1]).read_text(encoding="utf-8").strip()
Path("/installed-agent/mini-swe-system-prompt.yaml").write_text(
    json.dumps({"agent": {"system_template": prompt}}, ensure_ascii=False),
    encoding="utf-8",
)
PY
/opt/mini-swe-agent-venv/bin/mini \
  -c mini.yaml -c /installed-agent/mini-swe-system-prompt.yaml \
  -m "$TESTPRISM_MINI_SWE_MODEL" \
  -t "$(cat "$TESTPRISM_TASK_INSTRUCTION_PATH")" \
  -y --exit-immediately -o /agent-logs/mini-swe-trajectory.json
SH
chmod +x /installed-agent/run-mini-swe-agent.sh
'''


def _probe_class(agent_class: type, install_script: Path) -> type:
    class ProbeAgent(agent_class):
        @property
        def _install_agent_script_path(self) -> Path:
            return install_script

    ProbeAgent.__name__ = f"Probe{agent_class.__name__}"
    return ProbeAgent


def _read_probe(container: object, name: str) -> dict[str, object]:
    result = container.exec_run(["cat", f"/agent-logs/probe-{name}.json"])
    assert result.exit_code == 0, result.output.decode(errors="replace")
    return json.loads(result.output)


def test_native_prompt_channels_survive_real_container_install_and_launch(
    tmp_path: Path,
) -> None:
    install_script = tmp_path / "probe-install.sh"
    install_script.write_text(PROBE_INSTALL_SCRIPT, encoding="utf-8")
    install_script.chmod(0o755)

    client = docker.from_env()
    container_name = f"testprism-prompt-probe-{uuid.uuid4().hex[:12]}"
    container = client.containers.run(
        IMAGE,
        ["sleep", "infinity"],
        name=container_name,
        detach=True,
        network_mode="none",
    )
    try:
        mkdir = container.exec_run(["mkdir", "-p", "/logs", "/agent-logs"])
        assert mkdir.exit_code == 0

        profile_env = {"PROBE_SECRET": "container-only-sentinel"}
        agent_specs = [
            (
                ClaudeCodeAgent,
                "claude",
                dict(model_name="anthropic/probe", profile_env=profile_env),
            ),
            (
                CodexAgent,
                "codex",
                dict(model_name="probe", profile_env=profile_env),
            ),
            (
                GeminiCliAgent,
                "gemini",
                dict(model_name="gemini-probe", profile_env=profile_env),
            ),
            (
                GooseAgent,
                "goose",
                dict(
                    model_name="openai/probe",
                    api_key="unused",
                    base_url="https://unused.invalid/v1",
                    profile_env=profile_env,
                ),
            ),
            (
                KimiCodeAgent,
                "kimi",
                dict(
                    model_name="kimi-for-coding",
                    api_key="unused",
                    profile_env=profile_env,
                ),
            ),
            (
                MiniSweAgent,
                "mini",
                dict(
                    model_name="openai/probe",
                    api_key="unused",
                    base_url="https://unused.invalid/v1",
                    profile_env=profile_env,
                ),
            ),
            (
                OpenCodeAgent,
                "opencode",
                dict(
                    model_name="openai/probe",
                    api_key="unused",
                    base_url="https://unused.invalid/v1",
                    profile_env=profile_env,
                ),
            ),
            (
                OpenHandsAgent,
                "python",
                dict(
                    model_name="openai/probe",
                    profile_env={"LLM_API_KEY": "unused", **profile_env},
                ),
            ),
            (
                QwenCodeAgent,
                "qwen",
                dict(
                    model_name="openai/probe",
                    api_key="unused",
                    base_url="https://unused.invalid/v1",
                    profile_env=profile_env,
                ),
            ),
            (
                AgentScriptAgent,
                "node",
                dict(
                    model_name="openai/probe",
                    api_key="unused",
                    base_url="https://unused.invalid/v1",
                    profile_env=profile_env,
                ),
            ),
        ]

        captures: dict[str, dict[str, object]] = {}
        for index, (agent_class, probe_name, kwargs) in enumerate(agent_specs):
            probe_class = _probe_class(agent_class, install_script)
            agent = probe_class(system_prompt=SYSTEM_PROMPT, **kwargs)
            session_name = f"prompt-probe-{index}"
            session = TmuxSession(
                session_name=session_name,
                container=container,
                disable_recording=True,
                user="root",
            )
            session.start()
            try:
                result = agent.perform_task(USER_PROMPT, session)
                assert result == AgentResult(), agent_class.__name__
                captures[probe_name] = _read_probe(container, probe_name)
            finally:
                container.exec_run(["tmux", "kill-session", "-t", session_name])

        for capture in captures.values():
            files = capture["files"]
            assert files["/installed-agent/task-instruction.txt"] == USER_PROMPT
            system_path = (
                "/installed-agent/openhands-system-prompt.j2"
                if "/installed-agent/openhands-system-prompt.j2" in files
                else "/installed-agent/kimi-system-agent.md"
                if "/installed-agent/kimi-system-agent.md" in files
                else "/installed-agent/system-prompt.txt"
            )
            assert SYSTEM_PROMPT in files[system_path]
            assert capture["env"]["PROBE_SECRET"] == "container-only-sentinel"

        assert "--system-prompt-file" in captures["claude"]["argv"]
        assert USER_PROMPT in captures["claude"]["argv"]

        assert any(
            "model_instructions_file" in arg for arg in captures["codex"]["argv"]
        )
        assert captures["codex"]["stdin"] == USER_PROMPT

        assert captures["gemini"]["env"]["GEMINI_SYSTEM_MD"] == (
            "/installed-agent/system-prompt.txt"
        )
        assert USER_PROMPT in captures["gemini"]["argv"]

        recipe = yaml.safe_load(
            captures["goose"]["files"]["/agent-logs/goose-recipe.yaml"]
        )
        assert recipe["instructions"] == SYSTEM_PROMPT
        assert recipe["prompt"] == USER_PROMPT

        kimi_args = captures["kimi"]["argv"]
        assert "--agent-file" in kimi_args
        assert "/installed-agent/kimi-system-agent.md" in kimi_args
        assert "--model" in kimi_args
        assert kimi_args[kimi_args.index("--model") + 1] == "testprism/default"
        assert USER_PROMPT in kimi_args
        assert (
            "${base_prompt}"
            not in captures["kimi"]["files"]["/installed-agent/kimi-system-agent.md"]
        )

        mini_config = json.loads(
            captures["mini"]["files"][
                "/installed-agent/mini-swe-system-prompt.yaml"
            ]
        )
        assert mini_config["agent"]["system_template"] == SYSTEM_PROMPT
        assert USER_PROMPT in captures["mini"]["argv"]

        opencode_config = json.loads(
            captures["opencode"]["env"]["OPENCODE_CONFIG_CONTENT"]
        )
        assert opencode_config["agent"]["testprism"]["prompt"] == (
            "{file:/installed-agent/system-prompt.txt}"
        )
        assert USER_PROMPT in captures["opencode"]["argv"]

        assert captures["python"]["env"]["AGENT_SYSTEM_PROMPT_FILENAME"] == (
            "/installed-agent/openhands-system-prompt.j2"
        )
        assert USER_PROMPT in captures["python"]["argv"]

        qwen_args = captures["qwen"]["argv"]
        assert "--system-prompt" in qwen_args
        assert SYSTEM_PROMPT in qwen_args
        assert captures["qwen"]["stdin"] == USER_PROMPT

        node_args = captures["node"]["argv"]
        encoded_instruction = node_args[node_args.index("--instruction-b64") + 1]
        assert base64.b64decode(encoded_instruction).decode() == USER_PROMPT
        assert captures["node"]["env"]["TESTPRISM_SYSTEM_PROMPT_PATH"] == (
            "/installed-agent/system-prompt.txt"
        )
        assert captures["node"]["env"]["TESTPRISM_SYSTEM_PROMPT_MODE"] == (
            "replace"
        )

        pwned = container.exec_run(["test", "-e", "/tmp/prompt-injection-pwned"])
        assert pwned.exit_code != 0
    finally:
        # Remove only the uniquely named container created by this test. Never
        # touch unrelated task/patch-generation containers.
        container.remove(force=True)
