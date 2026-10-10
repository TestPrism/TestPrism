import hashlib
import json
import shutil
import subprocess
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from testprism.agents.failure_mode import FailureMode
from testprism.agents.installed_agents.mini_swe_agent import MiniSweAgent
from testprism.cli.tb.testbench import _print_testbench_results
from testprism.harness.entry_coverage_applicability import (
    EntryCoverageApplicabilityManifest,
)
from testprism.harness.harness import Harness
from testprism.harness.models import TestBenchResults as TBResults
from testprism.harness.testbench_harness import (
    TESTBENCH_SYSTEM_PROMPT,
    _canonical_diff_command,
    _has_output_marker,
)
from testprism.harness.testbench_harness import (
    TestBenchHarness as RuntimeTestBenchHarness,
)
from testprism.harness.testbench_system_prompt import (
    TESTBENCH_SYSTEM_PROMPT as CANONICAL_TESTBENCH_SYSTEM_PROMPT,
)
from testprism.harness.testbench_system_prompt import get_testbench_system_prompt
from testprism.parsers.base_parser import UnitTestStatus


class DummyAgent:
    def __init__(self) -> None:
        self.system_prompt = None


class DummyNonPromptingAgent(DummyAgent):
    consumes_task_instruction = False


class DummyTask:
    workdir = "/app"
    max_test_timeout_sec = 30


class DummyTrialHandler:
    task_id = "demo-task"
    task = DummyTask()


class DummySession:
    def __init__(self, output: str) -> None:
        self.output = output
        self.commands: list[list[str]] = []

    def send_keys(self, keys, **kwargs) -> None:
        self.commands.append(keys)

    def capture_pane(self, capture_entire: bool = False) -> str:
        return self.output


def _new_testbench_harness() -> RuntimeTestBenchHarness:
    harness = object.__new__(RuntimeTestBenchHarness)
    harness._global_test_timeout_sec = None
    harness._global_timeout_multiplier = 1
    return harness


def _run_fake_legacy_trial(
    tmp_path,
    *,
    entry_metadata=None,
    setup_error=None,
    collection_error=None,
    task_input_path=None,
    entry_applicability_resolver=None,
    report_files=None,
):
    """Run the legacy solution path with deterministic container doubles."""

    harness = _new_testbench_harness()
    harness._logger = __import__("logging").getLogger("legacy-entry-test")
    harness._cleanup = False
    harness._livestream = False
    harness._no_rebuild = True
    harness._agent_kwargs = {}

    class AgentClass:
        @staticmethod
        def name():
            return "test-agent"

    harness._agent_class = AgentClass

    task = SimpleNamespace(
        workdir="/app",
        max_test_timeout_sec=30,
        disable_asciinema=False,
        test_entry_inferences=entry_metadata,
    )
    handler = SimpleNamespace(
        task_id="legacy-entry-task",
        trial_name="legacy-entry-trial",
        instruction="write tests",
        task=task,
        task_paths=SimpleNamespace(
            docker_compose_path=tmp_path / "compose.yaml",
            input_path=task_input_path,
        ),
        trial_paths=SimpleNamespace(
            task_output_path=tmp_path,
            sessions_path=tmp_path / "sessions",
            agent_logging_dir=tmp_path / "agent-logs",
            commands_path=tmp_path / "commands.txt",
            coverage_path=tmp_path / "coverage",
        ),
        client_container_name="legacy-entry-trial",
        client_image_name="legacy-entry-image",
        docker_image_name_prefix="legacy-entry",
    )

    if report_files is None:
        report_files = {
            "pkg/entry.py": {
                "executed_lines": [10],
                "missing_lines": [11, 12],
            }
        }
    report = tmp_path / "generated-gold.json"
    report.write_text(json.dumps({"files": report_files}))
    run_calls = []
    setup_calls = []
    collect_calls = []
    terminals = []

    class Session:
        def send_keys(self, keys, **kwargs):
            return None

        def capture_pane(self, capture_entire=False):
            return ""

    class Terminal:
        def create_session(self, name, **kwargs):
            return Session()

        def copy_from_container(self, source, destination):
            destination = Path(destination)
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "run-tests.sh").write_text("#!/bin/sh\nexit 0\n")

        def copy_to_container(self, *args, **kwargs):
            return None

    @contextmanager
    def fake_spin_up_terminal(**kwargs):
        terminal = Terminal()
        terminals.append(terminal)
        yield terminal

    def fake_run_agent_tests(
        terminal,
        session,
        trial_handler,
        timeout_sec=None,
        extra_env=None,
    ):
        run_calls.append(extra_env)
        status = (
            UnitTestStatus.FAILED
            if len(run_calls) == 1
            else UnitTestStatus.PASSED
        )
        return {"agent_tests": status}, FailureMode.NONE

    def fake_setup_runtime(
        terminal,
        session,
        trial_handler,
        target_paths,
        namespace="generated",
    ):
        setup_calls.append((target_paths, namespace))
        if setup_error is not None:
            return None, setup_error
        return {"COVERAGE_PROCESS_START": "generated-coveragerc"}, None

    def fake_collect_artifact(
        terminal,
        session,
        trial_handler,
        state_id,
        namespace="generated",
    ):
        collect_calls.append((state_id, namespace))
        if collection_error is not None:
            return None, collection_error
        return report, None

    harness._setup_agent_test_dir = lambda *args, **kwargs: None
    harness._create_agent_for_task = lambda *args, **kwargs: DummyAgent()
    harness._run_agent = lambda *args, **kwargs: (None, FailureMode.NONE)
    harness._run_agent_tests = fake_run_agent_tests
    harness._apply_solution = lambda *args, **kwargs: FailureMode.NONE
    harness._setup_coverage_runtime = fake_setup_runtime
    harness._collect_coverage_artifact = fake_collect_artifact
    harness._read_coverage_from_container = lambda *args, **kwargs: (None, None)

    resolver_patch = (
        patch(
            "testprism.harness.testbench_harness.resolve_entry_coverage_applicability",
            entry_applicability_resolver,
        )
        if entry_applicability_resolver is not None
        else nullcontext()
    )
    with resolver_patch:
        with patch(
            "testprism.harness.testbench_harness.spin_up_terminal",
            fake_spin_up_terminal,
        ):
            results = harness._run_legacy_trial(handler)

    return results, run_calls, setup_calls, collect_calls, terminals


ENTRY_METADATA = {
    "schema_version": 1,
    "language": "python",
    "source": "static_test_invocation",
    "entries": [
        {
            "path": "pkg/entry.py",
            "start_line": 10,
            "end_line": 12,
        }
    ],
}

JAVA_ENTRY_METADATA = {
    "schema_version": 1,
    "language": "java",
    "source": "static_test_invocation",
    "entries": [
        {
            "path": "src/Main.java",
            "start_line": 10,
            "end_line": 12,
        }
    ],
}


def _governed_entry_task(
    tasks_root: Path,
    task_key: str = "from-example/legacy-entry-task",
    solution: str = "#!/bin/sh\nexit 0\n",
) -> Path:
    task_dir = tasks_root / task_key
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "task.yaml").write_text("instruction: test\n", encoding="utf-8")
    (task_dir / "solution.sh").write_text(solution, encoding="utf-8")
    return task_dir


def _governed_entry_record(
    task_dir: Path,
    *,
    decision: str = "applicable",
    reason_code: str | None = None,
    languages: list[str] | None = None,
    solution_sha256: str | None = None,
) -> dict[str, object]:
    if languages is None:
        languages = ["python"]
    if decision == "not_applicable":
        languages = []
    if reason_code is None:
        reason_code = (
            "artifact_only_verifier"
            if decision == "not_applicable"
            else "direct_local_entry_surface"
        )
    if solution_sha256 is None:
        solution_sha256 = hashlib.sha256(
            (task_dir / "solution.sh").read_bytes()
        ).hexdigest()
    return {
        "schema_version": 1,
        "task_key": task_dir.relative_to(task_dir.parents[1]).as_posix(),
        "solution_sha256": solution_sha256,
        "decision": decision,
        "reason_code": reason_code,
        "languages": languages,
        "evidence_paths": ["pkg/entry.py", "tests/test_entry.py"],
    }


def _write_governed_manifest(path: Path, *records: object) -> None:
    payload = "\n".join(
        record if isinstance(record, str) else json.dumps(record, sort_keys=True)
        for record in records
    )
    if payload:
        payload += "\n"
    path.write_text(payload, encoding="utf-8")


def test_legacy_solution_entry_coverage_reuses_single_post_fix_execution(tmp_path):
    results, run_calls, setup_calls, collect_calls, _ = _run_fake_legacy_trial(
        tmp_path,
        entry_metadata=ENTRY_METADATA,
    )

    # Pre-fix and post-fix are the only generated-test executions; Entry
    # Coverage reuses the post-fix report rather than running a second suite.
    assert len(run_calls) == 2
    assert run_calls[0] is None
    assert run_calls[1] == {"COVERAGE_PROCESS_START": "generated-coveragerc"}
    assert setup_calls == [(["pkg/entry.py"], "generated")]
    assert collect_calls == [("gold", "generated")]

    assert results.pre_fix_results == {"agent_tests": UnitTestStatus.FAILED}
    assert results.post_fix_results == {"agent_tests": UnitTestStatus.PASSED}
    assert results.is_resolved is True
    assert results.n_effective == 1
    assert results.entry_coverage is not None
    assert results.entry_coverage.status.value == "collected"
    assert results.entry_coverage.ratio == pytest.approx(1 / 3)
    assert (tmp_path / "coverage" / "generated" / "gold" / "coverage.json").is_file()
    assert (tmp_path / "coverage" / "entry-coverage.json").is_file()


@pytest.mark.parametrize("failure_kind", ["setup", "collection"])
def test_legacy_entry_coverage_failure_is_observational(tmp_path, failure_kind):
    setup_error = (
        "generated coverage setup failed: status=unsupported"
        if failure_kind == "setup"
        else None
    )
    collection_error = (
        None
        if failure_kind == "setup"
        else "generated coverage collection failed: status=failed"
    )
    results, run_calls, setup_calls, collect_calls, _ = _run_fake_legacy_trial(
        tmp_path,
        entry_metadata=ENTRY_METADATA,
        setup_error=setup_error,
        collection_error=collection_error,
    )

    assert len(run_calls) == 2
    assert results.pre_fix_results == {"agent_tests": UnitTestStatus.FAILED}
    assert results.post_fix_results == {"agent_tests": UnitTestStatus.PASSED}
    assert results.verdicts
    assert results.is_resolved is True
    assert results.n_effective == 1
    assert results.entry_coverage is not None
    assert results.entry_coverage.status.value == "failed"
    if failure_kind == "setup":
        assert setup_calls == [(["pkg/entry.py"], "generated")]
        assert collect_calls == []
    else:
        assert setup_calls == [(["pkg/entry.py"], "generated")]
        assert collect_calls == [("gold", "generated")]


def test_legacy_solution_without_entry_metadata_skips_entry_instrumentation(tmp_path):
    results, run_calls, setup_calls, collect_calls, _ = _run_fake_legacy_trial(
        tmp_path,
        entry_metadata=None,
    )

    assert len(run_calls) == 2
    assert run_calls == [None, None]
    assert setup_calls == []
    assert collect_calls == []
    assert results.entry_coverage is not None
    assert results.entry_coverage.status.value == "unavailable"
    assert results.entry_coverage.ratio is None
    assert (tmp_path / "coverage" / "entry-coverage.json").is_file()


def test_governed_legacy_entry_coverage_not_applicable_is_unsupported(tmp_path):
    governed_root = tmp_path / "verified_tasks" / "test-hardest-500"
    task_dir = _governed_entry_task(governed_root)
    manifest_path = governed_root / "ENTRY_COVERAGE_APPLICABILITY.jsonl"
    _write_governed_manifest(
        manifest_path,
        _governed_entry_record(
            task_dir,
            decision="not_applicable",
            reason_code="artifact_only_verifier",
        ),
    )
    resolver = EntryCoverageApplicabilityManifest(
        tasks_root=governed_root,
        manifest_path=manifest_path,
    ).resolve

    results, run_calls, setup_calls, collect_calls, _ = _run_fake_legacy_trial(
        tmp_path,
        entry_metadata=ENTRY_METADATA,
        task_input_path=task_dir,
        entry_applicability_resolver=resolver,
    )

    assert run_calls == [None, None]
    assert setup_calls == []
    assert collect_calls == []
    assert results.pre_fix_results == {"agent_tests": UnitTestStatus.FAILED}
    assert results.post_fix_results == {"agent_tests": UnitTestStatus.PASSED}
    assert results.is_resolved is True
    assert results.entry_coverage is not None
    assert results.entry_coverage.status.value == "unsupported"
    assert results.entry_coverage.reason == "artifact_only_verifier"


def test_governed_legacy_entry_coverage_manifest_failure_is_failed(tmp_path):
    governed_root = tmp_path / "verified_tasks" / "test-hardest-500"
    task_dir = _governed_entry_task(governed_root)
    other_task = _governed_entry_task(
        governed_root,
        task_key="from-example/other-task",
    )
    manifest_path = governed_root / "ENTRY_COVERAGE_APPLICABILITY.jsonl"
    _write_governed_manifest(
        manifest_path,
        _governed_entry_record(other_task),
    )
    resolver = EntryCoverageApplicabilityManifest(
        tasks_root=governed_root,
        manifest_path=manifest_path,
    ).resolve

    results, run_calls, setup_calls, collect_calls, _ = _run_fake_legacy_trial(
        tmp_path,
        entry_metadata=ENTRY_METADATA,
        task_input_path=task_dir,
        entry_applicability_resolver=resolver,
    )

    assert run_calls == [None, None]
    assert setup_calls == []
    assert collect_calls == []
    assert results.pre_fix_results == {"agent_tests": UnitTestStatus.FAILED}
    assert results.post_fix_results == {"agent_tests": UnitTestStatus.PASSED}
    assert results.is_resolved is True
    assert results.entry_coverage is not None
    assert results.entry_coverage.status.value == "failed"
    assert results.entry_coverage.reason is not None
    assert "entry coverage applicability failed:" in results.entry_coverage.reason
    assert "entry coverage applicability manifest has no record" in (
        results.entry_coverage.reason
    )


def test_governed_legacy_entry_coverage_collects_supported_non_python_metadata(
    tmp_path,
):
    governed_root = tmp_path / "verified_tasks" / "test-hardest-500"
    task_dir = _governed_entry_task(governed_root)
    manifest_path = governed_root / "ENTRY_COVERAGE_APPLICABILITY.jsonl"
    _write_governed_manifest(
        manifest_path,
        _governed_entry_record(task_dir, languages=["java"]),
    )
    resolver = EntryCoverageApplicabilityManifest(
        tasks_root=governed_root,
        manifest_path=manifest_path,
    ).resolve

    results, run_calls, setup_calls, collect_calls, _ = _run_fake_legacy_trial(
        tmp_path,
        entry_metadata=JAVA_ENTRY_METADATA,
        task_input_path=task_dir,
        entry_applicability_resolver=resolver,
        report_files={
            "src/Main.java": {
                "executed_lines": [10],
                "missing_lines": [11, 12],
            }
        },
    )

    assert len(run_calls) == 2
    assert run_calls[0] is None
    assert run_calls[1] == {"COVERAGE_PROCESS_START": "generated-coveragerc"}
    assert setup_calls == [(["src/Main.java"], "generated")]
    assert collect_calls == [("gold", "generated")]
    assert results.pre_fix_results == {"agent_tests": UnitTestStatus.FAILED}
    assert results.post_fix_results == {"agent_tests": UnitTestStatus.PASSED}
    assert results.is_resolved is True
    assert results.entry_coverage is not None
    assert results.entry_coverage.status.value == "collected"
    assert results.entry_coverage.language == "java"
    assert results.entry_coverage.ratio == pytest.approx(1 / 3)
    raw = json.loads(
        (tmp_path / "coverage" / "generated" / "gold" / "coverage.json").read_text()
    )
    assert "src/Main.java" in raw["files"]


def test_testbench_harness_injects_system_prompt_into_agent():
    harness = object.__new__(RuntimeTestBenchHarness)
    agent = DummyAgent()

    with patch.object(Harness, "_create_agent_for_task", return_value=agent):
        created = RuntimeTestBenchHarness._create_agent_for_task(harness, "demo-task")

    assert created is agent
    assert created.system_prompt == TESTBENCH_SYSTEM_PROMPT
    assert TESTBENCH_SYSTEM_PROMPT == CANONICAL_TESTBENCH_SYSTEM_PROMPT
    assert RuntimeTestBenchHarness._results_cls is TBResults


def test_testbench_harness_skips_prompt_for_non_prompting_agents():
    harness = object.__new__(RuntimeTestBenchHarness)
    agent = DummyNonPromptingAgent()

    with patch.object(Harness, "_create_agent_for_task", return_value=agent):
        created = RuntimeTestBenchHarness._create_agent_for_task(harness, "demo-task")

    assert created is agent
    assert created.system_prompt is None


def test_testbench_harness_enables_neutral_mini_swe_config():
    harness = object.__new__(RuntimeTestBenchHarness)
    agent = MiniSweAgent(
        model_name="openai/demo-model",
        api_key="test-key",
        base_url="https://example.test/v1",
    )

    with patch.object(Harness, "_create_agent_for_task", return_value=agent):
        created = RuntimeTestBenchHarness._create_agent_for_task(harness, "demo-task")

    assert created is agent
    assert created.system_prompt == TESTBENCH_SYSTEM_PROMPT
    assert (
        created._runtime_env()["TESTPRISM_MINI_SWE_BASE_CONFIG"]
        == "/installed-agent/mini-swe-testbench.yaml"
    )


def test_get_testbench_system_prompt_matches_constant() -> None:
    assert get_testbench_system_prompt() == CANONICAL_TESTBENCH_SYSTEM_PROMPT


def test_testbench_system_prompt_requires_canonical_test_entry_point() -> None:
    prompt = get_testbench_system_prompt()

    assert (
        "`agent-tests/run-tests.sh` as the single executable test entry point"
        in prompt
    )
    assert "It must exits with code 0 on success and non-zero on failure." in prompt
    assert "supporting test files required by `run-tests.sh`" in prompt


def test_output_marker_does_not_match_echoed_command_text() -> None:
    marker = "__TESTPRISM_COVERAGE_COLLECT__=collected"
    pane = f"if command; then echo {marker}; fi\n"

    assert not _has_output_marker(pane, marker)
    assert _has_output_marker(f"{pane}{marker}\n", marker)


def test_run_agent_tests_treats_script_internal_127_as_failed() -> None:
    harness = _new_testbench_harness()
    session = DummySession(
        "/app/agent-tests/run-tests.sh: line 4: pytest: command not found\n"
        "__AGENT_TESTS_EXIT__=127\n"
    )

    results, failure_mode = harness._run_agent_tests(
        terminal=None,
        session=session,
        trial_handler=DummyTrialHandler(),
    )

    assert failure_mode == FailureMode.NONE
    assert results == {"agent_tests": UnitTestStatus.FAILED}
    command = session.commands[0][0]
    assert "__AGENT_TESTS_MISSING__=1" in command
    assert "__AGENT_TESTS_EXIT__=$?" in command


def test_run_agent_tests_reports_agent_test_missing_when_runner_missing() -> None:
    harness = _new_testbench_harness()
    session = DummySession("__AGENT_TESTS_MISSING__=1\n")

    results, failure_mode = harness._run_agent_tests(
        terminal=None,
        session=session,
        trial_handler=DummyTrialHandler(),
    )

    assert results is None
    assert failure_mode == FailureMode.AGENT_TEST_MISSING


def test_reference_runner_nonzero_is_diagnostic_and_runs_after_agent_capture() -> None:
    harness = _new_testbench_harness()
    harness._setup_test_env = lambda terminal, handler: None
    session = DummySession("trusted test failed\n__REFERENCE_TESTS_EXIT__=1\n")

    failure_mode = harness._run_reference_tests(
        terminal=None,
        session=session,
        trial_handler=DummyTrialHandler(),
        extra_env={
            "COVERAGE_PROCESS_START": "/tmp/testbench/coverage/reference/coveragerc",
            "COVERAGE_FILE": "/tmp/testbench/coverage/reference/data/.coverage",
            "PYTHONPATH": (
                "/tmp/testbench/coverage/reference/bootstrap:/app"
                "${PYTHONPATH:+:${PYTHONPATH}}"
            ),
        },
    )

    assert failure_mode == FailureMode.NONE
    command = session.commands[0][0]
    assert "bash /tests/run-tests.sh" in command
    assert "/app/agent-tests/run-tests.sh" not in command
    assert "unset PYTHONPATH COVERAGE_PROCESS_START COVERAGE_FILE" in command
    assert "coverage/reference/data/.coverage" in command


def test_apply_patch_entry_installs_noninteractive_patch_wrapper(tmp_path) -> None:
    harness = _new_testbench_harness()
    session = DummySession("__PATCH_SOLUTION_EXIT__=0\n")
    copied = []

    class Terminal:
        def copy_to_container(self, *args, **kwargs):
            copied.append((args, kwargs))

    test_dir = tmp_path / "tests"
    test_dir.mkdir()
    patch_root = tmp_path / "patches"
    patch_root.mkdir()
    patch_path = patch_root / "apply.sh"
    patch_path.write_text("#!/bin/sh\nexit 0\n")
    trial_handler = SimpleNamespace(
        task_id="demo-task",
        task=SimpleNamespace(
            workdir="/app",
            effective_max_solution_timeout_sec=30,
        ),
        task_paths=SimpleNamespace(test_dir=test_dir),
    )
    patch = SimpleNamespace(id="gold", path="apply.sh")

    failure = harness._apply_patch_entry(
        Terminal(),
        session,
        trial_handler,
        patch,
        patch_root,
    )

    assert failure == FailureMode.NONE
    assert len(copied) == 2
    command = session.commands[0][0]
    assert "exec \"$patch_bin\" \"$forward_flag\" --batch \"$@\"" in command
    assert "__TESTPRISM_PATCH_BINARY_MISSING__=1" in command
    assert "PATH=/tmp/testbench/patch-tool:$PATH" in command
    assert "bash /tmp/testbench/apply.sh" in command


def test_inspect_gold_patch_reports_reversed_patch_prompt_deterministically(
    tmp_path,
) -> None:
    harness = _new_testbench_harness()
    harness._apply_patch_entry = lambda *args, **kwargs: FailureMode.TEST_TIMEOUT
    snapshot_session = DummySession("__GOLD_SNAPSHOT_EXIT__=0\n")
    apply_session = DummySession(
        "Reversed (or previously applied) patch detected!  Assume -R? [n]\n"
    )

    class Terminal:
        def create_session(self, name, **kwargs):
            if name == "snapshot-coverage-inspect":
                return snapshot_session
            if name == "apply-coverage-inspect":
                return apply_session
            raise AssertionError(name)

    @contextmanager
    def fake_spin_up_terminal(**kwargs):
        yield Terminal()

    trial_handler = SimpleNamespace(
        task_id="aws__aws-cli-7612",
        client_container_name="demo",
        client_image_name="demo-image",
        docker_image_name_prefix="demo-prefix",
        task=SimpleNamespace(workdir="/app", disable_asciinema=False),
        task_paths=SimpleNamespace(docker_compose_path=tmp_path / "compose.yaml"),
        trial_paths=SimpleNamespace(
            sessions_path=tmp_path / "sessions",
            coverage_path=tmp_path / "coverage",
            agent_logging_dir=tmp_path / "agent-logs",
            commands_path=tmp_path / "commands.txt",
        ),
    )

    with patch(
        "testprism.harness.testbench_harness.spin_up_terminal",
        fake_spin_up_terminal,
    ):
        changes, error = harness._inspect_gold_patch(
            trial_handler,
            SimpleNamespace(id="gold"),
            tmp_path,
        )

    assert changes is None
    assert (
        error
        == "gold patch inspection apply failed: "
        "patch rejected as reversed or previously applied"
    )


def test_inspect_gold_patch_reports_missing_patch_tool_deterministically(
    tmp_path,
) -> None:
    harness = _new_testbench_harness()
    harness._apply_patch_entry = (
        lambda *args, **kwargs: FailureMode.UNKNOWN_AGENT_ERROR
    )
    snapshot_session = DummySession("__GOLD_SNAPSHOT_EXIT__=0\n")
    apply_session = DummySession("__TESTPRISM_PATCH_BINARY_MISSING__=1\n")

    class Terminal:
        def create_session(self, name, **kwargs):
            if name == "snapshot-coverage-inspect":
                return snapshot_session
            if name == "apply-coverage-inspect":
                return apply_session
            raise AssertionError(name)

    @contextmanager
    def fake_spin_up_terminal(**kwargs):
        yield Terminal()

    trial_handler = SimpleNamespace(
        task_id="lark-parser__lark-916",
        client_container_name="demo",
        client_image_name="demo-image",
        docker_image_name_prefix="demo-prefix",
        task=SimpleNamespace(workdir="/app", disable_asciinema=False),
        task_paths=SimpleNamespace(docker_compose_path=tmp_path / "compose.yaml"),
        trial_paths=SimpleNamespace(
            sessions_path=tmp_path / "sessions",
            coverage_path=tmp_path / "coverage",
            agent_logging_dir=tmp_path / "agent-logs",
            commands_path=tmp_path / "commands.txt",
        ),
    )

    with patch(
        "testprism.harness.testbench_harness.spin_up_terminal",
        fake_spin_up_terminal,
    ):
        changes, error = harness._inspect_gold_patch(
            trial_handler,
            SimpleNamespace(id="gold"),
            tmp_path,
        )

    assert changes is None
    assert error == "gold patch inspection apply failed: patch fallback binary missing"


def test_canonical_diff_excludes_tool_backups_but_keeps_new_python_source(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"], check=True
    )
    (repo / ".gitignore").write_text("solution_patch.diff\n")
    (repo / "tracked.py").write_text("before = True\n")
    subprocess.run(
        ["git", "-C", str(repo), "add", ".gitignore", "tracked.py"], check=True
    )
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "base"], check=True)
    (repo / "tracked.py").write_text("before = False\n")
    (repo / "intentional_new.py").write_text("new = True\n")
    (repo / "solution.sh").write_text("#!/bin/sh\n# intentional source payload\n")
    (repo / "tracked.py.orig").write_text("backup\n")
    (repo / "tracked.py.rej").write_text("reject\n")
    (repo / "solution_patch.diff").write_text("payload\n")
    (repo / "agent-tests").mkdir()
    (repo / "agent-tests" / "test_generated.py").write_text("assert True\n")
    output = repo / "canonical.diff"

    subprocess.run(
        _canonical_diff_command(str(repo), str(output)),
        shell=True,
        check=True,
    )
    diff = output.read_text()
    assert "intentional_new.py" in diff
    assert "solution.sh" in diff
    assert "tracked.py" in diff
    assert "tracked.py.orig" not in diff
    assert "tracked.py.rej" not in diff
    assert "solution_patch.diff" not in diff
    assert "test_generated.py" not in diff


def test_run_agent_tests_injects_trusted_coverage_environment() -> None:
    harness = _new_testbench_harness()
    session = DummySession("__AGENT_TESTS_EXIT__=0\n")

    results, failure_mode = harness._run_agent_tests(
        terminal=None,
        session=session,
        trial_handler=DummyTrialHandler(),
        extra_env={
            "COVERAGE_PROCESS_START": "/tmp/testbench/coverage/coveragerc",
            "COVERAGE_FILE": "/tmp/testbench/coverage/data/.coverage",
            "PYTHONPATH": (
                "/tmp/testbench/coverage/bootstrap:/app${PYTHONPATH:+:${PYTHONPATH}}"
            ),
        },
    )

    assert failure_mode == FailureMode.NONE
    assert results == {"agent_tests": UnitTestStatus.PASSED}
    command = session.commands[0][0]
    assert "export COVERAGE_PROCESS_START=/tmp/testbench/coverage/coveragerc" in command
    assert "export COVERAGE_FILE=/tmp/testbench/coverage/data/.coverage" in command
    assert "PYTHONPATH=/tmp/testbench/coverage/bootstrap:/app" in command
    assert "bash /app/agent-tests/run-tests.sh" in command


def test_persist_agent_tests_writes_replayable_snapshot_and_hashes(tmp_path):
    harness = _new_testbench_harness()
    harness._logger = __import__("logging").getLogger("persist-agent-tests")
    extracted = tmp_path / "extracted" / "agent-tests"
    extracted.mkdir(parents=True)
    runner = extracted / "run-tests.sh"
    runner.write_text("#!/bin/bash\nexit 0\n")
    test_file = extracted / "test_example.py"
    test_file.write_text("def test_example():\n    assert True\n")
    trial_root = tmp_path / "trial"
    paths = SimpleNamespace(
        agent_tests_path=trial_root / "agent-tests",
        agent_tests_manifest_path=trial_root / "agent-tests-manifest.json",
    )
    handler = SimpleNamespace(
        task_id="demo-task",
        trial_paths=paths,
    )

    harness._persist_agent_tests(handler, extracted.parent)

    assert (paths.agent_tests_path / "run-tests.sh").read_text() == runner.read_text()
    manifest = json.loads(paths.agent_tests_manifest_path.read_text())
    assert manifest["schema_version"] == 1
    assert manifest["task_id"] == "demo-task"
    assert (
        manifest["files"]["test_example.py"]
        == hashlib.sha256(test_file.read_bytes()).hexdigest()
    )


def test_generate_tests_can_replay_saved_snapshot(tmp_path):
    harness = _new_testbench_harness()
    harness._logger = __import__("logging").getLogger("replay-agent-tests")
    saved = tmp_path / "saved" / "agent-tests"
    saved.mkdir(parents=True)
    (saved / "run-tests.sh").write_text("#!/bin/bash\nexit 0\n")
    (saved / "test_example.py").write_text("def test_example():\n    assert True\n")
    harness._agent_tests_path = saved.parent
    trial_root = tmp_path / "trial"
    handler = SimpleNamespace(
        task_id="demo-task",
        trial_paths=SimpleNamespace(
            agent_tests_path=trial_root / "agent-tests",
            agent_tests_manifest_path=trial_root / "agent-tests-manifest.json",
        ),
    )

    generated = harness._generate_tests_for_patch_evaluation(
        handler,
        SimpleNamespace(),
    )

    assert generated is not None
    assert (generated / "agent-tests" / "test_example.py").is_file()
    assert (handler.trial_paths.agent_tests_path / "test_example.py").is_file()
    assert handler.trial_paths.agent_tests_manifest_path.is_file()
    shutil.rmtree(generated)


def test_print_testbench_results_uses_aggregate_counts(tmp_path, capsys):
    results_path = tmp_path / "results.json"
    results_path.write_text(
        json.dumps(
            {
                "results": [{"task_id": "task1", "is_resolved": False}],
                "n_resolved": 0,
                "n_total_tests": 1,
                "n_effective": 0,
                "n_false_positive": 0,
                "n_false_negative": 1,
                "n_regression": 0,
                "n_error": 0,
                "avg_f2p_rate": 0.0,
            }
        )
    )

    _print_testbench_results(results_path)

    output = capsys.readouterr().out
    assert "Total Agent Tests" in output
    assert "Total False Negative (F->F)" in output
    assert "Total Regression (P->F)" in output
    assert "Total Error" in output

    total_tests_row = next(
        line for line in output.splitlines() if "Total Agent Tests" in line
    )
    false_negative_row = next(
        line for line in output.splitlines() if "Total False Negative (F->F)" in line
    )
    assert "| 1" in total_tests_row
    assert "| 1" in false_negative_row


def test_print_testbench_results_includes_patch_coverage(tmp_path, capsys):
    results_path = tmp_path / "results.json"
    results_path.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "task_id": "task1",
                        "is_resolved": True,
                        "patch_set_id": "set-1",
                    }
                ],
                "n_resolved": 1,
                "n_total_tests": 1,
                "n_effective": 1,
                "n_false_positive": 0,
                "n_false_negative": 0,
                "n_regression": 0,
                "n_error": 0,
                "avg_f2p_rate": 1.0,
                "coverage": 0.75,
                "n_gold_patch_lines": 4,
                "n_gold_patch_lines_covered": 3,
                "n_coverage_tasks": 1,
                "n_coverage_unsupported": 0,
                "n_coverage_failed": 2,
            }
        )
    )

    _print_testbench_results(results_path)

    output = capsys.readouterr().out
    assert "Gold Patch Coverage" in output
    assert "75.00%" in output
    assert "Covered / Executable Gold Lines" in output
    assert "3 / 4" in output
    assert "Coverage Collected / No Executable / Unsupported" in output
    assert "Coverage Failed (excluded from mean)" in output
    assert "| 2" in next(
        line
        for line in output.splitlines()
        if "Coverage Failed (excluded from mean)" in line
    )
