import importlib.util
import sys
from pathlib import Path

import pytest
import yaml


MODULE_PATH = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "validation"
    / "verify_tasks.py"
)


def _load_verify_tasks_module():
    spec = importlib.util.spec_from_file_location("verify_tasks", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def verify_tasks_module():
    return _load_verify_tasks_module()


def _write_task_files(
    task_dir: Path,
    *,
    task_yaml: dict | None = None,
    solution_sh: str | None = "#!/bin/bash\necho fixed\n",
    solution_yaml: list[dict] | None = None,
) -> None:
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "run-tests.sh").write_text("#!/bin/bash\nexit 1\n")
    (task_dir / "docker-compose.yaml").write_text(
        "services:\n  client:\n    image: busybox\n    command: ['sh', '-c', 'sleep infinity']\n"
    )
    payload = {
        "instruction": "demo",
        "author_name": "tester",
        "difficulty": "easy",
        "parser_name": "pytest",
        "workdir": "/app",
    }
    if task_yaml:
        payload.update(task_yaml)
    (task_dir / "task.yaml").write_text(yaml.safe_dump(payload))
    if solution_sh is not None:
        (task_dir / "solution.sh").write_text(solution_sh)
    if solution_yaml is not None:
        (task_dir / "solution.yaml").write_text(yaml.safe_dump(solution_yaml))


def _make_candidate(task_dir: Path) -> dict:
    return {
        "task_id": task_dir.name,
        "source": "from-terminal-bench",
        "source_dir": "train/from-terminal-bench",
        "difficulty_score": 1.0,
        "task_path": str(task_dir),
    }


def _make_contract(module, task_dir: Path, *, solution_kind: str = "sh"):
    solution_path = task_dir / ("solution.yaml" if solution_kind == "yaml" else "solution.sh")
    solution_commands = None
    if solution_kind == "yaml":
        solution_commands = [
            module.TerminalCommand(
                command="echo fixed > hello.txt",
                min_timeout_sec=0.0,
                max_timeout_sec=5.0,
                block=False,
                append_enter=True,
            )
        ]
    return module.TaskContract(
        task_path=task_dir,
        compose_path=task_dir / "docker-compose.yaml",
        run_tests_host_path=task_dir / "run-tests.sh",
        solution_host_path=solution_path,
        solution_kind=solution_kind,
        solution_commands=solution_commands,
        tests_dir=task_dir / "tests",
        workdir="/app",
        parser_name="pytest",
        target_service="client",
        run_tests_setup_hook=False,
        run_tests_in_same_shell=False,
        has_root_dockerfile=True,
        max_agent_timeout_sec=60.0,
        max_test_timeout_sec=30.0,
    )


def test_load_task_contract_supports_solution_yaml(tmp_path, verify_tasks_module):
    module = verify_tasks_module
    task_dir = tmp_path / "yaml-task"
    _write_task_files(
        task_dir,
        solution_sh=None,
        solution_yaml=[
            {
                "command": "echo fixed > hello.txt",
                "min_timeout_sec": 0.0,
                "max_timeout_sec": 5.0,
                "block": False,
                "append_enter": True,
            }
        ],
    )

    contract, failure = module.load_task_contract(task_dir)

    assert failure is None
    assert contract is not None
    assert contract.solution_kind == "yaml"
    assert contract.solution_commands is not None
    assert contract.solution_commands[0].command == "echo fixed > hello.txt"


def test_load_task_contract_allows_same_shell_tasks(tmp_path, verify_tasks_module):
    module = verify_tasks_module
    task_dir = tmp_path / "same-shell-task"
    _write_task_files(task_dir, task_yaml={"run_tests_in_same_shell": True})

    contract, failure = module.load_task_contract(task_dir)

    assert failure is None
    assert contract is not None
    assert contract.run_tests_in_same_shell is True


def test_discover_candidates_from_single_task_preserves_source(
    tmp_path,
    verify_tasks_module,
):
    module = verify_tasks_module
    task_dir = tmp_path / "verified_tasks" / "train" / "from-terminal-bench" / "hello-world"
    _write_task_files(task_dir)

    candidates = module.discover_candidates_from_dataset(task_dir)

    assert len(candidates) == 1
    assert candidates[0]["source"] == "from-terminal-bench"
    assert candidates[0]["source_dir"] == "train/from-terminal-bench"


def test_load_task_contract_rejects_unknown_parser(tmp_path, verify_tasks_module):
    module = verify_tasks_module
    task_dir = tmp_path / "bad-parser"
    _write_task_files(task_dir, task_yaml={"parser_name": "made-up-parser"})

    contract, failure = module.load_task_contract(task_dir)

    assert contract is None
    assert failure is not None
    assert failure.verdict == "invalid_structure"
    assert "Unknown parser_name" in failure.message


@pytest.mark.parametrize(
    ("fail_phase", "fail_reason", "verdict"),
    [
        ("structure", "missing task.yaml", "invalid_structure"),
        ("build", "compose failed", "invalid_build"),
        ("build", "Cannot connect to the Docker daemon", "infra_error"),
        ("initial_test", "tests passed", "invalid_initial_test_behavior"),
        ("solution", "solution failed", "invalid_solution_behavior"),
        ("final_test", "tests failed", "invalid_final_test"),
    ],
)
def test_derive_verdict_mapping(
    verify_tasks_module,
    fail_phase,
    fail_reason,
    verdict,
):
    module = verify_tasks_module
    result = module.VerificationResult(
        task_id="demo",
        source="src",
        source_dir="train/src",
        difficulty_score=1.0,
        verified=False,
        fail_phase=fail_phase,
        fail_reason=fail_reason,
    )

    assert module.derive_verdict(result) == verdict


def test_verify_single_task_rejects_unexpected_initial_test_pass(
    tmp_path,
    monkeypatch,
    verify_tasks_module,
):
    module = verify_tasks_module
    task_dir = tmp_path / "initial-pass"
    _write_task_files(task_dir)
    contract = _make_contract(module, task_dir)
    cleanup_calls = []

    monkeypatch.setattr(module, "load_task_contract", lambda path: (contract, None))
    monkeypatch.setattr(
        module,
        "build_compose_env",
        lambda *args, **kwargs: {"T_BENCH_TASK_DOCKER_CLIENT_IMAGE_NAME": "demo:latest"},
    )
    monkeypatch.setattr(
        module,
        "_resolve_container_id",
        lambda *args, **kwargs: ("pre-container" if "pre" in kwargs["project_name"] else "post-container", None),
    )
    monkeypatch.setattr(
        module,
        "_prepare_container",
        lambda *args, **kwargs: (True, "", {"container_id": kwargs["container_id"]}),
    )
    monkeypatch.setattr(
        module,
        "_run_solution",
        lambda *args, **kwargs: pytest.fail("solution should not run after initial pass"),
    )
    monkeypatch.setattr(
        module,
        "cleanup_task",
        lambda *args, **kwargs: cleanup_calls.append(kwargs["project_name"]),
    )

    def fake_run_command(cmd, cwd=None, timeout=300, env=None):
        del cwd, timeout, env
        if cmd == ["docker", "compose", "build"]:
            return 0, "build ok", ""
        if cmd[:5] == ["docker", "compose", "-p", cmd[3], "down"]:
            return 0, "", ""
        if cmd[:5] == ["docker", "compose", "-p", cmd[3], "up"]:
            return 0, "up ok", ""
        if cmd[:4] == ["docker", "exec", "pre-container", "sh"]:
            return 0, "tests passed", ""
        pytest.fail(f"unexpected command: {cmd}")

    monkeypatch.setattr(module, "run_command", fake_run_command)

    result = module.verify_single_task(_make_candidate(task_dir))

    assert result.verified is False
    assert result.fail_phase == "initial_test"
    assert result.verdict == "invalid_initial_test_behavior"
    assert cleanup_calls == [
        "tb-verify-from-terminal-bench-initial-pass-pre",
        "tb-verify-from-terminal-bench-initial-pass-post",
    ]


def test_verify_single_task_marks_failed_final_test(
    tmp_path,
    monkeypatch,
    verify_tasks_module,
):
    module = verify_tasks_module
    task_dir = tmp_path / "final-fail"
    _write_task_files(task_dir)
    contract = _make_contract(module, task_dir)

    monkeypatch.setattr(module, "load_task_contract", lambda path: (contract, None))
    monkeypatch.setattr(
        module,
        "build_compose_env",
        lambda *args, **kwargs: {"T_BENCH_TASK_DOCKER_CLIENT_IMAGE_NAME": "demo:latest"},
    )
    monkeypatch.setattr(
        module,
        "_resolve_container_id",
        lambda *args, **kwargs: ("pre-container" if "pre" in kwargs["project_name"] else "post-container", None),
    )
    monkeypatch.setattr(
        module,
        "_prepare_container",
        lambda *args, **kwargs: (True, "", {"container_id": kwargs["container_id"]}),
    )
    monkeypatch.setattr(module, "cleanup_task", lambda *args, **kwargs: None)
    monkeypatch.setattr(module, "_run_solution", lambda *args, **kwargs: (0, "solution ok", ""))

    def fake_run_command(cmd, cwd=None, timeout=300, env=None):
        del cwd, timeout, env
        if cmd == ["docker", "compose", "build"]:
            return 0, "build ok", ""
        if cmd[:5] == ["docker", "compose", "-p", cmd[3], "down"]:
            return 0, "", ""
        if cmd[:5] == ["docker", "compose", "-p", cmd[3], "up"]:
            return 0, "up ok", ""
        if cmd[:4] == ["docker", "exec", "pre-container", "sh"]:
            return 1, "", "expected fail"
        if cmd[:4] == ["docker", "exec", "post-container", "sh"]:
            return 2, "", "still failing"
        pytest.fail(f"unexpected command: {cmd}")

    monkeypatch.setattr(module, "run_command", fake_run_command)

    result = module.verify_single_task(_make_candidate(task_dir))

    assert result.verified is False
    assert result.fail_phase == "final_test"
    assert result.verdict == "invalid_final_test"


def test_verify_single_task_accepts_solution_yaml_only_task(
    tmp_path,
    monkeypatch,
    verify_tasks_module,
):
    module = verify_tasks_module
    task_dir = tmp_path / "yaml-only"
    _write_task_files(
        task_dir,
        solution_sh=None,
        solution_yaml=[
            {
                "command": "echo fixed > hello.txt",
                "min_timeout_sec": 0.0,
                "max_timeout_sec": 5.0,
                "block": False,
                "append_enter": True,
            }
        ],
    )
    contract, failure = module.load_task_contract(task_dir)
    assert failure is None
    assert contract is not None

    monkeypatch.setattr(module, "load_task_contract", lambda path: (contract, None))
    monkeypatch.setattr(
        module,
        "build_compose_env",
        lambda *args, **kwargs: {"T_BENCH_TASK_DOCKER_CLIENT_IMAGE_NAME": "demo:latest"},
    )
    monkeypatch.setattr(
        module,
        "_resolve_container_id",
        lambda *args, **kwargs: ("pre-container" if "pre" in kwargs["project_name"] else "post-container", None),
    )
    monkeypatch.setattr(
        module,
        "_prepare_container",
        lambda *args, **kwargs: (True, "", {"container_id": kwargs["container_id"]}),
    )
    monkeypatch.setattr(module, "cleanup_task", lambda *args, **kwargs: None)

    def fake_run_solution(contract_arg, *, container_id, solution_timeout):
        del container_id, solution_timeout
        assert contract_arg.solution_kind == "yaml"
        assert contract_arg.solution_commands is not None
        return 0, "solution ok", ""

    monkeypatch.setattr(module, "_run_solution", fake_run_solution)

    def fake_run_command(cmd, cwd=None, timeout=300, env=None):
        del cwd, timeout, env
        if cmd == ["docker", "compose", "build"]:
            return 0, "build ok", ""
        if cmd[:5] == ["docker", "compose", "-p", cmd[3], "down"]:
            return 0, "", ""
        if cmd[:5] == ["docker", "compose", "-p", cmd[3], "up"]:
            return 0, "up ok", ""
        if cmd[:4] == ["docker", "exec", "pre-container", "sh"]:
            return 1, "", "expected fail"
        if cmd[:4] == ["docker", "exec", "post-container", "sh"]:
            return 0, "tests pass", ""
        pytest.fail(f"unexpected command: {cmd}")

    monkeypatch.setattr(module, "run_command", fake_run_command)

    result = module.verify_single_task(_make_candidate(task_dir))

    assert result.verified is True
    assert result.verdict == "valid"


def test_verify_single_task_can_skip_post_image_cleanup(
    tmp_path,
    monkeypatch,
    verify_tasks_module,
):
    module = verify_tasks_module
    task_dir = tmp_path / "keep-images"
    _write_task_files(task_dir)
    contract = _make_contract(module, task_dir)
    cleanup_calls: list[tuple[str, bool]] = []

    monkeypatch.setattr(module, "load_task_contract", lambda path: (contract, None))
    monkeypatch.setattr(
        module,
        "build_compose_env",
        lambda *args, **kwargs: {"T_BENCH_TASK_DOCKER_CLIENT_IMAGE_NAME": "demo:latest"},
    )
    monkeypatch.setattr(
        module,
        "_resolve_container_id",
        lambda *args, **kwargs: ("pre-container" if "pre" in kwargs["project_name"] else "post-container", None),
    )
    monkeypatch.setattr(
        module,
        "_prepare_container",
        lambda *args, **kwargs: (True, "", {"container_id": kwargs["container_id"]}),
    )
    monkeypatch.setattr(module, "_run_solution", lambda *args, **kwargs: (0, "solution ok", ""))
    monkeypatch.setattr(
        module,
        "cleanup_task",
        lambda *args, **kwargs: cleanup_calls.append(
            (kwargs["project_name"], kwargs["cleanup_images"])
        ),
    )

    def fake_run_command(cmd, cwd=None, timeout=300, env=None):
        del cwd, timeout, env
        if cmd == ["docker", "compose", "build"]:
            return 0, "build ok", ""
        if cmd[:5] == ["docker", "compose", "-p", cmd[3], "down"]:
            return 0, "", ""
        if cmd[:5] == ["docker", "compose", "-p", cmd[3], "up"]:
            return 0, "up ok", ""
        if cmd[:4] == ["docker", "exec", "pre-container", "sh"]:
            return 1, "", "expected fail"
        if cmd[:4] == ["docker", "exec", "post-container", "sh"]:
            return 0, "tests passed", ""
        pytest.fail(f"unexpected command: {cmd}")

    monkeypatch.setattr(module, "run_command", fake_run_command)

    result = module.verify_single_task(_make_candidate(task_dir), cleanup_images=False)

    assert result.verified is True
    assert cleanup_calls == [
        ("tb-verify-from-terminal-bench-keep-images-pre", False),
        ("tb-verify-from-terminal-bench-keep-images-post", False),
    ]


def test_gravitational_teleport_contract_points_to_app(verify_tasks_module):
    module = verify_tasks_module
    repo_root = Path(__file__).resolve().parents[3]
    task_dir = (
        repo_root
        / "verified_tasks"
        / "test"
        / "from-swebench-pro"
        / "instance_gravitational__teleport-3fa6904377c006497169945428e8197158667910-v626ec2a48416b10a88641359a169d99e935ff037"
    )

    contract, failure = module.load_task_contract(task_dir)

    assert failure is None
    assert contract is not None
    assert contract.workdir == "/app"
    assert "cd /app" in (task_dir / "run-tests.sh").read_text()
    assert "WORKDIR /app" in (task_dir / "Dockerfile").read_text()
