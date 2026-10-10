import hashlib
import json
import subprocess
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from testprism.agents.failure_mode import FailureMode
from testprism.harness.entry_coverage_applicability import (
    EntryCoverageApplicabilityManifest,
)
from testprism.harness.gold_coverage_applicability import (
    GoldCoverageApplicabilityManifest,
)
from testprism.harness.java_coverage_backend import normalize_jacoco_xml
from testprism.harness.patch_coverage import PatchChanges, parse_unified_diff
from testprism.harness.testbench_harness import (
    _AGENT_TEST_QUARANTINE_PREFIX,
    _CANONICAL_DIFF_EXIT_PREFIX,
    _CANONICAL_SNAPSHOT_EXIT_PREFIX,
    AGENT_TEST_QUARANTINE_ROOT,
    _agent_test_quarantine_command,
    _canonical_diff_command,
    _canonical_snapshot_command,
    _inspection_command_diagnostic,
)
from testprism.harness.testbench_harness import TestBenchHarness as RuntimeHarness
from testprism.parsers.base_parser import UnitTestStatus
from testprism.patches.models import PatchLabel, load_patch_set_manifest

TASK_ID = "astropy__astropy-13236"


def _patch_root() -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "results/hardest500-patchgen-solutions/from-swe-bench-merged"
    )


def _governed_task(
    tasks_root: Path,
    task_key: str,
    *,
    solution: str = "#!/bin/sh\nexit 0\n",
) -> Path:
    task_dir = tasks_root / task_key
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "task.yaml").write_text("instruction: test\n", encoding="utf-8")
    (task_dir / "solution.sh").write_text(solution, encoding="utf-8")
    return task_dir


def _governed_solution_sha256(task_dir: Path) -> str:
    return hashlib.sha256((task_dir / "solution.sh").read_bytes()).hexdigest()


def _governed_record(
    task_dir: Path,
    *,
    decision: str = "applicable",
    reason_code: str | None = None,
    languages: list[str] | None = None,
    solution_sha256: str | None = None,
) -> dict[str, object]:
    if languages is None:
        languages = ["java"]
    if decision == "not_applicable":
        languages = []
    if reason_code is None:
        reason_code = (
            "tests_only"
            if decision == "not_applicable"
            else "production_source_only"
        )
    if solution_sha256 is None:
        solution_sha256 = _governed_solution_sha256(task_dir)
    return {
        "task_key": task_dir.relative_to(task_dir.parents[1]).as_posix(),
        "decision": decision,
        "reason_code": reason_code,
        "languages": languages,
        "evidence_paths": ["BREADTH_FIRST_SEARCH.java"],
        "solution_sha256": solution_sha256,
    }


def _entry_governed_record(
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
        solution_sha256 = _governed_solution_sha256(task_dir)
    return {
        "schema_version": 1,
        "task_key": task_dir.relative_to(task_dir.parents[1]).as_posix(),
        "decision": decision,
        "reason_code": reason_code,
        "languages": languages,
        "evidence_paths": ["src/Main.java", "tests/test_main.py"],
        "solution_sha256": solution_sha256,
    }


def _write_governed_manifest(path: Path, *records: object) -> None:
    lines = [
        record if isinstance(record, str) else json.dumps(record, sort_keys=True)
        for record in records
    ]
    payload = "\n".join(lines)
    if payload:
        payload += "\n"
    path.write_text(payload, encoding="utf-8")


def _run_governed_applicability_trial(
    tmp_path: Path,
    *,
    manifest_records: list[object],
    solution: str = "#!/bin/sh\nexit 0\n",
    entry_manifest_records: list[object] | None = None,
    entry_metadata: dict[str, object] | None = None,
    gold_generated_artifacts: object | None = None,
    gold_reference_artifacts: object | None = None,
    gold_coverage_error: str | None = None,
):
    manifest, root = load_patch_set_manifest(_patch_root(), TASK_ID)
    gold = next(patch for patch in manifest.solutions if patch.is_gold)
    harness = object.__new__(RuntimeHarness)
    harness._patch_manifests = {TASK_ID: (manifest, root)}
    harness._patch_concurrency = 3
    harness._logger = __import__("logging").getLogger("applicability-gate")

    governed_root = tmp_path / "verified_tasks" / "test-hardest-500"
    task_dir = _governed_task(
        governed_root,
        f"from-swe-bench-merged/{TASK_ID}",
        solution=solution,
    )
    manifest_path = governed_root / "GOLD_CHANGE_COVERAGE_APPLICABILITY.jsonl"
    _write_governed_manifest(manifest_path, *manifest_records)
    applicability_manifest = GoldCoverageApplicabilityManifest(
        tasks_root=governed_root,
        manifest_path=manifest_path,
    )
    entry_manifest = None
    if entry_manifest_records is not None:
        entry_manifest_path = governed_root / "ENTRY_COVERAGE_APPLICABILITY.jsonl"
        _write_governed_manifest(entry_manifest_path, *entry_manifest_records)
        entry_manifest = EntryCoverageApplicabilityManifest(
            tasks_root=governed_root,
            manifest_path=entry_manifest_path,
        )

    class Handler:
        task_id = TASK_ID
        trial_name = "governed-applicability-trial"
        instruction = "write tests"
        task = SimpleNamespace(test_entry_inferences=entry_metadata)
        task_paths = SimpleNamespace(input_path=task_dir)

        class trial_paths:
            task_output_path = tmp_path
            coverage_path = tmp_path / "coverage"

    harness._generate_tests_for_patch_evaluation = lambda handler, results: (
        tmp_path / "agent-tests"
    )
    inspect_calls: list[str] = []

    def fake_inspect(handler, patch, patch_root):
        inspect_calls.append(patch.id)
        raise AssertionError("applicability-gated tasks must skip patch inspection")

    harness._inspect_gold_patch = fake_inspect
    dispatches: list[tuple[str, list[str] | None]] = []

    def fake_run(
        handler,
        tests,
        loaded_manifest,
        patch_root,
        patch,
        coverage_targets=None,
    ):
        state = "baseline" if patch is None else patch.id
        dispatches.append((state, coverage_targets))
        if patch is None:
            return UnitTestStatus.FAILED, FailureMode.NONE, None
        expected = (
            UnitTestStatus.PASSED
            if patch.label is PatchLabel.ORACLE_VALID
            else UnitTestStatus.FAILED
        )
        if patch.is_gold:
            return (
                expected,
                FailureMode.NONE,
                gold_generated_artifacts,
                gold_reference_artifacts,
                gold_coverage_error,
            )
        return expected, FailureMode.NONE, None

    harness._run_patch_container = fake_run
    entry_patch = (
        patch(
            "testprism.harness.testbench_harness.resolve_entry_coverage_applicability",
            entry_manifest.resolve,
        )
        if entry_manifest is not None
        else nullcontext()
    )
    with patch(
        "testprism.harness.testbench_harness.resolve_gold_coverage_applicability",
        applicability_manifest.resolve,
    ):
        with entry_patch:
            result = harness._run_patch_set_trial(Handler())
    return result, dispatches, inspect_calls, gold, task_dir


def test_schema_v3_loader_finds_nested_sidecar_and_validates_solution_scripts():
    manifest, root = load_patch_set_manifest(_patch_root(), TASK_ID)

    assert manifest.schema_version == 3
    assert root.name == TASK_ID
    assert len(manifest.solutions) == 10
    assert {solution.artifact_type for solution in manifest.solutions} == {
        "solution_sh"
    }
    assert all(
        (root / solution.path).name == "solution.sh" for solution in manifest.solutions
    )


def _run_inspection_command(command: str) -> str:
    return subprocess.run(
        ["bash", "-c", command],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def test_non_git_snapshot_yields_checked_rename_aware_zero_context_diff(tmp_path):
    workdir = tmp_path / "task workdir"
    workdir.mkdir()
    (workdir / "BREADTH_FIRST_SEARCH.java").write_text("return false;\n")
    (workdir / "old-name.txt").write_text("rename me\n")
    snapshot_pane = _run_inspection_command(_canonical_snapshot_command(str(workdir)))

    assert _inspection_command_diagnostic(
        snapshot_pane,
        prefix=_CANONICAL_SNAPSHOT_EXIT_PREFIX,
        stage="snapshot",
    ) is None
    assert (workdir / ".git").is_dir()
    assert subprocess.run(
        ["git", "-C", str(workdir), "config", "--local", "--get", "user.name"],
        capture_output=True,
    ).returncode == 1

    (workdir / "BREADTH_FIRST_SEARCH.java").write_text("return true;\n")
    (workdir / "old-name.txt").rename(workdir / "new-name.txt")
    (workdir / "agent-tests").mkdir()
    (workdir / "agent-tests" / "generated.java").write_text("ignored\n")
    (workdir / "coverage").mkdir()
    (workdir / "coverage" / "coverage.json").write_text("{}\n")
    (workdir / "BREADTH_FIRST_SEARCH.java.orig").write_text("ignored\n")
    output = tmp_path / "gold.patch.diff"
    diff_pane = _run_inspection_command(
        _canonical_diff_command(str(workdir), str(output))
    )

    assert _inspection_command_diagnostic(
        diff_pane,
        prefix=_CANONICAL_DIFF_EXIT_PREFIX,
        stage="diff",
    ) is None
    diff = output.read_text()
    assert "@@ -1 +1 @@" in diff
    assert "similarity index 100%" in diff
    assert "rename from old-name.txt" in diff
    assert "rename to new-name.txt" in diff
    assert "agent-tests" not in diff
    assert "coverage.json" not in diff
    assert ".orig" not in diff
    changes = parse_unified_diff(diff)
    assert changes.changed_files == ["BREADTH_FIRST_SEARCH.java", "new-name.txt"]
    java_change = next(
        changed
        for changed in changes.files
        if changed.new_path == "BREADTH_FIRST_SEARCH.java"
    )
    assert java_change.modified_old_lines == {1}
    assert java_change.modified_new_lines == {1}


def test_empty_non_git_snapshot_creates_valid_empty_baseline(tmp_path):
    workdir = tmp_path / "empty-task workdir"
    workdir.mkdir()

    snapshot_pane = _run_inspection_command(_canonical_snapshot_command(str(workdir)))

    assert _inspection_command_diagnostic(
        snapshot_pane,
        prefix=_CANONICAL_SNAPSHOT_EXIT_PREFIX,
        stage="snapshot",
    ) is None
    head = subprocess.run(
        ["git", "-C", str(workdir), "rev-parse", "--verify", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    tree = subprocess.run(
        ["git", "-C", str(workdir), "rev-parse", "--verify", "HEAD^{tree}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert len(head) == 40
    assert len(tree) == 40
    assert subprocess.run(
        ["git", "-C", str(workdir), "cat-file", "-t", tree],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip() == "tree"
    assert subprocess.run(
        ["git", "-C", str(workdir), "ls-tree", "-r", "--name-only", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout == ""


def test_snapshot_with_only_canonical_excludes_creates_empty_baseline(tmp_path):
    workdir = tmp_path / "excluded-only-task"
    workdir.mkdir()
    excluded_paths = (
        "solution_patch.diff",
        "notes.orig",
        "agent-tests/generated.java",
        "coverage/coverage.json",
    )
    for relative_path in excluded_paths:
        path = workdir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ignored\n")

    snapshot_pane = _run_inspection_command(_canonical_snapshot_command(str(workdir)))

    assert _inspection_command_diagnostic(
        snapshot_pane,
        prefix=_CANONICAL_SNAPSHOT_EXIT_PREFIX,
        stage="snapshot",
    ) is None
    baseline_paths = subprocess.run(
        ["git", "-C", str(workdir), "ls-tree", "-r", "--name-only", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert baseline_paths == []
    assert all(relative_path not in baseline_paths for relative_path in excluded_paths)


def test_snapshot_reuses_existing_valid_head(tmp_path):
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "tracked.txt").write_text("baseline\n")
    subprocess.run(["git", "init", str(workdir)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(workdir), "add", "tracked.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(workdir),
            "-c",
            "user.name=Existing",
            "-c",
            "user.email=existing@example.test",
            "commit",
            "-m",
            "existing baseline",
        ],
        check=True,
        capture_output=True,
    )
    before = subprocess.run(
        ["git", "-C", str(workdir), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    pane = _run_inspection_command(_canonical_snapshot_command(str(workdir)))

    assert _inspection_command_diagnostic(
        pane,
        prefix=_CANONICAL_SNAPSHOT_EXIT_PREFIX,
        stage="snapshot",
    ) is None
    after = subprocess.run(
        ["git", "-C", str(workdir), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert after == before


@pytest.mark.parametrize(
    ("pane", "prefix", "stage", "diagnostic"),
    [
        (
            "__GOLD_SNAPSHOT_EXIT__=7\n",
            _CANONICAL_SNAPSHOT_EXIT_PREFIX,
            "snapshot",
            "gold patch inspection snapshot failed with exit code 7",
        ),
        (
            "command echo only\n",
            _CANONICAL_SNAPSHOT_EXIT_PREFIX,
            "snapshot",
            "gold patch inspection snapshot missing exit marker",
        ),
        (
            "__GOLD_DIFF_EXIT__=2\n",
            _CANONICAL_DIFF_EXIT_PREFIX,
            "diff",
            "gold patch inspection diff failed with exit code 2",
        ),
        (
            "command echo only\n",
            _CANONICAL_DIFF_EXIT_PREFIX,
            "diff",
            "gold patch inspection diff missing exit marker",
        ),
    ],
)
def test_inspection_command_failures_are_explicit(
    pane,
    prefix,
    stage,
    diagnostic,
):
    assert (
        _inspection_command_diagnostic(pane, prefix=prefix, stage=stage)
        == diagnostic
    )


def _inspection_handler(tmp_path):
    return SimpleNamespace(
        task_id="inspection-task",
        client_container_name="inspection-trial",
        client_image_name="task-image",
        docker_image_name_prefix="prefix",
        task=SimpleNamespace(workdir="/app", disable_asciinema=False),
        task_paths=SimpleNamespace(docker_compose_path=tmp_path / "compose.yaml"),
        trial_paths=SimpleNamespace(
            sessions_path=tmp_path / "sessions",
            coverage_path=tmp_path / "coverage",
            agent_logging_dir=tmp_path / "agent-logs",
            commands_path=tmp_path / "commands.txt",
        ),
    )


@pytest.mark.parametrize(
    ("copied_diff", "copy_error", "expected_error"),
    [
        (None, None, "gold patch inspection did not produce a diff artifact"),
        (
            None,
            OSError("copy unavailable"),
            "gold patch inspection diff artifact copy failed: copy unavailable",
        ),
        (
            "diff --git a/only\n",
            None,
            "gold patch inspection diff parse failed: malformed diff --git header",
        ),
    ],
)
def test_inspection_artifact_failures_are_explicit(
    tmp_path,
    copied_diff,
    copy_error,
    expected_error,
):
    harness = object.__new__(RuntimeHarness)
    harness._logger = __import__("logging").getLogger("inspection-artifacts")

    class Session:
        def __init__(self, pane):
            self.pane = pane

        def send_keys(self, keys, **kwargs):
            return None

        def capture_pane(self, capture_entire=False):
            return self.pane

    class Terminal:
        def __init__(self):
            self.panes = iter(
                [
                    f"{_CANONICAL_SNAPSHOT_EXIT_PREFIX}0\n",
                    "",
                    f"{_CANONICAL_DIFF_EXIT_PREFIX}0\n",
                ]
            )

        def create_session(self, name, **kwargs):
            return Session(next(self.panes))

        def copy_from_container(self, source, destination):
            if copy_error is not None:
                raise copy_error
            if copied_diff is not None:
                (destination / "gold.patch.diff").write_text(copied_diff)

    @contextmanager
    def fake_terminal(**kwargs):
        yield Terminal()

    harness._apply_patch_entry = lambda *args, **kwargs: FailureMode.NONE
    handler = _inspection_handler(tmp_path)
    handler.trial_paths.coverage_path.mkdir()
    (handler.trial_paths.coverage_path / "gold.patch.diff").write_text("stale\n")
    with patch(
        "testprism.harness.testbench_harness.spin_up_terminal", fake_terminal
    ):
        changes, error = harness._inspect_gold_patch(
            handler,
            SimpleNamespace(),
            tmp_path,
        )

    assert changes is None
    assert error == expected_error


def test_checked_successful_empty_inspection_diff_is_not_a_failure(tmp_path):
    harness = object.__new__(RuntimeHarness)
    harness._logger = __import__("logging").getLogger("empty-inspection")

    class Session:
        def __init__(self, pane):
            self.pane = pane

        def send_keys(self, keys, **kwargs):
            return None

        def capture_pane(self, capture_entire=False):
            return self.pane

    class Terminal:
        def __init__(self):
            self.panes = iter(
                [
                    f"{_CANONICAL_SNAPSHOT_EXIT_PREFIX}0\n",
                    "",
                    f"{_CANONICAL_DIFF_EXIT_PREFIX}0\n",
                ]
            )

        def create_session(self, name, **kwargs):
            return Session(next(self.panes))

        def copy_from_container(self, source, destination):
            (destination / "gold.patch.diff").write_text("")

    @contextmanager
    def fake_terminal(**kwargs):
        yield Terminal()

    harness._apply_patch_entry = lambda *args, **kwargs: FailureMode.NONE
    with patch(
        "testprism.harness.testbench_harness.spin_up_terminal", fake_terminal
    ):
        changes, error = harness._inspect_gold_patch(
            _inspection_handler(tmp_path),
            SimpleNamespace(),
            tmp_path,
        )

    assert changes == PatchChanges()
    assert error is None


def test_patch_set_trial_dispatches_baseline_and_all_schema_v3_solutions(tmp_path):
    manifest, root = load_patch_set_manifest(_patch_root(), TASK_ID)
    harness = object.__new__(RuntimeHarness)
    harness._patch_manifests = {TASK_ID: (manifest, root)}
    harness._patch_concurrency = 3
    harness._logger = __import__("logging").getLogger("testbench-v3")
    calls: list[str] = []

    class Handler:
        task_id = TASK_ID
        trial_name = "astropy-trial"
        instruction = "write tests"

        class trial_paths:
            task_output_path = tmp_path
            coverage_path = tmp_path / "coverage"

    harness._generate_tests_for_patch_evaluation = lambda handler, results: (
        tmp_path / "agent-tests"
    )
    harness._inspect_gold_patch = lambda handler, patch, patch_root: (
        PatchChanges(),
        None,
    )

    def fake_run(
        handler,
        tests,
        loaded_manifest,
        patch_root,
        patch,
        coverage_targets=None,
    ):
        calls.append("baseline" if patch is None else patch.id)
        if patch is None:
            return UnitTestStatus.FAILED, FailureMode.NONE, None
        expected = (
            UnitTestStatus.PASSED
            if patch.label is PatchLabel.ORACLE_VALID
            else UnitTestStatus.FAILED
        )
        return expected, FailureMode.NONE, None

    harness._run_patch_container = fake_run
    result = harness._run_patch_set_trial(Handler())

    assert len(calls) == 11
    assert set(calls) == {"baseline", *(solution.id for solution in manifest.solutions)}
    assert result.baseline_detected is True
    assert result.valid_acceptance_rate == 1.0
    assert result.invalid_rejection_rate == 1.0
    assert result.balanced_patch_accuracy == 1.0
    assert result.n_patch_errors == 0
    assert result.is_resolved is True
    assert result.gold_patch_coverage is not None
    assert result.gold_patch_coverage.status.value == "no_changed_lines"
    assert (tmp_path / "coverage" / "gold-patch.json").is_file()


def test_patch_set_trial_propagates_missing_agent_test_runner_failure_mode(tmp_path):
    manifest, root = load_patch_set_manifest(_patch_root(), TASK_ID)
    harness = object.__new__(RuntimeHarness)
    harness._patch_manifests = {TASK_ID: (manifest, root)}
    harness._patch_concurrency = 3
    harness._logger = __import__("logging").getLogger("missing-agent-test-runner")

    class Handler:
        task_id = TASK_ID
        trial_name = "astropy-trial"
        instruction = "write tests"

        class trial_paths:
            task_output_path = tmp_path
            coverage_path = tmp_path / "coverage"

    harness._generate_tests_for_patch_evaluation = lambda handler, results: (
        tmp_path / "agent-tests"
    )
    harness._inspect_gold_patch = lambda handler, patch, patch_root: (
        PatchChanges(),
        None,
    )
    harness._run_patch_container = lambda *args, **kwargs: (
        None,
        FailureMode.AGENT_TEST_MISSING,
        None,
    )

    result = harness._run_patch_set_trial(Handler())

    assert result.failure_mode == FailureMode.AGENT_TEST_MISSING
    assert result.pre_fix_results is None
    assert result.post_fix_results is None
    assert result.baseline_detected is False
    assert result.is_resolved is False
    assert result.n_patch_errors == len(manifest.solutions)
    assert result.patch_results
    assert all(
        item.failure_mode == FailureMode.AGENT_TEST_MISSING
        and item.observed_status is None
        for item in result.patch_results
    )


def test_inspection_failure_serializes_failed_without_changing_verdicts(tmp_path):
    manifest, root = load_patch_set_manifest(_patch_root(), TASK_ID)
    harness = object.__new__(RuntimeHarness)
    harness._patch_manifests = {TASK_ID: (manifest, root)}
    harness._patch_concurrency = 3
    harness._logger = __import__("logging").getLogger("inspection-fail-soft")

    class Handler:
        task_id = TASK_ID
        trial_name = "astropy-trial"
        instruction = "write tests"

        class trial_paths:
            task_output_path = tmp_path
            coverage_path = tmp_path / "coverage"

    harness._generate_tests_for_patch_evaluation = lambda handler, results: (
        tmp_path / "agent-tests"
    )
    diagnostic = "gold patch inspection diff failed with exit code 128"
    harness._inspect_gold_patch = lambda handler, patch, patch_root: (
        None,
        diagnostic,
    )

    def fake_run(
        handler,
        tests,
        loaded_manifest,
        patch_root,
        patch,
        coverage_targets=None,
    ):
        if patch is None or patch.is_gold:
            assert coverage_targets == []
        else:
            assert coverage_targets is None
        if patch is None:
            return UnitTestStatus.FAILED, FailureMode.NONE, None
        expected = (
            UnitTestStatus.PASSED
            if patch.label is PatchLabel.ORACLE_VALID
            else UnitTestStatus.FAILED
        )
        return expected, FailureMode.NONE, None

    harness._run_patch_container = fake_run
    result = harness._run_patch_set_trial(Handler())

    assert result.gold_patch_coverage is not None
    assert result.gold_patch_coverage.status.value == "failed"
    assert result.gold_patch_coverage.reason == diagnostic
    assert result.valid_acceptance_rate == 1.0
    assert result.invalid_rejection_rate == 1.0
    assert result.balanced_patch_accuracy == 1.0
    assert result.n_patch_errors == 0
    assert result.is_resolved is True
    serialized = json.loads(
        (tmp_path / "coverage" / "gold-patch.json").read_text()
    )
    assert serialized["status"] == "failed"
    assert serialized["reason"] == diagnostic


def test_governed_not_applicable_skips_gold_coverage_and_serializes_unsupported(
    tmp_path,
):
    governed_root = tmp_path / "verified_tasks" / "test-hardest-500"
    task_dir = _governed_task(
        governed_root,
        f"from-swe-bench-merged/{TASK_ID}",
    )
    result, dispatches, inspect_calls, gold, _ = (
        _run_governed_applicability_trial(
            tmp_path,
            manifest_records=[
                _governed_record(
                    task_dir,
                    decision="not_applicable",
                    reason_code="tests_only",
                )
            ],
        )
    )

    assert inspect_calls == []
    assert ("baseline", []) in dispatches
    assert (gold.id, []) in dispatches
    assert all(
        targets is None
        for state, targets in dispatches
        if state not in {"baseline", gold.id}
    )
    assert result.pre_fix_results == {"agent_tests": UnitTestStatus.FAILED}
    assert result.post_fix_results == {"agent_tests": UnitTestStatus.PASSED}
    assert result.is_resolved is True
    assert result.verdicts
    assert result.gold_patch_coverage is not None
    assert result.gold_patch_coverage.status.value == "unsupported"
    assert result.gold_patch_coverage.reason == "tests_only"
    serialized = json.loads(
        (tmp_path / "coverage" / "gold-patch.json").read_text()
    )
    assert serialized["status"] == "unsupported"
    assert serialized["reason"] == "tests_only"


@pytest.mark.parametrize(
    ("manifest_records", "expected_reason_fragment"),
    [
        (
            "missing_record",
            "gold coverage applicability manifest has no record",
        ),
        (
            "stale_record",
            "gold coverage applicability manifest record",
        ),
        (
            "invalid_reason_code",
            "malformed record(s)",
        ),
    ],
)
def test_governed_applicability_fail_closed_preserves_verdicts(
    tmp_path,
    manifest_records,
    expected_reason_fragment,
):
    governed_root = tmp_path / "verified_tasks" / "test-hardest-500"
    task_dir = _governed_task(
        governed_root,
        f"from-swe-bench-merged/{TASK_ID}",
        solution="#!/bin/sh\necho governed\n",
    )
    if manifest_records == "missing_record":
        other_task = _governed_task(
            governed_root,
            "from-swe-bench-merged/other-task",
        )
        records = [_governed_record(other_task)]
    elif manifest_records == "invalid_reason_code":
        records = [
            _governed_record(
                task_dir,
                decision="not_applicable",
                reason_code="test_only_change",
            )
        ]
    else:
        records = [
            _governed_record(
                task_dir,
                solution_sha256="0" * 64,
            )
        ]

    result, dispatches, inspect_calls, gold, _ = (
        _run_governed_applicability_trial(
            tmp_path,
            manifest_records=records,
            solution="#!/bin/sh\necho governed\n",
        )
    )

    assert inspect_calls == []
    assert ("baseline", []) in dispatches
    assert (gold.id, []) in dispatches
    assert all(
        targets is None
        for state, targets in dispatches
        if state not in {"baseline", gold.id}
    )
    assert result.pre_fix_results == {"agent_tests": UnitTestStatus.FAILED}
    assert result.post_fix_results == {"agent_tests": UnitTestStatus.PASSED}
    assert result.is_resolved is True
    assert result.verdicts
    assert result.gold_patch_coverage is not None
    assert result.gold_patch_coverage.status.value == "failed"
    assert result.gold_patch_coverage.reason is not None
    assert "gold coverage applicability failed:" in result.gold_patch_coverage.reason
    assert expected_reason_fragment in result.gold_patch_coverage.reason
    serialized = json.loads(
        (tmp_path / "coverage" / "gold-patch.json").read_text()
    )
    assert serialized["status"] == "failed"
    assert expected_reason_fragment in serialized["reason"]


def test_governed_entry_coverage_collects_when_gold_coverage_is_gated_off(tmp_path):
    governed_root = tmp_path / "verified_tasks" / "test-hardest-500"
    task_dir = _governed_task(
        governed_root,
        f"from-swe-bench-merged/{TASK_ID}",
    )
    gold_report = tmp_path / "governed-entry-java.json"
    gold_report.write_text(
        json.dumps(
            {
                "files": {
                    "src/Main.java": {
                        "executed_lines": [10],
                        "missing_lines": [11, 12],
                    }
                }
            }
        )
    )

    result, dispatches, inspect_calls, gold, _ = _run_governed_applicability_trial(
        tmp_path,
        manifest_records=[
            _governed_record(
                task_dir,
                decision="not_applicable",
                reason_code="tests_only",
            )
        ],
        entry_manifest_records=[
            _entry_governed_record(task_dir, languages=["java"])
        ],
        entry_metadata={
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
        },
        gold_generated_artifacts={"java": gold_report},
    )

    assert inspect_calls == []
    assert ("baseline", []) in dispatches
    assert (gold.id, ["src/Main.java"]) in dispatches
    assert sum(state == gold.id for state, _ in dispatches) == 1
    assert all(
        targets is None
        for state, targets in dispatches
        if state not in {"baseline", gold.id}
    )
    assert result.pre_fix_results == {"agent_tests": UnitTestStatus.FAILED}
    assert result.post_fix_results == {"agent_tests": UnitTestStatus.PASSED}
    assert result.is_resolved is True
    assert result.verdicts
    assert result.gold_patch_coverage is not None
    assert result.gold_patch_coverage.status.value == "unsupported"
    assert result.gold_patch_coverage.reason == "tests_only"
    assert result.entry_coverage is not None
    assert result.entry_coverage.status.value == "collected"
    assert result.entry_coverage.language == "java"
    assert result.entry_coverage.ratio == pytest.approx(1 / 3)
    raw = json.loads(
        (tmp_path / "coverage" / "generated" / "gold" / "coverage.json").read_text()
    )
    assert "src/Main.java" in raw["files"]


def test_governed_entry_coverage_without_metadata_is_unavailable(tmp_path):
    governed_root = tmp_path / "verified_tasks" / "test-hardest-500"
    task_dir = _governed_task(
        governed_root,
        f"from-swe-bench-merged/{TASK_ID}",
    )

    result, dispatches, inspect_calls, gold, _ = _run_governed_applicability_trial(
        tmp_path,
        manifest_records=[
            _governed_record(
                task_dir,
                decision="not_applicable",
                reason_code="tests_only",
            )
        ],
        entry_manifest_records=[
            _entry_governed_record(task_dir, languages=["python"])
        ],
        entry_metadata=None,
    )

    assert inspect_calls == []
    assert ("baseline", []) in dispatches
    assert (gold.id, []) in dispatches
    assert result.pre_fix_results == {"agent_tests": UnitTestStatus.FAILED}
    assert result.post_fix_results == {"agent_tests": UnitTestStatus.PASSED}
    assert result.is_resolved is True
    assert result.verdicts
    assert result.gold_patch_coverage is not None
    assert result.gold_patch_coverage.status.value == "unsupported"
    assert result.gold_patch_coverage.reason == "tests_only"
    assert result.entry_coverage is not None
    assert result.entry_coverage.status.value == "unavailable"
    assert result.entry_coverage.reason == "task has no test entry metadata"


def _checked_non_git_java_changes(tmp_path):
    workdir = tmp_path / "non-git-java-task"
    workdir.mkdir()
    source = workdir / "BREADTH_FIRST_SEARCH.java"
    source.write_text("return false;\n")
    snapshot_pane = _run_inspection_command(_canonical_snapshot_command(str(workdir)))
    assert _inspection_command_diagnostic(
        snapshot_pane,
        prefix=_CANONICAL_SNAPSHOT_EXIT_PREFIX,
        stage="snapshot",
    ) is None

    source.write_text("return true;\n")
    diff_path = tmp_path / "checked-gold.patch.diff"
    diff_pane = _run_inspection_command(
        _canonical_diff_command(str(workdir), str(diff_path))
    )
    assert _inspection_command_diagnostic(
        diff_pane,
        prefix=_CANONICAL_DIFF_EXIT_PREFIX,
        stage="diff",
    ) is None
    return parse_unified_diff(diff_path.read_text())


def _write_java_report(path, *, executed_lines, missing_lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "files": {
                    "BREADTH_FIRST_SEARCH.java": {
                        "executed_lines": executed_lines,
                        "missing_lines": missing_lines,
                    }
                }
            }
        )
    )
    return path


def _run_non_git_java_trial(
    tmp_path,
    *,
    missing_report=None,
    changes_override=None,
    reports_override=None,
):
    manifest, root = load_patch_set_manifest(_patch_root(), TASK_ID)
    gold = next(patch for patch in manifest.solutions if patch.is_gold)
    harness = object.__new__(RuntimeHarness)
    harness._patch_manifests = {TASK_ID: (manifest, root)}
    harness._patch_concurrency = 3
    harness._logger = __import__("logging").getLogger("non-git-java-contract")

    class Handler:
        task_id = TASK_ID
        trial_name = "non-git-java-trial"
        instruction = "write tests"

        class trial_paths:
            task_output_path = tmp_path
            coverage_path = tmp_path / "coverage"

    changes = changes_override or _checked_non_git_java_changes(tmp_path)
    harness._generate_tests_for_patch_evaluation = lambda handler, results: (
        tmp_path / "agent-tests"
    )
    harness._inspect_gold_patch = lambda handler, patch, patch_root: (changes, None)

    reports = reports_override or {
        "generated_baseline": _write_java_report(
            tmp_path
            / "coverage/generated/baseline/java/coverage.json",
            executed_lines=[],
            missing_lines=[1],
        ),
        "reference_baseline": _write_java_report(
            tmp_path
            / "coverage/reference/baseline/java/coverage.json",
            executed_lines=[1],
            missing_lines=[],
        ),
        "generated_gold": _write_java_report(
            tmp_path
            / f"coverage/generated/{gold.id}/java/coverage.json",
            executed_lines=[1],
            missing_lines=[],
        ),
        "reference_gold": _write_java_report(
            tmp_path
            / f"coverage/reference/{gold.id}/java/coverage.json",
            executed_lines=[1],
            missing_lines=[],
        ),
    }
    if missing_report is not None:
        reports[missing_report] = None

    dispatches = []

    def fake_run(
        handler,
        tests,
        loaded_manifest,
        patch_root,
        patch,
        coverage_targets=None,
    ):
        state = "baseline" if patch is None else patch.id
        language = (
            harness._coverage_backend_for_targets(coverage_targets).language
            if coverage_targets
            else None
        )
        dispatches.append((state, coverage_targets, language))
        if patch is None:
            return (
                UnitTestStatus.FAILED,
                FailureMode.NONE,
                reports["generated_baseline"],
                reports["reference_baseline"],
                None,
            )
        expected = (
            UnitTestStatus.PASSED
            if patch.label is PatchLabel.ORACLE_VALID
            else UnitTestStatus.FAILED
        )
        if patch.is_gold:
            return (
                expected,
                FailureMode.NONE,
                reports["generated_gold"],
                reports["reference_gold"],
                None,
            )
        return expected, FailureMode.NONE, None, None, None

    harness._run_patch_container = fake_run
    return harness._run_patch_set_trial(Handler()), changes, dispatches, gold


def test_non_git_java_inspection_dispatches_and_consumes_four_reports(tmp_path):
    result, changes, dispatches, gold = _run_non_git_java_trial(tmp_path)

    assert changes.changed_files == ["BREADTH_FIRST_SEARCH.java"]
    assert [(item.old_path, item.new_path) for item in changes.files] == [
        ("BREADTH_FIRST_SEARCH.java", "BREADTH_FIRST_SEARCH.java")
    ]
    assert (
        "baseline",
        ["BREADTH_FIRST_SEARCH.java"],
        "java",
    ) in dispatches
    assert (
        gold.id,
        ["BREADTH_FIRST_SEARCH.java"],
        "java",
    ) in dispatches
    assert all(
        targets is None and language is None
        for state, targets, language in dispatches
        if state not in {"baseline", gold.id}
    )

    coverage = result.gold_patch_coverage
    assert coverage is not None
    assert coverage.status.value == "collected"
    assert coverage.language == "java"
    assert coverage.languages == ["java"]
    assert coverage.changed_files == ["BREADTH_FIRST_SEARCH.java"]
    assert coverage.reference_executed_line_count == 2
    assert coverage.generated_covered_line_count == 1
    assert coverage.ratio == 0.5
    assert coverage.line_classifications["generated_uncovered"] == [
        "baseline:BREADTH_FIRST_SEARCH.java:1"
    ]
    assert coverage.line_classifications["generated_covered"] == [
        "gold:BREADTH_FIRST_SEARCH.java:1"
    ]
    serialized = json.loads(
        (tmp_path / "coverage" / "gold-patch.json").read_text()
    )
    assert serialized["status"] == "collected"
    assert serialized["language"] == "java"
    assert serialized["languages"] == ["java"]
    assert serialized["ratio"] == 0.5


def test_packaged_java_line_24_reports_define_denominator_before_generated_ratio(
    tmp_path,
):
    workdir = tmp_path / "packaged-java-task"
    workdir.mkdir()
    source = workdir / "BREADTH_FIRST_SEARCH.java"

    def source_text(condition):
        lines = ["package java_programs;", "public class BREADTH_FIRST_SEARCH {"]
        lines.extend(f"  // filler {number}" for number in range(3, 24))
        lines.extend((f"  while ({condition}) {{}}", "}"))
        return "\n".join(lines) + "\n"

    source.write_text(source_text("true"))
    snapshot_pane = _run_inspection_command(_canonical_snapshot_command(str(workdir)))
    assert _inspection_command_diagnostic(
        snapshot_pane,
        prefix=_CANONICAL_SNAPSHOT_EXIT_PREFIX,
        stage="snapshot",
    ) is None
    source.write_text(source_text("!queue.isEmpty()"))
    diff_path = tmp_path / "packaged-gold.patch.diff"
    diff_pane = _run_inspection_command(
        _canonical_diff_command(str(workdir), str(diff_path))
    )
    assert _inspection_command_diagnostic(
        diff_pane,
        prefix=_CANONICAL_DIFF_EXIT_PREFIX,
        stage="diff",
    ) is None
    changes = parse_unified_diff(diff_path.read_text())

    def normalized_report(path, *, covered):
        path.parent.mkdir(parents=True, exist_ok=True)
        report = normalize_jacoco_xml(
            "<report><package name='java_programs'>"
            "<sourcefile name='BREADTH_FIRST_SEARCH.java'>"
            f"<line nr='24' mi='{0 if covered else 1}' "
            f"ci='{1 if covered else 0}' />"
            "</sourcefile></package></report>",
            workdir=workdir,
            target_paths=["BREADTH_FIRST_SEARCH.java"],
        )
        path.write_text(json.dumps(report))
        return path

    reports = {
        "generated_baseline": normalized_report(
            tmp_path / "coverage/generated/baseline/java/coverage.json",
            covered=False,
        ),
        "reference_baseline": normalized_report(
            tmp_path / "coverage/reference/baseline/java/coverage.json",
            covered=True,
        ),
        "generated_gold": normalized_report(
            tmp_path / "coverage/generated/gold/java/coverage.json",
            covered=True,
        ),
        "reference_gold": normalized_report(
            tmp_path / "coverage/reference/gold/java/coverage.json",
            covered=True,
        ),
    }

    result, _changes, _dispatches, _gold = _run_non_git_java_trial(
        tmp_path,
        changes_override=changes,
        reports_override=reports,
    )

    coverage = result.gold_patch_coverage
    assert coverage is not None
    assert coverage.status.value == "collected"
    assert coverage.executable_changed_line_count == 2
    assert coverage.reference_executed_line_count == 2
    assert coverage.generated_covered_line_count == 1
    assert coverage.ratio == 0.5
    assert coverage.line_classifications["generated_uncovered"] == [
        "baseline:BREADTH_FIRST_SEARCH.java:24"
    ]
    assert coverage.line_classifications["generated_covered"] == [
        "gold:BREADTH_FIRST_SEARCH.java:24"
    ]


@pytest.mark.parametrize(
    "missing_report",
    [
        "generated_baseline",
        "reference_baseline",
        "generated_gold",
        "reference_gold",
    ],
)
def test_missing_required_java_report_fails_coverage_and_preserves_verdicts(
    tmp_path,
    missing_report,
):
    result, _changes, _dispatches, _gold = _run_non_git_java_trial(
        tmp_path,
        missing_report=missing_report,
    )

    coverage = result.gold_patch_coverage
    assert coverage is not None
    assert coverage.status.value == "failed"
    assert coverage.language == "java"
    assert coverage.languages == ["java"]
    assert coverage.changed_files == ["BREADTH_FIRST_SEARCH.java"]
    assert coverage.reason == (
        f"missing required {missing_report.replace('_', ' ')} coverage report"
    )
    assert result.pre_fix_results == {"agent_tests": UnitTestStatus.FAILED}
    assert result.post_fix_results == {"agent_tests": UnitTestStatus.PASSED}
    assert result.valid_acceptance_rate == 1.0
    assert result.invalid_rejection_rate == 1.0
    assert result.balanced_patch_accuracy == 1.0
    assert result.n_patch_errors == 0
    assert result.is_resolved is True
    assert result.verdicts


def test_patch_set_trial_combines_baseline_and_gold_changed_line_coverage(tmp_path):
    manifest, root = load_patch_set_manifest(_patch_root(), TASK_ID)
    harness = object.__new__(RuntimeHarness)
    harness._patch_manifests = {TASK_ID: (manifest, root)}
    harness._patch_concurrency = 3
    harness._logger = __import__("logging").getLogger("testbench-v3-coverage")

    class Handler:
        task_id = TASK_ID
        trial_name = "astropy-trial"
        instruction = "write tests"

        class trial_paths:
            task_output_path = tmp_path
            coverage_path = tmp_path / "coverage"

    changes = parse_unified_diff(
        """diff --git a/pkg/code.py b/pkg/code.py
--- a/pkg/code.py
+++ b/pkg/code.py
@@ -1 +1 @@
-before()
+after()
"""
    )
    harness._generate_tests_for_patch_evaluation = lambda handler, results: (
        tmp_path / "agent-tests"
    )
    harness._inspect_gold_patch = lambda handler, patch, patch_root: (changes, None)

    baseline_report = tmp_path / "baseline.json"
    baseline_report.write_text(
        json.dumps(
            {"files": {"pkg/code.py": {"executed_lines": [], "missing_lines": [1]}}}
        )
    )
    gold_report = tmp_path / "gold.json"
    gold_report.write_text(
        json.dumps(
            {"files": {"pkg/code.py": {"executed_lines": [1], "missing_lines": []}}}
        )
    )
    baseline_reference_report = tmp_path / "baseline-reference.json"
    baseline_reference_report.write_text(
        json.dumps(
            {"files": {"pkg/code.py": {"executed_lines": [1], "missing_lines": []}}}
        )
    )
    gold_reference_report = tmp_path / "gold-reference.json"
    gold_reference_report.write_text(
        json.dumps(
            {"files": {"pkg/code.py": {"executed_lines": [1], "missing_lines": []}}}
        )
    )

    def fake_run(
        handler,
        tests,
        loaded_manifest,
        patch_root,
        patch,
        coverage_targets=None,
    ):
        if patch is None:
            assert coverage_targets == ["pkg/code.py"]
            return (
                UnitTestStatus.FAILED,
                FailureMode.NONE,
                baseline_report,
                baseline_reference_report,
                None,
            )
        expected = (
            UnitTestStatus.PASSED
            if patch.label is PatchLabel.ORACLE_VALID
            else UnitTestStatus.FAILED
        )
        report = gold_report if patch.is_gold else None
        if patch.is_gold:
            assert coverage_targets == ["pkg/code.py"]
        else:
            assert coverage_targets is None
        return (
            expected,
            FailureMode.NONE,
            report,
            gold_reference_report if patch.is_gold else None,
            None,
        )

    harness._run_patch_container = fake_run

    result = harness._run_patch_set_trial(Handler())

    assert result.gold_patch_coverage is not None
    assert result.gold_patch_coverage.status.value == "collected"
    assert result.gold_patch_coverage.executable_changed_line_count == 2
    assert result.gold_patch_coverage.covered_changed_line_count == 1
    assert result.coverage == 0.5
    summary = json.loads((tmp_path / "coverage" / "gold-patch.json").read_text())
    assert summary["ratio"] == 0.5


def test_patch_set_trial_collects_entry_coverage_from_existing_gold_report(tmp_path):
    manifest, root = load_patch_set_manifest(_patch_root(), TASK_ID)
    harness = object.__new__(RuntimeHarness)
    harness._patch_manifests = {TASK_ID: (manifest, root)}
    harness._patch_concurrency = 3
    harness._logger = __import__("logging").getLogger("testbench-v3-entry")

    class Handler:
        task_id = TASK_ID
        trial_name = "astropy-entry-trial"
        instruction = "write tests"
        task = SimpleNamespace(
            test_entry_inferences={
                "schema_version": 1,
                "language": "python",
                "source": "static_test_invocation",
                "entries": [
                    {
                        "path": "pkg/entry.py",
                        "start_line": 10,
                        "end_line": 12,
                    },
                    # Duplicate declarations must not expand targets twice.
                    {
                        "path": "pkg/entry.py",
                        "start_line": 10,
                        "end_line": 12,
                    },
                ],
            }
        )

        class trial_paths:
            task_output_path = tmp_path
            coverage_path = tmp_path / "coverage"

    changes = parse_unified_diff(
        """diff --git a/pkg/code.py b/pkg/code.py
--- a/pkg/code.py
+++ b/pkg/code.py
@@ -1 +1 @@
-before()
+after()
"""
    )
    harness._generate_tests_for_patch_evaluation = lambda handler, results: (
        tmp_path / "agent-tests"
    )
    harness._inspect_gold_patch = lambda handler, patch, patch_root: (changes, None)

    baseline_report = tmp_path / "entry-baseline.json"
    baseline_report.write_text(
        json.dumps(
            {"files": {"pkg/code.py": {"executed_lines": [], "missing_lines": [1]}}}
        )
    )
    gold_report = tmp_path / "entry-gold.json"
    gold_report.write_text(
        json.dumps(
            {
                "files": {
                    "pkg/code.py": {"executed_lines": [1], "missing_lines": []},
                    "pkg/entry.py": {
                        "executed_lines": [10],
                        "missing_lines": [11, 12],
                    },
                }
            }
        )
    )
    reference_report = tmp_path / "entry-reference.json"
    reference_report.write_text(
        json.dumps(
            {
                "files": {
                    "pkg/code.py": {"executed_lines": [1], "missing_lines": []},
                    "pkg/entry.py": {
                        "executed_lines": [10, 11, 12],
                        "missing_lines": [],
                    },
                }
            }
        )
    )

    def fake_run(
        handler,
        tests,
        loaded_manifest,
        patch_root,
        patch,
        coverage_targets=None,
    ):
        if patch is None:
            assert coverage_targets == ["pkg/code.py"]
            return (
                UnitTestStatus.FAILED,
                FailureMode.NONE,
                baseline_report,
                reference_report,
                None,
            )
        expected = (
            UnitTestStatus.PASSED
            if patch.label is PatchLabel.ORACLE_VALID
            else UnitTestStatus.FAILED
        )
        if patch.is_gold:
            assert coverage_targets == ["pkg/code.py", "pkg/entry.py"]
            return expected, FailureMode.NONE, gold_report, reference_report, None
        assert coverage_targets is None
        return expected, FailureMode.NONE, None, None, None

    harness._run_patch_container = fake_run
    result = harness._run_patch_set_trial(Handler())

    assert result.entry_coverage is not None
    assert result.entry_coverage.status.value == "collected"
    assert result.entry_coverage.ratio == pytest.approx(1 / 3)
    assert result.gold_patch_coverage is not None
    assert result.gold_patch_coverage.ratio == 0.5
    assert result.is_resolved is True
    summary = json.loads((tmp_path / "coverage" / "entry-coverage.json").read_text())
    assert summary["status"] == "collected"
    assert summary["ratio"] == pytest.approx(1 / 3)
    raw = json.loads(
        (tmp_path / "coverage" / "generated" / "gold" / "coverage.json").read_text()
    )
    assert "pkg/entry.py" in raw["files"]


def test_patch_set_trial_selects_entry_runtime_report_from_mixed_gold_reports(
    tmp_path,
):
    manifest, root = load_patch_set_manifest(_patch_root(), TASK_ID)
    harness = object.__new__(RuntimeHarness)
    harness._patch_manifests = {TASK_ID: (manifest, root)}
    harness._patch_concurrency = 3
    harness._logger = __import__("logging").getLogger("testbench-v3-entry-mixed")

    class Handler:
        task_id = TASK_ID
        trial_name = "astropy-entry-mixed-trial"
        instruction = "write tests"
        task = SimpleNamespace(
            test_entry_inferences={
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
        )

        class trial_paths:
            task_output_path = tmp_path
            coverage_path = tmp_path / "coverage"

    changes = parse_unified_diff(
        """diff --git a/native/tool.cpp b/native/tool.cpp
--- a/native/tool.cpp
+++ b/native/tool.cpp
@@ -5 +5 @@
-before_native();
+after_native();
diff --git a/pkg/code.py b/pkg/code.py
--- a/pkg/code.py
+++ b/pkg/code.py
@@ -1 +1 @@
-before_python()
+after_python()
"""
    )
    harness._generate_tests_for_patch_evaluation = lambda handler, results: (
        tmp_path / "agent-tests"
    )
    harness._inspect_gold_patch = lambda handler, patch, patch_root: (changes, None)

    def write_report(path: Path, files: dict[str, object]) -> Path:
        path.write_text(json.dumps({"files": files}))
        return path

    baseline_python_report = write_report(
        tmp_path / "mixed-baseline-python.json",
        {"pkg/code.py": {"executed_lines": [], "missing_lines": [1]}},
    )
    baseline_native_report = write_report(
        tmp_path / "mixed-baseline-native.json",
        {"native/tool.cpp": {"executed_lines": [], "missing_lines": [5]}},
    )
    baseline_reference_python = write_report(
        tmp_path / "mixed-baseline-reference-python.json",
        {"pkg/code.py": {"executed_lines": [1], "missing_lines": []}},
    )
    baseline_reference_native = write_report(
        tmp_path / "mixed-baseline-reference-native.json",
        {"native/tool.cpp": {"executed_lines": [5], "missing_lines": []}},
    )
    gold_python_report = write_report(
        tmp_path / "mixed-gold-python.json",
        {
            "pkg/code.py": {"executed_lines": [1], "missing_lines": []},
            "pkg/entry.py": {
                "executed_lines": [10],
                "missing_lines": [11, 12],
            },
        },
    )
    gold_native_report = write_report(
        tmp_path / "mixed-gold-native.json",
        {"native/tool.cpp": {"executed_lines": [5], "missing_lines": []}},
    )
    gold_reference_python = write_report(
        tmp_path / "mixed-gold-reference-python.json",
        {
            "pkg/code.py": {"executed_lines": [1], "missing_lines": []},
            "pkg/entry.py": {
                "executed_lines": [10, 11, 12],
                "missing_lines": [],
            },
        },
    )
    gold_reference_native = write_report(
        tmp_path / "mixed-gold-reference-native.json",
        {"native/tool.cpp": {"executed_lines": [5], "missing_lines": []}},
    )

    def fake_run(
        handler,
        tests,
        loaded_manifest,
        patch_root,
        patch,
        coverage_targets=None,
    ):
        if patch is None:
            assert coverage_targets == ["native/tool.cpp", "pkg/code.py"]
            return (
                UnitTestStatus.FAILED,
                FailureMode.NONE,
                {
                    "native": baseline_native_report,
                    "python": baseline_python_report,
                },
                {
                    "native": baseline_reference_native,
                    "python": baseline_reference_python,
                },
                None,
            )
        expected = (
            UnitTestStatus.PASSED
            if patch.label is PatchLabel.ORACLE_VALID
            else UnitTestStatus.FAILED
        )
        if patch.is_gold:
            assert coverage_targets == [
                "native/tool.cpp",
                "pkg/code.py",
                "pkg/entry.py",
            ]
            return (
                expected,
                FailureMode.NONE,
                {"native": gold_native_report, "python": gold_python_report},
                {
                    "native": gold_reference_native,
                    "python": gold_reference_python,
                },
                None,
            )
        return expected, FailureMode.NONE, None, None, None

    harness._run_patch_container = fake_run
    result = harness._run_patch_set_trial(Handler())

    assert result.entry_coverage is not None
    assert result.entry_coverage.status.value == "collected"
    assert result.entry_coverage.ratio == pytest.approx(1 / 3)
    assert result.gold_patch_coverage is not None
    assert result.gold_patch_coverage.status.value == "collected"
    assert result.gold_patch_coverage.language == "mixed"
    assert result.gold_patch_coverage.languages == ["native", "python"]
    assert result.gold_patch_coverage.ratio == pytest.approx(0.5)
    raw = json.loads(
        (tmp_path / "coverage" / "generated" / "gold" / "coverage.json").read_text()
    )
    assert "pkg/entry.py" in raw["files"]
    assert "native/tool.cpp" not in raw["files"]


def test_patch_set_entry_coverage_failures_do_not_change_verdicts(tmp_path):
    manifest, root = load_patch_set_manifest(_patch_root(), TASK_ID)
    harness = object.__new__(RuntimeHarness)
    harness._patch_manifests = {TASK_ID: (manifest, root)}
    harness._patch_concurrency = 2
    harness._logger = __import__("logging").getLogger("testbench-v3-entry-fail")

    class Handler:
        task_id = TASK_ID
        trial_name = "astropy-entry-failure-trial"
        instruction = "write tests"
        task = SimpleNamespace(
            test_entry_inferences={
                "schema_version": 1,
                "language": "python",
                "source": "static_test_invocation",
                "entries": [
                    {"path": "pkg/entry.py", "start_line": 10, "end_line": 12}
                ],
            }
        )

        class trial_paths:
            task_output_path = tmp_path
            coverage_path = tmp_path / "coverage"

    changes = parse_unified_diff(
        """diff --git a/pkg/code.py b/pkg/code.py
--- a/pkg/code.py
+++ b/pkg/code.py
@@ -1 +1 @@
-before()
+after()
"""
    )
    harness._generate_tests_for_patch_evaluation = lambda handler, results: (
        tmp_path / "agent-tests"
    )
    harness._inspect_gold_patch = lambda handler, patch, patch_root: (changes, None)
    baseline_report = tmp_path / "failure-baseline.json"
    baseline_report.write_text(
        json.dumps(
            {"files": {"pkg/code.py": {"executed_lines": [1], "missing_lines": []}}}
        )
    )

    def fake_run(
        handler,
        tests,
        loaded_manifest,
        patch_root,
        patch,
        coverage_targets=None,
    ):
        if patch is None:
            return (
                UnitTestStatus.FAILED,
                FailureMode.NONE,
                baseline_report,
                baseline_report,
                None,
            )
        expected = (
            UnitTestStatus.PASSED
            if patch.label is PatchLabel.ORACLE_VALID
            else UnitTestStatus.FAILED
        )
        if patch.is_gold:
            return (
                expected,
                FailureMode.NONE,
                None,
                None,
                "generated coverage collection failed: status=failed",
            )
        return expected, FailureMode.NONE, None, None, None

    harness._run_patch_container = fake_run
    result = harness._run_patch_set_trial(Handler())

    assert result.entry_coverage is not None
    assert result.entry_coverage.status.value == "failed"
    assert result.pre_fix_results == {"agent_tests": UnitTestStatus.FAILED}
    assert result.post_fix_results == {"agent_tests": UnitTestStatus.PASSED}
    assert result.is_resolved is True
    assert result.verdicts


@pytest.mark.parametrize(
    "coverage_error",
    [
        "reference test runner test_timeout",
        "reference coverage setup failed: status=unsupported",
        "reference coverage collection failed: status=failed",
    ],
)
def test_reference_coverage_failures_are_observational(tmp_path, coverage_error):
    manifest, root = load_patch_set_manifest(_patch_root(), TASK_ID)
    harness = object.__new__(RuntimeHarness)
    harness._patch_manifests = {TASK_ID: (manifest, root)}
    harness._patch_concurrency = 2
    harness._logger = __import__("logging").getLogger("testbench-v3-fail-soft")

    class Handler:
        task_id = TASK_ID
        trial_name = "astropy-trial"
        instruction = "write tests"

        class trial_paths:
            task_output_path = tmp_path
            coverage_path = tmp_path / "coverage"

    changes = parse_unified_diff(
        """diff --git a/pkg/code.py b/pkg/code.py
--- a/pkg/code.py
+++ b/pkg/code.py
@@ -1 +1 @@
-before()
+after()
"""
    )
    harness._generate_tests_for_patch_evaluation = lambda handler, results: (
        tmp_path / "agent-tests"
    )
    harness._inspect_gold_patch = lambda handler, patch, patch_root: (changes, None)
    baseline_report = tmp_path / "baseline.json"
    baseline_report.write_text(
        json.dumps(
            {"files": {"pkg/code.py": {"executed_lines": [1], "missing_lines": []}}}
        )
    )
    gold_report = tmp_path / "gold.json"
    gold_report.write_text(baseline_report.read_text())

    def fake_run(
        handler,
        tests,
        loaded_manifest,
        patch_root,
        patch,
        coverage_targets=None,
    ):
        if patch is None:
            return (
                UnitTestStatus.FAILED,
                FailureMode.NONE,
                baseline_report,
                baseline_report,
                None,
            )
        expected = (
            UnitTestStatus.PASSED
            if patch.label is PatchLabel.ORACLE_VALID
            else UnitTestStatus.FAILED
        )
        if patch.is_gold:
            return expected, FailureMode.NONE, gold_report, gold_report, coverage_error
        return expected, FailureMode.NONE, None, None, None

    harness._run_patch_container = fake_run
    result = harness._run_patch_set_trial(Handler())

    coverage = result.gold_patch_coverage
    assert coverage is not None
    assert coverage.status.value == "failed"
    assert coverage.reason == coverage_error
    assert result.pre_fix_results == {"agent_tests": UnitTestStatus.FAILED}
    assert result.post_fix_results == {"agent_tests": UnitTestStatus.PASSED}
    assert result.is_resolved is True
    assert result.n_patch_errors == 0
    assert result.verdicts


def test_patch_container_orders_agent_capture_before_reference_and_skips_non_gold(
    tmp_path,
):
    manifest, root = load_patch_set_manifest(_patch_root(), TASK_ID)
    harness = object.__new__(RuntimeHarness)
    harness._global_test_timeout_sec = None
    harness._global_timeout_multiplier = 1
    harness._logger = __import__("logging").getLogger("testbench-v3-order")
    events: list[str] = []
    quarantine_error: list[str | None] = [None]
    agent_result = [({"agent_tests": UnitTestStatus.PASSED}, FailureMode.NONE)]

    class Session:
        def send_keys(self, keys, **kwargs):
            return None

        def capture_pane(self, capture_entire=False):
            return ""

    class Terminal:
        def create_session(self, name, **kwargs):
            return Session()

        def copy_to_container(self, *args, **kwargs):
            return None

    @contextmanager
    def fake_terminal(**kwargs):
        yield Terminal()

    handler = SimpleNamespace(
        task_id=TASK_ID,
        trial_name="order-trial",
        client_container_name="order-trial",
        client_image_name="image",
        docker_image_name_prefix="prefix",
        task=SimpleNamespace(
            workdir="/app",
            max_test_timeout_sec=30,
            disable_asciinema=False,
        ),
        task_paths=SimpleNamespace(docker_compose_path=tmp_path / "compose.yaml"),
        trial_paths=SimpleNamespace(
            sessions_path=tmp_path / "sessions",
            agent_logging_dir=tmp_path / "agent-logs",
            commands_path=tmp_path / "commands.txt",
            coverage_path=tmp_path / "coverage",
        ),
    )
    tests_dir = tmp_path / "agent-tests"
    tests_dir.mkdir()
    (tests_dir / "run-tests.sh").write_text("#!/bin/sh\nexit 0\n")

    def fake_setup_runtime(*args, namespace, **kwargs):
        events.append("setup_" + namespace)
        return {"COVERAGE_PROCESS_START": "config"}, None

    def fake_collect(*args, namespace, **kwargs):
        events.append("collect_" + namespace)
        report = tmp_path / (namespace + ".json")
        report.write_text('{"files": {}}')
        return report, None

    def fake_agent(*args, **kwargs):
        events.append("agent_status")
        return agent_result[0]

    def fake_reference(*args, **kwargs):
        events.append("reference_runner")
        return FailureMode.NONE

    def fake_quarantine(*args, **kwargs):
        events.append("quarantine_agent_tests")
        return quarantine_error[0]

    harness._setup_coverage_runtime = fake_setup_runtime
    harness._collect_coverage_artifact = fake_collect
    harness._run_agent_tests = fake_agent
    harness._run_reference_tests = fake_reference
    harness._quarantine_agent_tests = fake_quarantine
    harness._apply_patch_entry = lambda *args, **kwargs: FailureMode.NONE

    with patch(
        "testprism.harness.testbench_harness.spin_up_terminal", fake_terminal
    ):
        result = harness._run_patch_container(
            handler,
            tests_dir,
            manifest,
            root,
            None,
            coverage_targets=["pkg/code.py"],
        )
    assert result[0] is UnitTestStatus.PASSED
    assert events.index("agent_status") < events.index("reference_runner")
    assert events.index("collect_generated") < events.index("reference_runner")
    assert events.index("collect_generated") < events.index("quarantine_agent_tests")
    assert events.index("quarantine_agent_tests") < events.index("setup_reference")
    assert events.index("setup_reference") < events.index("reference_runner")

    events.clear()
    non_gold = next(solution for solution in manifest.solutions if not solution.is_gold)
    with patch(
        "testprism.harness.testbench_harness.spin_up_terminal", fake_terminal
    ):
        harness._run_patch_container(
            handler,
            tests_dir,
            manifest,
            root,
            non_gold,
            coverage_targets=None,
        )
    assert events == ["agent_status"]

    events.clear()
    quarantine_error[0] = (
        "agent-test quarantine failed in task container (status=collision)"
    )
    with patch(
        "testprism.harness.testbench_harness.spin_up_terminal", fake_terminal
    ):
        isolated = harness._run_patch_container(
            handler,
            tests_dir,
            manifest,
            root,
            None,
            coverage_targets=["pkg/code.py"],
        )
    assert isolated[0] is UnitTestStatus.PASSED
    assert events == [
        "setup_generated",
        "agent_status",
        "collect_generated",
        "quarantine_agent_tests",
    ]
    assert "status=collision" in isolated[4]

    events.clear()
    quarantine_error[0] = None
    agent_result[0] = (None, FailureMode.PARSE_ERROR)
    with patch(
        "testprism.harness.testbench_harness.spin_up_terminal", fake_terminal
    ):
        incomplete = harness._run_patch_container(
            handler,
            tests_dir,
            manifest,
            root,
            None,
            coverage_targets=["pkg/code.py"],
        )
    assert incomplete[0] is None
    assert "collect_generated" not in events
    assert events == [
        "setup_generated",
        "agent_status",
        "quarantine_agent_tests",
        "setup_reference",
        "reference_runner",
        "collect_reference",
    ]
    assert "did not complete with a parseable status" in incomplete[4]


def test_agent_test_quarantine_is_quoted_non_following_and_collision_safe(tmp_path):
    workdir = tmp_path / "task workdir's"
    generated = workdir / "agent-tests"
    generated.mkdir(parents=True)
    (workdir / "BREADTH_FIRST_SEARCH.java").write_text("class Production {}\n")
    (generated / "BREADTH_FIRST_SEARCH.java").write_text("class Generated {}\n")
    assert sorted(path.relative_to(workdir) for path in workdir.rglob("*.java")) == [
        Path("BREADTH_FIRST_SEARCH.java"),
        Path("agent-tests/BREADTH_FIRST_SEARCH.java"),
    ]
    quarantine_root = tmp_path / "tmp" / "testbench" / "quarantine"
    handler = SimpleNamespace(task=SimpleNamespace(workdir=str(workdir)))

    with patch(
        "testprism.harness.testbench_harness.AGENT_TEST_QUARANTINE_ROOT",
        str(quarantine_root),
    ):
        command = _agent_test_quarantine_command(handler, "baseline")
    first = subprocess.run(
        ["sh", "-c", command],
        check=True,
        capture_output=True,
        text=True,
    )

    assert f"{_AGENT_TEST_QUARANTINE_PREFIX}quarantined" in first.stdout
    assert not generated.exists()
    quarantined = list(quarantine_root.glob("*/agent-tests"))
    assert len(quarantined) == 1
    assert (quarantined[0] / "BREADTH_FIRST_SEARCH.java").is_file()
    assert list(workdir.rglob("*.java")) == [
        workdir / "BREADTH_FIRST_SEARCH.java"
    ]

    generated.mkdir()
    (generated / "new.java").write_text("class New {}\n")
    collision = subprocess.run(
        ["sh", "-c", command],
        check=True,
        capture_output=True,
        text=True,
    )
    assert f"{_AGENT_TEST_QUARANTINE_PREFIX}collision" in collision.stdout
    assert (generated / "new.java").is_file()
    assert not (quarantined[0] / "new.java").exists()

    for child in generated.iterdir():
        child.unlink()
    generated.rmdir()
    outside = tmp_path / "outside-agent-tests"
    outside.mkdir()
    (outside / "sentinel").write_text("preserved\n")
    generated.symlink_to(outside, target_is_directory=True)
    linked = subprocess.run(
        ["sh", "-c", command],
        check=True,
        capture_output=True,
        text=True,
    )
    assert f"{_AGENT_TEST_QUARANTINE_PREFIX}invalid-source" in linked.stdout
    assert generated.is_symlink()
    assert (outside / "sentinel").read_text() == "preserved\n"
    assert AGENT_TEST_QUARANTINE_ROOT == "/tmp/testbench/quarantine"
