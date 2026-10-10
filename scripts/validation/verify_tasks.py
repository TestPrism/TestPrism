#!/usr/bin/env python3
"""Docker-based golden-standard validator for task legality."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from math import ceil
from pathlib import Path
from threading import Lock
from typing import Any, Optional

import yaml

from testprism.parsers.parser_factory import ParserName
from testprism.terminal.models import TerminalCommand

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CANDIDATES = REPO_ROOT / "candidates.json"
DEFAULT_DATASET_PATH = REPO_ROOT / "verified_tasks" / "test"
DEFAULT_RESULTS = REPO_ROOT / "verification_results.json"
RUNTIME_DIR = (
    REPO_ROOT / "scripts" / "validation" / "results" / "runtime" / "verified-subset"
)
CONTAINER_TEST_DIR = "/tests"
CONTAINER_VERIFY_DIR = "/tmp/testprism-verify"
DEFAULT_WORKDIR = "/app"
DEFAULT_LOGS_DIR = "/tmp/terminal-bench/logs"
DEFAULT_AGENT_LOGS_DIR = "/tmp/terminal-bench/agent-logs"
VALID_VERDICT = "valid"
STRUCTURE_FAIL_PHASE = "structure"
SUPPORTED_VERDICTS = {
    VALID_VERDICT,
    "invalid_structure",
    "invalid_build",
    "invalid_initial_test_behavior",
    "invalid_solution_behavior",
    "invalid_final_test",
    "unsupported_multi_container",
    "unsupported_solution_format",
    "unsupported_same_shell_semantics",
    "infra_error",
}
VALID_PARSER_NAMES = {parser.value for parser in ParserName}
DIFFICULTY_SCORE_MAP = {"easy": 1.0, "medium": 2.0, "hard": 3.0}


@dataclass
class VerificationResult:
    task_id: str
    source: str
    source_dir: str
    difficulty_score: float
    verified: bool
    verdict: str = ""
    fail_phase: str = ""
    fail_reason: str = ""
    duration: float = 0.0
    phases: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ContractLoadFailure:
    verdict: str
    message: str


@dataclass(frozen=True)
class TaskContract:
    task_path: Path
    compose_path: Path
    run_tests_host_path: Path
    solution_host_path: Path
    solution_kind: str
    solution_commands: list[TerminalCommand] | None
    tests_dir: Path
    workdir: str
    parser_name: str
    target_service: str
    run_tests_setup_hook: bool
    run_tests_in_same_shell: bool
    has_root_dockerfile: bool
    max_agent_timeout_sec: float
    max_test_timeout_sec: float

    @property
    def run_tests_container_path(self) -> str:
        return f"{CONTAINER_TEST_DIR}/{self.run_tests_host_path.name}"

    @property
    def solution_container_dir(self) -> str:
        return CONTAINER_VERIFY_DIR

    @property
    def solution_container_path(self) -> str:
        return f"{self.solution_container_dir}/{self.solution_host_path.name}"


def run_command(
    cmd: list[str],
    cwd: Optional[Path] = None,
    timeout: int = 300,
    env: Optional[dict[str, str]] = None,
) -> tuple[int, str, str]:
    """Run a command and return ``(returncode, stdout, stderr)``."""
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=merged_env,
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"Command timed out after {timeout}s"
    except Exception as exc:  # pragma: no cover
        return -1, "", str(exc)


def _truncate_output(stdout: str, stderr: str, limit: int = 500) -> str:
    return (stdout + stderr)[-limit:]


def _docker_basename(source: str, task_id: str) -> str:
    tid = re.sub(r"[^a-z0-9._-]+", "-", task_id.lower()).strip("-_.") or "task"
    src = re.sub(r"[^a-z0-9._-]+", "-", source.lower()).strip("-_.") or "src"
    return f"tb-verify-{src}-{tid}"


def _docker_project_name(source: str, task_id: str, suffix: str) -> str:
    return f"{_docker_basename(source, task_id)}-{suffix}"


def build_compose_env(
    task_path: Path,
    source: str,
    task_id: str,
    *,
    runtime_label: str,
    container_suffix: str = "",
) -> dict[str, str]:
    """Build ``T_BENCH_*`` environment variables for docker compose."""
    del task_path
    base_name = _docker_basename(source, task_id)
    runtime_root = RUNTIME_DIR / source / task_id / runtime_label
    logs_host = runtime_root / "logs"
    agent_logs_host = runtime_root / "agent-logs"
    logs_host.mkdir(parents=True, exist_ok=True)
    agent_logs_host.mkdir(parents=True, exist_ok=True)
    container_name = f"{base_name}-{container_suffix}" if container_suffix else base_name

    return {
        "T_BENCH_TASK_DOCKER_CLIENT_IMAGE_NAME": f"{base_name}:latest",
        "T_BENCH_TASK_DOCKER_SERVER_IMAGE_NAME": f"{base_name}-server:latest",
        "T_BENCH_TASK_DOCKER_CLIENT_CONTAINER_NAME": container_name,
        "T_BENCH_TEST_DIR": CONTAINER_TEST_DIR,
        "T_BENCH_TASK_LOGS_PATH": str(logs_host),
        "T_BENCH_CONTAINER_LOGS_PATH": DEFAULT_LOGS_DIR,
        "T_BENCH_TASK_AGENT_LOGS_PATH": str(agent_logs_host),
        "T_BENCH_CONTAINER_AGENT_LOGS_PATH": DEFAULT_AGENT_LOGS_DIR,
    }


def cleanup_task(
    task_path: Path,
    compose_env: dict[str, str],
    *,
    project_name: str,
    cleanup_images: bool = False,
) -> None:
    """Stop and remove compose resources for a specific project."""
    down_cmd = ["docker", "compose", "-p", project_name, "down", "-v"]
    if cleanup_images:
        down_cmd.extend(["--rmi", "local"])
    run_command(down_cmd, cwd=task_path, timeout=120, env=compose_env)


def _resolve_compose_path(task_path: Path) -> Optional[Path]:
    for name in ("docker-compose.yaml", "docker-compose.yml"):
        candidate = task_path / name
        if candidate.exists():
            return candidate
    return None


def _coerce_timeout(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _load_yaml_mapping(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        return None, str(exc)
    if not isinstance(data, dict):
        return None, "expected a YAML mapping"
    return data, None


def _resolve_target_service(
    compose_path: Path,
) -> tuple[str | None, ContractLoadFailure | None]:
    compose_data, compose_error = _load_yaml_mapping(compose_path)
    if compose_data is None:
        return None, ContractLoadFailure(
            verdict="invalid_structure",
            message=f"Could not parse {compose_path.name}: {compose_error}",
        )

    services = compose_data.get("services")
    if not isinstance(services, dict) or not services:
        return None, ContractLoadFailure(
            verdict="invalid_structure",
            message="docker-compose file must define at least one service",
        )

    service_names = [str(name) for name in services.keys()]
    if len(service_names) > 1 and "client" not in services:
        return None, ContractLoadFailure(
            verdict="unsupported_multi_container",
            message=(
                "compose defines multiple services but no explicit 'client' service; "
                "validator refuses ambiguous target selection"
            ),
        )
    return ("client" if "client" in services else service_names[0]), None


def load_task_contract(
    task_path: Path,
) -> tuple[TaskContract | None, ContractLoadFailure | None]:
    """Load the runtime contract needed for contract-aware local verification."""
    task_yaml_path = task_path / "task.yaml"
    run_tests_path = task_path / "run-tests.sh"
    solution_path = task_path / "solution.sh"
    solution_yaml_path = task_path / "solution.yaml"
    compose_path = _resolve_compose_path(task_path)

    if compose_path is None:
        return None, ContractLoadFailure("invalid_structure", "Missing docker-compose.yaml")
    if not task_yaml_path.exists():
        return None, ContractLoadFailure("invalid_structure", "Missing task.yaml")
    if not run_tests_path.exists():
        return None, ContractLoadFailure("invalid_structure", "Missing run-tests.sh")
    if not solution_path.exists() and not solution_yaml_path.exists():
        return None, ContractLoadFailure(
            "invalid_structure", "Missing solution.sh or solution.yaml"
        )

    task_data, task_error = _load_yaml_mapping(task_yaml_path)
    if task_data is None:
        return None, ContractLoadFailure(
            "invalid_structure", f"Could not parse task.yaml: {task_error}"
        )

    workdir = str(task_data.get("workdir") or DEFAULT_WORKDIR).strip()
    if not workdir.startswith("/"):
        return None, ContractLoadFailure(
            "invalid_structure",
            f"workdir must be an absolute container path, got {workdir!r}",
        )

    parser_name = str(task_data.get("parser_name") or ParserName.PYTEST.value).strip()
    if parser_name not in VALID_PARSER_NAMES:
        return None, ContractLoadFailure(
            "invalid_structure",
            f"Unknown parser_name {parser_name!r}; expected one of {sorted(VALID_PARSER_NAMES)}",
        )

    target_service, compose_failure = _resolve_target_service(compose_path)
    if compose_failure is not None:
        return None, compose_failure

    solution_kind = "sh"
    solution_host_path = solution_path
    solution_commands: list[TerminalCommand] | None = None
    if not solution_path.exists():
        solution_kind = "yaml"
        solution_host_path = solution_yaml_path
        try:
            solution_commands = TerminalCommand.from_yaml_list(solution_yaml_path)
        except Exception as exc:
            return None, ContractLoadFailure(
                "unsupported_solution_format",
                f"Could not parse solution.yaml into TerminalCommand list: {exc}",
            )
        if not solution_commands:
            return None, ContractLoadFailure(
                "unsupported_solution_format", "solution.yaml must contain at least one command"
            )

    return TaskContract(
        task_path=task_path,
        compose_path=compose_path,
        run_tests_host_path=run_tests_path,
        solution_host_path=solution_host_path,
        solution_kind=solution_kind,
        solution_commands=solution_commands,
        tests_dir=task_path / "tests",
        workdir=workdir,
        parser_name=parser_name,
        target_service=target_service or "client",
        run_tests_setup_hook=bool(task_data.get("run_tests_setup_hook", False)),
        run_tests_in_same_shell=bool(task_data.get("run_tests_in_same_shell", False)),
        has_root_dockerfile=(task_path / "Dockerfile").exists(),
        max_agent_timeout_sec=_coerce_timeout(
            task_data.get("max_agent_timeout_sec"), 360.0
        ),
        max_test_timeout_sec=_coerce_timeout(
            task_data.get("max_test_timeout_sec"), 60.0
        ),
    ), None


def _timeout_from_metadata(value: float, minimum: int) -> int:
    return max(minimum, int(ceil(value)))


def _compose_cmd(project_name: str, *args: str) -> list[str]:
    return ["docker", "compose", "-p", project_name, *args]


def _container_bash_command(
    container_id: str,
    *,
    workdir: str,
    script_path: str,
    args: Optional[list[str]] = None,
) -> list[str]:
    quoted_workdir = shlex.quote(workdir)
    quoted_script = shlex.quote(script_path)
    quoted_args = " ".join(shlex.quote(arg) for arg in (args or []))
    suffix = f" {quoted_args}" if quoted_args else ""
    command = f"cd {quoted_workdir} && bash {quoted_script}{suffix}"
    return ["docker", "exec", container_id, "sh", "-lc", command]


def _container_shell_command(container_id: str, *, workdir: str, command: str) -> list[str]:
    quoted_workdir = shlex.quote(workdir)
    return ["docker", "exec", container_id, "sh", "-lc", f"cd {quoted_workdir} && {command}"]


def _resolve_container_id(
    task_path: Path,
    compose_env: dict[str, str],
    *,
    project_name: str,
    target_service: str,
) -> tuple[str | None, str | None]:
    rc, out, err = run_command(
        _compose_cmd(project_name, "ps", "-q", target_service),
        cwd=task_path,
        timeout=30,
        env=compose_env,
    )
    if rc != 0:
        return None, (err or out)[:300]
    container_id = out.strip().splitlines()[0] if out.strip() else ""
    if not container_id:
        return None, "Could not resolve container ID"
    return container_id, None


def _record_phase(
    phases: dict[str, Any],
    name: str,
    *,
    duration: float,
    exit_code: int | None = None,
    output_tail: str | None = None,
    **extra: Any,
) -> None:
    phase: dict[str, Any] = {"duration": round(duration, 3)}
    if exit_code is not None:
        phase["exit_code"] = exit_code
    if output_tail:
        phase["output_tail"] = output_tail
    phase.update(extra)
    phases[name] = phase


def _prepare_container(
    contract: TaskContract,
    *,
    container_id: str,
    setup_timeout: int,
) -> tuple[bool, str, dict[str, Any]]:
    phase_start = time.time()
    metadata: dict[str, Any] = {"container_id": container_id}

    for cmd in (
        ["docker", "exec", container_id, "mkdir", "-p", CONTAINER_TEST_DIR],
        ["docker", "exec", container_id, "mkdir", "-p", contract.solution_container_dir],
    ):
        rc, out, err = run_command(cmd, timeout=10)
        if rc != 0:
            metadata["exit_code"] = rc
            metadata["output_tail"] = _truncate_output(out, err, 300)
            metadata["duration"] = round(time.time() - phase_start, 3)
            return False, "Failed to create validator scratch directories", metadata

    if contract.tests_dir.exists():
        rc, out, err = run_command(
            ["docker", "cp", f"{contract.tests_dir}/.", f"{container_id}:{CONTAINER_TEST_DIR}"],
            timeout=30,
        )
        if rc != 0:
            metadata["exit_code"] = rc
            metadata["output_tail"] = _truncate_output(out, err, 300)
            metadata["duration"] = round(time.time() - phase_start, 3)
            return False, "Failed to copy tests directory", metadata

    rc, out, err = run_command(
        [
            "docker",
            "cp",
            str(contract.run_tests_host_path),
            f"{container_id}:{contract.run_tests_container_path}",
        ],
        timeout=30,
    )
    if rc != 0:
        metadata["exit_code"] = rc
        metadata["output_tail"] = _truncate_output(out, err, 300)
        metadata["duration"] = round(time.time() - phase_start, 3)
        return False, "Failed to copy run-tests.sh", metadata

    rc, out, err = run_command(
        ["docker", "exec", container_id, "chmod", "+x", contract.run_tests_container_path],
        timeout=10,
    )
    if rc != 0:
        metadata["exit_code"] = rc
        metadata["output_tail"] = _truncate_output(out, err, 300)
        metadata["duration"] = round(time.time() - phase_start, 3)
        return False, "Failed to chmod run-tests.sh", metadata

    metadata["setup_hook_enabled"] = contract.run_tests_setup_hook
    if contract.run_tests_setup_hook:
        rc, out, err = run_command(
            _container_bash_command(
                container_id,
                workdir=contract.workdir,
                script_path=contract.run_tests_container_path,
                args=["--setup"],
            ),
            timeout=setup_timeout,
        )
        if rc != 0:
            metadata["exit_code"] = rc
            metadata["output_tail"] = _truncate_output(out, err, 300)
            metadata["duration"] = round(time.time() - phase_start, 3)
            return False, "run-tests.sh --setup failed", metadata

    metadata["duration"] = round(time.time() - phase_start, 3)
    return True, "", metadata


def _run_solution(
    contract: TaskContract,
    *,
    container_id: str,
    solution_timeout: int,
) -> tuple[int, str, str]:
    if contract.solution_kind == "sh":
        rc, out, err = run_command(
            [
                "docker",
                "cp",
                str(contract.solution_host_path),
                f"{container_id}:{contract.solution_container_path}",
            ],
            timeout=30,
        )
        if rc != 0:
            return rc, out, err
        return run_command(
            _container_bash_command(
                container_id,
                workdir=contract.workdir,
                script_path=contract.solution_container_path,
            ),
            timeout=solution_timeout,
        )

    combined_stdout: list[str] = []
    combined_stderr: list[str] = []
    assert contract.solution_commands is not None
    for index, command in enumerate(contract.solution_commands):
        rc, out, err = run_command(
            _container_shell_command(
                container_id,
                workdir=contract.workdir,
                command=command.command,
            ),
            timeout=_timeout_from_metadata(command.max_timeout_sec, 30),
        )
        combined_stdout.append(f"# command[{index}]: {command.command}\n{out}")
        combined_stderr.append(err)
        if rc != 0:
            return rc, "\n".join(combined_stdout), "\n".join(combined_stderr)
        if command.min_timeout_sec > 0:
            time.sleep(command.min_timeout_sec)
    return 0, "\n".join(combined_stdout), "\n".join(combined_stderr)


def _looks_like_infra_error(message: str) -> bool:
    lowered = message.lower()
    return any(
        token in lowered
        for token in (
            "docker daemon",
            "cannot connect to the docker daemon",
            "context deadline exceeded",
            "timed out",
            "connection refused",
            "temporary failure",
            "network",
        )
    )


def derive_verdict(result: VerificationResult) -> str:
    if result.verified:
        return VALID_VERDICT
    if result.verdict in SUPPORTED_VERDICTS:
        return result.verdict
    if result.fail_phase == STRUCTURE_FAIL_PHASE:
        return "invalid_structure"
    if result.fail_phase in {"build", "container_up"}:
        if _looks_like_infra_error(result.fail_reason):
            return "infra_error"
        return "invalid_build"
    if result.fail_phase == "setup":
        return "infra_error" if _looks_like_infra_error(result.fail_reason) else "invalid_build"
    if result.fail_phase == "initial_test":
        return "invalid_initial_test_behavior"
    if result.fail_phase == "solution":
        return "invalid_solution_behavior"
    if result.fail_phase == "final_test":
        return "invalid_final_test"
    return "infra_error"


def _build_failure_result(
    result: VerificationResult,
    *,
    fail_phase: str,
    fail_reason: str,
    verdict: str | None = None,
) -> VerificationResult:
    result.fail_phase = fail_phase
    result.fail_reason = fail_reason[:500]
    result.verdict = verdict or derive_verdict(result)
    result.duration = round(result.duration, 3)
    result.verified = result.verdict == VALID_VERDICT
    return result


def verify_single_task(
    candidate: dict[str, Any],
    *,
    cleanup_images: bool = True,
) -> VerificationResult:
    """Run the full F2P verification for a single task."""
    task_id = candidate["task_id"]
    source = candidate["source"]
    source_dir = candidate["source_dir"]
    difficulty = float(candidate.get("difficulty_score", 0.0) or 0.0)
    task_path = Path(candidate["task_path"])

    result = VerificationResult(
        task_id=task_id,
        source=source,
        source_dir=source_dir,
        difficulty_score=difficulty,
        verified=False,
    )
    start_time = time.time()
    pre_env: dict[str, str] | None = None
    post_env: dict[str, str] | None = None
    pre_project_name = _docker_project_name(source, task_id, "pre")
    post_project_name = _docker_project_name(source, task_id, "post")

    try:
        contract, contract_failure = load_task_contract(task_path)
        if contract is None:
            result.duration = time.time() - start_time
            return _build_failure_result(
                result,
                fail_phase=STRUCTURE_FAIL_PHASE,
                fail_reason=(
                    contract_failure.message
                    if contract_failure
                    else "Task contract could not be loaded"
                ),
                verdict=(
                    contract_failure.verdict
                    if contract_failure
                    else "invalid_structure"
                ),
            )

        build_timeout = _timeout_from_metadata(
            contract.max_agent_timeout_sec + contract.max_test_timeout_sec, 600
        )
        up_timeout = _timeout_from_metadata(contract.max_test_timeout_sec, 120)
        setup_timeout = _timeout_from_metadata(contract.max_test_timeout_sec, 300)
        test_timeout = _timeout_from_metadata(contract.max_test_timeout_sec, 60)
        solution_timeout = _timeout_from_metadata(contract.max_agent_timeout_sec, 300)

        build_env = build_compose_env(task_path, source, task_id, runtime_label="build")
        pre_env = build_compose_env(
            task_path,
            source,
            task_id,
            runtime_label="pre",
            container_suffix="pre",
        )
        post_env = build_compose_env(
            task_path,
            source,
            task_id,
            runtime_label="post",
            container_suffix="post",
        )

        result.phases["contract"] = {
            "workdir": contract.workdir,
            "parser_name": contract.parser_name,
            "target_service": contract.target_service,
            "solution_kind": contract.solution_kind,
            "run_tests_setup_hook": contract.run_tests_setup_hook,
            "run_tests_in_same_shell": contract.run_tests_in_same_shell,
            "has_tests_dir": contract.tests_dir.exists(),
            "has_root_dockerfile": contract.has_root_dockerfile,
            "max_agent_timeout_sec": contract.max_agent_timeout_sec,
            "max_test_timeout_sec": contract.max_test_timeout_sec,
            "run_tests_container_path": contract.run_tests_container_path,
            "solution_container_path": contract.solution_container_path,
        }

        phase_start = time.time()
        rc, out, err = run_command(
            ["docker", "compose", "build"],
            cwd=task_path,
            timeout=build_timeout,
            env=build_env,
        )
        _record_phase(
            result.phases,
            "build",
            duration=time.time() - phase_start,
            exit_code=rc,
            output_tail=_truncate_output(out, err),
        )
        if rc != 0:
            result.duration = time.time() - start_time
            return _build_failure_result(
                result,
                fail_phase="build",
                fail_reason=err or out or "docker compose build failed",
            )

        for env, project_name, phase_name in (
            (pre_env, pre_project_name, "up_pre"),
            (post_env, post_project_name, "up_post"),
        ):
            phase_start = time.time()
            run_command(
                _compose_cmd(project_name, "down", "-v"),
                cwd=task_path,
                timeout=60,
                env=env,
            )
            rc, out, err = run_command(
                _compose_cmd(project_name, "up", "-d"),
                cwd=task_path,
                timeout=up_timeout,
                env=env,
            )
            _record_phase(
                result.phases,
                phase_name,
                duration=time.time() - phase_start,
                exit_code=rc,
                output_tail=_truncate_output(out, err),
                project_name=project_name,
            )
            if rc != 0:
                result.duration = time.time() - start_time
                return _build_failure_result(
                    result,
                    fail_phase="container_up",
                    fail_reason=err or out or f"{phase_name} failed",
                )

        pre_container_id, pre_container_error = _resolve_container_id(
            task_path,
            pre_env,
            project_name=pre_project_name,
            target_service=contract.target_service,
        )
        if pre_container_id is None:
            result.duration = time.time() - start_time
            return _build_failure_result(
                result,
                fail_phase="container_up",
                fail_reason=pre_container_error or "Could not resolve pre container",
            )

        post_container_id, post_container_error = _resolve_container_id(
            task_path,
            post_env,
            project_name=post_project_name,
            target_service=contract.target_service,
        )
        if post_container_id is None:
            result.duration = time.time() - start_time
            return _build_failure_result(
                result,
                fail_phase="container_up",
                fail_reason=post_container_error or "Could not resolve post container",
            )

        ok, message, setup_metadata = _prepare_container(
            contract,
            container_id=pre_container_id,
            setup_timeout=setup_timeout,
        )
        result.phases["setup_pre"] = setup_metadata
        if not ok:
            result.duration = time.time() - start_time
            return _build_failure_result(result, fail_phase="setup", fail_reason=message)

        ok, message, setup_metadata = _prepare_container(
            contract,
            container_id=post_container_id,
            setup_timeout=setup_timeout,
        )
        result.phases["setup_post"] = setup_metadata
        if not ok:
            result.duration = time.time() - start_time
            return _build_failure_result(result, fail_phase="setup", fail_reason=message)

        phase_start = time.time()
        rc, out, err = run_command(
            _container_bash_command(
                pre_container_id,
                workdir=contract.workdir,
                script_path=contract.run_tests_container_path,
            ),
            timeout=test_timeout,
        )
        _record_phase(
            result.phases,
            "initial_test",
            duration=time.time() - phase_start,
            exit_code=rc,
            output_tail=_truncate_output(out, err),
            container_id=pre_container_id,
        )
        if rc == 0 or rc == -1:
            reason = (
                "Tests passed BEFORE solution (expected failure)"
                if rc == 0
                else f"Initial test could not complete cleanly: {(err or out)[:300]}"
            )
            result.duration = time.time() - start_time
            return _build_failure_result(
                result,
                fail_phase="initial_test",
                fail_reason=reason,
            )

        phase_start = time.time()
        rc, out, err = _run_solution(
            contract,
            container_id=post_container_id,
            solution_timeout=solution_timeout,
        )
        _record_phase(
            result.phases,
            "solution",
            duration=time.time() - phase_start,
            exit_code=rc,
            output_tail=_truncate_output(out, err),
            container_id=post_container_id,
            solution_kind=contract.solution_kind,
        )
        if rc != 0:
            result.duration = time.time() - start_time
            return _build_failure_result(
                result,
                fail_phase="solution",
                fail_reason=f"Solution execution failed (exit {rc}): {(err or out)[:300]}",
            )

        phase_start = time.time()
        rc, out, err = run_command(
            _container_bash_command(
                post_container_id,
                workdir=contract.workdir,
                script_path=contract.run_tests_container_path,
            ),
            timeout=test_timeout,
        )
        _record_phase(
            result.phases,
            "final_test",
            duration=time.time() - phase_start,
            exit_code=rc,
            output_tail=_truncate_output(out, err),
            container_id=post_container_id,
        )
        if rc != 0:
            result.duration = time.time() - start_time
            return _build_failure_result(
                result,
                fail_phase="final_test",
                fail_reason=f"Tests FAILED after solution (exit {rc}): {(err or out)[:300]}",
            )

        result.verified = True
        result.verdict = VALID_VERDICT
        result.duration = round(time.time() - start_time, 3)
        return result

    except Exception as exc:  # pragma: no cover
        result.duration = time.time() - start_time
        return _build_failure_result(
            result,
            fail_phase="exception",
            fail_reason=str(exc),
            verdict="infra_error",
        )
    finally:
        if pre_env is not None:
            cleanup_task(
                task_path,
                pre_env,
                project_name=pre_project_name,
                cleanup_images=False,
            )
        if post_env is not None:
            cleanup_task(
                task_path,
                post_env,
                project_name=post_project_name,
                cleanup_images=cleanup_images,
            )


class ProgressTracker:
    """Thread-safe progress tracking."""

    def __init__(self, total: int):
        self.total = total
        self.completed = 0
        self.verified = 0
        self.failed = 0
        self.lock = Lock()
        self.start_time = time.time()
        self.source_stats: dict[str, dict[str, int]] = {}

    def update(self, result: VerificationResult) -> None:
        with self.lock:
            self.completed += 1
            if result.verified:
                self.verified += 1
            else:
                self.failed += 1

            src = result.source
            if src not in self.source_stats:
                self.source_stats[src] = {"verified": 0, "failed": 0, "total": 0}
            self.source_stats[src]["total"] += 1
            if result.verified:
                self.source_stats[src]["verified"] += 1
            else:
                self.source_stats[src]["failed"] += 1

            elapsed = time.time() - self.start_time
            rate = self.completed / elapsed if elapsed > 0 else 0
            remaining = (self.total - self.completed) / rate if rate > 0 else 0
            status = VALID_VERDICT if result.verified else (result.verdict or result.fail_phase)

            print(
                f"  [{self.completed}/{self.total}] {result.source}/{result.task_id}: "
                f"{status} ({result.duration:.1f}s) | "
                f"Verified: {self.verified} | Failed: {self.failed} | "
                f"ETA: {remaining/60:.0f}m"
            )


def load_existing_results(results_path: Path) -> dict[str, dict[str, Any]]:
    """Load already-completed results for resume support."""
    if not results_path.exists():
        return {}
    with open(results_path) as f:
        data = json.load(f)
    return {f"{r['source']}/{r['task_id']}": r for r in data.get("results", [])}


def save_results(
    results: list[VerificationResult],
    results_path: Path,
    candidates_total: int,
) -> None:
    """Save verification results to JSON."""
    results_data = []
    verdict_counts = Counter()
    for result in results:
        verdict = result.verdict or derive_verdict(result)
        verdict_counts[verdict] += 1
        results_data.append(
            {
                "task_id": result.task_id,
                "source": result.source,
                "source_dir": result.source_dir,
                "difficulty_score": result.difficulty_score,
                "verified": result.verified,
                "verdict": verdict,
                "fail_phase": result.fail_phase,
                "fail_reason": result.fail_reason,
                "duration": round(result.duration, 2),
                "phases": result.phases,
            }
        )

    verified_count = sum(1 for result in results if result.verified)
    failed_count = sum(1 for result in results if not result.verified)
    source_summary: dict[str, dict[str, Any]] = {}
    for result in results:
        verdict = result.verdict or derive_verdict(result)
        if result.source not in source_summary:
            source_summary[result.source] = {
                "verified": 0,
                "failed": 0,
                "total": 0,
                "verdicts": {},
            }
        source_summary[result.source]["total"] += 1
        source_summary[result.source]["verdicts"][verdict] = (
            source_summary[result.source]["verdicts"].get(verdict, 0) + 1
        )
        if result.verified:
            source_summary[result.source]["verified"] += 1
        else:
            source_summary[result.source]["failed"] += 1

    output = {
        "metadata": {
            "candidates_total": candidates_total,
            "completed": len(results),
            "verified": verified_count,
            "failed": failed_count,
            "pass_rate": round(verified_count / len(results), 4) if results else 0,
            "verdicts": dict(sorted(verdict_counts.items())),
            "timestamp": datetime.now().isoformat(),
        },
        "source_summary": source_summary,
        "results": results_data,
    }

    results_path.parent.mkdir(parents=True, exist_ok=True)
    with open(results_path, "w") as f:
        json.dump(output, f, indent=2)


def _candidate_key(candidate: dict[str, Any]) -> str:
    return f"{candidate['source']}/{candidate['task_id']}"


def _derive_candidate_identity(task_dir: Path, dataset_path: Path) -> tuple[str, str]:
    relative = task_dir.relative_to(dataset_path)
    parts = relative.parts
    if len(parts) >= 3 and parts[0] in {"train", "test", "dev"}:
        return parts[1], "/".join(parts[:2])
    if len(parts) >= 2:
        return parts[0], parts[0]
    return "unknown", "."


def _task_difficulty_score(task_dir: Path) -> float:
    task_yaml_path = task_dir / "task.yaml"
    task_data, _ = _load_yaml_mapping(task_yaml_path)
    if task_data is None:
        return 0.0
    difficulty = str(task_data.get("difficulty", "")).strip().lower()
    return DIFFICULTY_SCORE_MAP.get(difficulty, 0.0)


def _dataset_root_for_single_task(task_dir: Path) -> Path:
    for ancestor in task_dir.parents:
        if ancestor.name in {"train", "test", "dev"}:
            return ancestor.parent
    return task_dir.parent


def discover_candidates_from_dataset(dataset_path: Path) -> list[dict[str, Any]]:
    if (dataset_path / "task.yaml").exists():
        task_dirs = [dataset_path]
        root = _dataset_root_for_single_task(dataset_path)
    else:
        task_dirs = sorted(path.parent for path in dataset_path.rglob("task.yaml"))
        root = dataset_path

    candidates = []
    for task_dir in task_dirs:
        source, source_dir = _derive_candidate_identity(task_dir, root)
        candidates.append(
            {
                "task_id": task_dir.name,
                "source": source,
                "source_dir": source_dir,
                "difficulty_score": _task_difficulty_score(task_dir),
                "task_path": str(task_dir),
            }
        )
    return candidates


def load_candidates_from_json(candidates_path: Path) -> tuple[list[dict[str, Any]], int]:
    with open(candidates_path) as f:
        data = json.load(f)
    candidates = data["candidates"]
    return candidates, int(data.get("total_candidates", len(candidates)))


def _build_source_profile(
    task_dirs: list[Path],
    dataset_path: Path | None = None,
) -> dict[str, Any]:
    by_source: dict[str, dict[str, Any]] = {}
    for task_dir in task_dirs:
        source, source_dir = _derive_candidate_identity(
            task_dir,
            dataset_path or task_dir.parent,
        )
        task_data, task_error = _load_yaml_mapping(task_dir / "task.yaml")
        if task_data is None:
            task_data = {}
            parse_error = task_error
        else:
            parse_error = None

        source_profile = by_source.setdefault(
            source,
            {
                "task_count": 0,
                "source_dirs": Counter(),
                "workdir": Counter(),
                "parser_name": Counter(),
                "solution_kind": Counter(),
                "missing_tests_count": 0,
                "missing_root_dockerfile_count": 0,
                "run_tests_in_same_shell_count": 0,
                "task_yaml_parse_error_count": 0,
            },
        )
        source_profile["task_count"] += 1
        source_profile["source_dirs"][source_dir] += 1
        source_profile["workdir"][str(task_data.get("workdir") or DEFAULT_WORKDIR)] += 1
        source_profile["parser_name"][
            str(task_data.get("parser_name") or ParserName.PYTEST.value)
        ] += 1
        if (task_dir / "solution.sh").exists():
            source_profile["solution_kind"]["sh"] += 1
        elif (task_dir / "solution.yaml").exists():
            source_profile["solution_kind"]["yaml"] += 1
        else:
            source_profile["solution_kind"]["missing"] += 1
        if not (task_dir / "tests").exists():
            source_profile["missing_tests_count"] += 1
        if not (task_dir / "Dockerfile").exists():
            source_profile["missing_root_dockerfile_count"] += 1
        if bool(task_data.get("run_tests_in_same_shell", False)):
            source_profile["run_tests_in_same_shell_count"] += 1
        if parse_error:
            source_profile["task_yaml_parse_error_count"] += 1

    normalized_sources = {}
    for source, profile in sorted(by_source.items()):
        normalized_sources[source] = {
            "task_count": profile["task_count"],
            "source_dirs": dict(sorted(profile["source_dirs"].items())),
            "workdir": dict(sorted(profile["workdir"].items())),
            "parser_name": dict(sorted(profile["parser_name"].items())),
            "solution_kind": dict(sorted(profile["solution_kind"].items())),
            "missing_tests_count": profile["missing_tests_count"],
            "missing_root_dockerfile_count": profile["missing_root_dockerfile_count"],
            "run_tests_in_same_shell_count": profile["run_tests_in_same_shell_count"],
            "task_yaml_parse_error_count": profile["task_yaml_parse_error_count"],
        }

    return {
        "generated_at": datetime.now().isoformat(),
        "task_count": sum(profile["task_count"] for profile in by_source.values()),
        "sources": normalized_sources,
    }


def write_source_profile(
    output_path: Path,
    *,
    dataset_path: Path | None = None,
    candidates: list[dict[str, Any]] | None = None,
) -> None:
    if dataset_path is not None:
        task_dirs = [
            Path(candidate["task_path"])
            for candidate in discover_candidates_from_dataset(dataset_path)
        ]
        profile = _build_source_profile(task_dirs, dataset_path)
    elif candidates is not None:
        task_dirs = [Path(candidate["task_path"]) for candidate in candidates]
        common_root = (
            Path(os.path.commonpath([str(path) for path in task_dirs]))
            if task_dirs
            else None
        )
        profile = _build_source_profile(task_dirs, common_root)
    else:
        raise ValueError("dataset_path or candidates must be provided for source profile")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(profile, indent=2))


def _load_candidates(args: argparse.Namespace) -> tuple[list[dict[str, Any]], int, str]:
    if args.candidates is not None:
        candidates, total_candidates = load_candidates_from_json(args.candidates)
        return candidates, total_candidates, f"Loaded {len(candidates)} candidates from {args.candidates}"

    dataset_path = args.dataset_path or DEFAULT_DATASET_PATH
    candidates = discover_candidates_from_dataset(dataset_path)
    return (
        candidates,
        len(candidates),
        f"Discovered {len(candidates)} tasks under {dataset_path}",
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Docker-based F2P task verification")
    parser.add_argument(
        "--candidates",
        type=Path,
        default=None,
        help="Path to candidates.json",
    )
    parser.add_argument(
        "--dataset-path",
        type=Path,
        default=None,
        help="Path to a verified_tasks directory tree or a single task directory",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_RESULTS,
        help="Path to output verification_results.json",
    )
    parser.add_argument(
        "--concurrency",
        "--n-concurrent",
        dest="concurrency",
        type=int,
        default=8,
        help="Number of concurrent Docker validations",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from previous results (skip already-completed tasks)",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Retry previously failed tasks (keep verified, rerun failed)",
    )
    parser.add_argument(
        "--retry-phases",
        type=str,
        default=None,
        help="Comma-separated list of fail phases to retry",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limit number of tasks to verify (for testing)",
    )
    parser.add_argument(
        "--source",
        type=str,
        default=None,
        help="Only verify tasks from this source",
    )
    parser.add_argument(
        "--task-id",
        action="append",
        default=[],
        help="Only verify a specific task_id. Repeatable.",
    )
    parser.add_argument(
        "--source-profile-out",
        type=Path,
        default=None,
        help="Write a one-shot source-profile.json before validation",
    )
    parser.add_argument(
        "--cleanup",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "Remove locally built compose images during post-test cleanup "
            "(default: true). Use --no-cleanup to keep images for debugging."
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    candidates, total_candidates, load_message = _load_candidates(args)
    print(load_message)

    if args.source_profile_out is not None:
        write_source_profile(
            args.source_profile_out,
            dataset_path=args.dataset_path if args.candidates is None else None,
            candidates=candidates if args.candidates is not None else None,
        )
        print(f"Wrote source profile to {args.source_profile_out}")

    if args.source:
        candidates = [candidate for candidate in candidates if candidate["source"] == args.source]
        print(f"Filtered to {len(candidates)} candidates from {args.source}")

    if args.task_id:
        allowed_task_ids = set(args.task_id)
        candidates = [
            candidate for candidate in candidates if candidate["task_id"] in allowed_task_ids
        ]
        print(f"Filtered to {len(candidates)} candidates by task_id")

    if args.limit:
        candidates = candidates[: args.limit]
        print(f"Limited to {len(candidates)} tasks")

    existing_results: dict[str, dict[str, Any]] = {}
    completed_results: list[VerificationResult] = []
    if args.resume or args.retry_failed:
        existing = load_existing_results(args.output)
        retry_phases = (
            {phase.strip() for phase in args.retry_phases.split(",")}
            if args.retry_phases
            else None
        )

        for candidate in candidates:
            key = _candidate_key(candidate)
            if key not in existing:
                continue
            record = existing[key]
            should_skip = False
            if record["verified"]:
                should_skip = True
            elif args.resume and not args.retry_failed:
                should_skip = True
            elif args.retry_failed:
                should_skip = bool(
                    retry_phases and record.get("fail_phase", "") not in retry_phases
                )

            if should_skip:
                existing_results[key] = record

        print(f"Keeping {len(existing_results)} previous results (skip)")
        skipped_verified = sum(1 for record in existing_results.values() if record["verified"])
        skipped_failed = len(existing_results) - skipped_verified
        print(f"  Verified (kept): {skipped_verified}, Failed (kept): {skipped_failed}")

        for record in existing_results.values():
            completed_results.append(
                VerificationResult(
                    task_id=record["task_id"],
                    source=record["source"],
                    source_dir=record["source_dir"],
                    difficulty_score=float(record.get("difficulty_score", 0.0) or 0.0),
                    verified=bool(record["verified"]),
                    verdict=record.get("verdict", VALID_VERDICT if record["verified"] else ""),
                    fail_phase=record.get("fail_phase", ""),
                    fail_reason=record.get("fail_reason", ""),
                    duration=float(record.get("duration", 0.0) or 0.0),
                    phases=record.get("phases", {}),
                )
            )
        candidates = [
            candidate
            for candidate in candidates
            if _candidate_key(candidate) not in existing_results
        ]
        print(f"Remaining to verify: {len(candidates)}")

    if not candidates:
        print("No tasks to verify.")
        if completed_results:
            save_results(completed_results, args.output, total_candidates)
            print(f"Results saved to {args.output}")
        return 0

    print(
        f"\nStarting verification of {len(candidates)} tasks "
        f"with {args.concurrency} workers..."
    )
    print("=" * 80)

    tracker = ProgressTracker(len(candidates))
    new_results: list[VerificationResult] = []
    results_lock = Lock()

    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = {
            executor.submit(
                verify_single_task,
                candidate,
                cleanup_images=args.cleanup,
            ): candidate
            for candidate in candidates
        }

        for future in as_completed(futures):
            candidate = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # pragma: no cover
                result = VerificationResult(
                    task_id=candidate["task_id"],
                    source=candidate["source"],
                    source_dir=candidate["source_dir"],
                    difficulty_score=float(candidate.get("difficulty_score", 0.0) or 0.0),
                    verified=False,
                    verdict="infra_error",
                    fail_phase="executor_error",
                    fail_reason=str(exc)[:500],
                )

            tracker.update(result)
            with results_lock:
                new_results.append(result)

            if tracker.completed % 50 == 0:
                with results_lock:
                    save_results(completed_results + new_results, args.output, total_candidates)

    all_results = completed_results + new_results
    save_results(all_results, args.output, total_candidates)

    print("\n" + "=" * 80)
    print("VERIFICATION SUMMARY")
    print("=" * 80)
    verified_total = sum(1 for result in all_results if result.verified)
    failed_total = sum(1 for result in all_results if not result.verified)
    print(f"Total tasks verified: {verified_total}")
    print(f"Total tasks failed: {failed_total}")
    if all_results:
        print(f"Pass rate: {verified_total / len(all_results):.1%}")
    print(f"\nResults saved to: {args.output}")

    print(f"\n{'Source':<35} {'Verified':>8} {'Failed':>8} {'Total':>8} {'Rate':>8}")
    print("-" * 75)
    source_stats: dict[str, dict[str, int]] = defaultdict(
        lambda: {"verified": 0, "failed": 0}
    )
    for result in all_results:
        if result.verified:
            source_stats[result.source]["verified"] += 1
        else:
            source_stats[result.source]["failed"] += 1
    for source in sorted(source_stats.keys()):
        stats = source_stats[source]
        total = stats["verified"] + stats["failed"]
        rate = stats["verified"] / total if total > 0 else 0
        print(
            f"  {source:<33} {stats['verified']:>8} {stats['failed']:>8} "
            f"{total:>8} {rate:>7.1%}"
        )

    if failed_total > 0:
        verdict_counts = Counter(
            (result.verdict or derive_verdict(result))
            for result in all_results
            if not result.verified
        )
        phase_counts = Counter(result.fail_phase for result in all_results if not result.verified)
        print("\nFailure verdict breakdown:")
        for verdict, count in sorted(verdict_counts.items(), key=lambda item: (-item[1], item[0])):
            print(f"  {verdict:<35}: {count}")
        print("\nFailure phase breakdown:")
        for phase, count in sorted(phase_counts.items(), key=lambda item: (-item[1], item[0])):
            print(f"  {phase:<20}: {count}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
