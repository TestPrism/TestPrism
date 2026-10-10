"""Pure calculation helpers for TestBench Entry Coverage.

Entry Coverage consumes the versioned ``test_entry_inferences`` metadata written
at dataset-preparation time and one generated gold/head normalized coverage
report. It intentionally has no dependency on Docker, sessions, or harness
orchestration so that the line-set arithmetic is independently auditable.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Any, Iterable, Mapping

ReportInput = Mapping[str, Any] | str | Path
MetadataInput = Any

ENTRY_METADATA_LANGUAGE_TO_RUNTIME_LANGUAGE = MappingProxyType(
    {
        "python": "python",
        "javascript": "javascript",
        "typescript": "javascript",
        "java": "java",
        "cpp": "native",
        "go": "go",
        "rust": None,
    }
)


@dataclass
class EntryCoverageMetric:
    """Serializable result of calculating Entry Coverage for one trial.

    The count names mirror the existing changed-line metric while making the
    Entry Coverage namespace explicit.  ``entry_lines`` is the union of
    coverage.py executable statement coordinates inside all declared spans;
    ``covered_entry_lines`` is its intersection with executed coordinates.
    """

    status: str
    language: str | None = None
    entry_count: int = 0
    executable_entry_line_count: int = 0
    covered_entry_line_count: int = 0
    uncovered_entry_line_count: int = 0
    line_classifications: dict[str, list[str]] = field(default_factory=dict)
    ratio: float | None = None
    reason: str | None = None
    artifact_path: str | None = None

    @property
    def total_entry_line_count(self) -> int:
        """Alias for the executable-entry denominator."""

        return self.executable_entry_line_count

    @property
    def covered_line_count(self) -> int:
        """Short alias used by report consumers."""

        return self.covered_entry_line_count

    @property
    def entry_lines(self) -> int:
        """Alias for :attr:`executable_entry_line_count`."""

        return self.executable_entry_line_count

    @property
    def covered_entry_lines(self) -> int:
        """Alias for :attr:`covered_entry_line_count`."""

        return self.covered_entry_line_count

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible dictionary for artifact persistence."""

        return asdict(self)


@dataclass(frozen=True)
class _EntrySpan:
    path: str
    start_line: int
    end_line: int


def _normalise_report_path(value: str) -> str:
    """Normalize coverage.py's absolute/relative path spellings."""

    normalised = value.replace("\\", "/")
    while normalised.startswith("./"):
        normalised = normalised[2:]
    return normalised


def _load_report(report: ReportInput | None) -> Mapping[str, Any]:
    if report is None:
        raise ValueError("missing generated gold coverage report")
    if isinstance(report, Mapping):
        data: Any = report
    else:
        path = Path(report)
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid coverage report {path}: {exc}") from exc
    if not isinstance(data, Mapping):
        raise ValueError("invalid coverage report: root must be an object")
    return data


def _line_set(value: Any, *, path: str, field_name: str) -> set[int]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple, set)):
        raise ValueError(
            f"coverage entry for {path!r} has invalid {field_name} line list"
        )
    result: set[int] = set()
    for line in value:
        if isinstance(line, bool) or not isinstance(line, int) or line < 1:
            raise ValueError(
                f"coverage entry for {path!r} has invalid {field_name} line list"
            )
        result.add(line)
    return result


def _report_files(report: Mapping[str, Any]) -> dict[str, tuple[set[int], set[int]]]:
    files = report.get("files")
    if not isinstance(files, Mapping):
        raise ValueError("coverage report is missing a files object")

    result: dict[str, tuple[set[int], set[int]]] = {}
    for raw_path, raw_data in files.items():
        if not isinstance(raw_path, str) or not raw_path or "\x00" in raw_path:
            raise ValueError("coverage report contains an invalid file path")
        if not isinstance(raw_data, Mapping):
            raise ValueError(f"coverage entry for {raw_path!r} must be an object")
        try:
            executed = _line_set(
                raw_data["executed_lines"],
                path=raw_path,
                field_name="executed_lines",
            )
            missing = _line_set(
                raw_data["missing_lines"],
                path=raw_path,
                field_name="missing_lines",
            )
        except KeyError as exc:
            raise ValueError(
                f"coverage entry for {raw_path!r} lacks valid line lists"
            ) from exc
        # Keep the raw key so two spellings that normalize to the same path are
        # still visible to the ambiguity check.  A report containing both
        # ``pkg/mod.py`` and ``./pkg/mod.py`` must not silently merge them.
        if not _normalise_report_path(raw_path):
            raise ValueError("coverage report contains an invalid file path")
        result[raw_path] = (set(executed), set(executed | missing))
    return result


def _raw_report_matches(
    report_files: Mapping[str, tuple[set[int], set[int]]], target: str
) -> list[tuple[str, tuple[set[int], set[int]]]]:
    normalized_target = _normalise_report_path(target)
    matches = [
        (candidate, values)
        for candidate, values in report_files.items()
        if _normalise_report_path(candidate) == normalized_target
        or _normalise_report_path(candidate).endswith(f"/{normalized_target}")
    ]
    return matches


def _object_to_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="python")
        if isinstance(dumped, Mapping):
            return dumped
    return None


def _metadata_mapping(metadata: MetadataInput) -> Mapping[str, Any] | None:
    # Accept a Task-like object as a convenience for harness callers while
    # keeping the calculator independent from the Task model itself.
    if hasattr(metadata, "test_entry_inferences"):
        nested = getattr(metadata, "test_entry_inferences")
        metadata = nested
    elif isinstance(metadata, Mapping) and "test_entry_inferences" in metadata:
        metadata = metadata["test_entry_inferences"]
    if metadata is None:
        return None
    return _object_to_mapping(metadata)


def _invalid_metric(reason: str, *, language: str | None = None) -> EntryCoverageMetric:
    return EntryCoverageMetric(status="failed", language=language, reason=reason)


def _safe_entry_path(path: Any) -> str:
    if not isinstance(path, str) or not path.strip() or "\\" in path:
        raise ValueError("path must be a non-empty POSIX repository-relative path")
    if "\x00" in path:
        raise ValueError("path contains a NUL byte")
    while path.startswith("./"):
        path = path[2:]
    posix = PurePosixPath(path)
    windows = PureWindowsPath(path)
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        raise ValueError("path must be repository-relative")
    parts = posix.parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("path must not contain empty, '.' or '..' segments")
    return path


def _entry_spans(metadata: Mapping[str, Any]) -> tuple[str, list[_EntrySpan]]:
    language = metadata.get("language")
    if not isinstance(language, str) or not language:
        raise ValueError("language must be a non-empty string")
    if language not in ENTRY_METADATA_LANGUAGE_TO_RUNTIME_LANGUAGE:
        raise ValueError(f"unsupported language {language!r}")
    if metadata.get("schema_version") != 1:
        raise ValueError("schema_version must be 1")
    if metadata.get("source") != "static_test_invocation":
        raise ValueError("source must be 'static_test_invocation'")
    entries = metadata.get("entries")
    if not isinstance(entries, list):
        raise ValueError("entries must be a list")

    spans: list[_EntrySpan] = []
    seen: set[tuple[str, int, int]] = set()
    for index, raw_entry in enumerate(entries):
        entry = _object_to_mapping(raw_entry)
        if entry is None:
            raise ValueError(f"entry {index} must be an object")
        try:
            path = _safe_entry_path(entry["path"])
            start_line = entry["start_line"]
            end_line = entry["end_line"]
        except KeyError as exc:
            raise ValueError(f"entry {index} is missing {exc.args[0]!r}") from exc
        if (
            isinstance(start_line, bool)
            or isinstance(end_line, bool)
            or not isinstance(start_line, int)
            or not isinstance(end_line, int)
            or start_line < 1
            or end_line < start_line
        ):
            raise ValueError(f"entry {index} has invalid source line range")
        key = (path, start_line, end_line)
        if key in seen:
            continue
        seen.add(key)
        spans.append(_EntrySpan(path, start_line, end_line))
    return language, spans


def calculate_entry_coverage(
    metadata: MetadataInput = None,
    report: ReportInput | None = None,
    *,
    entry_metadata: MetadataInput = None,
    test_entry_inferences: MetadataInput = None,
    entry_inferences: MetadataInput = None,
    generated_gold_report: ReportInput | None = None,
    generated_report: ReportInput | None = None,
    generated_gold_reports: Iterable[ReportInput] | None = None,
    coverage_report: ReportInput | None = None,
    coverage_reports: Iterable[ReportInput] | None = None,
    artifact_path: str | None = None,
    repository_root: str | Path | None = None,
    repo_root: str | Path | None = None,
    source_root: str | Path | None = None,
) -> EntryCoverageMetric:
    """Calculate Entry Coverage from metadata and one generated report.

    ``metadata`` and ``report`` are the positional public interface.  The
    keyword aliases keep callers that use the task/report terminology
    readable while preserving one implementation.  No report is needed for a
    metadata object with zero entries: that is a valid empty executable scope,
    not a collection failure.
    """

    aliases = [
        value
        for value in (entry_metadata, test_entry_inferences, entry_inferences)
        if value is not None
    ]
    if aliases:
        if metadata is not None and metadata is not aliases[0]:
            return _invalid_metric("multiple entry metadata values supplied")
        metadata = aliases[0]
    report_aliases = [
        value
        for value in (generated_gold_report, generated_report, coverage_report)
        if value is not None
    ]
    report_collections = [
        value
        for value in (generated_gold_reports, coverage_reports)
        if value is not None
    ]
    if report_collections:
        if report is not None or report_aliases:
            return _invalid_metric("multiple generated coverage reports supplied")
        collection = list(report_collections[0])
        if len(collection) != 1:
            return _invalid_metric(
                "Entry Coverage requires exactly one generated gold coverage report"
            )
        report_aliases.append(collection[0])
    if report_aliases:
        if report is not None and report is not report_aliases[0]:
            return _invalid_metric("multiple generated coverage reports supplied")
        report = report_aliases[0]

    if metadata is None or (
        hasattr(metadata, "test_entry_inferences")
        and getattr(metadata, "test_entry_inferences") is None
    ) or (
        isinstance(metadata, Mapping)
        and "test_entry_inferences" in metadata
        and metadata["test_entry_inferences"] is None
    ):
        return EntryCoverageMetric(
            status="unavailable",
            ratio=None,
            reason="task has no test entry metadata",
            artifact_path=artifact_path,
        )

    metadata_map = _metadata_mapping(metadata)
    if metadata_map is None:
        return _invalid_metric("invalid entry metadata: expected an object")
    if "language" not in metadata_map:
        return _invalid_metric("invalid entry metadata: missing language")
    raw_language = metadata_map.get("language")
    if raw_language is None:
        return _invalid_metric("invalid entry metadata: language must be declared")

    try:
        language, spans = _entry_spans(metadata_map)
    except (TypeError, ValueError) as exc:
        return _invalid_metric(
            f"invalid entry metadata: {exc}",
            language=str(raw_language) if raw_language is not None else None,
        )

    runtime_language = ENTRY_METADATA_LANGUAGE_TO_RUNTIME_LANGUAGE[language]
    if runtime_language is None:
        return EntryCoverageMetric(
            status="unsupported",
            language=language,
            ratio=None,
            reason=f"unsupported entry metadata language: {language!r}",
            artifact_path=artifact_path,
        )

    if repository_root is None:
        repository_root = repo_root if repo_root is not None else source_root
    if repository_root is not None:
        root = Path(repository_root).resolve()
        for span in spans:
            candidate = (root / span.path).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                return _invalid_metric(
                    f"invalid entry metadata path outside repository: {span.path!r}",
                    language=language,
                )
            if not candidate.is_file():
                return _invalid_metric(
                    f"entry source path does not exist: {span.path!r}",
                    language=language,
                )

    # A valid empty metadata list has no denominator and should not require a
    # generated report.  This distinction is useful for auditable historical
    # or intentionally entry-less tasks.
    if not spans:
        return EntryCoverageMetric(
            status="no_executable_lines",
            language=language,
            entry_count=0,
            ratio=None,
            reason="entry metadata contains no declared entries",
            artifact_path=artifact_path,
        )

    try:
        report_map = _report_files(_load_report(report))
    except (OSError, TypeError, ValueError) as exc:
        return _invalid_metric(str(exc), language=language)

    executable: set[tuple[str, int]] = set()
    executed: set[tuple[str, int]] = set()
    matched_paths: dict[str, str] = {}
    for span in spans:
        if span.path not in matched_paths:
            matches = _raw_report_matches(report_map, span.path)
            if not matches:
                return _invalid_metric(
                    f"generated gold coverage report missing entry file {span.path!r}",
                    language=language,
                )
            if len(matches) > 1:
                candidates = ", ".join(sorted(candidate for candidate, _ in matches))
                return _invalid_metric(
                    f"ambiguous coverage report path for entry {span.path!r}: "
                    f"{candidates}",
                    language=language,
                )
            matched_paths[span.path] = matches[0][0]
        report_executed, report_executable = report_map[matched_paths[span.path]]
        for line in report_executable:
            if span.start_line <= line <= span.end_line:
                executable.add((span.path, line))
        for line in report_executed:
            if span.start_line <= line <= span.end_line:
                executed.add((span.path, line))

    covered = executable & executed
    def identifiers(values: Iterable[tuple[str, int]]) -> list[str]:
        return [
            f"{path}:{line}"
            for path, line in sorted(values, key=lambda item: (item[0], item[1]))
        ]
    executable_ids = identifiers(executable)
    covered_ids = identifiers(covered)
    uncovered_ids = identifiers(executable - covered)
    classifications = {
        "executable": executable_ids,
        "covered": covered_ids,
        "uncovered": uncovered_ids,
    }
    if not executable:
        return EntryCoverageMetric(
            status="no_executable_lines",
            language=language,
            entry_count=len(spans),
            line_classifications=classifications,
            ratio=None,
            reason="entry spans contain no executable coverage statements",
            artifact_path=artifact_path,
        )

    executable_count = len(executable)
    covered_count = len(covered)
    return EntryCoverageMetric(
        status="collected",
        language=language,
        entry_count=len(spans),
        executable_entry_line_count=executable_count,
        covered_entry_line_count=covered_count,
        uncovered_entry_line_count=executable_count - covered_count,
        line_classifications=classifications,
        ratio=covered_count / executable_count,
        artifact_path=artifact_path,
    )


# Descriptive aliases used by downstream code and older experiments.
calculate_entry_coverage_metric = calculate_entry_coverage
EntryCoverageResult = EntryCoverageMetric
