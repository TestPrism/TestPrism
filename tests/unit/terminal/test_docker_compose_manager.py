import logging
import subprocess
from pathlib import Path
from unittest.mock import call

import pytest

from testprism.terminal.docker_compose_manager import (
    DockerBuildError,
    DockerCleanupError,
    DockerComposeManager,
    DockerStartupError,
)


def _manager() -> DockerComposeManager:
    manager = object.__new__(DockerComposeManager)
    manager.env = {}
    manager._agent_name = None
    manager._agent_kwargs = {}
    manager._server_image_name = None
    manager._logger = logging.getLogger(__name__)
    return manager


def _clear_relays() -> None:
    DockerComposeManager._PROXY_RELAYS.clear()


@pytest.mark.parametrize(
    "message,failure_cls",
    [
        ("failed to solve: context deadline exceeded", DockerBuildError),
        (
            "removal of container demo is already in progress",
            DockerCleanupError,
        ),
    ],
)
def test_compose_transient_failures_are_retried(
    monkeypatch, message, failure_cls
):
    manager = _manager()
    manager._docker_compose_path = Path("/tmp/docker-compose.yaml")
    manager._compose_override_path = None
    manager._client_container_name = "demo"
    transient = subprocess.CalledProcessError(
        1,
        ["docker", "compose"],
        stderr=message,
    )
    completed = subprocess.CompletedProcess(["docker", "compose"], 0)
    run_calls = iter([transient, completed])

    def fake_run(*args, **kwargs):
        outcome = next(run_calls)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(subprocess, "run", fake_run)
    sleep = []
    monkeypatch.setattr("time.sleep", lambda delay: sleep.append(delay))

    result = manager._run_docker_compose_command(
        ["build"] if failure_cls is DockerBuildError else ["down"],
        timeout_sec=10,
        failure_cls=failure_cls,
    )

    assert result is completed
    assert sleep == [manager.COMPOSE_TRANSIENT_RETRY_DELAY_SEC]


def test_compose_non_transient_failure_is_not_retried(monkeypatch):
    manager = _manager()
    manager._docker_compose_path = Path("/tmp/docker-compose.yaml")
    manager._compose_override_path = None
    manager._client_container_name = "demo"
    attempts = []

    def fake_run(*args, **kwargs):
        attempts.append(call(*args, **kwargs))
        raise subprocess.CalledProcessError(
            1,
            ["docker", "compose"],
            stderr="Dockerfile syntax error",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(DockerBuildError, match="Dockerfile syntax error"):
        manager._run_docker_compose_command(
            ["build"],
            timeout_sec=10,
            failure_cls=DockerBuildError,
        )

    assert len(attempts) == 1


def test_docker_build_slot_uses_configured_cross_process_lock(
    monkeypatch, tmp_path
):
    manager = _manager()
    monkeypatch.setenv("TESTPRISM_MAX_CONCURRENT_DOCKER_BUILDS", "1")
    monkeypatch.setenv("TESTPRISM_DOCKER_BUILD_LOCK_DIR", str(tmp_path))

    with manager._docker_build_slot():
        assert (tmp_path / "testprism-docker-build-0.lock").is_file()


def test_runtime_proxy_env_empty_by_default(monkeypatch):
    monkeypatch.delenv("TESTPRISM_RUNTIME_PROXY", raising=False)
    monkeypatch.delenv("TESTPRISM_DISABLE_PROXY", raising=False)
    _clear_relays()
    manager = _manager()

    env = manager._resolve_runtime_proxy_env()

    assert env == {}


def test_runtime_proxy_env_rewrites_loopback_proxy(monkeypatch):
    _clear_relays()
    monkeypatch.setenv("TESTPRISM_RUNTIME_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setattr(
        DockerComposeManager,
        "_start_loopback_proxy_relay",
        lambda self, target_host, target_port: 17890,
    )
    manager = _manager()

    env = manager._resolve_runtime_proxy_env()

    assert env["HTTP_PROXY"] == "http://172.17.0.1:17890"
    assert env["HTTPS_PROXY"] == "http://172.17.0.1:17890"
    assert env["ALL_PROXY"] == "http://172.17.0.1:17890"
    assert env["http_proxy"] == "http://172.17.0.1:17890"
    assert env["https_proxy"] == "http://172.17.0.1:17890"
    assert env["all_proxy"] == "http://172.17.0.1:17890"
    assert "172.17.0.1" in env["NO_PROXY"]


def test_runtime_proxy_env_disable_overrides_explicit_proxy(monkeypatch):
    _clear_relays()
    monkeypatch.setenv("TESTPRISM_RUNTIME_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("TESTPRISM_DISABLE_PROXY", "1")
    manager = _manager()

    env = manager._resolve_runtime_proxy_env()

    assert env == {}


def test_container_reachable_loopback_url_rewrites_service_url(monkeypatch):
    _clear_relays()
    monkeypatch.setattr(
        DockerComposeManager,
        "_start_shared_loopback_relay",
        classmethod(lambda cls, target_host, target_port: 19142),
    )

    rewritten = DockerComposeManager.container_reachable_loopback_url(
        "http://127.0.0.1:4142/v1"
    )

    assert rewritten == "http://172.17.0.1:19142/v1"


def test_runtime_proxy_env_preserves_sidecar_no_proxy_entries(monkeypatch):
    _clear_relays()
    monkeypatch.setenv("TESTPRISM_RUNTIME_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("NO_PROXY", "example.com")
    monkeypatch.setattr(
        DockerComposeManager,
        "_start_loopback_proxy_relay",
        lambda self, target_host, target_port: 17890,
    )
    manager = _manager()
    manager.env = {"NO_PROXY": "example.com"}

    env = manager._resolve_runtime_proxy_env()

    assert "example.com" in env["NO_PROXY"]
    assert "172.17.0.1" in env["NO_PROXY"]
    assert "host.docker.internal" in env["NO_PROXY"]


def test_runtime_service_overrides_skip_proxy_by_default(monkeypatch):
    _clear_relays()
    monkeypatch.delenv("TESTPRISM_RUNTIME_PROXY", raising=False)
    manager = _manager()

    overrides = manager._build_runtime_service_overrides(
        {
            "services": {
                "client": {"image": "client:latest"},
                "db": {"image": "postgres:latest"},
            }
        }
    )

    assert set(overrides) == {"client", "db"}
    for service_override in overrides.values():
        assert "environment" not in service_override

    assert overrides["client"]["networks"] == [
        "default",
        DockerComposeManager.SHARED_NETWORK_NAME,
    ]
    assert "networks" not in overrides["db"]


def test_runtime_service_overrides_inject_explicit_proxy_into_all_services(monkeypatch):
    _clear_relays()
    monkeypatch.setenv("TESTPRISM_RUNTIME_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setattr(
        DockerComposeManager,
        "_start_loopback_proxy_relay",
        lambda self, target_host, target_port: 17890,
    )
    manager = _manager()

    overrides = manager._build_runtime_service_overrides(
        {
            "services": {
                "client": {"image": "client:latest"},
                "db": {"image": "postgres:latest"},
            }
        }
    )

    assert set(overrides) == {"client", "db"}
    for service_override in overrides.values():
        assert (
            service_override["environment"]["HTTP_PROXY"]
            == "http://172.17.0.1:17890"
        )

    assert overrides["client"]["networks"] == [
        "default",
        DockerComposeManager.SHARED_NETWORK_NAME,
    ]
    assert "networks" not in overrides["db"]


def test_build_proxy_args_empty_by_default(monkeypatch):
    _clear_relays()
    for key in [
        "TESTPRISM_DOCKER_BUILD_PROXY",
        "TESTPRISM_BUILD_PROXY",
        "TESTPRISM_RUNTIME_PROXY",
        "TESTPRISM_DISABLE_PROXY",
    ]:
        monkeypatch.delenv(key, raising=False)
    manager = _manager()

    proxy_args = manager._resolve_build_proxy_args()

    assert proxy_args == {}


def test_build_proxy_args_use_explicit_relay(monkeypatch):
    _clear_relays()
    monkeypatch.setenv("TESTPRISM_DOCKER_BUILD_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setattr(
        DockerComposeManager,
        "_start_loopback_proxy_relay",
        lambda self, target_host, target_port: 17890,
    )
    manager = _manager()

    proxy_args = manager._resolve_build_proxy_args()

    assert proxy_args["HTTP_PROXY"] == "http://172.17.0.1:17890"
    assert proxy_args["HTTPS_PROXY"] == "http://172.17.0.1:17890"


def test_build_proxy_args_disable_overrides_explicit_proxy(monkeypatch):
    _clear_relays()
    monkeypatch.setenv("TESTPRISM_DOCKER_BUILD_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("TESTPRISM_DISABLE_PROXY", "1")
    manager = _manager()

    proxy_args = manager._resolve_build_proxy_args()

    assert proxy_args == {}


def test_strip_unusable_loopback_proxy_env_removes_localhost_proxy_vars():
    manager = _manager()
    manager.env = {
        "HTTP_PROXY": "http://127.0.0.1:7890",
        "HTTPS_PROXY": "http://localhost:7890",
        "ALL_PROXY": "socks5://[::1]:7890",
        "NO_PROXY": "example.com",
    }

    manager._strip_unusable_loopback_proxy_env()

    assert "HTTP_PROXY" not in manager.env
    assert "HTTPS_PROXY" not in manager.env
    assert "ALL_PROXY" not in manager.env
    assert manager.env["NO_PROXY"] == "example.com"


def test_patch_dockerfile_sanitizes_swebench_network_fragility_by_default():
    manager = _manager()

    patched = manager._patch_dockerfile_contents(
        "\n".join(
            [
                "FROM debian:bookworm",
                "",
                "# China-network friendly: rewrite apt mirrors to aliyun (no-op outside CN networks).",
                "RUN if [ -f /etc/apt/sources.list ]; then \\",
                "      sed -i 's|http://archive.ubuntu.com|http://mirrors.aliyun.com|g' /etc/apt/sources.list 2>/dev/null || true; \\",
                "    fi",
                "",
                "RUN apt-get update && apt-get install -y git tmux asciinema patch",
                "RUN curl -LsSf https://astral.sh/uv/0.7.13/install.sh | sh",
                'ENV PATH="/root/.local/bin:${PATH}"',
                "",
            ]
        )
    )

    assert "mirrors.aliyun.com" not in patched
    assert "curl --retry 5 --retry-delay 2 --retry-all-errors" in patched
    assert 'export PATH="/root/.local/bin:${PATH}"' in patched
    assert '-o "${uv_installer}"' in patched
    assert 'python3 -m pip install --no-cache-dir "uv==0.7.13"' in patched
    assert "python3 -m pip install --no-cache-dir uv || true" not in patched
    assert patched.count("command -v uv >/dev/null 2>&1") == 3
    assert (
        "RUN apt-get update && apt-get install -y git tmux asciinema patch "
        "ca-certificates curl"
    ) in patched
    assert "command -v patch >/dev/null 2>&1" in patched
    assert (
        "DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "
        "git tmux bash asciinema patch ca-certificates curl"
    ) in patched
    assert 'ENV PATH="/root/.local/bin:${PATH}"' in patched


def test_patch_dockerfile_does_not_duplicate_existing_terminal_dependencies():
    manager = _manager()
    source = "\n".join(
        [
            "FROM debian:bookworm",
            DockerComposeManager._TERMINAL_DEPENDENCY_SNIPPET,
            "",
        ]
    )

    patched = manager._patch_dockerfile_contents(source)

    assert patched.count("DEBIAN_FRONTEND=noninteractive apt-get install") == 1
    assert patched.count("if command -v tmux >/dev/null 2>&1") == 1


def test_patch_dockerfile_is_idempotent_for_terminal_dependencies():
    manager = _manager()

    patched_once = manager._patch_dockerfile_contents("FROM debian:bookworm\n")
    patched_twice = manager._patch_dockerfile_contents(patched_once)

    assert patched_twice.count("DEBIAN_FRONTEND=noninteractive apt-get install") == 1


def test_multimodal_template_uses_runtime_dependency_snippets():
    template_path = (
        Path(__file__).resolve().parents[3]
        / "adapters"
        / "swe-bench-multimodal"
        / "template"
        / "Dockerfile"
    )
    template = template_path.read_text()

    assert "mirrors.aliyun.com" not in template
    assert DockerComposeManager._UV_INSTALL_SNIPPET in template
    assert DockerComposeManager._TERMINAL_DEPENDENCY_SNIPPET in template


def test_patch_dockerfile_removes_preexisting_raw_aliyun_block():
    manager = _manager()

    patched = manager._patch_dockerfile_contents(
        "\n".join(
            [
                "FROM debian:bookworm",
                "RUN if [ -f /etc/apt/sources.list ]; then \\",
                "      sed -i 's|http://archive.ubuntu.com|http://mirrors.aliyun.com|g' /etc/apt/sources.list 2>/dev/null || true; \\",
                "      sed -i 's|http://security.ubuntu.com|http://mirrors.aliyun.com|g' /etc/apt/sources.list 2>/dev/null || true; \\",
                "      sed -i 's|http://deb.debian.org|http://mirrors.aliyun.com|g' /etc/apt/sources.list 2>/dev/null || true; \\",
                "    fi; \\",
                "    if [ -d /etc/apt/sources.list.d ] && ls /etc/apt/sources.list.d/*.sources >/dev/null 2>&1; then \\",
                "      sed -i 's|http://archive.ubuntu.com|http://mirrors.aliyun.com|g' /etc/apt/sources.list.d/*.sources 2>/dev/null || true; \\",
                "      sed -i 's|http://security.ubuntu.com|http://mirrors.aliyun.com|g' /etc/apt/sources.list.d/*.sources 2>/dev/null || true; \\",
                "      sed -i 's|http://deb.debian.org|http://mirrors.aliyun.com|g' /etc/apt/sources.list.d/*.sources 2>/dev/null || true; \\",
                "    fi",
                "RUN curl -LsSf https://astral.sh/uv/0.7.13/install.sh | sh ",
                "",
            ]
        )
    )

    assert "mirrors.aliyun.com" not in patched
    assert "curl --retry 5 --retry-delay 2 --retry-all-errors" in patched


def test_patch_dockerfile_injects_apt_mirror_only_when_explicitly_enabled(monkeypatch):
    manager = _manager()
    monkeypatch.setenv("TESTPRISM_APT_MIRROR", "aliyun")

    patched = manager._patch_dockerfile_contents("FROM debian:bookworm\nRUN true\n")

    assert "mirrors.aliyun.com" in patched


def test_mini_swe_agent_base_dockerfile_uses_single_run_for_runtime_marker():
    dockerfile = (
        Path(__file__).resolve().parents[3]
        / "testprism"
        / "agents"
        / "installed_agents"
        / "mini_swe_agent"
        / "mini_swe_agent_base"
        / "Dockerfile"
    ).read_text()

    assert "ARG MINI_SWE_AGENT_VERSION=2.4.6" in dockerfile
    assert "ARG RUNTIME_KEY=pypi-2.4.6-py311-v1" in dockerfile
    assert "/opt/testprism-mini-swe-agent-runtime.json; \\" in dockerfile
    assert "<<'PY'" not in dockerfile
    assert "rm -rf /root/.cache /var/lib/apt/lists/*" in dockerfile


def test_start_timeout_uses_default(monkeypatch):
    monkeypatch.delenv(
        DockerComposeManager.START_TIMEOUT_ENV_VAR,
        raising=False,
    )

    assert (
        DockerComposeManager._resolve_start_timeout_sec()
        == DockerComposeManager.START_TIMEOUT_SEC
    )


def test_start_timeout_can_be_configured(monkeypatch):
    monkeypatch.setenv(DockerComposeManager.START_TIMEOUT_ENV_VAR, "1800")

    assert DockerComposeManager._resolve_start_timeout_sec() == 1800


@pytest.mark.parametrize("value", ["0", "-1", "not-a-number"])
def test_start_timeout_rejects_invalid_values(monkeypatch, value):
    monkeypatch.setenv(DockerComposeManager.START_TIMEOUT_ENV_VAR, value)

    with pytest.raises(DockerStartupError, match="must be a positive integer"):
        DockerComposeManager._resolve_start_timeout_sec()


def test_agentscript_patch_dockerfile_injects_runtime_layer():
    manager = _manager()
    manager._agent_name = "agentscript"

    patched = manager._patch_dockerfile_contents("FROM debian:bookworm\nRUN true\n")

    assert (
        "COPY --from=testprism-agentscript-base:latest /usr/local/ /usr/local/"
        in patched
    )
    assert (
        "COPY --from=testprism-agentscript-base:latest "
        "/root/.agentscript/ /root/.agentscript/"
    ) in patched
    assert 'ENV PATH="/usr/local/bin:${PATH}"' in patched


def test_agentscript_no_rebuild_layers_existing_client_image(monkeypatch, tmp_path):
    manager = _manager()
    manager._agent_name = "agentscript"
    manager._no_rebuild = True
    manager._client_image_name = "tb__task__client:latest"
    calls = []
    (tmp_path / "lay-on-image.sh").write_text("#!/usr/bin/env bash\n")

    monkeypatch.setattr(
        manager,
        "_ensure_agentscript_base_image",
        lambda: calls.append("base"),
    )
    monkeypatch.setattr(
        DockerComposeManager,
        "_agentscript_base_dir",
        staticmethod(lambda: Path(tmp_path)),
    )
    monkeypatch.setattr(
        manager,
        "_run_agentscript_base_command",
        lambda command: calls.append(command),
    )

    manager._maybe_prepare_agentscript_image(
        {"services": {"client": {"build": {"context": "."}}}}
    )

    assert calls == [
        "base",
        [
            str(tmp_path / "lay-on-image.sh"),
            "tb__task__client:latest",
            "tb__task__client:latest",
        ],
    ]


def test_agentscript_image_only_compose_layers_even_with_rebuild_enabled(
    monkeypatch,
    tmp_path,
):
    manager = _manager()
    manager._agent_name = "agentscript"
    manager._no_rebuild = False
    manager._client_image_name = "tb__task__client:latest"
    calls = []
    (tmp_path / "lay-on-image.sh").write_text("#!/usr/bin/env bash\n")

    monkeypatch.setattr(
        manager,
        "_ensure_agentscript_base_image",
        lambda: calls.append("base"),
    )
    monkeypatch.setattr(
        DockerComposeManager,
        "_agentscript_base_dir",
        staticmethod(lambda: Path(tmp_path)),
    )
    monkeypatch.setattr(
        manager,
        "_run_agentscript_base_command",
        lambda command: calls.append(command),
    )

    manager._maybe_prepare_agentscript_image(
        {"services": {"client": {"image": "ubuntu:latest"}}}
    )

    assert calls == [
        "base",
        [
            str(tmp_path / "lay-on-image.sh"),
            "tb__task__client:latest",
            "tb__task__client:latest",
        ],
    ]


def test_start_no_rebuild_fails_before_runtime_setup_when_client_image_missing(
    monkeypatch,
):
    manager = _manager()
    manager._no_rebuild = True
    manager._client_image_name = "tb__missing-task__client:latest"
    runtime_setup_calls = []
    monkeypatch.setattr(manager, "_docker_image_exists", lambda image: False)
    monkeypatch.setattr(
        manager,
        "_prepare_runtime_assets",
        lambda: runtime_setup_calls.append("prepare"),
    )

    with pytest.raises(
        DockerStartupError,
        match=r"Client image required by no_rebuild is missing: "
        r"tb__missing-task__client:latest.*exact image",
    ):
        manager.start()

    assert runtime_setup_calls == []


def test_start_no_rebuild_fails_before_runtime_setup_when_server_image_missing(
    monkeypatch,
    tmp_path,
):
    manager = _manager()
    manager._no_rebuild = True
    manager._client_image_name = "tb__task__client:latest"
    manager._server_image_name = "tb__task__server:latest"
    manager._docker_compose_path = tmp_path / "docker-compose.yaml"
    manager._docker_compose_path.write_text(
        """\
services:
  client:
    image: tb__task__client:latest
  server:
    image: tb__task__server:latest
"""
    )
    runtime_setup_calls = []

    def fake_image_exists(image: str) -> bool:
        return image == "tb__task__client:latest"

    monkeypatch.setattr(manager, "_docker_image_exists", fake_image_exists)
    monkeypatch.setattr(
        manager,
        "_prepare_runtime_assets",
        lambda: runtime_setup_calls.append("prepare"),
    )

    with pytest.raises(
        DockerStartupError,
        match=r"Server image required by no_rebuild is missing: "
        r"tb__task__server:latest.*exact image",
    ):
        manager.start()

    assert runtime_setup_calls == []


def test_start_no_rebuild_ignores_server_image_when_compose_has_no_server_service(
    monkeypatch,
    tmp_path,
):
    manager = _manager()
    manager._no_rebuild = True
    manager._client_image_name = "tb__task__client:latest"
    manager._server_image_name = "tb__task__server:latest"
    manager._docker_compose_path = tmp_path / "docker-compose.yaml"
    manager._docker_compose_path.write_text(
        """\
services:
  client:
    image: tb__task__client:latest
"""
    )
    expected_container = object()
    commands = []

    monkeypatch.setattr(
        manager,
        "_docker_image_exists",
        lambda image: image == "tb__task__client:latest",
    )
    monkeypatch.setattr(manager, "_prepare_runtime_assets", lambda: None)
    monkeypatch.setattr(
        manager,
        "_maybe_prepare_agentscript_image",
        lambda data: None,
    )
    monkeypatch.setattr(manager, "_maybe_prepare_mini_swe_agent_image", lambda: None)
    monkeypatch.setattr(manager, "_maybe_prepare_codex_image", lambda: None)
    monkeypatch.setattr(manager, "_maybe_prepare_claude_code_image", lambda: None)
    monkeypatch.setattr(manager, "_maybe_prepare_openhands_image", lambda: None)
    monkeypatch.setattr(manager, "_maybe_prepare_generic_agent_image", lambda: None)
    monkeypatch.setattr(
        manager,
        "_wait_for_client_container",
        lambda: expected_container,
    )
    monkeypatch.setattr(
        manager,
        "_run_docker_compose_command",
        lambda command, *, timeout_sec, failure_cls: commands.append(
            (command, timeout_sec, failure_cls)
        ),
    )

    assert manager.start() is expected_container
    assert commands == [
        (
            ["up", "-d", "--no-build"],
            DockerComposeManager.START_TIMEOUT_SEC,
            DockerStartupError,
        )
    ]


@pytest.mark.parametrize(
    ("no_rebuild", "expected_commands"),
    [
        (
            True,
            [
                (
                    ["up", "-d", "--no-build"],
                    DockerComposeManager.START_TIMEOUT_SEC,
                    DockerStartupError,
                )
            ],
        ),
        (
            False,
            [
                (
                    ["build"],
                    DockerComposeManager.BUILD_TIMEOUT_SEC,
                    DockerBuildError,
                ),
                (
                    ["up", "-d"],
                    DockerComposeManager.START_TIMEOUT_SEC,
                    DockerStartupError,
                ),
            ],
        ),
    ],
)
def test_start_disables_implicit_compose_build_only_for_no_rebuild(
    monkeypatch,
    no_rebuild,
    expected_commands,
):
    manager = _manager()
    manager._no_rebuild = no_rebuild
    manager._client_image_name = "tb__task__client:latest"
    expected_container = object()
    commands = []

    monkeypatch.setattr(manager, "_docker_image_exists", lambda image: True)
    monkeypatch.setattr(manager, "_prepare_runtime_assets", lambda: None)
    monkeypatch.setattr(manager, "_load_compose_data", lambda: {})
    monkeypatch.setattr(manager, "_maybe_prepare_agentscript_image", lambda data: None)
    monkeypatch.setattr(manager, "_maybe_prepare_mini_swe_agent_image", lambda: None)
    monkeypatch.setattr(manager, "_maybe_prepare_codex_image", lambda: None)
    monkeypatch.setattr(manager, "_maybe_prepare_claude_code_image", lambda: None)
    monkeypatch.setattr(manager, "_maybe_prepare_openhands_image", lambda: None)
    monkeypatch.setattr(
        manager,
        "_wait_for_client_container",
        lambda: expected_container,
    )
    monkeypatch.setattr(
        manager,
        "_run_docker_compose_command",
        lambda command, *, timeout_sec, failure_cls: commands.append(
            (command, timeout_sec, failure_cls)
        ),
    )

    assert manager.start() is expected_container
    assert commands == expected_commands


def test_openhands_runtime_key_uses_default_version(monkeypatch):
    monkeypatch.delenv("TESTPRISM_OPENHANDS_BASE", raising=False)
    manager = _manager()
    manager._agent_name = "openhands"

    assert manager._openhands_runtime_key() == "pypi-0.51.1-orjson-v1"
    assert manager._openhands_base_image() == (
        "testprism-openhands-base:pypi-0.51.1-orjson-v1"
    )


def test_openhands_runtime_key_uses_version_and_git_version(monkeypatch):
    monkeypatch.delenv("TESTPRISM_OPENHANDS_BASE", raising=False)
    manager = _manager()
    manager._agent_name = "openhands"
    manager._agent_kwargs = {"version": "0.50.0"}

    assert manager._openhands_runtime_key() == "pypi-0.50.0-orjson-v1"

    manager._agent_kwargs = {"version": "0.50.0", "git_version": "feature/test@abc"}

    assert manager._openhands_runtime_key() == "git-feature-test-abc-orjson-v1"
    assert manager._openhands_base_image() == (
        "testprism-openhands-base:git-feature-test-abc-orjson-v1"
    )


def test_mini_swe_agent_runtime_key_and_image_follow_requested_version(monkeypatch):
    monkeypatch.delenv("TESTPRISM_MINI_SWE_AGENT_BASE", raising=False)
    manager = _manager()
    manager._agent_name = "mini-swe-agent"

    assert manager._mini_swe_agent_runtime_key() == "pypi-2.4.6-py311-v1"
    assert manager._mini_swe_agent_base_image() == (
        "testprism-mini-swe-agent-base:pypi-2.4.6-py311-v1"
    )

    manager._agent_kwargs = {"version": "2.4.6"}

    assert manager._mini_swe_agent_runtime_key() == "pypi-2.4.6-py311-v1"
    assert manager._mini_swe_agent_base_image() == (
        "testprism-mini-swe-agent-base:pypi-2.4.6-py311-v1"
    )


def test_codex_runtime_key_and_image_follow_requested_version(monkeypatch):
    monkeypatch.delenv("TESTPRISM_CODEX_BASE", raising=False)
    manager = _manager()
    manager._agent_name = "codex"
    manager._agent_kwargs = {"version": "0.58.0"}

    assert manager._codex_runtime_key() == "0.58.0-node22-v1"
    assert manager._codex_base_image() == "testprism-codex-base:0.58.0-node22-v1"


def test_claude_code_runtime_key_and_image_follow_requested_version(monkeypatch):
    monkeypatch.delenv("TESTPRISM_CLAUDE_CODE_BASE", raising=False)
    manager = _manager()
    manager._agent_name = "claude-code"
    manager._agent_kwargs = {"version": "1.2.3"}

    assert manager._claude_code_runtime_key() == "1.2.3-node22-v1"
    assert manager._claude_code_base_image() == (
        "testprism-claude-code-base:1.2.3-node22-v1"
    )


def test_codex_layer_failure_falls_back_by_default(monkeypatch, tmp_path):
    manager = _manager()
    manager._agent_name = "codex"
    manager._client_image_name = "tb__task__client:latest"
    (tmp_path / "lay-on-image.sh").write_text("#!/usr/bin/env bash\n")

    monkeypatch.delenv("TESTPRISM_CODEX_PREBUILD_STRICT", raising=False)
    monkeypatch.setattr(
        DockerComposeManager,
        "_codex_base_dir",
        staticmethod(lambda: Path(tmp_path)),
    )
    monkeypatch.setattr(manager, "_ensure_codex_base_image", lambda: None)
    monkeypatch.setattr(
        manager,
        "_run_codex_base_command",
        lambda command: (_ for _ in ()).throw(DockerBuildError("layer failed")),
    )

    manager._maybe_prepare_codex_image()


def test_mini_swe_agent_layer_failure_falls_back_by_default(monkeypatch, tmp_path):
    manager = _manager()
    manager._agent_name = "mini-swe-agent"
    manager._client_image_name = "tb__task__client:latest"
    (tmp_path / "lay-on-image.sh").write_text("#!/usr/bin/env bash\n")

    monkeypatch.delenv("TESTPRISM_MINI_SWE_AGENT_PREBUILD_STRICT", raising=False)
    monkeypatch.setattr(
        DockerComposeManager,
        "_mini_swe_agent_base_dir",
        staticmethod(lambda: Path(tmp_path)),
    )
    monkeypatch.setattr(manager, "_ensure_mini_swe_agent_base_image", lambda: None)
    monkeypatch.setattr(
        manager,
        "_run_mini_swe_agent_base_command",
        lambda command: (_ for _ in ()).throw(DockerBuildError("layer failed")),
    )

    manager._maybe_prepare_mini_swe_agent_image()


def test_mini_swe_agent_layer_failure_strict_raises(monkeypatch, tmp_path):
    manager = _manager()
    manager._agent_name = "mini-swe-agent"
    manager._client_image_name = "tb__task__client:latest"
    (tmp_path / "lay-on-image.sh").write_text("#!/usr/bin/env bash\n")

    monkeypatch.setenv("TESTPRISM_MINI_SWE_AGENT_PREBUILD_STRICT", "1")
    monkeypatch.setattr(
        DockerComposeManager,
        "_mini_swe_agent_base_dir",
        staticmethod(lambda: Path(tmp_path)),
    )
    monkeypatch.setattr(manager, "_ensure_mini_swe_agent_base_image", lambda: None)
    monkeypatch.setattr(
        manager,
        "_run_mini_swe_agent_base_command",
        lambda command: (_ for _ in ()).throw(DockerBuildError("layer failed")),
    )

    with pytest.raises(DockerBuildError):
        manager._maybe_prepare_mini_swe_agent_image()


def test_codex_layer_failure_strict_raises(monkeypatch, tmp_path):
    manager = _manager()
    manager._agent_name = "codex"
    manager._client_image_name = "tb__task__client:latest"
    (tmp_path / "lay-on-image.sh").write_text("#!/usr/bin/env bash\n")

    monkeypatch.setenv("TESTPRISM_CODEX_PREBUILD_STRICT", "1")
    monkeypatch.setattr(
        DockerComposeManager,
        "_codex_base_dir",
        staticmethod(lambda: Path(tmp_path)),
    )
    monkeypatch.setattr(manager, "_ensure_codex_base_image", lambda: None)
    monkeypatch.setattr(
        manager,
        "_run_codex_base_command",
        lambda command: (_ for _ in ()).throw(DockerBuildError("layer failed")),
    )

    with pytest.raises(DockerBuildError):
        manager._maybe_prepare_codex_image()


def test_claude_code_layer_failure_falls_back_by_default(monkeypatch, tmp_path):
    manager = _manager()
    manager._agent_name = "claude-code"
    manager._client_image_name = "tb__task__client:latest"
    (tmp_path / "lay-on-image.sh").write_text("#!/usr/bin/env bash\n")

    monkeypatch.delenv("TESTPRISM_CLAUDE_CODE_PREBUILD_STRICT", raising=False)
    monkeypatch.setattr(
        DockerComposeManager,
        "_claude_code_base_dir",
        staticmethod(lambda: Path(tmp_path)),
    )
    monkeypatch.setattr(manager, "_ensure_claude_code_base_image", lambda: None)
    monkeypatch.setattr(
        manager,
        "_run_claude_code_base_command",
        lambda command: (_ for _ in ()).throw(DockerBuildError("layer failed")),
    )

    manager._maybe_prepare_claude_code_image()


def test_claude_code_layer_failure_strict_raises(monkeypatch, tmp_path):
    manager = _manager()
    manager._agent_name = "claude-code"
    manager._client_image_name = "tb__task__client:latest"
    (tmp_path / "lay-on-image.sh").write_text("#!/usr/bin/env bash\n")

    monkeypatch.setenv("TESTPRISM_CLAUDE_CODE_PREBUILD_STRICT", "1")
    monkeypatch.setattr(
        DockerComposeManager,
        "_claude_code_base_dir",
        staticmethod(lambda: Path(tmp_path)),
    )
    monkeypatch.setattr(manager, "_ensure_claude_code_base_image", lambda: None)
    monkeypatch.setattr(
        manager,
        "_run_claude_code_base_command",
        lambda command: (_ for _ in ()).throw(DockerBuildError("layer failed")),
    )

    with pytest.raises(DockerBuildError):
        manager._maybe_prepare_claude_code_image()


def test_openhands_patch_dockerfile_does_not_inject_runtime_layer():
    manager = _manager()
    manager._agent_name = "openhands"

    patched = manager._patch_dockerfile_contents("FROM debian:bookworm\nRUN true\n")

    assert "testprism-openhands-base" not in patched
    assert "/opt/openhands-venv" not in patched


def test_openhands_layer_failure_falls_back_by_default(monkeypatch, tmp_path):
    manager = _manager()
    manager._agent_name = "openhands"
    manager._client_image_name = "tb__task__client:latest"
    manager._no_rebuild = True
    (tmp_path / "lay-on-image.sh").write_text("#!/usr/bin/env bash\n")

    monkeypatch.delenv("TESTPRISM_OPENHANDS_PREBUILD_STRICT", raising=False)
    monkeypatch.setattr(
        DockerComposeManager,
        "_openhands_base_dir",
        staticmethod(lambda: Path(tmp_path)),
    )
    monkeypatch.setattr(manager, "_ensure_openhands_base_image", lambda: None)

    monkeypatch.setattr(
        manager,
        "_run_openhands_base_command",
        lambda command: (_ for _ in ()).throw(DockerBuildError("layer failed")),
    )

    manager._maybe_prepare_openhands_image()


def test_openhands_layer_failure_strict_raises(monkeypatch, tmp_path):
    manager = _manager()
    manager._agent_name = "openhands"
    manager._client_image_name = "tb__task__client:latest"
    (tmp_path / "lay-on-image.sh").write_text("#!/usr/bin/env bash\n")

    monkeypatch.setenv("TESTPRISM_OPENHANDS_PREBUILD_STRICT", "1")
    monkeypatch.setattr(
        DockerComposeManager,
        "_openhands_base_dir",
        staticmethod(lambda: Path(tmp_path)),
    )
    monkeypatch.setattr(manager, "_ensure_openhands_base_image", lambda: None)
    monkeypatch.setattr(
        manager,
        "_run_openhands_base_command",
        lambda command: (_ for _ in ()).throw(DockerBuildError("layer failed")),
    )

    with pytest.raises(DockerBuildError):
        manager._maybe_prepare_openhands_image()


@pytest.mark.parametrize(
    ("agent_name", "slug", "version"),
    [
        ("aider", "aider", "latest"),
        ("cursor-cli", "cursor-cli", "stable"),
        ("gemini-cli", "gemini-cli", "latest"),
        ("goose", "goose", "stable"),
        ("grok-cli", "grok-cli", "latest"),
        ("kimi-code", "kimi-code", "latest"),
        ("opencode", "opencode", "latest"),
        ("qwen-coder", "qwen-code", "latest"),
    ],
)
def test_generic_layer_agent_defaults(monkeypatch, agent_name, slug, version):
    manager = _manager()
    manager._agent_name = agent_name
    env_prefix = slug.replace("-", "_").upper()
    monkeypatch.delenv(f"TESTPRISM_{env_prefix}_BASE", raising=False)

    assert manager._generic_layer_agent_version() == version
    assert manager._generic_layer_agent_runtime_key() == f"{version}-runtime-v1"
    assert manager._generic_layer_env_prefix() == env_prefix
    assert manager._generic_layer_agent_base_image() == (
        f"testprism-{slug}-base:{version}-runtime-v1"
    )
    assert manager._generic_layer_agent_prebuild_enabled()


def test_kimi_generic_layer_uses_expected_env_contract(monkeypatch):
    manager = _manager()
    manager._agent_name = "kimi-code"
    monkeypatch.setenv("TESTPRISM_KIMI_CODE_BASE", "custom-kimi-base:probe")
    monkeypatch.setenv("TESTPRISM_KIMI_CODE_PREBUILD", "0")
    monkeypatch.setenv("TESTPRISM_KIMI_CODE_PREBUILD_STRICT", "1")

    assert manager._generic_layer_env_prefix() == "KIMI_CODE"
    assert manager._generic_layer_agent_base_image() == "custom-kimi-base:probe"
    assert not manager._generic_layer_agent_prebuild_enabled()
    assert manager._generic_layer_agent_prebuild_strict()


@pytest.mark.parametrize(
    ("agent_name", "slug", "version"),
    [
        ("gemini-cli", "gemini-cli", "1.2.3"),
        ("kimi-code", "kimi-code", "0.38.0"),
    ],
)
def test_generic_layer_agent_uses_requested_version_and_layers_image(
    monkeypatch, tmp_path, agent_name, slug, version
):
    manager = _manager()
    manager._agent_name = agent_name
    manager._agent_kwargs = {"version": version}
    manager._client_image_name = "tb__task__client:latest"
    calls = []
    (tmp_path / "lay-on-image.sh").write_text("#!/usr/bin/env bash\n")

    monkeypatch.setattr(
        DockerComposeManager,
        "_generic_agent_base_dir",
        staticmethod(lambda: Path(tmp_path)),
    )
    monkeypatch.setattr(
        manager, "_ensure_generic_agent_base_image", lambda: calls.append("base")
    )
    monkeypatch.setattr(
        manager,
        "_run_generic_agent_base_command",
        lambda command: calls.append(command),
    )

    manager._maybe_prepare_generic_agent_image()

    assert calls == [
        "base",
        [
            str(tmp_path / "lay-on-image.sh"),
            f"testprism-{slug}-base:{version}-runtime-v1",
            "tb__task__client:latest",
            "tb__task__client:latest",
            f"{version}-runtime-v1",
            slug,
        ],
    ]


def test_generic_layer_failure_falls_back_by_default(monkeypatch, tmp_path):
    manager = _manager()
    manager._agent_name = "qwen-coder"
    manager._client_image_name = "tb__task__client:latest"
    (tmp_path / "lay-on-image.sh").write_text("#!/usr/bin/env bash\n")

    monkeypatch.delenv("TESTPRISM_QWEN_CODE_PREBUILD_STRICT", raising=False)
    monkeypatch.setattr(
        DockerComposeManager,
        "_generic_agent_base_dir",
        staticmethod(lambda: Path(tmp_path)),
    )
    monkeypatch.setattr(manager, "_ensure_generic_agent_base_image", lambda: None)
    monkeypatch.setattr(
        manager,
        "_run_generic_agent_base_command",
        lambda command: (_ for _ in ()).throw(DockerBuildError("layer failed")),
    )

    manager._maybe_prepare_generic_agent_image()


def test_generic_layer_failure_strict_raises(monkeypatch, tmp_path):
    manager = _manager()
    manager._agent_name = "qwen-coder"
    manager._client_image_name = "tb__task__client:latest"
    (tmp_path / "lay-on-image.sh").write_text("#!/usr/bin/env bash\n")

    monkeypatch.setenv("TESTPRISM_QWEN_CODE_PREBUILD_STRICT", "1")
    monkeypatch.setattr(
        DockerComposeManager,
        "_generic_agent_base_dir",
        staticmethod(lambda: Path(tmp_path)),
    )
    monkeypatch.setattr(manager, "_ensure_generic_agent_base_image", lambda: None)
    monkeypatch.setattr(
        manager,
        "_run_generic_agent_base_command",
        lambda command: (_ for _ in ()).throw(DockerBuildError("layer failed")),
    )

    with pytest.raises(DockerBuildError):
        manager._maybe_prepare_generic_agent_image()
