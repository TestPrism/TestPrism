import json

from testprism.cli.tb.testbench import _print_testbench_results


def test_print_testbench_results_shows_independent_entry_coverage(tmp_path, capsys):
    results_path = tmp_path / "results.json"
    results_path.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "task_id": "task1",
                        "is_resolved": True,
                        "patch_set_id": "set-1",
                        "entry_coverage": {
                            "status": "collected",
                            "ratio": 0.25,
                            "executable_entry_line_count": 4,
                            "covered_entry_line_count": 1,
                        },
                    },
                    {
                        "task_id": "task2",
                        "is_resolved": False,
                        "patch_set_id": "set-2",
                        "entry_coverage": {
                            "status": "failed",
                            "ratio": None,
                            "reason": "coverage collection failed",
                        },
                    },
                ],
                "n_resolved": 1,
                "avg_f2p_rate": 1.0,
                "coverage": 0.75,
                "n_gold_patch_lines": 4,
                "n_gold_patch_lines_covered": 3,
                "n_coverage_tasks": 2,
                "n_coverage_unsupported": 0,
                "n_coverage_failed": 0,
                "entry_coverage": 0.25,
                "n_entry_coverage_tasks": 1,
                "n_entry_coverage_unavailable": 0,
                "n_entry_coverage_unsupported": 0,
                "n_entry_coverage_no_executable_lines": 0,
                "n_entry_coverage_failed": 1,
            }
        )
    )

    _print_testbench_results(results_path)

    output = capsys.readouterr().out
    assert "Gold Patch Coverage" in output
    assert "Entry Coverage" in output
    assert "25.00%" in output
    assert "Covered / Executable Entry Lines" in output
    assert "1 / 4" in output
    status_row = next(
        line
        for line in output.splitlines()
        if "Entry Coverage Status" in line
    )
    assert "1 / 0 / 0 / 0 / 1" in status_row


def test_print_testbench_results_marks_historical_entry_coverage_unavailable(
    tmp_path, capsys
):
    results_path = tmp_path / "historical-results.json"
    results_path.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "task_id": "legacy-task",
                        "is_resolved": False,
                        "n_total_tests": 1,
                    }
                ],
                "n_resolved": 0,
                "n_total_tests": 1,
                "n_effective": 0,
                "n_false_positive": 0,
                "n_false_negative": 1,
                "n_regression": 0,
                "n_error": 0,
                "avg_f2p_rate": 0.0,
            }
        )
    )

    _print_testbench_results(results_path)

    output = capsys.readouterr().out
    assert "Entry Coverage" in output
    assert "N/A" in next(
        line for line in output.splitlines() if "Entry Coverage" in line
    )
    status_row = next(
        line
        for line in output.splitlines()
        if "Entry Coverage Status" in line
    )
    assert "0 / 1 / 0 / 0 / 0" in status_row
    # Historical output did not have patch-set coverage; preserve that row's
    # existing conditional presentation while still exposing Entry Coverage.
    assert "Gold Patch Coverage" not in output
