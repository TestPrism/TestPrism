import json
import logging
from pathlib import Path
from types import SimpleNamespace

from testprism.agents.failure_mode import FailureMode
from testprism.harness.patch_coverage import parse_unified_diff
from testprism.harness.testbench_harness import TestBenchHarness as RuntimeHarness
from testprism.parsers.base_parser import UnitTestStatus
from testprism.patches.models import PatchLabel
from tests.unit.patches.test_models import _manifest as build_manifest


def test_patch_set_trial_requires_complete_classification(tmp_path: Path):
    manifest = build_manifest(tmp_path)
    patch_root = tmp_path
    harness = object.__new__(RuntimeHarness)
    harness._patch_manifests = {manifest.task_id: (manifest, patch_root)}
    harness._patch_concurrency = 3
    harness._logger = logging.getLogger("test")
    calls: list[str] = []
    trial_handler = SimpleNamespace(
        task_id=manifest.task_id,
        trial_name="demo-trial",
        instruction="write tests",
        trial_paths=SimpleNamespace(
            task_output_path=tmp_path,
            coverage_path=tmp_path / "coverage",
        ),
    )
    changes = parse_unified_diff(
        """diff --git a/pkg/code.py b/pkg/code.py
--- a/pkg/code.py
+++ b/pkg/code.py
@@ -1 +1 @@
-before()
+after()
"""
    )

    baseline_generated = tmp_path / "baseline-generated.json"
    baseline_generated.write_text(
        json.dumps(
            {"files": {"pkg/code.py": {"executed_lines": [], "missing_lines": [1]}}}
        )
    )
    baseline_reference = tmp_path / "baseline-reference.json"
    baseline_reference.write_text(
        json.dumps(
            {
                "files": {
                    "pkg/code.py": {"executed_lines": [1], "missing_lines": []}
                }
            }
        )
    )
    gold_generated = tmp_path / "gold-generated.json"
    gold_generated.write_text(
        json.dumps(
            {
                "files": {
                    "pkg/code.py": {"executed_lines": [1], "missing_lines": []}
                }
            }
        )
    )
    gold_reference = tmp_path / "gold-reference.json"
    gold_reference.write_text(gold_generated.read_text())

    harness._generate_tests_for_patch_evaluation = (
        lambda handler, results: tmp_path / "agent-tests"
    )
    harness._inspect_gold_patch = (
        lambda handler, patch, root: (changes, None)
    )

    def fake_run(
        handler,
        tests,
        loaded_manifest,
        root,
        patch,
        coverage_targets=None,
    ):
        calls.append("baseline" if patch is None else patch.id)
        if patch is None:
            assert coverage_targets == ["pkg/code.py"]
            return (
                UnitTestStatus.FAILED,
                FailureMode.NONE,
                baseline_generated,
                baseline_reference,
                None,
            )
        expected = (
            UnitTestStatus.PASSED
            if patch.label is PatchLabel.ORACLE_VALID
            else UnitTestStatus.FAILED
        )
        if patch.is_gold:
            assert coverage_targets == ["pkg/code.py"]
            return (
                expected,
                FailureMode.NONE,
                gold_generated,
                gold_reference,
                None,
            )
        assert coverage_targets is None
        return expected, FailureMode.NONE, None, None, None

    harness._run_patch_container = fake_run
    result = harness._run_patch_set_trial(trial_handler)

    assert len(calls) == 11
    assert set(calls) == {
        "baseline",
        *(solution.id for solution in manifest.solutions),
    }
    assert result.patch_set_id == manifest.patch_set_id
    assert result.baseline_detected is True
    assert result.is_resolved is True
    assert result.valid_acceptance_rate == 1.0
    assert result.invalid_rejection_rate == 1.0
    assert result.balanced_patch_accuracy == 1.0
    assert result.n_patch_errors == 0
    assert result.gold_patch_coverage is not None
    assert result.gold_patch_coverage.status.value == "collected"
    assert result.gold_patch_coverage.executable_changed_line_count == 2
    assert result.gold_patch_coverage.covered_changed_line_count == 1
    assert result.coverage == 0.5
    summary = json.loads(
        (tmp_path / "coverage" / "gold-patch.json").read_text()
    )
    assert summary["ratio"] == 0.5
