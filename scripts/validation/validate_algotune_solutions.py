#!/usr/bin/env python3
"""Docker-based Mode B validator for AlgoTune tasks (correctness + faster than baseline)."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import yaml


DEFAULT_DATASET_PATH = (
    Path(__file__).resolve().parents[2] / "verified_tasks" / "train" / "from-algotune"
)
DEFAULT_WORKDIR = "/app"
SOURCE_NAME = "from-algotune"
VALIDATION_MODE = "B"

WARMUP_RUNS_DEFAULT = 1
TIMED_RUNS_DEFAULT = 10
NEAR_PASS_RATIO_DEFAULT = 0.95
NOISE_MARGIN_DEFAULT = 0.03

THRESHOLD_FIELD_CANDIDATES = (
    "min_improvement_threshold",
    "target_speedup",
    "min_speedup",
    "speedup",
)
THRESHOLD_PATTERNS = (
    r"roughly\s+\*\*([0-9]+(?:\.[0-9]+)?)x\s+faster\*\*",
    r"around\s+\*\*([0-9]+(?:\.[0-9]+)?)x\s+faster\*\*",
    r"about\s+\*\*([0-9]+(?:\.[0-9]+)?)x\s+faster\*\*",
    r"roughly\s+\*\*([0-9]+(?:\.[0-9]+)?)x\*\*\s+the speed",
    r"Target speedup over baseline\*\*:\s*~?([0-9]+(?:\.[0-9]+)?)x",
    r"([0-9]+(?:\.[0-9]+)?)x\s+faster",
)
PROBLEM_SIZE_PATTERNS = (
    r"Problem size \(n\)\s*:\s*([0-9]+)",
    r"`n\s*=\s*([0-9]+)`",
)

EVAL_SCRIPT_IN_CONTAINER = "/tmp/testprism-algotune-eval.py"
BASELINE_SOLVER_BACKUP_IN_CONTAINER = "/tmp/testprism-algotune-baseline-solver.py"
SOLUTION_SCRIPT_IN_CONTAINER = "/tmp/testprism-algotune-solution.sh"
DEFAULT_CONTAINER_LOGS_DIR = "/tmp/terminal-bench/logs"
DEFAULT_CONTAINER_AGENT_LOGS_DIR = "/tmp/terminal-bench/agent-logs"

SpeedGate = Literal["faster-than-baseline", "meets-threshold"]


class SpeedGateEnum(str, Enum):
    FASTER_THAN_BASELINE = "faster-than-baseline"
    MEETS_THRESHOLD = "meets-threshold"


EVAL_SCRIPT_SOURCE = r"""#!/usr/bin/env python3
import argparse
import importlib.util
import json
import statistics
import time
from pathlib import Path


def load_solver(solver_path: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, solver_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("failed to load solver module spec")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    solver_cls = getattr(module, "Solver", None)
    if solver_cls is None:
        raise AttributeError("solver.py does not define Solver")
    return solver_cls


def generate_problem(reference_solver, n: int, seed: int):
    fn = getattr(reference_solver, "generate_problem", None)
    if fn is None:
        raise AttributeError("Solver.generate_problem is missing")
    try:
        return fn(n=n, random_seed=seed)
    except TypeError:
        try:
            return fn(n, random_seed=seed)
        except TypeError:
            return fn(n)


def check_correctness(solver_cls, reference_solver, n: int, seeds: list[int]):
    failures = []
    for seed in seeds:
        problem = generate_problem(reference_solver, n, seed)
        solver = solver_cls()
        output = solver.solve(problem)
        if not reference_solver.is_solution(problem, output):
            failures.append({"seed": seed, "message": "is_solution returned False"})
    return not failures, failures


def time_solver_min(solver_cls, reference_solver, n: int, seed: int, warmup_runs: int, timed_runs: int) -> float:
    problem = generate_problem(reference_solver, n, seed)
    solver = solver_cls()
    for _ in range(warmup_runs):
        solver.solve(problem)
    timed_secs = []
    for _ in range(timed_runs):
        solver = solver_cls()
        t0 = time.perf_counter()
        solver.solve(problem)
        timed_secs.append(time.perf_counter() - t0)
    if not timed_secs:
        raise RuntimeError("no timed runs recorded")
    return min(timed_secs)


def aggregate(values: list[float], method: str) -> float:
    if not values:
        raise RuntimeError("empty timing values")
    if method == "min":
        return min(values)
    if method == "mean":
        return statistics.mean(values)
    return statistics.median(values)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline-solver-path", required=True)
    parser.add_argument("--candidate-solver-path", required=True)
    parser.add_argument("--n", type=int, required=True)
    parser.add_argument("--correctness-seeds", type=str, default="1,2,3")
    parser.add_argument("--timing-seeds", type=str, default="1,2,3")
    parser.add_argument("--warmup-runs", type=int, default=1)
    parser.add_argument("--timed-runs", type=int, default=10)
    parser.add_argument("--aggregate", choices=("median", "min", "mean"), default="median")
    args = parser.parse_args()

    correctness_seeds = [int(x.strip()) for x in args.correctness_seeds.split(",") if x.strip()]
    timing_seeds = [int(x.strip()) for x in args.timing_seeds.split(",") if x.strip()]
    if not correctness_seeds:
        raise SystemExit("at least one correctness seed is required")
    if not timing_seeds:
        raise SystemExit("at least one timing seed is required")

    baseline_path = str(Path(args.baseline_solver_path).resolve())
    candidate_path = str(Path(args.candidate_solver_path).resolve())
    baseline_cls = load_solver(baseline_path, "baseline_solver_module")
    candidate_cls = load_solver(candidate_path, "candidate_solver_module")
    reference_solver = baseline_cls()

    baseline_ok, baseline_failures = check_correctness(
        baseline_cls, reference_solver, args.n, correctness_seeds
    )
    candidate_ok, candidate_failures = check_correctness(
        candidate_cls, reference_solver, args.n, correctness_seeds
    )

    seed_rows = []
    baseline_mins = []
    candidate_mins = []
    for seed in timing_seeds:
        baseline_min = time_solver_min(
            baseline_cls, reference_solver, args.n, seed, args.warmup_runs, args.timed_runs
        )
        candidate_min = time_solver_min(
            candidate_cls, reference_solver, args.n, seed, args.warmup_runs, args.timed_runs
        )
        baseline_mins.append(baseline_min)
        candidate_mins.append(candidate_min)
        speedup = baseline_min / candidate_min if candidate_min > 0 else None
        seed_rows.append(
            {
                "seed": seed,
                "baseline_min_sec": baseline_min,
                "candidate_min_sec": candidate_min,
                "speedup": speedup,
            }
        )

    baseline_median_sec = aggregate(baseline_mins, args.aggregate)
    candidate_median_sec = aggregate(candidate_mins, args.aggregate)
    measured_speedup = (
        baseline_median_sec / candidate_median_sec if candidate_median_sec > 0 else None
    )

    print(
        json.dumps(
            {
                "baseline_correct": baseline_ok,
                "candidate_correct": candidate_ok,
                "baseline_median_sec": baseline_median_sec,
                "candidate_median_sec": candidate_median_sec,
                "measured_speedup": measured_speedup,
                "seed_rows": seed_rows,
                "aggregate": args.aggregate,
                "baseline_failures": baseline_failures,
                "candidate_failures": candidate_failures,
                "error": None,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""


@dataclass
class TaskValidationResult:
    task_id: str
    status: str
    fail_phase: str = ""
    error: str | None = None
    threshold_speedup: float | None = None
    threshold_effective: float | None = None
    speedup_ratio_of_threshold: float | None = None
    speed_gate: str = SpeedGateEnum.FASTER_THAN_BASELINE.value
    problem_size_n: int | None = None
    workdir: str | None = None
    target_service: str | None = None
    baseline_median_sec: float | None = None
    optimized_median_sec: float | None = None
    measured_speedup: float | None = None
    baseline_correct: bool | None = None
    optimized_correct: bool | None = None
    per_seed: list[dict[str, Any]] = field(default_factory=list)
    correctness_failures: list[dict[str, Any]] = field(default_factory=list)
    retest_performed: bool = False
    duration_sec: float = 0.0
    phases: dict[str, Any] | None = None


def run_command(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    timeout: int = 300,
    env: dict[str, str] | None = None,
) -> tuple[int, str, str]:
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


def _truncate_output(stdout: str, stderr: str, limit: int = 400) -> str:
    return (stdout + stderr)[-limit:]


def _docker_basename(task_id: str) -> str:
    tid = re.sub(r"[^a-z0-9._-]+", "-", task_id.lower()).strip("-_.") or "task"
    return f"tb-algotune-{tid}"


def _docker_project_name(task_id: str) -> str:
    return _docker_basename(task_id)


def _compose_cmd(project_name: str, *args: str) -> list[str]:
    return ["docker", "compose", "-p", project_name, *args]


def _container_shell_command(container_id: str, *, workdir: str, command: str) -> list[str]:
    return [
        "docker",
        "exec",
        container_id,
        "bash",
        "-lc",
        f"cd {shlex.quote(workdir)} && {command}",
    ]


def build_compose_env(task_path: Path, task_id: str, runtime_label: str) -> dict[str, str]:
    base_name = _docker_basename(task_id)
    runtime_root = task_path / ".verify-runtime" / runtime_label
    logs_host = runtime_root / "logs"
    agent_logs_host = runtime_root / "agent-logs"
    logs_host.mkdir(parents=True, exist_ok=True)
    agent_logs_host.mkdir(parents=True, exist_ok=True)
    return {
        "T_BENCH_TASK_DOCKER_CLIENT_IMAGE_NAME": f"{base_name}:latest",
        "T_BENCH_TASK_DOCKER_SERVER_IMAGE_NAME": f"{base_name}-server:latest",
        "T_BENCH_TASK_DOCKER_CLIENT_CONTAINER_NAME": base_name,
        "T_BENCH_TEST_DIR": "/tests",
        "T_BENCH_TASK_LOGS_PATH": str(logs_host),
        "T_BENCH_CONTAINER_LOGS_PATH": DEFAULT_CONTAINER_LOGS_DIR,
        "T_BENCH_TASK_AGENT_LOGS_PATH": str(agent_logs_host),
        "T_BENCH_CONTAINER_AGENT_LOGS_PATH": DEFAULT_CONTAINER_AGENT_LOGS_DIR,
    }


def cleanup_project(
    task_path: Path, project_name: str, compose_env: dict[str, str], cleanup_images: bool
) -> None:
    cmd = _compose_cmd(project_name, "down", "-v")
    if cleanup_images:
        cmd.extend(["--rmi", "local"])
    run_command(cmd, cwd=task_path, timeout=120, env=compose_env)


def _load_yaml_mapping(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        return None, str(exc)
    if not isinstance(data, dict):
        return None, "expected a YAML mapping"
    return data, None


def _resolve_target_service(compose_path: Path) -> str:
    compose_data, err = _load_yaml_mapping(compose_path)
    if compose_data is None:
        raise ValueError(f"invalid docker-compose.yaml: {err}")
    services = compose_data.get("services")
    if not isinstance(services, dict) or not services:
        raise ValueError("docker-compose.yaml has no services mapping")
    if "client" in services:
        return "client"
    if len(services) == 1:
        return next(iter(services))
    raise ValueError("multi-service compose unsupported for this validator")


def _parse_threshold(task_yaml: dict[str, Any]) -> tuple[float, str]:
    for key in THRESHOLD_FIELD_CANDIDATES:
        if key in task_yaml:
            return float(task_yaml[key]), f"task.yaml:{key}"
    instruction = str(task_yaml.get("instruction", ""))
    for pattern in THRESHOLD_PATTERNS:
        match = re.search(pattern, instruction, flags=re.IGNORECASE)
        if match:
            return float(match.group(1)), "instruction"
    raise ValueError("failed to parse speedup threshold from task.yaml")


def _try_parse_threshold(task_yaml: dict[str, Any]) -> tuple[float | None, str | None]:
    try:
        value, source = _parse_threshold(task_yaml)
        return value, source
    except ValueError:
        return None, None


def _effective_threshold(threshold: float, noise_margin: float) -> float:
    return threshold * (1.0 - noise_margin)


def _parse_problem_size(task_yaml: dict[str, Any], default_n: int) -> int:
    for key in ("problem_size_n", "problem_size", "n"):
        if key in task_yaml:
            return int(task_yaml[key])
    instruction = str(task_yaml.get("instruction", ""))
    for pattern in PROBLEM_SIZE_PATTERNS:
        match = re.search(pattern, instruction, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return default_n


def _parse_workdir(task_yaml: dict[str, Any]) -> str:
    value = task_yaml.get("workdir")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return DEFAULT_WORKDIR


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


def _copy_eval_script(container_id: str, eval_script_host_path: Path) -> tuple[int, str, str]:
    rc, out, err = run_command(
        ["docker", "cp", str(eval_script_host_path), f"{container_id}:{EVAL_SCRIPT_IN_CONTAINER}"],
        timeout=30,
    )
    if rc != 0:
        return rc, out, err
    return run_command(
        ["docker", "exec", container_id, "chmod", "+x", EVAL_SCRIPT_IN_CONTAINER],
        timeout=15,
    )


def _run_eval(
    *,
    container_id: str,
    workdir: str,
    baseline_solver_path_in_container: str,
    candidate_solver_path_in_container: str,
    problem_size_n: int,
    correctness_seeds: list[int],
    timing_seeds: list[int],
    warmup_runs: int,
    timed_runs: int,
    aggregate: str,
    timeout: int,
) -> tuple[int, dict[str, Any] | None, str]:
    correctness_csv = ",".join(str(seed) for seed in correctness_seeds)
    timing_csv = ",".join(str(seed) for seed in timing_seeds)
    cmd = (
        f"python3 {shlex.quote(EVAL_SCRIPT_IN_CONTAINER)} "
        f"--baseline-solver-path {shlex.quote(baseline_solver_path_in_container)} "
        f"--candidate-solver-path {shlex.quote(candidate_solver_path_in_container)} "
        f"--n {problem_size_n} "
        f"--correctness-seeds {shlex.quote(correctness_csv)} "
        f"--timing-seeds {shlex.quote(timing_csv)} "
        f"--warmup-runs {warmup_runs} "
        f"--timed-runs {timed_runs} "
        f"--aggregate {shlex.quote(aggregate)}"
    )
    rc, out, err = run_command(
        _container_shell_command(container_id, workdir=workdir, command=cmd),
        timeout=timeout,
    )
    if rc != 0:
        return rc, None, _truncate_output(out, err)
    line = out.strip().splitlines()[-1] if out.strip() else ""
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return -1, None, f"unexpected eval output: {_truncate_output(out, err)}"
    return 0, payload, ""


def _run_eval_with_retest(
    *,
    container_id: str,
    workdir: str,
    baseline_solver_path_in_container: str,
    candidate_solver_path_in_container: str,
    problem_size_n: int,
    correctness_seeds: list[int],
    timing_seeds: list[int],
    warmup_runs: int,
    timed_runs: int,
    aggregate: str,
    timeout: int,
    enable_retest: bool,
) -> tuple[dict[str, Any] | None, str, bool]:
    rc, payload, eval_error = _run_eval(
        container_id=container_id,
        workdir=workdir,
        baseline_solver_path_in_container=baseline_solver_path_in_container,
        candidate_solver_path_in_container=candidate_solver_path_in_container,
        problem_size_n=problem_size_n,
        correctness_seeds=correctness_seeds,
        timing_seeds=timing_seeds,
        warmup_runs=warmup_runs,
        timed_runs=timed_runs,
        aggregate=aggregate,
        timeout=timeout,
    )
    if rc != 0 or payload is None:
        return None, eval_error or "eval failed", False

    retest_performed = False
    if enable_retest:
        rc2, payload2, eval_error2 = _run_eval(
            container_id=container_id,
            workdir=workdir,
            baseline_solver_path_in_container=baseline_solver_path_in_container,
            candidate_solver_path_in_container=candidate_solver_path_in_container,
            problem_size_n=problem_size_n,
            correctness_seeds=correctness_seeds,
            timing_seeds=timing_seeds,
            warmup_runs=warmup_runs,
            timed_runs=timed_runs * 2,
            aggregate=aggregate,
            timeout=timeout,
        )
        if rc2 == 0 and payload2 is not None:
            retest_performed = True
            speedup1 = payload.get("measured_speedup") or 0.0
            speedup2 = payload2.get("measured_speedup") or 0.0
            if speedup2 > speedup1:
                payload = payload2
            elif speedup2 == speedup1:
                payload = payload2
        elif eval_error2:
            eval_error = eval_error2

    return payload, eval_error, retest_performed


def _apply_eval_payload(result: TaskValidationResult, payload: dict[str, Any]) -> None:
    result.baseline_correct = bool(payload.get("baseline_correct"))
    result.optimized_correct = bool(payload.get("candidate_correct"))
    result.baseline_median_sec = (
        float(payload["baseline_median_sec"])
        if payload.get("baseline_median_sec") is not None
        else None
    )
    result.optimized_median_sec = (
        float(payload["candidate_median_sec"])
        if payload.get("candidate_median_sec") is not None
        else None
    )
    result.measured_speedup = (
        float(payload["measured_speedup"]) if payload.get("measured_speedup") is not None else None
    )
    result.per_seed = list(payload.get("seed_rows") or [])
    failures = list(payload.get("candidate_failures") or [])
    if not result.optimized_correct and failures:
        result.correctness_failures = failures
    elif not result.baseline_correct:
        result.correctness_failures = list(payload.get("baseline_failures") or [])


def _judge_result(
    result: TaskValidationResult,
    *,
    speed_gate: SpeedGate,
    threshold: float | None,
    threshold_effective: float | None,
    near_pass_ratio: float,
) -> TaskValidationResult:
    if not result.baseline_correct:
        result.status = "fail_correctness"
        result.fail_phase = "eval"
        result.error = "baseline solver failed correctness check"
        return result

    if not result.optimized_correct:
        result.status = "fail_correctness"
        result.fail_phase = "eval"
        result.error = "candidate solver failed correctness check"
        return result

    if result.measured_speedup is None or result.optimized_median_sec is None:
        result.status = "error_runtime"
        result.fail_phase = "eval"
        result.error = "missing timing measurements"
        return result

    if result.measured_speedup <= 1.0:
        result.status = "fail_speed"
        result.fail_phase = "eval"
        result.error = f"measured_speedup {result.measured_speedup:.4f} <= 1.0 (not faster than baseline)"
        return result

    if speed_gate == SpeedGateEnum.FASTER_THAN_BASELINE.value:
        result.status = "pass"
        result.fail_phase = ""
        result.error = None
        return result

    if threshold is None or threshold_effective is None:
        result.status = "error_parse"
        result.fail_phase = "structure"
        result.error = "meets-threshold gate requires a parseable threshold in task.yaml"
        return result

    result.speedup_ratio_of_threshold = result.measured_speedup / threshold_effective
    if result.measured_speedup >= threshold_effective:
        result.status = "pass"
        result.fail_phase = ""
        result.error = None
    elif result.speedup_ratio_of_threshold >= near_pass_ratio:
        result.status = "near_pass"
        result.fail_phase = "eval"
        result.error = (
            f"speedup {result.measured_speedup:.4f} below threshold_eff "
            f"{threshold_effective:.4f} but within near-pass band"
        )
    else:
        result.status = "fail_speed"
        result.fail_phase = "eval"
        result.error = (
            f"speedup {result.measured_speedup:.4f} < threshold_eff {threshold_effective:.4f}"
        )
    return result


def _run_solution(task_dir: Path, *, container_id: str, workdir: str, timeout: int) -> tuple[int, str]:
    rc, out, err = run_command(
        ["docker", "cp", str(task_dir / "solution.sh"), f"{container_id}:{SOLUTION_SCRIPT_IN_CONTAINER}"],
        timeout=30,
    )
    if rc != 0:
        return rc, _truncate_output(out, err)
    rc, out, err = run_command(
        _container_shell_command(
            container_id,
            workdir=workdir,
            command=f"bash {shlex.quote(SOLUTION_SCRIPT_IN_CONTAINER)}",
        ),
        timeout=timeout,
    )
    return rc, _truncate_output(out, err)


def _record_phase(
    phases: dict[str, Any],
    name: str,
    *,
    duration: float,
    exit_code: int | None = None,
    output_tail: str | None = None,
    **extra: Any,
) -> None:
    payload: dict[str, Any] = {"duration": round(duration, 3)}
    if exit_code is not None:
        payload["exit_code"] = exit_code
    if output_tail:
        payload["output_tail"] = output_tail
    payload.update(extra)
    phases[name] = payload


def validate_task(
    task_dir: Path,
    *,
    correctness_seeds: list[int],
    timing_seeds: list[int],
    warmup_runs: int,
    timed_runs: int,
    timing_aggregate: str,
    speed_gate: SpeedGate,
    noise_margin: float,
    near_pass_ratio: float,
    enable_retest: bool,
    default_problem_size: int,
    cleanup_images: bool,
) -> TaskValidationResult:
    started_at = time.time()
    task_id = task_dir.name
    phases: dict[str, Any] = {}
    compose_env: dict[str, str] | None = None
    project_name = _docker_project_name(task_id)

    result = TaskValidationResult(
        task_id=task_id,
        status="error",
        speed_gate=speed_gate,
        phases=phases,
    )

    try:
        task_yaml_path = task_dir / "task.yaml"
        compose_path = task_dir / "docker-compose.yaml"
        solution_path = task_dir / "solution.sh"
        if not task_yaml_path.exists() or not compose_path.exists() or not solution_path.exists():
            missing = []
            if not task_yaml_path.exists():
                missing.append("task.yaml")
            if not compose_path.exists():
                missing.append("docker-compose.yaml")
            if not solution_path.exists():
                missing.append("solution.sh")
            result.status = "error"
            result.fail_phase = "structure"
            result.error = f"missing required files: {', '.join(missing)}"
            return result

        task_yaml, task_yaml_err = _load_yaml_mapping(task_yaml_path)
        if task_yaml is None:
            result.status = "error"
            result.fail_phase = "structure"
            result.error = f"invalid task.yaml: {task_yaml_err}"
            return result

        threshold: float | None = None
        threshold_source: str | None = None
        if speed_gate == SpeedGateEnum.MEETS_THRESHOLD.value:
            try:
                threshold, threshold_source = _parse_threshold(task_yaml)
            except ValueError as exc:
                result.status = "error_parse"
                result.fail_phase = "structure"
                result.error = str(exc)
                return result
        else:
            threshold, threshold_source = _try_parse_threshold(task_yaml)

        threshold_effective = (
            _effective_threshold(threshold, noise_margin) if threshold is not None else None
        )

        problem_size_n = _parse_problem_size(task_yaml, default_problem_size)
        workdir = _parse_workdir(task_yaml)
        target_service = _resolve_target_service(compose_path)
        candidate_path_in_container = str(PurePosixPath(workdir) / "solver.py")

        result.threshold_speedup = threshold
        result.threshold_effective = threshold_effective
        result.problem_size_n = problem_size_n
        result.workdir = workdir
        result.target_service = target_service

        build_timeout = int(
            float(task_yaml.get("max_agent_timeout_sec", 1800.0))
            + float(task_yaml.get("max_test_timeout_sec", 3600.0))
        )
        up_timeout = max(180, int(float(task_yaml.get("max_test_timeout_sec", 3600.0))))
        eval_timeout = max(300, int(float(task_yaml.get("max_test_timeout_sec", 3600.0))))
        solution_timeout = max(300, int(float(task_yaml.get("max_agent_timeout_sec", 1800.0))))

        build_env = build_compose_env(task_dir, task_id, runtime_label="build")
        compose_env = build_compose_env(task_dir, task_id, runtime_label="run")

        phase_start = time.time()
        rc, out, err = run_command(
            ["docker", "compose", "build"],
            cwd=task_dir,
            timeout=build_timeout,
            env=build_env,
        )
        _record_phase(
            phases,
            "build",
            duration=time.time() - phase_start,
            exit_code=rc,
            output_tail=_truncate_output(out, err),
        )
        if rc != 0:
            result.status = "error_build"
            result.fail_phase = "build"
            result.error = err or out or "docker compose build failed"
            return result

        phase_start = time.time()
        run_command(_compose_cmd(project_name, "down", "-v"), cwd=task_dir, timeout=60, env=compose_env)
        rc, out, err = run_command(
            _compose_cmd(project_name, "up", "-d"),
            cwd=task_dir,
            timeout=up_timeout,
            env=compose_env,
        )
        _record_phase(
            phases,
            "up",
            duration=time.time() - phase_start,
            exit_code=rc,
            output_tail=_truncate_output(out, err),
        )
        if rc != 0:
            result.status = "error_up"
            result.fail_phase = "container_up"
            result.error = err or out or "container up failed"
            return result

        container_id, container_err = _resolve_container_id(
            task_dir,
            compose_env,
            project_name=project_name,
            target_service=target_service,
        )
        if container_id is None:
            result.status = "error_up"
            result.fail_phase = "container_up"
            result.error = container_err or "failed to resolve container"
            return result

        with tempfile.NamedTemporaryFile(prefix="algotune-eval-", suffix=".py", delete=False) as tmp:
            eval_script_host_path = Path(tmp.name)
            tmp.write(EVAL_SCRIPT_SOURCE.encode("utf-8"))
        try:
            phase_start = time.time()
            rc, out, err = _copy_eval_script(container_id, eval_script_host_path)
            _record_phase(
                phases,
                "prepare",
                duration=time.time() - phase_start,
                exit_code=rc,
                output_tail=_truncate_output(out, err),
            )
            if rc != 0:
                result.status = "error_runtime"
                result.fail_phase = "setup"
                result.error = "failed to prepare eval script"
                return result
        finally:
            eval_script_host_path.unlink(missing_ok=True)

        phase_start = time.time()
        rc, out, err = run_command(
            _container_shell_command(
                container_id,
                workdir=workdir,
                command=(
                    f"cp {shlex.quote(candidate_path_in_container)} "
                    f"{shlex.quote(BASELINE_SOLVER_BACKUP_IN_CONTAINER)}"
                ),
            ),
            timeout=30,
        )
        _record_phase(
            phases,
            "backup_baseline",
            duration=time.time() - phase_start,
            exit_code=rc,
            output_tail=_truncate_output(out, err),
        )
        if rc != 0:
            result.status = "error_runtime"
            result.fail_phase = "setup"
            result.error = "failed to backup baseline solver"
            return result

        phase_start = time.time()
        rc, solution_tail = _run_solution(
            task_dir,
            container_id=container_id,
            workdir=workdir,
            timeout=solution_timeout,
        )
        _record_phase(
            phases,
            "solution",
            duration=time.time() - phase_start,
            exit_code=rc,
            output_tail=solution_tail,
        )
        if rc != 0:
            result.status = "error_runtime"
            result.fail_phase = "solution"
            result.error = f"solution.sh failed (exit={rc})"
            return result

        phase_start = time.time()
        payload, eval_error, retest_performed = _run_eval_with_retest(
            container_id=container_id,
            workdir=workdir,
            baseline_solver_path_in_container=BASELINE_SOLVER_BACKUP_IN_CONTAINER,
            candidate_solver_path_in_container=candidate_path_in_container,
            problem_size_n=problem_size_n,
            correctness_seeds=correctness_seeds,
            timing_seeds=timing_seeds,
            warmup_runs=warmup_runs,
            timed_runs=timed_runs,
            aggregate=timing_aggregate,
            timeout=eval_timeout,
            enable_retest=enable_retest and speed_gate == SpeedGateEnum.MEETS_THRESHOLD.value,
        )
        result.retest_performed = retest_performed
        _record_phase(
            phases,
            "eval",
            duration=time.time() - phase_start,
            output_tail=eval_error,
            payload=payload,
            threshold_source=threshold_source,
        )
        if payload is None:
            result.status = "error_timeout" if "timed out" in (eval_error or "").lower() else "error_runtime"
            result.fail_phase = "eval"
            result.error = eval_error or "eval failed"
            return result

        _apply_eval_payload(result, payload)
        return _judge_result(
            result,
            speed_gate=speed_gate,
            threshold=threshold,
            threshold_effective=threshold_effective,
            near_pass_ratio=near_pass_ratio,
        )
    except Exception as exc:
        if not result.fail_phase:
            result.fail_phase = "exception"
        result.status = "error_runtime"
        result.error = str(exc)
        return result
    finally:
        result.duration_sec = round(time.time() - started_at, 3)
        if compose_env is not None:
            cleanup_project(task_dir, project_name, compose_env, cleanup_images=cleanup_images)


def _collect_task_dirs(dataset_path: Path, task_ids: list[str]) -> list[Path]:
    if task_ids:
        return [dataset_path / task_id for task_id in task_ids]
    return sorted(path for path in dataset_path.iterdir() if path.is_dir())


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate AlgoTune tasks in Docker (Mode B): candidate must be correct "
            "and faster than baseline."
        )
    )
    parser.add_argument(
        "--dataset-path",
        type=Path,
        default=DEFAULT_DATASET_PATH,
        help=f"Path to from-algotune task directory (default: {DEFAULT_DATASET_PATH})",
    )
    parser.add_argument(
        "--task-id",
        action="append",
        default=[],
        help="Task ID to validate (repeatable). If omitted, validates all tasks.",
    )
    parser.add_argument(
        "--correctness-seeds",
        type=str,
        default="1,2,3",
        help="Comma-separated seeds for correctness checks (default: 1,2,3).",
    )
    parser.add_argument(
        "--timing-seeds",
        type=str,
        default=None,
        help="Comma-separated seeds for timing (default: same as --correctness-seeds).",
    )
    parser.add_argument(
        "--seeds",
        type=str,
        default=None,
        help="Deprecated alias for --correctness-seeds and --timing-seeds.",
    )
    parser.add_argument(
        "--warmup-runs",
        type=int,
        default=WARMUP_RUNS_DEFAULT,
        help=f"Warmup runs per timing seed (default: {WARMUP_RUNS_DEFAULT}).",
    )
    parser.add_argument(
        "--timed-runs",
        type=int,
        default=TIMED_RUNS_DEFAULT,
        help=f"Timed runs per timing seed; min is taken (default: {TIMED_RUNS_DEFAULT}).",
    )
    parser.add_argument(
        "--timing-aggregate",
        choices=("median", "min", "mean"),
        default="median",
        help="Aggregate per-seed minima across seeds (default: median).",
    )
    parser.add_argument(
        "--speed-gate",
        choices=[gate.value for gate in SpeedGateEnum],
        default=SpeedGateEnum.FASTER_THAN_BASELINE.value,
        help=(
            "faster-than-baseline: pass if correct and speedup>1.0; "
            "meets-threshold: also require task.yaml threshold (with noise margin)."
        ),
    )
    parser.add_argument(
        "--noise-margin",
        type=float,
        default=NOISE_MARGIN_DEFAULT,
        help=f"Threshold relaxation for meets-threshold gate (default: {NOISE_MARGIN_DEFAULT}).",
    )
    parser.add_argument(
        "--near-pass-ratio",
        type=float,
        default=NEAR_PASS_RATIO_DEFAULT,
        help=f"Near-pass band for meets-threshold gate (default: {NEAR_PASS_RATIO_DEFAULT}).",
    )
    parser.add_argument(
        "--enable-retest",
        action="store_true",
        help="On meets-threshold near-pass band, rerun eval with doubled timed-runs.",
    )
    parser.add_argument(
        "--default-problem-size",
        type=int,
        default=128,
        help="Fallback n when task.yaml does not include parseable problem size (default: 128).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional path to write JSON results.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero when any task is not pass.",
    )
    parser.add_argument(
        "--keep-images",
        action="store_true",
        help="Do not remove built local images during cleanup.",
    )
    return parser


def _parse_seed_csv(value: str | None, fallback: str) -> list[int]:
    raw = value if value is not None else fallback
    seeds = [int(part.strip()) for part in raw.split(",") if part.strip()]
    if not seeds:
        raise ValueError("at least one seed is required")
    return seeds


def _summarize_results(results: list[TaskValidationResult]) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    for item in results:
        by_status[item.status] = by_status.get(item.status, 0) + 1
    passed = by_status.get("pass", 0)
    failed_statuses = {
        "fail_speed",
        "fail_correctness",
        "near_pass",
    }
    failed = sum(by_status.get(status, 0) for status in failed_statuses)
    error_statuses = [status for status in by_status if status.startswith("error")]
    errors = sum(by_status.get(status, 0) for status in error_statuses)
    return {
        "source": SOURCE_NAME,
        "mode": VALIDATION_MODE,
        "dataset_path": "",
        "total": len(results),
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "by_status": by_status,
        "results": [asdict(result) for result in results],
    }


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    dataset_path = args.dataset_path.resolve()
    if not dataset_path.exists():
        raise SystemExit(f"dataset path not found: {dataset_path}")

    seed_fallback = args.seeds or "1,2,3"
    correctness_seeds = _parse_seed_csv(args.correctness_seeds or args.seeds, seed_fallback)
    timing_seeds = _parse_seed_csv(args.timing_seeds or args.seeds, seed_fallback)

    task_dirs = _collect_task_dirs(dataset_path, args.task_id)
    results: list[TaskValidationResult] = []

    for task_dir in task_dirs:
        if not task_dir.exists():
            results.append(
                TaskValidationResult(
                    task_id=task_dir.name,
                    status="error",
                    fail_phase="structure",
                    error="task directory not found",
                    phases={},
                )
            )
            continue
        results.append(
            validate_task(
                task_dir,
                correctness_seeds=correctness_seeds,
                timing_seeds=timing_seeds,
                warmup_runs=args.warmup_runs,
                timed_runs=args.timed_runs,
                timing_aggregate=args.timing_aggregate,
                speed_gate=args.speed_gate,
                noise_margin=args.noise_margin,
                near_pass_ratio=args.near_pass_ratio,
                enable_retest=args.enable_retest,
                default_problem_size=args.default_problem_size,
                cleanup_images=not args.keep_images,
            )
        )

    summary = _summarize_results(results)
    summary["dataset_path"] = str(dataset_path)
    summary["speed_gate"] = args.speed_gate

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n")

    print(json.dumps(summary, indent=2, ensure_ascii=False))

    if args.strict and summary["passed"] != summary["total"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
