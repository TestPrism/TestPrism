"""Synthetic preprocess-to-results evidence for TestBench Entry Coverage."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from testprism.agents.failure_mode import FailureMode
from testprism.handlers.trial_handler import Task
from testprism.harness.models import CoverageStatus, GoldPatchCoverage
from testprism.harness.models import TestBenchResults as TBResults
from testprism.harness.models import TestBenchTrialResults as TBTrialResults
from testprism.harness.models import TestCaseVerdict as TBVerdict
from testprism.harness.testbench_harness import TestBenchHarness as RuntimeHarness
from testprism.parsers.base_parser import UnitTestStatus
from scripts.generation.infer_test_entries import main as infer_entries_cli


def _write_synthetic_repository(root: Path) -> tuple[Path, Path]:
    (root / "pkg").mkdir()
    (root / "tests").mkdir()
    (root / "pkg" / "calculator.py").write_text(
        "\n".join(
            [
                "def add_one(value):",
                "    adjusted = value + 1",
                "    if adjusted > 1:",
                "        return adjusted",
                "    return 0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    trusted_test = root / "tests" / "test_calculator.py"
    trusted_test.write_text(
        "from pkg.calculator import add_one\n\n"
        "def test_add_one():\n"
        "    assert add_one(1) == 2\n",
        encoding="utf-8",
    )
    return trusted_test, root / "task.yaml"


def test_synthetic_entry_workflow_preprocesses_and_preserves_existing_results(
    tmp_path: Path,
) -> None:
    """Run preprocessing, reload the task, and consume one generated report."""

    trusted_test, task_yaml = _write_synthetic_repository(tmp_path)
    task_yaml.write_text(
        "instruction: write a regression test\n"
        "difficulty: easy\n"
        "canary: preserve-me\n",
        encoding="utf-8",
    )

    # The fixture deliberately starts as a historical task with no metadata.
    assert Task.from_yaml(task_yaml).test_entry_inferences is None

    cli_args = [
        "--repo",
        str(tmp_path),
        "--tests",
        str(trusted_test),
        str(task_yaml),
    ]
    assert infer_entries_cli(cli_args) == 0
    generated_yaml = task_yaml.read_bytes()
    assert b"test_entry_inferences:" in generated_yaml
    assert b"canary: preserve-me" in generated_yaml

    # A second generation is byte-stable, and --check validates without writing.
    assert infer_entries_cli(cli_args) == 0
    assert task_yaml.read_bytes() == generated_yaml
    assert infer_entries_cli([*cli_args[:-1], str(task_yaml), "--check"]) == 0
    task = Task.from_yaml(task_yaml)
    assert task.test_entry_inferences is not None
    assert [entry.symbol for entry in task.test_entry_inferences.entries] == [
        "pkg/calculator.py:add_one"
    ]

    generated_report = tmp_path / "generated-gold.json"
    generated_report.write_text(
        json.dumps(
            {
                "files": {
                    "pkg/calculator.py": {
                        "executed_lines": [1, 2, 3],
                        "missing_lines": [4, 5],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    trial_root = tmp_path / "trial"
    handler = SimpleNamespace(
        task_id="synthetic-entry-task",
        task=task,
        trial_paths=SimpleNamespace(
            task_output_path=trial_root,
            coverage_path=trial_root / "coverage",
        ),
    )
    harness = object.__new__(RuntimeHarness)
    harness._logger = logging.getLogger("synthetic-entry-workflow")

    # Existing changed-line coverage and verdict fields are populated before
    # the observational Entry Coverage summary is attached.
    trial = TBTrialResults(
        trial_name="synthetic-entry-trial",
        task_id=handler.task_id,
        instruction=task.instruction,
        is_resolved=True,
        failure_mode=FailureMode.NONE,
        pre_fix_results={"agent_tests": UnitTestStatus.FAILED},
        post_fix_results={"agent_tests": UnitTestStatus.PASSED},
        verdicts={"agent_tests": TBVerdict.EFFECTIVE},
        n_effective=1,
        gold_patch_coverage=GoldPatchCoverage(
            status=CoverageStatus.COLLECTED,
            executable_changed_line_count=2,
            covered_changed_line_count=1,
            ratio=0.5,
        ),
    )

    harness._calculate_and_persist_entry_coverage(
        handler,
        trial,
        generated_report,
    )

    assert trial.entry_coverage is not None
    assert trial.entry_coverage.status.value == "collected"
    assert trial.entry_coverage.ratio == pytest.approx(3 / 5)
    assert trial.entry_coverage.entry_count == 1
    assert trial.entry_coverage.executable_entry_line_count == 5
    assert trial.entry_coverage.covered_entry_line_count == 3
    assert trial.entry_coverage.uncovered_entry_line_count == 2
    assert trial.entry_coverage.line_classifications["covered"] == [
        "pkg/calculator.py:1",
        "pkg/calculator.py:2",
        "pkg/calculator.py:3",
    ]

    # The harness-facing path writes both the reusable raw report and summary.
    raw_artifact = trial_root / "coverage" / "generated" / "gold" / "coverage.json"
    summary_artifact = trial_root / "coverage" / "entry-coverage.json"
    assert raw_artifact.is_file()
    assert summary_artifact.is_file()
    assert json.loads(raw_artifact.read_text(encoding="utf-8")) == json.loads(
        generated_report.read_text(encoding="utf-8")
    )
    summary = json.loads(summary_artifact.read_text(encoding="utf-8"))
    assert summary["ratio"] == pytest.approx(3 / 5)

    # Entry Coverage is observational: existing coverage and verdicts remain
    # unchanged, and run-level metrics expose independent ratios/counts.
    assert trial.coverage == pytest.approx(0.5)
    assert trial.is_resolved is True
    assert trial.verdicts == {"agent_tests": TBVerdict.EFFECTIVE}
    aggregate = TBResults(results=[trial])
    assert aggregate.coverage == pytest.approx(0.5)
    assert aggregate.entry_coverage == pytest.approx(3 / 5)
    assert aggregate.n_entry_coverage_tasks == 1
    assert aggregate.n_entry_lines == 5
    assert aggregate.n_entry_lines_covered == 3
