from __future__ import annotations

import hashlib
import json
from pathlib import Path

from testprism.harness.gold_coverage_applicability import (
    ApplicabilityDiagnosticCode,
    GoldCoverageApplicabilityManifest,
    map_manifest_languages_to_runtime_languages,
    runtime_language_for_manifest_family,
)


def _task(
    tasks_root: Path,
    task_key: str,
    solution: str = "#!/bin/sh\nexit 0\n",
) -> Path:
    task_dir = tasks_root / task_key
    task_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text("instruction: test\n", encoding="utf-8")
    (task_dir / "solution.sh").write_text(solution, encoding="utf-8")
    return task_dir


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _record(
    task_dir: Path,
    *,
    decision: str = "applicable",
    reason_code: str | None = None,
    languages: list[str] | None = None,
    evidence_paths: list[str] | None = None,
    solution_sha256: str | None = None,
) -> dict[str, object]:
    if languages is None:
        languages = ["python"]
    if decision == "not_applicable":
        languages = []
    if reason_code is None:
        reason_code = (
            "tests_only"
            if decision == "not_applicable"
            else "production_source_only"
        )
    if evidence_paths is None:
        evidence_paths = ["pkg/main.py"]
    if solution_sha256 is None:
        solution_sha256 = _sha256(task_dir / "solution.sh")
    return {
        "task_key": task_dir.relative_to(task_dir.parents[1]).as_posix(),
        "decision": decision,
        "reason_code": reason_code,
        "languages": languages,
        "evidence_paths": evidence_paths,
        "solution_sha256": solution_sha256,
    }


def _manifest(path: Path, *records: object) -> None:
    lines = [
        record if isinstance(record, str) else json.dumps(record, sort_keys=True)
        for record in records
    ]
    payload = "\n".join(lines)
    if payload:
        payload += "\n"
    path.write_text(payload, encoding="utf-8")


def test_resolve_uses_relative_task_path_not_basename(tmp_path: Path) -> None:
    tasks_root = tmp_path / "tasks"
    task_a = _task(tasks_root, "from-alpha/shared-name", "#!/bin/sh\necho alpha\n")
    task_b = _task(tasks_root, "from-beta/shared-name", "#!/bin/sh\necho beta\n")
    manifest_path = tasks_root / "GOLD_CHANGE_COVERAGE_APPLICABILITY.jsonl"
    _manifest(
        manifest_path,
        _record(task_a, reason_code="production_source_only"),
        _record(
            task_b,
            reason_code="production_source_with_incidental_artifacts",
            evidence_paths=["web/app.tsx"],
        ),
    )

    loader = GoldCoverageApplicabilityManifest(
        tasks_root=tasks_root,
        manifest_path=manifest_path,
    )

    resolution_a = loader.resolve(task_a)
    resolution_b = loader.resolve(task_b)

    assert resolution_a.resolved is True
    assert resolution_a.task_key == "from-alpha/shared-name"
    assert resolution_a.reason_code == "production_source_only"
    assert resolution_b.resolved is True
    assert resolution_b.task_key == "from-beta/shared-name"
    assert resolution_b.reason_code == "production_source_with_incidental_artifacts"
    assert resolution_b.evidence_paths == ("web/app.tsx",)


def test_tasks_outside_governed_root_resolve_to_legacy_state(tmp_path: Path) -> None:
    tasks_root = tmp_path / "governed"
    manifest_path = tasks_root / "GOLD_CHANGE_COVERAGE_APPLICABILITY.jsonl"
    outside_task = _task(tmp_path / "other-root", "from-other/task-one")
    loader = GoldCoverageApplicabilityManifest(
        tasks_root=tasks_root,
        manifest_path=manifest_path,
    )

    resolution = loader.resolve(outside_task)

    assert resolution.legacy is True
    assert resolution.governed is False
    assert resolution.record is None
    assert resolution.diagnostic is None


def test_governed_task_reports_missing_record(tmp_path: Path) -> None:
    tasks_root = tmp_path / "tasks"
    missing_task = _task(tasks_root, "from-example/task-one")
    other_task = _task(tasks_root, "from-example/task-two")
    manifest_path = tasks_root / "GOLD_CHANGE_COVERAGE_APPLICABILITY.jsonl"
    _manifest(manifest_path, _record(other_task))

    resolution = GoldCoverageApplicabilityManifest(
        tasks_root=tasks_root,
        manifest_path=manifest_path,
    ).resolve(missing_task)

    assert resolution.governed is True
    assert resolution.record is None
    assert resolution.diagnostic is not None
    assert resolution.diagnostic.code is ApplicabilityDiagnosticCode.MISSING_RECORD
    assert "from-example/task-one" in resolution.diagnostic.message


def test_governed_task_reports_duplicate_records(tmp_path: Path) -> None:
    tasks_root = tmp_path / "tasks"
    task_dir = _task(tasks_root, "from-example/task-one")
    manifest_path = tasks_root / "GOLD_CHANGE_COVERAGE_APPLICABILITY.jsonl"
    _manifest(
        manifest_path,
        _record(task_dir, reason_code="production_source_only"),
        _record(
            task_dir,
            reason_code="production_source_with_incidental_artifacts",
            evidence_paths=["pkg/alt.py"],
        ),
    )

    resolution = GoldCoverageApplicabilityManifest(
        tasks_root=tasks_root,
        manifest_path=manifest_path,
    ).resolve(task_dir)

    assert resolution.record is None
    assert resolution.diagnostic is not None
    assert resolution.diagnostic.code is ApplicabilityDiagnosticCode.DUPLICATE_RECORD
    assert resolution.diagnostic.line_numbers == (1, 2)


def test_governed_task_reports_malformed_record(tmp_path: Path) -> None:
    tasks_root = tmp_path / "tasks"
    task_dir = _task(tasks_root, "from-example/task-one")
    manifest_path = tasks_root / "GOLD_CHANGE_COVERAGE_APPLICABILITY.jsonl"
    broken = _record(task_dir)
    broken["languages"] = "python"
    _manifest(manifest_path, broken)

    resolution = GoldCoverageApplicabilityManifest(
        tasks_root=tasks_root,
        manifest_path=manifest_path,
    ).resolve(task_dir)

    assert resolution.record is None
    assert resolution.diagnostic is not None
    assert resolution.diagnostic.code is ApplicabilityDiagnosticCode.MALFORMED_RECORD
    assert resolution.diagnostic.line_numbers == (1,)


def test_governed_task_reports_invalid_reason_code_as_malformed(tmp_path: Path) -> None:
    tasks_root = tmp_path / "tasks"
    task_dir = _task(tasks_root, "from-example/task-one")
    manifest_path = tasks_root / "GOLD_CHANGE_COVERAGE_APPLICABILITY.jsonl"
    _manifest(
        manifest_path,
        _record(
            task_dir,
            decision="not_applicable",
            reason_code="test_only_change",
        ),
    )

    resolution = GoldCoverageApplicabilityManifest(
        tasks_root=tasks_root,
        manifest_path=manifest_path,
    ).resolve(task_dir)

    assert resolution.record is None
    assert resolution.diagnostic is not None
    assert resolution.diagnostic.code is ApplicabilityDiagnosticCode.MALFORMED_RECORD
    assert resolution.diagnostic.line_numbers == (1,)


def test_governed_task_reports_stale_solution_sha256(tmp_path: Path) -> None:
    tasks_root = tmp_path / "tasks"
    task_dir = _task(tasks_root, "from-example/task-one", "#!/bin/sh\necho stale\n")
    manifest_path = tasks_root / "GOLD_CHANGE_COVERAGE_APPLICABILITY.jsonl"
    _manifest(
        manifest_path,
        _record(task_dir, solution_sha256="0" * 64),
    )

    resolution = GoldCoverageApplicabilityManifest(
        tasks_root=tasks_root,
        manifest_path=manifest_path,
    ).resolve(task_dir)

    assert resolution.record is not None
    assert resolution.current_solution_sha256 == _sha256(task_dir / "solution.sh")
    assert resolution.diagnostic is not None
    assert resolution.diagnostic.code is ApplicabilityDiagnosticCode.STALE_RECORD
    assert resolution.diagnostic.expected_solution_sha256 == "0" * 64


def test_language_mapping_is_deterministic_and_evidence_remains_audit_only(
    tmp_path: Path,
) -> None:
    tasks_root = tmp_path / "tasks"
    task_dir = _task(tasks_root, "from-example/task-one")
    manifest_path = tasks_root / "GOLD_CHANGE_COVERAGE_APPLICABILITY.jsonl"
    _manifest(
        manifest_path,
        _record(
            task_dir,
            languages=["python", "javascript_typescript", "c_cpp", "go", "rust"],
            evidence_paths=["src/app.py", "ui/app.tsx", "native/app.cc"],
        ),
    )

    resolution = GoldCoverageApplicabilityManifest(
        tasks_root=tasks_root,
        manifest_path=manifest_path,
    ).resolve(task_dir / "solution.sh")

    assert resolution.resolved is True
    assert resolution.runtime_languages == (
        "python",
        "javascript",
        "native",
        "go",
    )
    assert resolution.record is not None
    assert resolution.record.unmapped_languages == ("rust",)
    assert resolution.evidence_paths == (
        "src/app.py",
        "ui/app.tsx",
        "native/app.cc",
    )
    assert runtime_language_for_manifest_family("javascript_typescript") == "javascript"
    assert runtime_language_for_manifest_family("c_cpp") == "native"
    assert map_manifest_languages_to_runtime_languages(
        ["python", "javascript_typescript", "python", "rust"]
    ) == ("python", "javascript")


def test_governed_task_reports_missing_manifest(tmp_path: Path) -> None:
    tasks_root = tmp_path / "tasks"
    task_dir = _task(tasks_root, "from-example/task-one")
    manifest_path = tasks_root / "GOLD_CHANGE_COVERAGE_APPLICABILITY.jsonl"

    resolution = GoldCoverageApplicabilityManifest(
        tasks_root=tasks_root,
        manifest_path=manifest_path,
    ).resolve(task_dir)

    assert resolution.governed is True
    assert resolution.record is None
    assert resolution.diagnostic is not None
    assert resolution.diagnostic.code is ApplicabilityDiagnosticCode.MANIFEST_MISSING
