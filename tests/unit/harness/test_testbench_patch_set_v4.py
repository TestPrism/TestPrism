"""Harness evaluation tests for dynamic and partial schema-v4 patch sets."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

from testprism.agents.failure_mode import FailureMode
from testprism.cli.tb.testbench import _print_testbench_results
from testprism.harness.harness import Harness
from testprism.harness.models import TestBenchResults as HarnessResults
from testprism.harness.testbench_harness import TestBenchHarness as RuntimeHarness
from testprism.parsers.base_parser import UnitTestStatus
from testprism.patches.models import PatchLabel
from tests.unit.patches.test_models_v4 import (
    _manifest as build_v4_manifest,
)
from tests.unit.patches.test_models_v4 import (
    _write_v4_manifest,
)


def _run_v4_trial(
    tmp_path: Path,
    *,
    valid_count: int,
    invalid_count: int,
    target_valid: int | None = None,
    target_invalid: int | None = None,
):
    manifest = build_v4_manifest(
        tmp_path,
        valid_count=valid_count,
        invalid_count=invalid_count,
        target_valid=target_valid,
        target_invalid=target_invalid,
    )
    harness = object.__new__(RuntimeHarness)
    harness._patch_manifests = {manifest.task_id: (manifest, tmp_path)}
    harness._patch_concurrency = 3
    harness._logger = logging.getLogger("v4-patch-set-test")
    tests_dir = tmp_path / "agent-tests"
    tests_dir.mkdir()
    trial_handler = SimpleNamespace(
        task_id=manifest.task_id,
        trial_name="v4-trial",
        instruction="write tests",
        trial_paths=SimpleNamespace(
            task_output_path=tmp_path,
            coverage_path=tmp_path / "coverage",
        ),
    )
    harness._generate_tests_for_patch_evaluation = lambda handler, results: tests_dir
    # Coverage remains an additive observation.  This unit test focuses on
    # patch-set dispatch and metrics, so disable its applicability gate while
    # leaving the normal coverage branch untouched in production.
    harness._gate_gold_patch_coverage_from_applicability = lambda handler: (False, None)
    calls: list[str] = []

    def fake_run(
        handler,
        tests,
        loaded_manifest,
        root,
        patch,
        coverage_targets=None,
    ):
        state = "baseline" if patch is None else patch.id
        calls.append(state)
        if patch is None:
            return UnitTestStatus.FAILED, FailureMode.NONE, None
        status = (
            UnitTestStatus.PASSED
            if patch.label is PatchLabel.ORACLE_VALID
            else UnitTestStatus.FAILED
        )
        return status, FailureMode.NONE, None

    harness._run_patch_container = fake_run
    result = harness._run_patch_set_trial(trial_handler)
    return result, manifest, calls


def test_v4_partial_1_plus_0_dispatches_actual_members_and_keeps_null_rates(
    tmp_path: Path,
):
    result, manifest, calls = _run_v4_trial(
        tmp_path,
        valid_count=1,
        invalid_count=0,
        target_valid=5,
        target_invalid=5,
    )

    assert set(calls) == {"baseline", *(solution.id for solution in manifest.solutions)}
    assert len(calls) == 2
    assert result.patch_set_schema_version == 4
    assert result.patch_set_status == "partial"
    assert result.patch_set_complete is False
    assert result.patch_set_target_valid == 5
    assert result.patch_set_target_invalid == 5
    assert result.patch_set_actual_valid == 1
    assert result.patch_set_actual_invalid == 0
    assert result.valid_acceptance_rate == 1.0
    assert result.invalid_rejection_rate is None
    assert result.mutation_score is None
    assert result.balanced_patch_accuracy is None
    assert result.is_resolved is False


def test_v4_partial_with_two_labels_resolves_by_actual_members_not_target(
    tmp_path: Path,
):
    result, manifest, calls = _run_v4_trial(
        tmp_path,
        valid_count=2,
        invalid_count=1,
        target_valid=5,
        target_invalid=5,
    )

    assert len(calls) == 1 + len(manifest.solutions)
    assert result.patch_set_status == "partial"
    assert result.patch_set_complete is False
    assert result.patch_set_actual_valid == 2
    assert result.patch_set_actual_invalid == 1
    assert result.valid_acceptance_rate == 1.0
    assert result.invalid_rejection_rate == 1.0
    assert result.mutation_score == 1.0
    assert result.balanced_patch_accuracy == 1.0
    assert result.is_resolved is True


def test_v4_manifest_digest_is_path_aware_deterministic_and_byte_sensitive(
    tmp_path: Path,
):
    source = tmp_path / "source"
    _write_v4_manifest(source, "fixture/digest")
    manifest_bytes = (source / "manifest.json").read_bytes()

    left_root = tmp_path / "left-root"
    right_root = tmp_path / "right-root"
    (left_root / "left").mkdir(parents=True)
    (right_root / "right").mkdir(parents=True)
    (left_root / "left" / "manifest.json").write_bytes(manifest_bytes)
    (right_root / "right" / "manifest.json").write_bytes(manifest_bytes)

    left_digest = Harness._compute_patch_set_digest(left_root)
    right_digest = Harness._compute_patch_set_digest(right_root)
    assert left_digest != right_digest

    multi_root = tmp_path / "multi"
    (multi_root / "left").mkdir(parents=True)
    (multi_root / "right").mkdir()
    left_manifest = multi_root / "left" / "manifest.json"
    left_manifest.write_bytes(manifest_bytes)
    (multi_root / "right" / "manifest.json").write_bytes(manifest_bytes)

    digest = Harness._compute_patch_set_digest(multi_root)
    assert digest == Harness._compute_patch_set_digest(multi_root)

    left_manifest.write_bytes(manifest_bytes + b"\n")
    assert Harness._compute_patch_set_digest(multi_root) != digest


def test_v4_macro_rates_skip_null_classes_and_report_participants():
    partial_one = SimpleNamespace(
        valid_acceptance_rate=1.0,
        invalid_rejection_rate=None,
        balanced_patch_accuracy=None,
        patch_set_schema_version=4,
        patch_set_status="partial",
    )
    partial_two = SimpleNamespace(
        valid_acceptance_rate=0.5,
        invalid_rejection_rate=1.0,
        balanced_patch_accuracy=0.75,
        patch_set_schema_version=4,
        patch_set_status="partial",
    )
    v3_ready = SimpleNamespace(
        valid_acceptance_rate=0.0,
        invalid_rejection_rate=0.5,
        balanced_patch_accuracy=0.25,
        patch_set_schema_version=3,
        patch_set_status="ready",
    )

    # Use the concrete trial model so aggregate computed fields are evaluated
    # exactly as they are when results.json is serialized.
    from testprism.harness.models import TestBenchTrialResults as TrialResultsV4

    def trial(source):
        return TrialResultsV4(
            trial_name="trial",
            task_id="fixture/demo",
            instruction="test",
            patch_set_id="set",
            **source.__dict__,
        )

    results = HarnessResults(
        results=[trial(partial_one), trial(partial_two), trial(v3_ready)]
    )
    assert results.avg_valid_acceptance_rate == (1.0 + 0.5 + 0.0) / 3
    assert results.avg_invalid_rejection_rate == (1.0 + 0.5) / 2
    assert results.avg_balanced_patch_accuracy == (0.75 + 0.25) / 2
    assert results.n_valid_rate_tasks == 3
    assert results.n_invalid_rate_tasks == 2
    assert results.n_balanced_rate_tasks == 2
    assert results.patch_set_counts_by_schema == {"v4": 2, "v3": 1}
    assert results.patch_set_counts_by_status == {"partial": 2, "ready": 1}
    assert results.patch_set_counts_by_schema_status == {
        "v4/partial": 2,
        "v3/ready": 1,
    }


def test_cli_report_labels_versions_statuses_and_null_participants(
    tmp_path: Path, capsys
):
    output = tmp_path / "results.json"
    output.write_text(
        '{"results": [{"patch_set_id": "set-v4", '
        '"patch_set_schema_version": 4, "patch_set_status": "partial", '
        '"patch_set_complete": false, "valid_acceptance_rate": 1.0, '
        '"invalid_rejection_rate": null, "balanced_patch_accuracy": null, '
        '"is_resolved": false, "verdicts": {}}]}',
        encoding="utf-8",
    )

    _print_testbench_results(output)
    rendered = capsys.readouterr().out
    assert "Patch Sets v4 Partial" in rendered
    assert "Patch Sets v3 Ready" in rendered
    assert "N/A (n=0)" in rendered
