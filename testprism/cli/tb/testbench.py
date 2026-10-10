"""CLI commands for TestBench evaluation.

Provides ``tb testbench run`` and ``tb testbench validate`` sub-commands.
"""

import importlib.util
import json
import logging
import sys
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Annotated

import dotenv
import typer
from rich import print as rich_print
from tabulate import tabulate
from typer import Option, Typer

from testprism.agents import AgentName
from testprism.cli.tb.runs import _process_agent_kwargs

dotenv.load_dotenv()

testbench_app = Typer(no_args_is_help=True)


class HelpPanel(str, Enum):
    DATASET = "Dataset"
    OUTPUT = "Output"
    TASKS = "Tasks"
    BUILD = "Build"
    AGENT = "Agent"
    LOGGING = "Logging"
    CONCURRENCY = "Concurrency"
    TIMEOUT = "Timeout"


class LogLevel(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


def _print_testbench_results(results_path: Path) -> None:
    """Print TestBench-specific results summary."""
    if not results_path.exists():
        rich_print("[red]Results file not found.[/]")
        return

    data = json.loads(results_path.read_text())
    trial_results = data.get("results", [])

    if not trial_results:
        rich_print("[yellow]No trial results found.[/]")
        return

    def total_for(field: str) -> int:
        if field in data:
            return int(data[field] or 0)
        return sum(int(r.get(field, 0) or 0) for r in trial_results)

    total = len(trial_results)
    resolved = int(
        data.get("n_resolved")
        if "n_resolved" in data
        else sum(1 for r in trial_results if r.get("is_resolved"))
    )
    total_tests = total_for("n_total_tests")
    if total_tests == 0:
        total_tests = sum(
            len(r.get("verdicts", {})) for r in trial_results if r.get("verdicts")
        )
    total_effective = total_for("n_effective")
    total_fp = total_for("n_false_positive")
    total_fn = total_for("n_false_negative")
    total_regression = total_for("n_regression")
    total_error = total_for("n_error")
    avg_f2p = (
        float(data["avg_f2p_rate"])
        if "avg_f2p_rate" in data
        else (
            sum(r.get("f2p_rate", 0.0) for r in trial_results) / total
            if total > 0
            else 0.0
        )
    )
    patch_trials = [result for result in trial_results if result.get("patch_set_id")]

    def _macro_value(field: str, aggregate_field: str):
        """Read a serialized macro metric, tolerating historical results."""

        aggregate_value = data.get(aggregate_field)
        if aggregate_value is not None:
            return aggregate_value
        values = [
            result.get(field)
            for result in patch_trials
            if result.get(field) is not None
        ]
        return sum(values) / len(values) if values else None

    avg_valid_acceptance = _macro_value(
        "valid_acceptance_rate", "avg_valid_acceptance_rate"
    )
    avg_invalid_rejection = _macro_value(
        "invalid_rejection_rate", "avg_invalid_rejection_rate"
    )
    avg_balanced_patch = _macro_value(
        "balanced_patch_accuracy", "avg_balanced_patch_accuracy"
    )

    def _rate_text(value: object) -> str:
        return f"{float(value):.2%}" if value is not None else "N/A"

    def _patch_set_group_counts() -> dict[str, int]:
        """Return explicit schema/status buckets for v3 and v4 reports."""

        counts = {
            key: 0 for key in ("v3/ready", "v3/partial", "v4/ready", "v4/partial")
        }
        for result in patch_trials:
            version = result.get("patch_set_schema_version")
            if version is None:
                # Historical patch-set results predate the version fields and
                # were produced by the v3 consumer. Infer status only when
                # possible from the observed member counts; this does not
                # rewrite those result files.
                version = 3
            status = result.get("patch_set_status")
            if status is None:
                actual_valid = result.get("patch_set_actual_valid")
                actual_invalid = result.get("patch_set_actual_invalid")
                if actual_valid is None:
                    actual_valid = sum(
                        item.get("label") == "oracle_valid"
                        for item in result.get("patch_results", [])
                    )
                if actual_invalid is None:
                    actual_invalid = sum(
                        item.get("label") == "oracle_invalid"
                        for item in result.get("patch_results", [])
                    )
                status = (
                    "ready" if actual_valid == 5 and actual_invalid == 5 else "partial"
                )
            key = f"v{version}/{status}"
            if key in counts:
                counts[key] += 1
        return counts

    patch_set_groups = _patch_set_group_counts()

    def _participant_count(field: str, metric_field: str) -> int:
        value = data.get(field)
        if value is not None:
            return int(value)
        return sum(result.get(metric_field) is not None for result in patch_trials)

    n_valid_rate_tasks = _participant_count(
        "n_valid_rate_tasks", "valid_acceptance_rate"
    )
    n_invalid_rate_tasks = _participant_count(
        "n_invalid_rate_tasks", "invalid_rejection_rate"
    )
    n_balanced_rate_tasks = _participant_count(
        "n_balanced_rate_tasks", "balanced_patch_accuracy"
    )
    coverage_records = [
        result.get("gold_patch_coverage")
        for result in trial_results
        if isinstance(result.get("gold_patch_coverage"), dict)
    ]
    entry_trial_values = [result.get("entry_coverage") for result in trial_results]
    entry_records = [
        value for value in entry_trial_values if isinstance(value, dict)
    ]
    # A result produced before Entry Coverage existed has no trial field at
    # all.  Keep that historical shape readable by treating each such trial as
    # explicitly ``unavailable`` in the diagnostic display.
    entry_has_records = bool(entry_records)

    def coverage_status_count(status: str, aggregate_field: str) -> int:
        """Count a coverage status while tolerating historical result shapes."""

        if coverage_records:
            return sum(
                record.get("status") == status
                and (
                    status != "collected"
                    or record.get("ratio") is not None
                )
                for record in coverage_records
            )
        return int(data.get(aggregate_field, 0) or 0)

    coverage_countable = coverage_status_count("collected", "n_coverage_tasks")
    coverage_no_changed = coverage_status_count(
        "no_changed_lines", "n_coverage_no_changed_lines"
    )
    coverage_unsupported = coverage_status_count(
        "unsupported", "n_coverage_unsupported"
    )
    coverage_failed = coverage_status_count("failed", "n_coverage_failed")

    coverage = data.get("coverage")
    if coverage is None and coverage_records:
        ratios = [
            record.get("ratio")
            for record in coverage_records
            if record.get("status") == "collected"
            and record.get("ratio") is not None
        ]
        coverage = sum(ratios) / len(ratios) if ratios else None
    coverage_rows = coverage_records or any(
        field in data
        for field in (
            "coverage",
            "n_coverage_tasks",
            "n_coverage_no_changed_lines",
            "n_coverage_unsupported",
            "n_coverage_failed",
        )
    )

    def entry_status_count(status: str, aggregate_field: str) -> int:
        """Count Entry Coverage statuses across new and historical JSON."""

        if entry_has_records:
            return sum(
                (
                    value.get("status") == status
                    if isinstance(value, dict)
                    else status == "unavailable"
                )
                for value in entry_trial_values
            )
        if aggregate_field in data:
            return int(data.get(aggregate_field, 0) or 0)
        return total if status == "unavailable" else 0

    def entry_line_total(field: str) -> int:
        if entry_has_records:
            return sum(
                int(value.get(field, 0) or 0)
                for value in entry_records
            )
        return int(data.get(field, 0) or 0)

    entry_countable = entry_status_count("collected", "n_entry_coverage_tasks")
    entry_unavailable = entry_status_count(
        "unavailable", "n_entry_coverage_unavailable"
    )
    entry_unsupported = entry_status_count(
        "unsupported", "n_entry_coverage_unsupported"
    )
    entry_no_executable = entry_status_count(
        "no_executable_lines", "n_entry_coverage_no_executable_lines"
    )
    entry_failed = entry_status_count("failed", "n_entry_coverage_failed")

    entry_coverage = data.get("entry_coverage")
    if entry_coverage is None and entry_records:
        ratios = [
            record.get("ratio")
            for record in entry_records
            if record.get("status") == "collected"
            and record.get("ratio") is not None
        ]
        entry_coverage = sum(ratios) / len(ratios) if ratios else None

    headers = ["Metric", "Value"]
    data_rows = [
        ["Total Tasks", str(total)],
        ["Resolved (has effective F2P tests)", str(resolved)],
        ["Task Resolve Rate", f"{resolved / total:.2%}" if total else "N/A"],
        ["Avg F2P Rate", f"{avg_f2p:.2%}"],
        ["Total Agent Tests", str(total_tests)],
        ["Total Effective (F->P)", str(total_effective)],
        ["Total False Positive (P->P)", str(total_fp)],
        ["Total False Negative (F->F)", str(total_fn)],
        ["Total Regression (P->F)", str(total_regression)],
        ["Total Error", str(total_error)],
    ]
    if patch_trials or coverage_rows:
        patch_set_rows = (
            [
                ["Patch Sets v3 Ready", str(patch_set_groups["v3/ready"])],
                ["Patch Sets v3 Partial", str(patch_set_groups["v3/partial"])],
                ["Patch Sets v4 Ready", str(patch_set_groups["v4/ready"])],
                ["Patch Sets v4 Partial", str(patch_set_groups["v4/partial"])],
            ]
            if patch_trials
            else []
        )
        data_rows.extend(
            patch_set_rows
            + [
                [
                    "Avg Valid Patch Acceptance",
                    f"{_rate_text(avg_valid_acceptance)} (n={n_valid_rate_tasks})",
                ],
                [
                    "Avg Invalid Patch Rejection",
                    f"{_rate_text(avg_invalid_rejection)} (n={n_invalid_rate_tasks})",
                ],
                [
                    "Avg Balanced Patch Accuracy",
                    f"{_rate_text(avg_balanced_patch)} (n={n_balanced_rate_tasks})",
                ],
                [
                    "Patch Evaluation Errors",
                    str(int(data.get("n_patch_errors", 0) or 0)),
                ],
                [
                    "Gold Patch Coverage",
                    f"{float(coverage):.2%}" if coverage is not None else "N/A",
                ],
                [
                    "Covered / Executable Gold Lines",
                    (
                        f"{int(data.get('n_gold_patch_lines_covered', 0) or 0)} / "
                        f"{int(data.get('n_gold_patch_lines', 0) or 0)}"
                    ),
                ],
                [
                    "Coverage Collected / No Executable / Unsupported",
                    (
                        f"{coverage_countable} / "
                        f"{coverage_no_changed} / "
                        f"{coverage_unsupported}"
                    ),
                ],
                [
                    "Coverage Failed (excluded from mean)",
                    str(coverage_failed),
                ],
            ]
        )

    # Entry Coverage is observational and intentionally displayed in its own
    # row/status family.  It remains visible for historical result files, where
    # all trials are shown as ``unavailable`` rather than silently disappearing.
    if total:
        data_rows.extend(
            [
                [
                    "Entry Coverage",
                    (
                        f"{float(entry_coverage):.2%}"
                        if entry_coverage is not None
                        else "N/A"
                    ),
                ],
                [
                    "Covered / Executable Entry Lines",
                    (
                        f"{entry_line_total('covered_entry_line_count')} / "
                        f"{entry_line_total('executable_entry_line_count')}"
                    ),
                ],
                [
                    (
                        "Entry Coverage Status "
                        "(Collected / Unavailable / Unsupported / "
                        "No Executable / Failed)"
                    ),
                    (
                        f"{entry_countable} / {entry_unavailable} / "
                        f"{entry_unsupported} / {entry_no_executable} / "
                        f"{entry_failed}"
                    ),
                ],
            ]
        )

    print("\nTestBench Results Summary:")
    print(tabulate(data_rows, headers=headers, tablefmt="grid"))
    rich_print(f"\nResults written to [bold green]{results_path.absolute()}[/]")


@testbench_app.command(name="run", help="Run TestBench evaluation (test-generation).")
def testbench_run(
    dataset_path: Annotated[
        Path,
        Option(
            "-p",
            "--dataset-path",
            help="Path to the tasks directory (containing TestBench-form tasks)",
            rich_help_panel=HelpPanel.DATASET,
        ),
    ] = Path("tasks"),
    output_path: Annotated[
        Path,
        Option(help="Path to output directory", rich_help_panel=HelpPanel.OUTPUT),
    ] = Path("runs"),
    run_id: Annotated[
        str | None,
        Option(
            help="Unique identifier for this run",
            rich_help_panel=HelpPanel.OUTPUT,
            show_default="testbench-YYYY-MM-DD__HH-MM-SS",
        ),
    ] = None,
    task_ids: Annotated[
        list[str] | None,
        Option(
            "-t",
            "--task-id",
            help="Specific task IDs to run. Can be set multiple times.",
            rich_help_panel=HelpPanel.TASKS,
        ),
    ] = None,
    n_tasks: Annotated[
        int | None,
        Option(
            help="Number of tasks to run",
            rich_help_panel=HelpPanel.TASKS,
        ),
    ] = None,
    exclude_task_ids: Annotated[
        list[str] | None,
        Option(
            "-e",
            "--exclude-task-id",
            help="Task IDs to exclude.",
            rich_help_panel=HelpPanel.TASKS,
        ),
    ] = None,
    no_rebuild: Annotated[
        bool,
        Option(
            "--no-rebuild/--rebuild",
            help="Skip rebuilding containers",
            rich_help_panel=HelpPanel.BUILD,
        ),
    ] = False,
    cleanup: Annotated[
        bool,
        Option(help="Remove run docker images", rich_help_panel=HelpPanel.BUILD),
    ] = True,
    model_name: Annotated[
        str | None,
        Option(
            "-m",
            "--model",
            help="Model to use (provider/model_name)",
            rich_help_panel=HelpPanel.AGENT,
        ),
    ] = None,
    agent: Annotated[
        AgentName,
        Option(
            "-a",
            "--agent",
            help="Agent to use.",
            rich_help_panel=HelpPanel.AGENT,
        ),
    ] = AgentName.ORACLE,
    agent_import_path: Annotated[
        str | None,
        Option(
            help="Import path of a custom agent (e.g. some.module:Agent).",
            rich_help_panel=HelpPanel.AGENT,
        ),
    ] = None,
    agent_config: Annotated[
        Path | None,
        Option(
            "--agent-config",
            help=(
                "Path to a JSON config file for the agent. "
                "For claude-code, the file should contain 'env' (dict of "
                "environment variables) and optionally 'model'."
            ),
            rich_help_panel=HelpPanel.AGENT,
        ),
    ] = None,
    agent_kwarg: Annotated[
        list[str],
        Option(
            "--agent-kwarg",
            help=(
                "Extra keyword argument for the agent as key=value "
                "(e.g. api_base=https://api.openai.com/v1). Repeatable."
            ),
            rich_help_panel=HelpPanel.AGENT,
        ),
    ] = [],
    log_level: Annotated[
        LogLevel,
        Option(help="Logging level", rich_help_panel=HelpPanel.LOGGING),
    ] = LogLevel.INFO,
    livestream: Annotated[
        bool,
        Option(help="Enable livestreaming", rich_help_panel=HelpPanel.LOGGING),
    ] = False,
    n_concurrent_trials: Annotated[
        int,
        Option(
            "--n-concurrent",
            help="Number of concurrent trials",
            rich_help_panel=HelpPanel.CONCURRENCY,
        ),
    ] = 4,
    n_attempts: Annotated[
        int,
        Option(
            help="Number of attempts per task",
            rich_help_panel=HelpPanel.CONCURRENCY,
        ),
    ] = 1,
    global_timeout_multiplier: Annotated[
        float,
        Option(
            help="Global timeout multiplier",
            rich_help_panel=HelpPanel.TIMEOUT,
        ),
    ] = 1.0,
    global_agent_timeout_sec: Annotated[
        float | None,
        Option(
            help="Override agent timeout (seconds)",
            rich_help_panel=HelpPanel.TIMEOUT,
        ),
    ] = None,
    global_test_timeout_sec: Annotated[
        float | None,
        Option(
            help="Override test timeout (seconds)",
            rich_help_panel=HelpPanel.TIMEOUT,
        ),
    ] = None,
    patch_set_path: Annotated[
        Path | None,
        Option(
            "--patch-set-path",
            help=(
                "Root containing schema-v3 or schema-v4 patch-set manifests. "
                "When set, evaluates baseline plus each published solution; "
                "v4 partial sets are accepted by default."
            ),
            rich_help_panel=HelpPanel.DATASET,
        ),
    ] = None,
    patch_concurrency: Annotated[
        int,
        Option(
            "--patch-concurrency",
            min=1,
            help="Number of isolated solution containers per task to run concurrently.",
            rich_help_panel=HelpPanel.CONCURRENCY,
        ),
    ] = 1,
    agent_tests_path: Annotated[
        Path | None,
        Option(
            "--agent-tests-path",
            help=(
                "Replay a previously saved agent-tests directory (or its trial "
                "directory) instead of invoking the agent. Requires one task."
            ),
            rich_help_panel=HelpPanel.DATASET,
        ),
    ] = None,
) -> None:
    """Run the TestBench harness for test-generation evaluation."""
    from testprism.harness.testbench_harness import TestBenchHarness

    if run_id is None:
        run_id = f"testbench-{datetime.now():%Y-%m-%d__%H-%M-%S}"

    log_level_int = getattr(logging, log_level.value.upper(), logging.INFO)

    agent_kwargs = _process_agent_kwargs(
        model_name=model_name,
        no_rebuild=no_rebuild,
        agent_kwargs=agent_kwarg,
    )

    if agent_config is not None:
        agent_kwargs["config"] = str(agent_config)

    harness = TestBenchHarness(
        output_path=output_path,
        run_id=run_id,
        agent_name=agent,
        agent_import_path=agent_import_path,
        dataset_path=dataset_path,
        model_name=model_name,
        agent_kwargs=agent_kwargs,
        no_rebuild=no_rebuild,
        cleanup=cleanup,
        log_level=log_level_int,
        task_ids=task_ids,
        n_tasks=n_tasks,
        livestream=livestream,
        n_concurrent_trials=n_concurrent_trials,
        exclude_task_ids=exclude_task_ids,
        n_attempts=n_attempts,
        global_timeout_multiplier=global_timeout_multiplier,
        global_agent_timeout_sec=global_agent_timeout_sec,
        global_test_timeout_sec=global_test_timeout_sec,
        patch_set_path=patch_set_path,
        patch_concurrency=patch_concurrency,
        agent_tests_path=agent_tests_path,
    )

    harness.run()

    results_path = output_path / run_id / "results.json"
    _print_testbench_results(results_path)


def _load_verify_tasks_module():
    validation_script = (
        Path(__file__).resolve().parent.parent.parent.parent
        / "scripts"
        / "validation"
        / "verify_tasks.py"
    )
    if not validation_script.exists():
        rich_print(f"[red]Validation script not found: {validation_script}[/]")
        raise typer.Exit(1)

    spec = importlib.util.spec_from_file_location("verify_tasks", validation_script)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def validate_tasks(
    dataset_path: Annotated[
        Path | None,
        Option(
            "-p",
            "--dataset-path",
            help="Path to a verified_tasks directory tree or a single task directory.",
        ),
    ] = None,
    candidates: Annotated[
        Path | None,
        Option(
            "--candidates",
            help="Path to candidates.json. If omitted, validator scans --dataset-path.",
        ),
    ] = None,
    task_ids: Annotated[
        list[str] | None,
        Option(
            "-t",
            "--task-id",
            help="Validate a specific task only. Repeatable.",
        ),
    ] = None,
    source: Annotated[
        str | None,
        Option(
            "--source",
            help="Only validate tasks from this source.",
        ),
    ] = None,
    output: Annotated[
        Path | None,
        Option(
            "--output",
            help="Path to validation results JSON.",
        ),
    ] = None,
    n_concurrent: Annotated[
        int,
        Option(
            "--n-concurrent",
            help="Number of concurrent validations.",
        ),
    ] = 8,
    resume: Annotated[
        bool,
        Option(
            "--resume",
            help="Resume from previous validation results.",
        ),
    ] = False,
    retry_failed: Annotated[
        bool,
        Option(
            "--retry-failed",
            help="Retry previously failed validations.",
        ),
    ] = False,
    retry_phases: Annotated[
        str | None,
        Option(
            "--retry-phases",
            help="Comma-separated fail phases to retry.",
        ),
    ] = None,
    limit: Annotated[
        int | None,
        Option(
            "--limit",
            help="Limit task count for smoke runs.",
        ),
    ] = None,
    source_profile_out: Annotated[
        Path | None,
        Option(
            "--source-profile-out",
            help="Write one-shot source profile JSON before validation.",
        ),
    ] = None,
    cleanup: Annotated[
        bool,
        Option(
            "--cleanup/--no-cleanup",
            help=(
                "Remove locally built Docker images during post-test cleanup "
                "(default: enabled)."
            ),
        ),
    ] = True,
) -> None:
    """Validate task legality with the golden-standard validator."""
    module = _load_verify_tasks_module()
    argv: list[str] = []
    if dataset_path is not None:
        argv.extend(["--dataset-path", str(dataset_path)])
    if candidates is not None:
        argv.extend(["--candidates", str(candidates)])
    if output is not None:
        argv.extend(["--output", str(output)])
    if source is not None:
        argv.extend(["--source", source])
    if retry_phases is not None:
        argv.extend(["--retry-phases", retry_phases])
    if limit is not None:
        argv.extend(["--limit", str(limit)])
    if source_profile_out is not None:
        argv.extend(["--source-profile-out", str(source_profile_out)])
    if resume:
        argv.append("--resume")
    if retry_failed:
        argv.append("--retry-failed")
    if not cleanup:
        argv.append("--no-cleanup")
    argv.extend(["--n-concurrent", str(n_concurrent)])
    for task_id in task_ids or []:
        argv.extend(["--task-id", task_id])

    raise typer.Exit(int(module.main(argv)))


testbench_app.command(
    name="validate",
    help="Validate tasks with the golden-standard task validator.",
)(validate_tasks)
