"""Pure Entry Coverage arithmetic and failure classifications."""

import json

import pytest

from testprism.handlers.trial_handler import (
    TestEntry as _TestEntry,
)
from testprism.handlers.trial_handler import (
    TestEntryInferences as _TestEntryInferences,
)
from testprism.harness.entry_coverage import calculate_entry_coverage
from testprism.harness.models import (
    EntryCoverage,
    EntryCoverageStatus,
)
from testprism.harness.models import (
    TestBenchResults as _TestBenchResults,
)
from testprism.harness.models import (
    TestBenchTrialResults as _TestBenchTrialResults,
)


def metadata(
    *entries: _TestEntry, language: str = "python"
) -> _TestEntryInferences:
    return _TestEntryInferences(language=language, entries=list(entries))


def entry(path: str = "pkg/mod.py", start: int = 1, end: int = 8) -> _TestEntry:
    return _TestEntry(
        test_id="tests/test_mod.py::test_entry",
        path=path,
        symbol="pkg.mod:entry",
        start_line=start,
        end_line=end,
    )


def report(
    executed: list[int], missing: list[int], path: str = "pkg/mod.py"
) -> dict:
    return {"files": {path: {"executed_lines": executed, "missing_lines": missing}}}


@pytest.mark.parametrize(
    ("language", "path"),
    [
        ("python", "pkg/mod.py"),
        ("javascript", "web/mod.js"),
        ("typescript", "web/mod.ts"),
        ("java", "src/Main.java"),
        ("cpp", "native/mod.cpp"),
        ("go", "pkg/mod.go"),
    ],
)
def test_entry_coverage_supported_languages_share_same_ratio_semantics(
    language, path
):
    result = calculate_entry_coverage(
        metadata(entry(path=path, start=1, end=4), language=language),
        report(path=path, executed=[1, 2], missing=[1, 2, 3, 4]),
    )

    assert result.status == "collected"
    assert result.language == language
    assert result.executable_entry_line_count == 4
    assert result.covered_entry_line_count == 2
    assert result.uncovered_entry_line_count == 2
    assert result.ratio == pytest.approx(0.5)
    assert result.line_classifications["covered"] == [f"{path}:1", f"{path}:2"]


def test_entry_coverage_uses_union_and_deduplicates_overlapping_spans():
    result = calculate_entry_coverage(
        metadata(entry(start=1, end=5), entry(start=3, end=8)),
        report(executed=[1, 4], missing=[2, 3, 4, 5, 6, 7, 8]),
    )

    assert result.status == "collected"
    assert result.executable_entry_line_count == 8
    assert result.covered_entry_line_count == 2
    assert result.ratio == pytest.approx(0.25)
    assert result.line_classifications["executable"] == [
        f"pkg/mod.py:{line}" for line in range(1, 9)
    ]


@pytest.mark.parametrize(
    ("executed", "expected"),
    [([], 0.0), ([1, 2], 0.5), ([1, 2, 3, 4], 1.0)],
)
def test_entry_coverage_zero_partial_and_full_ratios(executed, expected):
    result = calculate_entry_coverage(
        metadata(entry(start=1, end=4)),
        report(executed=executed, missing=[1, 2, 3, 4]),
    )

    assert result.status == "collected"
    assert result.ratio == pytest.approx(expected)


def test_entry_coverage_executed_lines_define_executable_scope_too():
    result = calculate_entry_coverage(
        metadata(entry(start=10, end=12)),
        report(executed=[10, 12], missing=[]),
    )

    assert result.executable_entry_line_count == 2
    assert result.covered_entry_line_count == 2
    assert result.ratio == 1.0


def test_entry_coverage_statuses_are_nullable_and_explicit():
    unavailable = calculate_entry_coverage(None, None)
    assert unavailable.status == "unavailable"
    assert unavailable.ratio is None

    unsupported = calculate_entry_coverage(
        {
            "schema_version": 1,
            "language": "rust",
            "source": "static_test_invocation",
            "entries": [],
        },
        None,
    )
    assert unsupported.status == "unsupported"
    assert unsupported.language == "rust"
    assert unsupported.ratio is None

    empty = calculate_entry_coverage(
        {
            "schema_version": 1,
            "language": "python",
            "source": "static_test_invocation",
            "entries": [],
        },
        None,
    )
    assert empty.status == "no_executable_lines"
    assert empty.ratio is None


@pytest.mark.parametrize(
    "bad_metadata",
    [
        {
            "schema_version": 1,
            "language": "python",
            "source": "static_test_invocation",
            "entries": [
                {"path": "../mod.py", "start_line": 1, "end_line": 2}
            ],
        },
        {
            "schema_version": 1,
            "language": "python",
            "source": "static_test_invocation",
            "entries": [
                {"path": "pkg/mod.py", "start_line": 3, "end_line": 2}
            ],
        },
        {
            "schema_version": 2,
            "language": "python",
            "source": "static_test_invocation",
            "entries": [],
        },
        {
            "schema_version": 1,
            "language": "go",
            "source": "static_test_invocation",
            "entries": [{"path": "../mod.go", "start_line": 1, "end_line": 2}],
        },
    ],
)
def test_entry_coverage_invalid_metadata_is_failed(bad_metadata):
    result = calculate_entry_coverage(bad_metadata, report([1], [2]))
    assert result.status == "failed"
    assert result.ratio is None
    assert "metadata" in (result.reason or "")


def test_entry_coverage_supported_empty_non_python_metadata_is_no_executable_lines():
    result = calculate_entry_coverage(
        {
            "schema_version": 1,
            "language": "go",
            "source": "static_test_invocation",
            "entries": [],
        },
        None,
    )

    assert result.status == "no_executable_lines"
    assert result.language == "go"
    assert result.ratio is None


def test_entry_coverage_missing_and_ambiguous_report_paths_fail():
    missing = calculate_entry_coverage(metadata(entry()), {"files": {}})
    assert missing.status == "failed"
    assert "missing entry file" in (missing.reason or "")

    ambiguous = calculate_entry_coverage(
        metadata(entry()),
        {
            "files": {
                "pkg/mod.py": {"executed_lines": [1], "missing_lines": [2]},
                "/workspace/pkg/mod.py": {
                    "executed_lines": [1],
                    "missing_lines": [2],
                },
            }
        },
    )
    assert ambiguous.status == "failed"
    assert "ambiguous" in (ambiguous.reason or "")


def test_entry_coverage_malformed_report_file_fails(tmp_path):
    malformed = tmp_path / "coverage.json"
    malformed.write_text("not json")
    result = calculate_entry_coverage(metadata(entry()), malformed)
    assert result.status == "failed"
    assert "invalid coverage report" in (result.reason or "")

    malformed.write_text(json.dumps({"files": {"pkg/mod.py": {"executed_lines": [1]}}}))
    result = calculate_entry_coverage(metadata(entry()), malformed)
    assert result.status == "failed"
    assert "line lists" in (result.reason or "")


def test_entry_coverage_model_and_run_macro_are_backward_compatible():
    first = calculate_entry_coverage(
        metadata(entry(end=2)), report(executed=[1], missing=[2])
    )
    second = calculate_entry_coverage(
        metadata(entry(end=6)), report(executed=[1, 2], missing=[3, 4, 5, 6])
    )
    first_model = EntryCoverage.model_validate(first.as_dict())
    second_model = EntryCoverage.model_validate(second.as_dict())
    failed = EntryCoverage(
        status=EntryCoverageStatus.FAILED, reason="collection failed"
    )
    unavailable = EntryCoverage(
        status=EntryCoverageStatus.UNAVAILABLE, reason="no metadata"
    )
    unsupported = EntryCoverage(
        status=EntryCoverageStatus.UNSUPPORTED, reason="non-Python"
    )
    no_executable = EntryCoverage(
        status=EntryCoverageStatus.NO_EXECUTABLE_LINES,
        reason="empty executable scope",
    )
    historical = _TestBenchTrialResults(
        trial_name="historical", task_id="task-old", instruction="old"
    )
    results = _TestBenchResults(
        results=[
            _TestBenchTrialResults(
                trial_name="first",
                task_id="task-1",
                instruction="test",
                entry_coverage=first_model,
            ),
            _TestBenchTrialResults(
                trial_name="second",
                task_id="task-2",
                instruction="test",
                entry_coverage=second_model,
            ),
            _TestBenchTrialResults(
                trial_name="failed",
                task_id="task-3",
                instruction="test",
                entry_coverage=failed,
            ),
            _TestBenchTrialResults(
                trial_name="unavailable",
                task_id="task-4",
                instruction="test",
                entry_coverage=unavailable,
            ),
            _TestBenchTrialResults(
                trial_name="unsupported",
                task_id="task-5",
                instruction="test",
                entry_coverage=unsupported,
            ),
            _TestBenchTrialResults(
                trial_name="no-executable",
                task_id="task-6",
                instruction="test",
                entry_coverage=no_executable,
            ),
            historical,
        ]
    )

    # Macro averaging differs from a weighted micro ratio: (1/2 + 2/6) / 2
    # is 5/12, while the weighted result would be 3/8.
    assert results.entry_coverage == pytest.approx((0.5 + 2 / 6) / 2)
    assert results.entry_coverage != pytest.approx(3 / 8)
    assert results.n_entry_coverage_tasks == 2
    assert results.n_entry_coverage_failed == 1
    assert results.n_entry_coverage_unavailable == 1
    assert results.n_entry_coverage_unsupported == 1
    assert results.n_entry_coverage_no_executable_lines == 1
    assert results.n_entry_lines == 8
    assert results.n_entry_lines_covered == 3

    # The additive field defaults to None for old trial/result JSON.
    restored = _TestBenchResults.model_validate(
        {
            "results": [
                {
                    "trial_name": "old",
                    "task_id": "task-old",
                    "instruction": "old",
                }
            ]
        }
    )
    assert restored.results[0].entry_coverage is None
    assert restored.entry_coverage is None
