from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from testprism.harness.entry_coverage_applicability import (
    ApplicabilityDiagnosticCode,
    EntryCoverageApplicabilityManifest,
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
    schema_version: int = 1,
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
            "artifact_only_verifier"
            if decision == "not_applicable"
            else "direct_local_entry_surface"
        )
    if evidence_paths is None:
        evidence_paths = ["pkg/main.py", "tests/test_main.py"]
    if solution_sha256 is None:
        solution_sha256 = _sha256(task_dir / "solution.sh")
    return {
        "schema_version": schema_version,
        "task_key": task_dir.relative_to(task_dir.parents[1]).as_posix(),
        "solution_sha256": solution_sha256,
        "decision": decision,
        "reason_code": reason_code,
        "languages": languages,
        "evidence_paths": evidence_paths,
        "inspected_files": [
            "from-example/task-one/task.yaml",
            "from-example/task-one/solution.sh",
        ],
        "rationale": "Trusted tests expose a stable local entry surface.",
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
    manifest_path = tasks_root / "ENTRY_COVERAGE_APPLICABILITY.jsonl"
    _manifest(
        manifest_path,
        _record(task_a, reason_code="direct_local_entry_surface"),
        _record(
            task_b,
            reason_code="local_program_entry_surface",
            languages=["javascript_typescript"],
            evidence_paths=["web/app.tsx", "web/router.ts"],
        ),
    )

    loader = EntryCoverageApplicabilityManifest(
        tasks_root=tasks_root,
        manifest_path=manifest_path,
    )

    resolution_a = loader.resolve(task_a)
    resolution_b = loader.resolve(task_b)

    assert resolution_a.resolved is True
    assert resolution_a.task_key == "from-alpha/shared-name"
    assert resolution_a.reason_code == "direct_local_entry_surface"
    assert resolution_b.resolved is True
    assert resolution_b.task_key == "from-beta/shared-name"
    assert resolution_b.reason_code == "local_program_entry_surface"
    assert resolution_b.evidence_paths == ("web/app.tsx", "web/router.ts")


def test_tasks_outside_governed_root_resolve_to_legacy_state(tmp_path: Path) -> None:
    tasks_root = tmp_path / "governed"
    manifest_path = tasks_root / "ENTRY_COVERAGE_APPLICABILITY.jsonl"
    outside_task = _task(tmp_path / "other-root", "from-other/task-one")
    loader = EntryCoverageApplicabilityManifest(
        tasks_root=tasks_root,
        manifest_path=manifest_path,
    )

    resolution = loader.resolve(outside_task)

    assert resolution.legacy is True
    assert resolution.governed is False
    assert resolution.record is None
    assert resolution.diagnostic is None


def test_governed_task_reports_missing_manifest(tmp_path: Path) -> None:
    tasks_root = tmp_path / "tasks"
    task_dir = _task(tasks_root, "from-example/task-one")
    manifest_path = tasks_root / "ENTRY_COVERAGE_APPLICABILITY.jsonl"

    resolution = EntryCoverageApplicabilityManifest(
        tasks_root=tasks_root,
        manifest_path=manifest_path,
    ).resolve(task_dir)

    assert resolution.governed is True
    assert resolution.record is None
    assert resolution.diagnostic is not None
    assert resolution.diagnostic.code is ApplicabilityDiagnosticCode.MANIFEST_MISSING
    assert str(manifest_path) in resolution.diagnostic.message


def test_governed_task_reports_missing_record(tmp_path: Path) -> None:
    tasks_root = tmp_path / "tasks"
    missing_task = _task(tasks_root, "from-example/task-one")
    other_task = _task(tasks_root, "from-example/task-two")
    manifest_path = tasks_root / "ENTRY_COVERAGE_APPLICABILITY.jsonl"
    _manifest(manifest_path, _record(other_task))

    resolution = EntryCoverageApplicabilityManifest(
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
    manifest_path = tasks_root / "ENTRY_COVERAGE_APPLICABILITY.jsonl"
    _manifest(
        manifest_path,
        _record(task_dir, reason_code="direct_local_entry_surface"),
        _record(
            task_dir,
            reason_code="local_program_entry_surface",
            evidence_paths=["pkg/api.py", "tests/test_api.py"],
        ),
    )

    resolution = EntryCoverageApplicabilityManifest(
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
    manifest_path = tasks_root / "ENTRY_COVERAGE_APPLICABILITY.jsonl"
    broken = _record(task_dir, schema_version=2)
    _manifest(manifest_path, broken)

    resolution = EntryCoverageApplicabilityManifest(
        tasks_root=tasks_root,
        manifest_path=manifest_path,
    ).resolve(task_dir)

    assert resolution.record is None
    assert resolution.diagnostic is not None
    assert resolution.diagnostic.code is ApplicabilityDiagnosticCode.MALFORMED_RECORD
    assert resolution.diagnostic.line_numbers == (1,)


@pytest.mark.parametrize(
    "overrides",
    [
        {"languages": ["python", "python"]},
        {"languages": ["python", "c_cpp"]},
        {"evidence_paths": ["pkg/a.py", "pkg/a.py"]},
        {"evidence_paths": ["tests/test_main.py", "pkg/main.py"]},
    ],
)
def test_governed_task_reports_non_canonical_arrays_as_malformed(
    tmp_path: Path,
    overrides: dict[str, list[str]],
) -> None:
    tasks_root = tmp_path / "tasks"
    task_dir = _task(tasks_root, "from-example/task-one")
    manifest_path = tasks_root / "ENTRY_COVERAGE_APPLICABILITY.jsonl"
    broken = _record(task_dir)
    broken.update(overrides)
    _manifest(manifest_path, broken)

    resolution = EntryCoverageApplicabilityManifest(
        tasks_root=tasks_root,
        manifest_path=manifest_path,
    ).resolve(task_dir)

    assert resolution.record is None
    assert resolution.diagnostic is not None
    assert resolution.diagnostic.code is ApplicabilityDiagnosticCode.MALFORMED_RECORD
    assert resolution.diagnostic.line_numbers == (1,)
    assert "malformed record(s)" in resolution.diagnostic.message


def test_governed_task_reports_stale_solution_sha256(tmp_path: Path) -> None:
    tasks_root = tmp_path / "tasks"
    task_dir = _task(tasks_root, "from-example/task-one", "#!/bin/sh\necho stale\n")
    manifest_path = tasks_root / "ENTRY_COVERAGE_APPLICABILITY.jsonl"
    _manifest(
        manifest_path,
        _record(task_dir, solution_sha256="0" * 64),
    )

    resolution = EntryCoverageApplicabilityManifest(
        tasks_root=tasks_root,
        manifest_path=manifest_path,
    ).resolve(task_dir)

    assert resolution.record is not None
    assert resolution.current_solution_sha256 == _sha256(task_dir / "solution.sh")
    assert resolution.diagnostic is not None
    assert resolution.diagnostic.code is ApplicabilityDiagnosticCode.STALE_RECORD
    assert resolution.diagnostic.expected_solution_sha256 == "0" * 64


def test_language_mapping_is_deterministic_and_rust_has_no_runtime_backend(
    tmp_path: Path,
) -> None:
    tasks_root = tmp_path / "tasks"
    task_dir = _task(tasks_root, "from-example/task-one")
    manifest_path = tasks_root / "ENTRY_COVERAGE_APPLICABILITY.jsonl"
    _manifest(
        manifest_path,
        _record(
            task_dir,
            languages=["c_cpp", "go", "javascript_typescript", "python", "rust"],
        ),
    )

    resolution = EntryCoverageApplicabilityManifest(
        tasks_root=tasks_root,
        manifest_path=manifest_path,
    ).resolve(task_dir / "solution.sh")

    assert resolution.resolved is True
    assert resolution.runtime_languages == (
        "native",
        "go",
        "javascript",
        "python",
    )
    assert resolution.record is not None
    assert resolution.record.unmapped_languages == ("rust",)
    assert map_manifest_languages_to_runtime_languages(
        ["javascript_typescript", "c_cpp", "rust"]
    ) == ("javascript", "native")
    assert runtime_language_for_manifest_family("javascript_typescript") == "javascript"
    assert runtime_language_for_manifest_family("c_cpp") == "native"
    assert runtime_language_for_manifest_family("rust") is None
