"""Governed runtime loader for hardest-500 Entry Coverage applicability data."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_GOVERNED_TASKS_ROOT = (
    REPOSITORY_ROOT / "verified_tasks" / "test-hardest-500"
)
DEFAULT_APPLICABILITY_MANIFEST_PATH = (
    DEFAULT_GOVERNED_TASKS_ROOT / "ENTRY_COVERAGE_APPLICABILITY.jsonl"
)

SCHEMA_VERSION = 1
FINAL_APPLICABILITY_DECISIONS = frozenset({"applicable", "not_applicable"})
MANIFEST_REASON_CODES_BY_DECISION = MappingProxyType(
    {
        "applicable": (
            "direct_local_entry_surface",
            "local_program_entry_surface",
            "data_driven_local_entry_surface",
        ),
        "not_applicable": (
            "external_boundary",
            "artifact_only_verifier",
            "non_unique_local_entry",
            "non_source_core_behavior",
            "unsupported_language_surface",
        ),
        "needs_review": (
            "post_fix_tree_required",
            "authority_ambiguous",
        ),
    }
)
MANIFEST_LANGUAGE_FAMILY_TO_RUNTIME_LANGUAGE = MappingProxyType(
    {
        "python": "python",
        "javascript_typescript": "javascript",
        "java": "java",
        "c_cpp": "native",
        "go": "go",
        "rust": None,
    }
)

_TASK_INPUT_SENTINELS = frozenset(
    {
        "task.yaml",
        "solution.sh",
        "run-tests.sh",
        "docker-compose.yaml",
        "tests",
    }
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)


class ApplicabilityDiagnosticCode(str, Enum):
    """Stable failure codes exposed to harness planning."""

    MANIFEST_MISSING = "manifest_missing"
    MISSING_RECORD = "missing_record"
    DUPLICATE_RECORD = "duplicate_record"
    MALFORMED_RECORD = "malformed_record"
    STALE_RECORD = "stale_record"
    TASK_INPUT_ERROR = "task_input_error"


@dataclass(frozen=True)
class ApplicabilityDiagnostic:
    """Explicit diagnostic emitted when governed resolution fails closed."""

    code: ApplicabilityDiagnosticCode
    message: str
    task_key: str | None = None
    manifest_path: Path | None = None
    line_numbers: tuple[int, ...] = ()
    current_solution_sha256: str | None = None
    expected_solution_sha256: str | None = None

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class EntryCoverageApplicabilityRecord:
    """One validated manifest record for a governed hardest-500 task."""

    schema_version: int
    task_key: str
    decision: str
    reason_code: str
    languages: tuple[str, ...]
    evidence_paths: tuple[str, ...]
    solution_sha256: str
    manifest_path: Path
    line_number: int
    raw_record: Mapping[str, Any] = field(repr=False, compare=False)

    @property
    def runtime_languages(self) -> tuple[str, ...]:
        return map_manifest_languages_to_runtime_languages(self.languages)

    @property
    def unmapped_languages(self) -> tuple[str, ...]:
        return unmapped_manifest_languages(self.languages)


@dataclass(frozen=True)
class EntryCoverageApplicabilityResolution:
    """Path-based resolution result for the governed applicability manifest."""

    task_input_path: Path
    task_key: str | None
    governed: bool
    record: EntryCoverageApplicabilityRecord | None = None
    diagnostic: ApplicabilityDiagnostic | None = None
    current_solution_sha256: str | None = None

    @property
    def legacy(self) -> bool:
        return not self.governed

    @property
    def resolved(self) -> bool:
        return self.governed and self.record is not None and self.diagnostic is None

    @property
    def decision(self) -> str | None:
        return self.record.decision if self.record is not None else None

    @property
    def reason_code(self) -> str | None:
        return self.record.reason_code if self.record is not None else None

    @property
    def runtime_languages(self) -> tuple[str, ...]:
        return self.record.runtime_languages if self.record is not None else ()

    @property
    def evidence_paths(self) -> tuple[str, ...]:
        return self.record.evidence_paths if self.record is not None else ()


@dataclass(frozen=True)
class _GovernedTaskLocation:
    task_dir: Path
    task_key: str


class ManifestRecordError(ValueError):
    """Internal validation error for one manifest line."""


def runtime_language_for_manifest_family(language_family: str) -> str | None:
    """Map one manifest family to the harness runtime family."""

    if language_family not in MANIFEST_LANGUAGE_FAMILY_TO_RUNTIME_LANGUAGE:
        raise ValueError(
            f"unknown manifest language family: {language_family!r}"
        )
    return MANIFEST_LANGUAGE_FAMILY_TO_RUNTIME_LANGUAGE[language_family]


def map_manifest_languages_to_runtime_languages(
    language_families: Iterable[str],
) -> tuple[str, ...]:
    """Return unique runtime families in first-seen manifest order."""

    mapped: list[str] = []
    seen: set[str] = set()
    for language_family in language_families:
        runtime_language = runtime_language_for_manifest_family(language_family)
        if runtime_language is None or runtime_language in seen:
            continue
        seen.add(runtime_language)
        mapped.append(runtime_language)
    return tuple(mapped)


def unmapped_manifest_languages(
    language_families: Iterable[str],
) -> tuple[str, ...]:
    """Return manifest families without a runtime backend in this milestone."""

    unmapped: list[str] = []
    seen: set[str] = set()
    for language_family in language_families:
        runtime_language = runtime_language_for_manifest_family(language_family)
        if runtime_language is not None or language_family in seen:
            continue
        seen.add(language_family)
        unmapped.append(language_family)
    return tuple(unmapped)


class EntryCoverageApplicabilityManifest:
    """Load and resolve the governed hardest-500 applicability manifest."""

    def __init__(
        self,
        *,
        tasks_root: Path = DEFAULT_GOVERNED_TASKS_ROOT,
        manifest_path: Path = DEFAULT_APPLICABILITY_MANIFEST_PATH,
    ):
        self.tasks_root = Path(tasks_root).resolve()
        self.manifest_path = Path(manifest_path).resolve()
        self._loaded = False
        self._manifest_error: ApplicabilityDiagnostic | None = None
        self._manifest_diagnostics: tuple[ApplicabilityDiagnostic, ...] = ()
        self._records_by_task_key: dict[str, EntryCoverageApplicabilityRecord] = {}
        self._malformed_by_task_key: dict[str, ApplicabilityDiagnostic] = {}
        self._duplicate_by_task_key: dict[str, ApplicabilityDiagnostic] = {}

    @property
    def manifest_diagnostics(self) -> tuple[ApplicabilityDiagnostic, ...]:
        self._ensure_loaded()
        return self._manifest_diagnostics

    @property
    def records(self) -> tuple[EntryCoverageApplicabilityRecord, ...]:
        self._ensure_loaded()
        return tuple(
            self._records_by_task_key[key]
            for key in sorted(self._records_by_task_key)
        )

    def task_key_for_input_path(self, task_input_path: str | Path) -> str | None:
        location = self._governed_task_location(task_input_path)
        return location.task_key if location is not None else None

    def resolve(
        self, task_input_path: str | Path
    ) -> EntryCoverageApplicabilityResolution:
        location = self._governed_task_location(task_input_path)
        normalized_input_path = Path(task_input_path).resolve()
        if location is None:
            return EntryCoverageApplicabilityResolution(
                task_input_path=normalized_input_path,
                task_key=None,
                governed=False,
            )

        self._ensure_loaded()
        if self._manifest_error is not None:
            return EntryCoverageApplicabilityResolution(
                task_input_path=location.task_dir,
                task_key=location.task_key,
                governed=True,
                diagnostic=self._manifest_error,
            )

        diagnostic = self._malformed_by_task_key.get(location.task_key)
        if diagnostic is not None:
            return EntryCoverageApplicabilityResolution(
                task_input_path=location.task_dir,
                task_key=location.task_key,
                governed=True,
                diagnostic=diagnostic,
            )

        diagnostic = self._duplicate_by_task_key.get(location.task_key)
        if diagnostic is not None:
            return EntryCoverageApplicabilityResolution(
                task_input_path=location.task_dir,
                task_key=location.task_key,
                governed=True,
                diagnostic=diagnostic,
            )

        record = self._records_by_task_key.get(location.task_key)
        if record is None:
            return EntryCoverageApplicabilityResolution(
                task_input_path=location.task_dir,
                task_key=location.task_key,
                governed=True,
                diagnostic=ApplicabilityDiagnostic(
                    code=ApplicabilityDiagnosticCode.MISSING_RECORD,
                    message=(
                        "entry coverage applicability manifest has no record for "
                        f"governed task {location.task_key!r}"
                    ),
                    task_key=location.task_key,
                    manifest_path=self.manifest_path,
                ),
            )

        solution_path = location.task_dir / "solution.sh"
        if not solution_path.is_file():
            return EntryCoverageApplicabilityResolution(
                task_input_path=location.task_dir,
                task_key=location.task_key,
                governed=True,
                diagnostic=ApplicabilityDiagnostic(
                    code=ApplicabilityDiagnosticCode.TASK_INPUT_ERROR,
                    message=(
                        f"governed task {location.task_key!r} is missing "
                        f"solution.sh at {solution_path}"
                    ),
                    task_key=location.task_key,
                ),
            )

        current_solution_sha256 = _sha256(solution_path)
        if current_solution_sha256 != record.solution_sha256:
            return EntryCoverageApplicabilityResolution(
                task_input_path=location.task_dir,
                task_key=location.task_key,
                governed=True,
                record=record,
                current_solution_sha256=current_solution_sha256,
                diagnostic=ApplicabilityDiagnostic(
                    code=ApplicabilityDiagnosticCode.STALE_RECORD,
                    message=(
                        "entry coverage applicability manifest record for "
                        f"{location.task_key!r} is stale: current solution.sh "
                        f"SHA-256 {current_solution_sha256} does not match "
                        f"manifest SHA-256 {record.solution_sha256}"
                    ),
                    task_key=location.task_key,
                    manifest_path=self.manifest_path,
                    line_numbers=(record.line_number,),
                    current_solution_sha256=current_solution_sha256,
                    expected_solution_sha256=record.solution_sha256,
                ),
            )

        return EntryCoverageApplicabilityResolution(
            task_input_path=location.task_dir,
            task_key=location.task_key,
            governed=True,
            record=record,
            current_solution_sha256=current_solution_sha256,
        )

    def _governed_task_location(
        self, task_input_path: str | Path
    ) -> _GovernedTaskLocation | None:
        candidate = Path(task_input_path).resolve()
        if candidate.name in _TASK_INPUT_SENTINELS:
            candidate = candidate.parent
        try:
            relative_path = candidate.relative_to(self.tasks_root)
        except ValueError:
            return None
        if len(relative_path.parts) < 2:
            return None
        task_dir = self.tasks_root.joinpath(*relative_path.parts[:2])
        task_key = PurePosixPath(*relative_path.parts[:2]).as_posix()
        return _GovernedTaskLocation(task_dir=task_dir, task_key=task_key)

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True

        if not self.manifest_path.is_file():
            self._manifest_error = ApplicabilityDiagnostic(
                code=ApplicabilityDiagnosticCode.MANIFEST_MISSING,
                message=(
                    "entry coverage applicability manifest is missing at "
                    f"{self.manifest_path}"
                ),
                manifest_path=self.manifest_path,
            )
            return

        records_by_task_key: dict[str, list[EntryCoverageApplicabilityRecord]] = (
            defaultdict(list)
        )
        malformed_by_task_key: dict[str, list[ApplicabilityDiagnostic]] = (
            defaultdict(list)
        )
        manifest_diagnostics: list[ApplicabilityDiagnostic] = []

        try:
            manifest_lines = self.manifest_path.read_text(
                encoding="utf-8"
            ).splitlines()
        except OSError as exc:
            self._manifest_error = ApplicabilityDiagnostic(
                code=ApplicabilityDiagnosticCode.MANIFEST_MISSING,
                message=(
                    "entry coverage applicability manifest could not be read at "
                    f"{self.manifest_path}: {exc}"
                ),
                manifest_path=self.manifest_path,
            )
            return

        for line_number, line in enumerate(manifest_lines, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                manifest_diagnostics.append(
                    ApplicabilityDiagnostic(
                        code=ApplicabilityDiagnosticCode.MALFORMED_RECORD,
                        message=(
                            "entry coverage applicability manifest line "
                            f"{line_number} is not valid JSON: {exc.msg}"
                        ),
                        manifest_path=self.manifest_path,
                        line_numbers=(line_number,),
                    )
                )
                continue

            task_key: str | None = None
            if isinstance(payload, Mapping):
                try:
                    task_key = _normalize_task_key(payload.get("task_key"))
                except ManifestRecordError:
                    task_key = None

            try:
                record = _parse_record(
                    payload,
                    manifest_path=self.manifest_path,
                    line_number=line_number,
                )
            except ManifestRecordError as exc:
                diagnostic = ApplicabilityDiagnostic(
                    code=ApplicabilityDiagnosticCode.MALFORMED_RECORD,
                    message=(
                        "entry coverage applicability manifest record is "
                        f"malformed at {self.manifest_path}:{line_number}: {exc}"
                    ),
                    task_key=task_key,
                    manifest_path=self.manifest_path,
                    line_numbers=(line_number,),
                )
                if task_key is None:
                    manifest_diagnostics.append(diagnostic)
                else:
                    malformed_by_task_key[task_key].append(diagnostic)
                continue

            records_by_task_key[record.task_key].append(record)

        self._manifest_diagnostics = tuple(manifest_diagnostics)

        for task_key, diagnostics in malformed_by_task_key.items():
            line_numbers = tuple(
                line_number
                for diagnostic in diagnostics
                for line_number in diagnostic.line_numbers
            )
            self._malformed_by_task_key[task_key] = ApplicabilityDiagnostic(
                code=ApplicabilityDiagnosticCode.MALFORMED_RECORD,
                message=(
                    "entry coverage applicability manifest has malformed "
                    f"record(s) for governed task {task_key!r} at line(s) "
                    f"{line_numbers}"
                ),
                task_key=task_key,
                manifest_path=self.manifest_path,
                line_numbers=line_numbers,
            )

        for task_key, records in records_by_task_key.items():
            if len(records) == 1:
                self._records_by_task_key[task_key] = records[0]
                continue
            line_numbers = tuple(record.line_number for record in records)
            self._duplicate_by_task_key[task_key] = ApplicabilityDiagnostic(
                code=ApplicabilityDiagnosticCode.DUPLICATE_RECORD,
                message=(
                    "entry coverage applicability manifest has duplicate "
                    f"records for governed task {task_key!r} at line(s) "
                    f"{line_numbers}"
                ),
                task_key=task_key,
                manifest_path=self.manifest_path,
                line_numbers=line_numbers,
            )


@lru_cache(maxsize=1)
def get_default_entry_coverage_applicability_manifest(
    *,
    tasks_root: Path = DEFAULT_GOVERNED_TASKS_ROOT,
    manifest_path: Path = DEFAULT_APPLICABILITY_MANIFEST_PATH,
) -> EntryCoverageApplicabilityManifest:
    """Return the process-wide governed applicability loader."""

    return EntryCoverageApplicabilityManifest(
        tasks_root=tasks_root,
        manifest_path=manifest_path,
    )


def resolve_entry_coverage_applicability(
    task_input_path: str | Path,
    *,
    tasks_root: Path = DEFAULT_GOVERNED_TASKS_ROOT,
    manifest_path: Path = DEFAULT_APPLICABILITY_MANIFEST_PATH,
) -> EntryCoverageApplicabilityResolution:
    """Resolve the governed applicability manifest for one task input path."""

    return get_default_entry_coverage_applicability_manifest(
        tasks_root=tasks_root,
        manifest_path=manifest_path,
    ).resolve(task_input_path)


def _parse_record(
    payload: Any, *, manifest_path: Path, line_number: int
) -> EntryCoverageApplicabilityRecord:
    if not isinstance(payload, Mapping):
        raise ManifestRecordError("record must be a JSON object")

    schema_version = _validate_schema_version(payload.get("schema_version"))
    task_key = _normalize_task_key(payload.get("task_key"))
    decision = _validate_decision(payload.get("decision"))
    reason_code = _validate_reason_code(payload.get("reason_code"), decision=decision)
    languages = _validate_languages(payload.get("languages"), decision=decision)
    evidence_paths = _validate_evidence_paths(payload.get("evidence_paths"))
    solution_sha256 = _validate_solution_sha256(payload.get("solution_sha256"))
    return EntryCoverageApplicabilityRecord(
        schema_version=schema_version,
        task_key=task_key,
        decision=decision,
        reason_code=reason_code,
        languages=languages,
        evidence_paths=evidence_paths,
        solution_sha256=solution_sha256,
        manifest_path=manifest_path,
        line_number=line_number,
        raw_record=dict(payload),
    )


def _validate_schema_version(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ManifestRecordError(f"schema_version must be {SCHEMA_VERSION}")
    if value != SCHEMA_VERSION:
        raise ManifestRecordError(f"schema_version must be {SCHEMA_VERSION}")
    return value


def _normalize_task_key(value: Any) -> str:
    if not isinstance(value, str) or "\\" in value:
        raise ManifestRecordError(
            "task_key must be a non-empty repository-relative POSIX path"
        )
    rendered = value.strip()
    candidate = PurePosixPath(rendered)
    if (
        not rendered
        or candidate.is_absolute()
        or len(candidate.parts) < 2
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise ManifestRecordError(
            "task_key must be a non-empty repository-relative POSIX path"
        )
    return candidate.as_posix()


def _validate_decision(value: Any) -> str:
    decision = _validate_non_empty_string(value, "decision")
    if decision not in FINAL_APPLICABILITY_DECISIONS:
        raise ManifestRecordError(
            "decision must be one of "
            f"{sorted(FINAL_APPLICABILITY_DECISIONS)!r}"
        )
    return decision


def _validate_reason_code(value: Any, *, decision: str) -> str:
    reason_code = _validate_non_empty_string(value, "reason_code")
    allowed_reason_codes = MANIFEST_REASON_CODES_BY_DECISION[decision]
    if reason_code not in allowed_reason_codes:
        raise ManifestRecordError(
            f"reason_code {reason_code!r} is invalid for {decision}; "
            f"expected one of {list(allowed_reason_codes)!r}"
        )
    return reason_code


def _validate_languages(value: Any, *, decision: str) -> tuple[str, ...]:
    languages = _validate_sorted_unique_string_list(value, "languages")
    for language in languages:
        if language not in MANIFEST_LANGUAGE_FAMILY_TO_RUNTIME_LANGUAGE:
            raise ManifestRecordError(
                f"languages contains unknown family {language!r}"
            )
    if decision == "applicable" and not languages:
        raise ManifestRecordError(
            "applicable records must declare at least one language family"
        )
    if decision == "not_applicable" and languages:
        raise ManifestRecordError(
            "not_applicable records must not declare language families"
        )
    return languages


def _validate_evidence_paths(value: Any) -> tuple[str, ...]:
    evidence_paths = _validate_sorted_unique_string_list(value, "evidence_paths")
    if not evidence_paths:
        raise ManifestRecordError("evidence_paths must be a non-empty string list")
    return evidence_paths


def _validate_sorted_unique_string_list(
    value: Any, field_name: str
) -> tuple[str, ...]:
    normalized = _validate_string_list(value, field_name)
    if normalized != tuple(sorted(set(normalized))):
        raise ManifestRecordError(f"{field_name} must be sorted and unique")
    return normalized


def _validate_string_list(value: Any, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ManifestRecordError(f"{field_name} must be a list of strings")
    normalized: list[str] = []
    for item in value:
        normalized.append(_validate_non_empty_string(item, field_name))
    return tuple(normalized)


def _validate_non_empty_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ManifestRecordError(f"{field_name} must be a non-empty string")
    rendered = value.strip()
    if not rendered:
        raise ManifestRecordError(f"{field_name} must be a non-empty string")
    return rendered


def _validate_solution_sha256(value: Any) -> str:
    solution_sha256 = _validate_non_empty_string(value, "solution_sha256").lower()
    if _SHA256_RE.fullmatch(solution_sha256) is None:
        raise ManifestRecordError(
            "solution_sha256 must be a 64-character hexadecimal SHA-256 digest"
        )
    return solution_sha256


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


ApplicabilityManifest = EntryCoverageApplicabilityManifest
ApplicabilityRecord = EntryCoverageApplicabilityRecord
ApplicabilityResolution = EntryCoverageApplicabilityResolution

__all__ = [
    "ApplicabilityDiagnostic",
    "ApplicabilityDiagnosticCode",
    "ApplicabilityManifest",
    "ApplicabilityRecord",
    "ApplicabilityResolution",
    "DEFAULT_APPLICABILITY_MANIFEST_PATH",
    "DEFAULT_GOVERNED_TASKS_ROOT",
    "EntryCoverageApplicabilityManifest",
    "EntryCoverageApplicabilityRecord",
    "EntryCoverageApplicabilityResolution",
    "FINAL_APPLICABILITY_DECISIONS",
    "MANIFEST_LANGUAGE_FAMILY_TO_RUNTIME_LANGUAGE",
    "MANIFEST_REASON_CODES_BY_DECISION",
    "SCHEMA_VERSION",
    "get_default_entry_coverage_applicability_manifest",
    "map_manifest_languages_to_runtime_languages",
    "resolve_entry_coverage_applicability",
    "runtime_language_for_manifest_family",
    "unmapped_manifest_languages",
]
