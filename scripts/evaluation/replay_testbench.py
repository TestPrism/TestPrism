#!/usr/bin/env python3
"""Replay agent commands from a previous testbench run and re-evaluate F2P.

Scope: single-patch (oracle) testbench evaluation only.
Not for the multi-patch evaluation pipeline.

Correct flow:
1. Build container, replay agent commands → get /agent-tests/test_agent.py
2. Extract test_agent.py from container to host
3. Rebuild fresh container (clean state)
4. Copy test_agent.py into clean container
5. Pre-fix test: run agent's tests on original (unfixed) state → expect FAIL
6. Apply solution.sh (oracle fix)
7. Post-fix test: run agent's tests again → expect PASS
8. Compute F2P verdicts
"""

import argparse
import ast
import io
import json
import logging
import sys
import tarfile
import tempfile
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from testprism.agents.failure_mode import FailureMode
from testprism.handlers.trial_handler import TrialHandler
from testprism.harness.models import (
    TestBenchTrialResults,
    TestCaseVerdict,
)
from testprism.parsers.base_parser import UnitTestStatus
from testprism.parsers.pytest_parser import PytestParser
from testprism.terminal.terminal import spin_up_terminal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("replay_testbench")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AGENT_TEST_DIR = "/agent-tests"

AGENT_TEST_RUNNER_SCRIPT = """\
#!/bin/bash
set -e

pip install pytest pytest-timeout 2>/dev/null || true
pip install coverage 2>/dev/null || true

cd {workdir}
if command -v coverage >/dev/null 2>&1; then
    coverage run --source=. -m pytest {agent_test_dir}/test_agent.py \
        --timeout=30 --tb=short -rA 2>&1
    TEST_EXIT=$?
    coverage json -o {agent_test_dir}/coverage.json 2>/dev/null || true
    coverage report 2>/dev/null || true
else
    python -m pytest {agent_test_dir}/test_agent.py \
        --timeout=30 --tb=short -rA 2>&1
    TEST_EXIT=$?
fi

exit $TEST_EXIT
"""

SKIP_PREFIXES = ["asciinema rec", "clear"]
SKIP_EXACT = {"C-d"}


def should_skip_command(cmd_str: str) -> bool:
    clean = cmd_str.strip()
    if clean in SKIP_EXACT:
        return True
    return any(clean.startswith(p) for p in SKIP_PREFIXES)


# ---------------------------------------------------------------------------
# Task discovery
# ---------------------------------------------------------------------------


def discover_tasks(run_dir: Path, tasks_dir: Path, force: bool = False):
    tasks = []
    for task_dir in sorted(run_dir.iterdir()):
        if not task_dir.is_dir():
            continue
        task_id = task_dir.name
        trial_dirs = [d for d in task_dir.iterdir() if d.is_dir()]
        if not trial_dirs:
            continue
        trial_dir = trial_dirs[0]
        trial_name = trial_dir.name

        commands_path = trial_dir / "commands.txt"
        if not commands_path.exists() or commands_path.stat().st_size == 0:
            continue

        results_path = trial_dir / "results.json"
        if not force and results_path.exists():
            try:
                data = json.loads(results_path.read_text())
                if data.get("verdicts") is not None:
                    continue
            except (json.JSONDecodeError, KeyError):
                pass

        input_path = tasks_dir / task_id
        if not input_path.exists():
            logger.warning(f"Input path not found for {task_id}, skipping")
            continue

        tasks.append({
            "task_id": task_id,
            "trial_name": trial_name,
            "trial_dir": trial_dir,
            "commands_path": commands_path,
            "input_path": input_path,
        })
    return tasks


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def replay_commands(session, commands_path: Path):
    """Replay agent commands. Stops at second 'asciinema rec' (harness test phase)."""
    lines = commands_path.read_text().strip().splitlines()
    asciinema_count = 0

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = ast.literal_eval(line)
        except (ValueError, SyntaxError):
            logger.warning(f"Cannot parse command line: {line[:80]}")
            continue

        cmd_text = parsed[0] if isinstance(parsed, list) else parsed

        if isinstance(cmd_text, str) and cmd_text.strip().startswith("asciinema rec"):
            asciinema_count += 1
            if asciinema_count >= 2:
                logger.debug("Reached harness test phase, stopping replay")
                break

        if should_skip_command(cmd_text):
            continue

        if isinstance(parsed, list):
            try:
                session.send_keys(parsed, block=True, max_timeout_sec=120)
            except TimeoutError:
                logger.warning(f"Command timed out: {cmd_text[:80]}")
        elif isinstance(parsed, str):
            delay = 5.0 if len(parsed) > 500 else 2.0
            try:
                session.send_keys([parsed], block=False, min_timeout_sec=delay)
            except TimeoutError:
                logger.warning(f"Command timed out: {parsed[:80]}")


# ---------------------------------------------------------------------------
# Container file extraction
# ---------------------------------------------------------------------------


def extract_agent_tests_from_container(container, tmp_dir: Path) -> Path | None:
    """Extract /agent-tests/ from container to a local temp directory.

    Returns path to local agent-tests dir, or None if not found.
    """
    try:
        bits, _ = container.get_archive(AGENT_TEST_DIR)
    except Exception as e:
        logger.warning(f"Could not extract {AGENT_TEST_DIR} from container: {e}")
        return None

    # get_archive returns a tar stream
    tar_bytes = b"".join(bits)
    tar_buffer = io.BytesIO(tar_bytes)

    local_agent_tests = tmp_dir / "agent-tests"
    local_agent_tests.mkdir(parents=True, exist_ok=True)

    with tarfile.open(fileobj=tar_buffer) as tar:
        for member in tar.getmembers():
            # member names look like "agent-tests/test_agent.py"
            # Strip the top-level "agent-tests/" prefix
            parts = Path(member.name).parts
            if len(parts) <= 1:
                continue  # skip the directory entry itself
            rel_path = Path(*parts[1:])
            # Only extract .py files (the tests)
            if member.isfile() and rel_path.suffix == ".py":
                content = tar.extractfile(member)
                if content:
                    dest = local_agent_tests / rel_path
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(content.read())

    # Verify test_agent.py exists
    if (local_agent_tests / "test_agent.py").exists():
        return local_agent_tests
    else:
        logger.warning("test_agent.py not found in extracted agent-tests")
        return None


# ---------------------------------------------------------------------------
# Test execution
# ---------------------------------------------------------------------------


def parse_pytest_output(pane_output: str) -> dict[str, UnitTestStatus] | None:
    """Parse pytest output, with fallback for newer pytest without summary header."""
    parser = PytestParser()
    try:
        return parser.parse(pane_output)
    except ValueError:
        pass

    results = {}
    for line in pane_output.splitlines():
        line = line.strip()
        for status_name, status_val in [
            ("PASSED", UnitTestStatus.PASSED),
            ("FAILED", UnitTestStatus.FAILED),
        ]:
            if line.startswith(status_name + " "):
                parts = line.split(maxsplit=1)
                if len(parts) == 2:
                    test_path = parts[1].strip()
                    test_name = (
                        test_path.split("::", maxsplit=1)[-1]
                        if "::" in test_path
                        else test_path
                    )
                    if test_name:
                        results[test_name] = status_val
    return results if results else None


def run_agent_tests(terminal, session, workdir="/app"):
    """Copy test runner script into container, execute, parse results."""
    runner_script = AGENT_TEST_RUNNER_SCRIPT.format(
        workdir=workdir, agent_test_dir=AGENT_TEST_DIR
    )

    script_path = f"{AGENT_TEST_DIR}/run_tests.sh"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False) as tmp:
        tmp.write(runner_script)
        tmp_path = tmp.name

    try:
        terminal.copy_to_container(paths=[Path(tmp_path)], container_dir=AGENT_TEST_DIR)
        session.send_keys(
            [
                f"mv {AGENT_TEST_DIR}/{Path(tmp_path).name} {script_path} && chmod +x {script_path}",
                "Enter",
            ],
            block=True,
            max_timeout_sec=10,
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    try:
        session.send_keys(
            [f"bash {script_path}", "Enter"],
            block=True,
            max_timeout_sec=120,
        )
    except TimeoutError:
        return None, FailureMode.TEST_TIMEOUT

    pane_output = session.capture_pane(capture_entire=True)
    results = parse_pytest_output(pane_output)
    if results is not None:
        return results, FailureMode.NONE
    else:
        logger.warning("Failed to parse agent test output")
        return None, FailureMode.PARSE_ERROR


def apply_solution(terminal, session, solution_path: Path):
    terminal.copy_to_container(paths=[solution_path], container_dir="/tmp/testbench")
    try:
        session.send_keys(
            [f"bash /tmp/testbench/{solution_path.name}", "Enter"],
            block=True,
            max_timeout_sec=300,
        )
        return FailureMode.NONE
    except TimeoutError:
        return FailureMode.TEST_TIMEOUT


def compute_verdicts(pre_results, post_results):
    verdicts = {}
    for test_name in set(pre_results) | set(post_results):
        pre = pre_results.get(test_name)
        post = post_results.get(test_name)
        if pre is None or post is None:
            verdicts[test_name] = TestCaseVerdict.ERROR
        elif pre == UnitTestStatus.FAILED and post == UnitTestStatus.PASSED:
            verdicts[test_name] = TestCaseVerdict.EFFECTIVE
        elif pre == UnitTestStatus.PASSED and post == UnitTestStatus.PASSED:
            verdicts[test_name] = TestCaseVerdict.FALSE_POSITIVE
        elif pre == UnitTestStatus.FAILED and post == UnitTestStatus.FAILED:
            verdicts[test_name] = TestCaseVerdict.FALSE_NEGATIVE
        elif pre == UnitTestStatus.PASSED and post == UnitTestStatus.FAILED:
            verdicts[test_name] = TestCaseVerdict.REGRESSION
        else:
            verdicts[test_name] = TestCaseVerdict.ERROR
    return verdicts


# ---------------------------------------------------------------------------
# Main task processing
# ---------------------------------------------------------------------------


def process_task(task_info: dict, run_dir: Path) -> dict:
    """Process a single task with correct F2P flow:
    1. Replay → extract test_agent.py
    2. Clean container → copy tests in → pre-fix
    3. Apply solution → post-fix
    4. Grade
    """
    task_id = task_info["task_id"]
    trial_name = task_info["trial_name"]
    trial_dir = task_info["trial_dir"]
    input_path = task_info["input_path"]
    commands_path = task_info["commands_path"]

    logger.info(f"Processing {task_id}")

    handler = TrialHandler(trial_name=trial_name, input_path=input_path, output_path=None)

    results = TestBenchTrialResults(
        trial_name=trial_name, task_id=task_id, instruction="(replayed)"
    )
    results.trial_started_at = datetime.now(timezone.utc).isoformat()

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        local_agent_tests = None

        # ── Step 1: Replay agent commands, extract test_agent.py ─────────
        try:
            with spin_up_terminal(
                client_container_name=handler.client_container_name,
                client_image_name=handler.client_image_name,
                docker_image_name_prefix=handler.docker_image_name_prefix,
                docker_compose_path=handler.task_paths.docker_compose_path,
                sessions_logs_path=trial_dir / "sessions",
                agent_logs_path=trial_dir / "agent-logs",
                commands_path=None,
                no_rebuild=False,
                cleanup=True,
                livestream=False,
                disable_recording=True,
            ) as terminal:
                agent_session = terminal.create_session(
                    "replay-agent", is_active_stream=False, as_configured_user=True
                )
                agent_session.send_keys(
                    [f"mkdir -p {AGENT_TEST_DIR}", "Enter"],
                    block=True,
                    max_timeout_sec=10,
                )
                replay_commands(agent_session, commands_path)

                # Extract /agent-tests/ from container
                local_agent_tests = extract_agent_tests_from_container(
                    terminal.container, tmp_path
                )

        except Exception as e:
            logger.error(f"Replay failed for {task_id}: {e}")
            traceback.print_exc()
            results.failure_mode = FailureMode.UNKNOWN_AGENT_ERROR
            results.is_resolved = False
            results.trial_ended_at = datetime.now(timezone.utc).isoformat()
            _save_results(results, trial_dir)
            return {"task_id": task_id, "status": "replay_failed", "is_resolved": False,
                    "n_effective": 0, "n_false_positive": 0}

        if local_agent_tests is None:
            logger.warning(f"No test_agent.py found for {task_id}")
            results.failure_mode = FailureMode.PARSE_ERROR
            results.is_resolved = False
            results.trial_ended_at = datetime.now(timezone.utc).isoformat()
            _save_results(results, trial_dir)
            return {"task_id": task_id, "status": "no_tests", "is_resolved": False,
                    "n_effective": 0, "n_false_positive": 0}

        # ── Step 2: Fresh container → copy tests → pre-fix → solution → post-fix
        try:
            with spin_up_terminal(
                client_container_name=handler.client_container_name,
                client_image_name=handler.client_image_name,
                docker_image_name_prefix=handler.docker_image_name_prefix,
                docker_compose_path=handler.task_paths.docker_compose_path,
                sessions_logs_path=trial_dir / "sessions",
                agent_logs_path=trial_dir / "agent-logs",
                commands_path=None,
                no_rebuild=True,  # Image already built in step 1
                cleanup=True,
                livestream=False,
                disable_recording=True,
            ) as terminal:

                # Copy agent's test files into clean container
                setup_session = terminal.create_session(
                    "setup", is_active_stream=False, as_configured_user=False
                )
                setup_session.send_keys(
                    [f"mkdir -p {AGENT_TEST_DIR}", "Enter"],
                    block=True,
                    max_timeout_sec=10,
                )

                # Copy all .py files from local_agent_tests
                py_files = list(local_agent_tests.glob("**/*.py"))
                if py_files:
                    terminal.copy_to_container(
                        paths=py_files, container_dir=AGENT_TEST_DIR
                    )

                # ── Pre-fix test ─────────────────────────────────────────
                pre_session = terminal.create_session(
                    "pre-fix-tests", is_active_stream=False, as_configured_user=False
                )
                pre_results, pre_failure = run_agent_tests(terminal, pre_session)
                results.pre_fix_results = pre_results

                if pre_failure != FailureMode.NONE:
                    logger.warning(f"Pre-fix test issue for {task_id}: {pre_failure}")

                # ── Apply solution ───────────────────────────────────────
                fix_session = terminal.create_session(
                    "apply-fix", is_active_stream=False, as_configured_user=False
                )
                fix_failure = apply_solution(
                    terminal, fix_session, handler.task_paths.solution_path
                )
                if fix_failure != FailureMode.NONE:
                    results.failure_mode = fix_failure
                    results.trial_ended_at = datetime.now(timezone.utc).isoformat()
                    _save_results(results, trial_dir)
                    return {"task_id": task_id, "status": "fix_failed",
                            "is_resolved": False, "n_effective": 0, "n_false_positive": 0}

                # ── Post-fix test ────────────────────────────────────────
                post_session = terminal.create_session(
                    "post-fix-tests", is_active_stream=False, as_configured_user=False
                )
                post_results, post_failure = run_agent_tests(terminal, post_session)
                results.post_fix_results = post_results

                # ── Grade ────────────────────────────────────────────────
                if pre_results and post_results:
                    verdicts = compute_verdicts(pre_results, post_results)
                    results.verdicts = verdicts
                    results.n_effective = sum(
                        1 for v in verdicts.values() if v == TestCaseVerdict.EFFECTIVE
                    )
                    results.n_false_positive = sum(
                        1 for v in verdicts.values() if v == TestCaseVerdict.FALSE_POSITIVE
                    )
                    results.n_false_negative = sum(
                        1 for v in verdicts.values() if v == TestCaseVerdict.FALSE_NEGATIVE
                    )
                    results.n_regression = sum(
                        1 for v in verdicts.values() if v == TestCaseVerdict.REGRESSION
                    )
                    results.n_error = sum(
                        1 for v in verdicts.values() if v == TestCaseVerdict.ERROR
                    )
                    n_total = len(verdicts)
                    results.f2p_rate = (
                        results.n_effective / n_total if n_total > 0 else 0.0
                    )
                    results.is_resolved = (
                        results.n_effective > 0 and results.n_false_positive == 0
                    )
                else:
                    results.is_resolved = False
                    if pre_results is None and post_results is None:
                        results.failure_mode = FailureMode.PARSE_ERROR

        except Exception as e:
            logger.error(f"Evaluation failed for {task_id}: {e}")
            traceback.print_exc()
            results.failure_mode = FailureMode.UNKNOWN_AGENT_ERROR
            results.is_resolved = False

    results.trial_ended_at = datetime.now(timezone.utc).isoformat()
    _save_results(results, trial_dir)

    status = "resolved" if results.is_resolved else "unresolved"
    return {
        "task_id": task_id,
        "status": status,
        "n_effective": results.n_effective,
        "n_false_positive": results.n_false_positive,
        "is_resolved": results.is_resolved,
    }


def _save_results(results: TestBenchTrialResults, trial_dir: Path):
    results_path = trial_dir / "results.json"
    results_path.write_text(results.model_dump_json(indent=2))


def aggregate_results(run_dir: Path):
    all_results = []
    for task_dir in sorted(run_dir.iterdir()):
        if not task_dir.is_dir():
            continue
        for trial_dir in task_dir.iterdir():
            if not trial_dir.is_dir():
                continue
            results_path = trial_dir / "results.json"
            if results_path.exists():
                try:
                    all_results.append(json.loads(results_path.read_text()))
                except json.JSONDecodeError:
                    pass

    summary = {
        "total": len(all_results),
        "resolved": sum(1 for r in all_results if r.get("is_resolved")),
        "unresolved": sum(1 for r in all_results if r.get("is_resolved") is False),
        "error": sum(1 for r in all_results if r.get("is_resolved") is None),
        "results": all_results,
    }
    (run_dir / "results.json").write_text(json.dumps(summary, indent=2, default=str))
    logger.info(
        f"Aggregated {summary['total']} results: "
        f"{summary['resolved']} resolved, {summary['unresolved']} unresolved, "
        f"{summary['error']} error"
    )


def main():
    parser = argparse.ArgumentParser(
        description="Replay testbench commands and run F2P evaluation"
    )
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--tasks-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--task-id", type=str, default=None)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    run_dir = args.run_dir.resolve()
    tasks_dir = args.tasks_dir.resolve()
    tasks = discover_tasks(run_dir, tasks_dir, force=args.force)

    if args.task_id:
        tasks = [t for t in tasks if t["task_id"] == args.task_id]

    logger.info(f"Found {len(tasks)} tasks to process")
    if not tasks:
        return

    completed = resolved = failed = 0

    if args.workers <= 1:
        for task_info in tasks:
            result = process_task(task_info, run_dir)
            completed += 1
            if result.get("is_resolved"):
                resolved += 1
            else:
                failed += 1
            logger.info(
                f"[{completed}/{len(tasks)}] {result['task_id']}: {result['status']} "
                f"(resolved: {resolved}, failed: {failed})"
            )
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(process_task, ti, run_dir): ti for ti in tasks
            }
            for future in as_completed(futures):
                ti = futures[future]
                try:
                    result = future.result()
                    completed += 1
                    if result.get("is_resolved"):
                        resolved += 1
                    else:
                        failed += 1
                    logger.info(
                        f"[{completed}/{len(tasks)}] {result['task_id']}: {result['status']} "
                        f"(resolved: {resolved}, failed: {failed})"
                    )
                except Exception as e:
                    completed += 1
                    failed += 1
                    logger.error(
                        f"[{completed}/{len(tasks)}] {ti['task_id']}: EXCEPTION: {e}"
                    )

    aggregate_results(run_dir)
    logger.info(f"Done. {resolved}/{completed} resolved.")


if __name__ == "__main__":
    main()
