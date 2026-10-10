import importlib.util
import sys
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "validation"
    / "verify_tasks.py"
)


def _load_verify_module():
    spec = importlib.util.spec_from_file_location("verify_tasks_module", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_task(
    tmp_path: Path,
    *,
    workdir: str = "/workspace/project",
    max_agent_timeout_sec: float = 901.0,
    max_test_timeout_sec: float = 123.0,
) -> Path:
    task_dir = tmp_path / "demo-task"
    task_dir.mkdir()
    (task_dir / "tests").mkdir()
    (task_dir / "tests" / "test_demo.py").write_text("def test_demo():\n    assert True\n")
    (task_dir / "run-tests.sh").write_text(
        '#!/bin/bash\nif [ "${1:-}" = "--setup" ]; then exit 0; fi\nexit 0\n'
    )
    (task_dir / "solution.sh").write_text("#!/bin/bash\nexit 0\n")
    (task_dir / "docker-compose.yaml").write_text(
        """
services:
  client:
    image: ${T_BENCH_TASK_DOCKER_CLIENT_IMAGE_NAME}
    container_name: ${T_BENCH_TASK_DOCKER_CLIENT_CONTAINER_NAME}
    command: ["sh", "-c", "sleep infinity"]
""".strip()
        + "\n"
    )
    (task_dir / "task.yaml").write_text(
        f"""
instruction: |
  Fix the demo task.
difficulty: easy
parser_name: swebench
workdir: {workdir}
run_tests_setup_hook: true
max_agent_timeout_sec: {max_agent_timeout_sec}
max_test_timeout_sec: {max_test_timeout_sec}
""".strip()
        + "\n"
    )
    return task_dir


def test_verify_single_task_respects_workdir_paths_and_timeouts(monkeypatch, tmp_path):
    module = _load_verify_module()
    task_dir = _write_task(tmp_path)
    commands: list[dict] = []
    initial_test_seen = False

    def fake_run_command(cmd, cwd=None, timeout=300, env=None):
        nonlocal initial_test_seen
        commands.append({"cmd": cmd, "cwd": cwd, "timeout": timeout, "env": env})

        if cmd[:3] == ["docker", "compose", "build"]:
            return 0, "", ""
        if cmd[:3] == ["docker", "compose", "-p"] and cmd[4:] == ["up", "-d"]:
            return 0, "", ""
        if cmd[:3] == ["docker", "compose", "-p"] and cmd[4:6] == ["down", "-v"]:
            return 0, "", ""
        if cmd[:3] == ["docker", "compose", "-p"] and cmd[4:] == [
            "ps",
            "--services",
            "--status",
            "running",
        ]:
            return 0, "client\n", ""
        if cmd[:3] == ["docker", "compose", "-p"] and cmd[4:6] == ["ps", "-q"]:
            return 0, "container-123\n", ""
        if cmd[:3] == ["docker", "exec", "container-123"] and cmd[3:6] == [
            "mkdir",
            "-p",
            module.CONTAINER_TEST_DIR,
        ]:
            return 0, "", ""
        if cmd[:3] == ["docker", "exec", "container-123"] and cmd[3:6] == [
            "mkdir",
            "-p",
            module.CONTAINER_VERIFY_DIR,
        ]:
            return 0, "", ""
        if cmd[:2] == ["docker", "cp"] and cmd[2] == f"{task_dir / 'tests'}/.":
            assert cmd[3] == "container-123:/tests"
            return 0, "", ""
        if cmd[:2] == ["docker", "cp"] and cmd[2] == str(task_dir / "run-tests.sh"):
            assert cmd[3] == "container-123:/tests/run-tests.sh"
            return 0, "", ""
        if cmd[:2] == ["docker", "cp"] and cmd[2] == str(task_dir / "solution.sh"):
            assert cmd[3] == "container-123:/tmp/testprism-verify/solution.sh"
            return 0, "", ""
        if cmd[:3] == ["docker", "exec", "container-123"] and "chmod" in cmd:
            return 0, "", ""
        if cmd[:5] == ["docker", "exec", "container-123", "sh", "-lc"]:
            shell_command = cmd[5]
            assert "cd /workspace/project" in shell_command
            if "--setup" in shell_command:
                assert "bash /tests/run-tests.sh --setup" in shell_command
                return 0, "", ""
            if "/tmp/testprism-verify/solution.sh" in shell_command:
                return 0, "", ""
            if "bash /tests/run-tests.sh" in shell_command:
                if not initial_test_seen:
                    initial_test_seen = True
                    return 1, "", ""
                return 0, "", ""
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(module, "run_command", fake_run_command)

    result = module.verify_single_task(
        {
            "task_id": "demo-task",
            "source": "from-demo",
            "source_dir": "verified_tasks/from-demo",
            "difficulty_score": 0.5,
            "task_path": str(task_dir),
        }
    )

    assert result.verified is True
    assert result.phases["contract"]["workdir"] == "/workspace/project"
    assert result.phases["contract"]["run_tests_container_path"] == "/tests/run-tests.sh"
    assert (
        result.phases["contract"]["solution_container_path"]
        == "/tmp/testprism-verify/solution.sh"
    )

    build_call = next(call for call in commands if call["cmd"][:3] == ["docker", "compose", "build"])
    assert build_call["timeout"] == 1024

    setup_call = next(
        call for call in commands if call["cmd"][:5] == ["docker", "exec", "container-123", "sh", "-lc"]
        and "--setup" in call["cmd"][5]
    )
    assert setup_call["timeout"] == 300

    test_calls = [
        call for call in commands
        if call["cmd"][:5] == ["docker", "exec", "container-123", "sh", "-lc"]
        and call["cmd"][5].endswith("bash /tests/run-tests.sh")
    ]
    assert [call["timeout"] for call in test_calls] == [123, 123]

    solution_call = next(
        call for call in commands
        if call["cmd"][:5] == ["docker", "exec", "container-123", "sh", "-lc"]
        and "/tmp/testprism-verify/solution.sh" in call["cmd"][5]
    )
    assert solution_call["timeout"] == 901


def test_verify_single_task_rejects_relative_workdir(tmp_path):
    module = _load_verify_module()
    task_dir = _write_task(tmp_path, workdir="relative/workdir")

    result = module.verify_single_task(
        {
            "task_id": "demo-task",
            "source": "from-demo",
            "source_dir": "verified_tasks/from-demo",
            "difficulty_score": 0.5,
            "task_path": str(task_dir),
        }
    )

    assert result.verified is False
    assert result.fail_phase == "structure"
    assert "absolute container path" in result.fail_reason
