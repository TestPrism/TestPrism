"""Gold-patch changed-line coverage helpers.

The runtime collector deliberately emits a small coverage.py-compatible JSON
shape.  This module stays independent from Docker and Pydantic so parsing and
metric semantics can be tested without an evaluation environment.
"""

from __future__ import annotations

import json
import shlex
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from testprism.harness.coverage_backends import (
    CoverageBackendRegistry,
    get_default_coverage_backend_registry,
    normalize_target_path,
)


@dataclass
class ChangedFile:
    """Old/new-side changed line sets for one file in a unified diff."""

    old_path: str | None
    new_path: str | None
    added_lines: set[int] = field(default_factory=set)
    modified_old_lines: set[int] = field(default_factory=set)
    modified_new_lines: set[int] = field(default_factory=set)
    deleted_lines: set[int] = field(default_factory=set)

    @property
    def pre_target_lines(self) -> set[int]:
        return self.deleted_lines | self.modified_old_lines

    @property
    def post_target_lines(self) -> set[int]:
        return self.added_lines | self.modified_new_lines


@dataclass
class PatchChanges:
    """Changed files and lines extracted from a canonical gold patch."""

    files: list[ChangedFile] = field(default_factory=list)

    @property
    def changed_files(self) -> list[str]:
        return sorted(
            {
                path
                for item in self.files
                for path in (item.new_path or item.old_path,)
                if path is not None
            }
        )

    @property
    def added_line_count(self) -> int:
        return sum(len(item.added_lines) for item in self.files)

    @property
    def modified_line_count(self) -> int:
        return sum(len(item.modified_new_lines) for item in self.files)

    @property
    def deleted_line_count(self) -> int:
        return sum(len(item.deleted_lines) for item in self.files)

    @property
    def target_line_count(self) -> int:
        return sum(
            len(item.pre_target_lines) + len(item.post_target_lines)
            for item in self.files
        )


@dataclass
class PatchCoverageMetric:
    """Serializable result of the SWT-Bench changed-line metric.

    ``reference_*`` fields describe the denominator.  They are deliberately
    kept separate from ``generated_*`` fields: generated (agent) coverage may
    only mark a reference-qualified target as covered and can never introduce
    a target of its own.  The old ``executable_changed_line_count``,
    ``covered_changed_line_count`` and ``untraceable_line_count`` fields remain
    in the wire format for historical result compatibility.
    """

    status: str
    language: str | None = None
    languages: list[str] = field(default_factory=list)
    unsupported_changed_files: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    added_line_count: int = 0
    modified_line_count: int = 0
    deleted_line_count: int = 0
    executable_changed_line_count: int = 0
    covered_changed_line_count: int = 0
    untraceable_line_count: int = 0
    # Auditable SWT-Bench classifications.  The count fields are intentionally
    # explicit instead of requiring consumers to infer them from ``ratio``.
    reference_executed_line_count: int = 0
    generated_covered_line_count: int = 0
    reference_unexecuted_line_count: int = 0
    non_statement_line_count: int = 0
    generated_uncovered_line_count: int = 0
    generated_unavailable_line_count: int = 0
    # ``side:path`` identifiers (for example ``baseline:pkg/mod.py``) retain
    # enough detail to audit modified lines, which occur on both sides.
    line_classifications: dict[str, list[str]] = field(default_factory=dict)
    ratio: float | None = None
    reason: str | None = None
    artifact_path: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def reference_executable_changed_line_count(self) -> int:
        """Alias used by callers that spell out the denominator semantics."""

        return self.reference_executed_line_count

    @property
    def generated_covered_changed_line_count(self) -> int:
        """Alias for the generated/agent numerator count."""

        return self.generated_covered_line_count

    @property
    def reference_unexecuted_changed_line_count(self) -> int:
        """Alias for statement targets missed by the trusted reference suite."""

        return self.reference_unexecuted_line_count

    @property
    def non_statement_changed_line_count(self) -> int:
        """Alias for changed physical lines without a coverage statement."""

        return self.non_statement_line_count


def _normalise_path(path: str) -> str:
    normalised = path.replace("\\", "/")
    while normalised.startswith("./"):
        normalised = normalised[2:]
    if normalised.startswith("a/") or normalised.startswith("b/"):
        normalised = normalised[2:]
    return normalised


def _parse_header_path(value: str) -> str | None:
    value = value.strip()
    try:
        tokens = shlex.split(value)
    except ValueError:
        tokens = value.split()
    if not tokens or tokens[0] == "/dev/null":
        return None
    return _normalise_path(tokens[0])


def _parse_hunk_range(token: str) -> tuple[int, int]:
    token = token[1:]
    if "," in token:
        start, count = token.split(",", 1)
        return int(start), int(count)
    return int(token), 1


def parse_unified_diff(patch: str) -> PatchChanges:
    """Parse git-style unified diff text into old/new changed line sets.

    A contiguous change block pairs old and new lines as modifications. Extra
    old-side lines are deletions and extra new-side lines are additions.
    """

    files: list[ChangedFile] = []
    current: ChangedFile | None = None
    old_line = new_line = 0
    pending_old: list[int] = []
    pending_new: list[int] = []

    def flush_change_block() -> None:
        nonlocal pending_old, pending_new
        if current is None:
            pending_old = []
            pending_new = []
            return
        paired = min(len(pending_old), len(pending_new))
        current.modified_old_lines.update(pending_old[:paired])
        current.modified_new_lines.update(pending_new[:paired])
        current.deleted_lines.update(pending_old[paired:])
        current.added_lines.update(pending_new[paired:])
        pending_old = []
        pending_new = []

    for raw_line in patch.splitlines():
        if raw_line.startswith("diff --git "):
            flush_change_block()
            if current is not None:
                files.append(current)
            try:
                parts = shlex.split(raw_line)
            except ValueError as exc:
                raise ValueError("malformed diff --git header") from exc
            if len(parts) < 4:
                raise ValueError("malformed diff --git header")
            current = ChangedFile(
                old_path=_parse_header_path(parts[2]),
                new_path=_parse_header_path(parts[3]),
            )
            continue
        if current is None:
            continue
        if raw_line.startswith("--- "):
            current.old_path = _parse_header_path(raw_line[4:])
            continue
        if raw_line.startswith("+++ "):
            current.new_path = _parse_header_path(raw_line[4:])
            continue
        if raw_line.startswith("@@ "):
            flush_change_block()
            parts = raw_line.split()
            if len(parts) < 3:
                raise ValueError("malformed unified diff hunk header")
            try:
                old_line, _ = _parse_hunk_range(parts[1])
                new_line, _ = _parse_hunk_range(parts[2])
            except (TypeError, ValueError) as exc:
                raise ValueError("malformed unified diff hunk range") from exc
            continue
        if raw_line.startswith("-") and not raw_line.startswith("---"):
            pending_old.append(old_line)
            old_line += 1
            continue
        if raw_line.startswith("+") and not raw_line.startswith("+++"):
            pending_new.append(new_line)
            new_line += 1
            continue
        if raw_line.startswith(" "):
            flush_change_block()
            old_line += 1
            new_line += 1
            continue
        if raw_line.startswith("\\ No newline at end of file"):
            continue

    flush_change_block()
    if current is not None:
        files.append(current)
    return PatchChanges(files=files)


def _load_report(report: Mapping[str, Any] | str | Path) -> Mapping[str, Any]:
    if isinstance(report, Mapping):
        return report
    path = Path(report)
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid coverage report {path}: {exc}") from exc
    if not isinstance(data, Mapping):
        raise ValueError(f"invalid coverage report {path}: root must be an object")
    return data


ReportInput = Mapping[str, Any] | str | Path


def _merge_reports(
    reports: Iterable[ReportInput],
) -> dict[str, tuple[set[int], set[int]]]:
    merged: dict[str, tuple[set[int], set[int]]] = {}
    for raw_report in reports:
        report = _load_report(raw_report)
        files = report.get("files")
        if not isinstance(files, Mapping):
            raise ValueError("coverage report is missing a files object")
        for raw_path, raw_data in files.items():
            if not isinstance(raw_path, str) or not isinstance(raw_data, Mapping):
                raise ValueError("coverage file entries must be objects")
            try:
                raw_executed = raw_data["executed_lines"]
                raw_missing = raw_data["missing_lines"]
                if isinstance(raw_executed, (str, bytes)) or not isinstance(
                    raw_executed, Iterable
                ):
                    raise TypeError("executed_lines must be a sequence")
                if isinstance(raw_missing, (str, bytes)) or not isinstance(
                    raw_missing, Iterable
                ):
                    raise TypeError("missing_lines must be a sequence")
                executed = {int(value) for value in raw_executed}
                missing = {int(value) for value in raw_missing}
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"coverage entry for {raw_path!r} lacks valid line lists"
                ) from exc
            key = _normalise_path(raw_path)
            merged_executed, merged_executable = merged.setdefault(key, (set(), set()))
            merged_executed.update(executed)
            merged_executable.update(executed | missing)
    return merged


def _coverage_for_path(
    report: Mapping[str, tuple[set[int], set[int]]],
    path: str,
) -> tuple[set[int], set[int]] | None:
    """Resolve repo-relative targets against local or installed-package paths."""

    normalised = _normalise_path(path)
    matches = [
        value
        for candidate, value in report.items()
        if candidate == normalised or candidate.endswith(f"/{normalised}")
    ]
    if not matches:
        return None
    executed: set[int] = set()
    executable: set[int] = set()
    for matched_executed, matched_executable in matches:
        executed.update(matched_executed)
        executable.update(matched_executable)
    return executed, executable


def _report_map(
    reports: Iterable[ReportInput] | None,
    *,
    label: str,
    required: bool,
) -> dict[str, tuple[set[int], set[int]]]:
    """Load one state/suite report collection with an explicit failure reason."""

    if reports is None:
        if required:
            raise ValueError(f"missing required {label} coverage report")
        return {}
    report_items = list(reports)
    if not report_items:
        if required:
            raise ValueError(f"missing required {label} coverage report")
        return {}
    return _merge_reports(report_items)


def calculate_patch_coverage(
    changes: PatchChanges,
    *,
    generated_baseline_reports: Iterable[ReportInput] | None = None,
    generated_gold_reports: Iterable[ReportInput] | None = None,
    reference_baseline_reports: Iterable[ReportInput] | None = None,
    reference_gold_reports: Iterable[ReportInput] | None = None,
    artifact_path: str | None = None,
    backend_registry: CoverageBackendRegistry | None = None,
    registry: CoverageBackendRegistry | None = None,
) -> PatchCoverageMetric:
    """Calculate SWT-Bench changed-line coverage for one canonical patch.

    The four explicit report collections have independent responsibilities:

    * ``reference_baseline_reports`` and ``reference_gold_reports`` establish
      executable old/new-side targets;
    * ``generated_baseline_reports`` and ``generated_gold_reports`` only mark
      which of those targets the submitted tests covered.

    Modified lines are represented on both sides.  A missing generated file is
    therefore an uncovered target, not evidence that the denominator should be
    reduced.  All four report collections are explicit so an agent report can
    never silently become the denominator authority.
    """

    backend_registry = backend_registry or registry
    registry = backend_registry or get_default_coverage_backend_registry()
    changed_paths = [
        path
        for item in changes.files
        for path in (item.old_path, item.new_path)
        if path is not None
    ]
    base = PatchCoverageMetric(
        status="failed",
        changed_files=changes.changed_files,
        added_line_count=changes.added_line_count,
        modified_line_count=changes.modified_line_count,
        deleted_line_count=changes.deleted_line_count,
        artifact_path=artifact_path,
    )
    try:
        selection = registry.select(changed_paths)
    except (TypeError, ValueError) as exc:
        base.reason = str(exc)
        return base
    base.languages = selection.languages
    base.language = selection.language
    base.unsupported_changed_files = list(selection.unsupported_paths)

    if changes.target_line_count == 0:
        base.status = "no_changed_lines"
        base.reason = "gold patch has no changed source lines"
        return base

    supported_paths = set(selection.backends_by_path)

    def selected_path(path: str | None) -> str | None:
        if path is None:
            return None
        try:
            normalized = normalize_target_path(path)
        except (TypeError, ValueError):
            return None
        return normalized if normalized in supported_paths else None

    pre_target_count = sum(
        len(item.pre_target_lines)
        for item in changes.files
        if item.pre_target_lines and selected_path(item.old_path) is not None
    )
    post_target_count = sum(
        len(item.post_target_lines)
        for item in changes.files
        if item.post_target_lines and selected_path(item.new_path) is not None
    )
    if pre_target_count + post_target_count == 0:
        base.status = "unsupported"
        base.reason = "gold patch has no registered source changes"
        return base

    try:
        generated_baseline = _report_map(
            generated_baseline_reports,
            label="generated baseline",
            required=pre_target_count > 0,
        )
        generated_gold = _report_map(
            generated_gold_reports,
            label="generated gold",
            required=post_target_count > 0,
        )
        reference_baseline = _report_map(
            reference_baseline_reports,
            label="reference baseline",
            required=pre_target_count > 0,
        )
        reference_gold = _report_map(
            reference_gold_reports,
            label="reference gold",
            required=post_target_count > 0,
        )
    except ValueError as exc:
        base.reason = str(exc)
        return base

    # A valid reference report must include every changed target file.  An
    # omitted file could mean that the collector never analysed it (rather than
    # that changed lines are non-statements), so fail explicitly instead of
    # silently treating its targets as non-statement lines.
    for side, report, target_count, path_getter in (
        (
            "baseline",
            reference_baseline,
            pre_target_count,
            lambda item: item.old_path,
        ),
        (
            "gold",
            reference_gold,
            post_target_count,
            lambda item: item.new_path,
        ),
    ):
        if target_count == 0:
            continue
        for item in changes.files:
            target_lines = (
                item.pre_target_lines if side == "baseline" else item.post_target_lines
            )
            if not target_lines:
                continue
            path = path_getter(item)
            if selected_path(path) is None:
                continue
            if _coverage_for_path(report, path) is None:
                rendered_path = path if path is not None else "/dev/null"
                base.reason = (
                    f"reference {side} report missing target file "
                    f"{rendered_path!r}"
                )
                return base

    executable = covered = untraceable = 0
    reference_unexecuted = non_statement = 0
    generated_uncovered = generated_unavailable = 0
    classifications: dict[str, list[str]] = {
        "reference_executed": [],
        "generated_covered": [],
        "reference_unexecuted": [],
        "non_statement": [],
        "generated_uncovered": [],
        "generated_unavailable": [],
    }

    def record(category: str, side: str, path: str | None, line: int) -> None:
        rendered_path = path if path is not None else "/dev/null"
        classifications[category].append(f"{side}:{rendered_path}:{line}")

    for item in changes.files:
        states = (
            (
                "baseline",
                item.old_path,
                item.pre_target_lines,
                reference_baseline,
                generated_baseline,
            ),
            (
                "gold",
                item.new_path,
                item.post_target_lines,
                reference_gold,
                generated_gold,
            ),
        )
        for side, path, target_lines, reference, generated in states:
            if not target_lines or selected_path(path) is None:
                continue
            reference_coverage = (
                _coverage_for_path(reference, path) if path is not None else None
            )
            generated_coverage = (
                _coverage_for_path(generated, path) if path is not None else None
            )
            reference_executed_lines: set[int] = set()
            reference_executable_lines: set[int] = set()
            if reference_coverage is not None:
                reference_executed_lines, reference_executable_lines = (
                    reference_coverage
                )
            generated_executed_lines: set[int] = set()
            generated_executable_lines: set[int] = set()
            if generated_coverage is not None:
                generated_executed_lines, generated_executable_lines = (
                    generated_coverage
                )

            for line in sorted(target_lines):
                if line not in reference_executable_lines:
                    non_statement += 1
                    untraceable += 1
                    record("non_statement", side, path, line)
                    continue

                if line not in reference_executed_lines:
                    reference_unexecuted += 1
                    record("reference_unexecuted", side, path, line)
                    continue

                executable += 1
                record("reference_executed", side, path, line)

                if line in generated_executed_lines:
                    covered += 1
                    record("generated_covered", side, path, line)
                elif line in generated_executable_lines:
                    generated_uncovered += 1
                    record("generated_uncovered", side, path, line)
                else:
                    generated_unavailable += 1
                    record("generated_unavailable", side, path, line)

    supported_target_count = pre_target_count + post_target_count
    # Keep the summary internally auditable.  These assertions intentionally
    # fail loudly if a future parser change allows a target to escape one of
    # the mutually exclusive reference classifications or produces an invalid
    # ratio numerator.
    assert 0 <= covered <= executable
    assert len(classifications["reference_executed"]) == executable
    assert len(classifications["reference_unexecuted"]) == reference_unexecuted
    assert len(classifications["non_statement"]) == non_statement
    assert (
        executable + reference_unexecuted + non_statement == supported_target_count
    )
    assert (
        len(classifications["generated_covered"])
        + len(classifications["generated_uncovered"])
        + len(classifications["generated_unavailable"])
        == executable
    )

    base.executable_changed_line_count = executable
    base.covered_changed_line_count = covered
    base.untraceable_line_count = untraceable
    base.reference_executed_line_count = executable
    base.generated_covered_line_count = covered
    base.reference_unexecuted_line_count = reference_unexecuted
    base.non_statement_line_count = non_statement
    base.generated_uncovered_line_count = generated_uncovered
    base.generated_unavailable_line_count = generated_unavailable
    base.line_classifications = {
        category: sorted(lines)
        for category, lines in classifications.items()
        if lines
    }
    if executable == 0:
        base.status = "no_changed_lines"
        base.reason = "reference tests executed no changed lines"
        return base
    base.status = "collected"
    base.ratio = covered / executable
    base.reason = None
    return base
