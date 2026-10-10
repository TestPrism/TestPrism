from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from scripts.generation import test_entry_inferences as workflow


def _task(
    root: Path,
    key: str,
    *,
    decision: str = "applicable",
    languages: list[str] | None = None,
    task_yaml_prefix: str = "instruction: test\nsentinel: true\n",
    solution: str = "echo ok\n",
    run_tests: str = "pytest -q\n",
) -> tuple[Path, Path, Path, Path]:
    if languages is None:
        languages = ["python"] if decision == "applicable" else []
    task_dir = root / key
    task_dir.mkdir(parents=True)
    task_yaml = task_dir / "task.yaml"
    solution_sh = task_dir / "solution.sh"
    solution_sh.write_text(solution, encoding="utf-8")
    run_tests_sh = task_dir / "run-tests.sh"
    run_tests_sh.write_text(run_tests, encoding="utf-8")
    task_yaml.write_text(
        task_yaml_prefix
        + yaml.safe_dump(
            {
                "entry_coverage_applicability": {
                    "schema_version": 1,
                    "solution_sha256": workflow.applicability_workflow._sha256(
                        solution_sh
                    ),
                    "decision": decision,
                    "reason_code": (
                        "direct_local_entry_surface"
                        if decision == "applicable"
                        else "artifact_only_verifier"
                    ),
                    "languages": languages,
                    "evidence_paths": ["tests/test_demo.py"]
                    if decision == "applicable"
                    else ["answer.txt"],
                }
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return task_dir, task_yaml, solution_sh, run_tests_sh


def test_prepare_only_queues_applicable_tasks_and_ignores_existing_metadata(
    tmp_path: Path,
) -> None:
    tasks_root = tmp_path / "tasks"
    applicable_dir, applicable_yaml, _solution, _run_tests = _task(
        tasks_root,
        "from-example/task-one",
    )
    _task(tasks_root, "from-example/task-two", decision="not_applicable")
    (applicable_dir / "pkg").mkdir()
    (applicable_dir / "pkg" / "demo.py").write_text(
        "def run():\n    return 1\n",
        encoding="utf-8",
    )
    (applicable_dir / "tests").mkdir()
    (applicable_dir / "tests" / "test_demo.py").write_text(
        "from pkg.demo import run\n\ndef test_run():\n    assert run() == 1\n",
        encoding="utf-8",
    )
    applicable_yaml.write_text(
        applicable_yaml.read_text(encoding="utf-8")
        + yaml.safe_dump(
            {
                "test_entry_inferences": {
                    "schema_version": 1,
                    "language": "python",
                    "source": "static_test_invocation",
                    "entries": [
                        {
                            "test_id": "tests/test_demo.py::test_old",
                            "path": "pkg/demo.py",
                            "symbol": "pkg/demo.py:old",
                            "start_line": 1,
                            "end_line": 1,
                        }
                    ],
                }
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    task_count, batch_count = workflow.prepare(
        tasks_root,
        tmp_path / "work",
        batch_size=10,
        repo_cache_dir=tmp_path / "mirrors",
        checkout_root=tmp_path / "checkouts",
        materialize_missing_repos=False,
    )

    assert (task_count, batch_count) == (1, 1)
    queue_lines = (
        tmp_path / "work" / "review-queue.jsonl"
    ).read_text(encoding="utf-8").splitlines()
    assert len(queue_lines) == 1
    record = json.loads(queue_lines[0])
    assert record["task_key"] == "from-example/task-one"
    assert record["permitted_output_languages"] == ["python"]
    prompt = (tmp_path / "work" / "prompts" / "batch-000.md").read_text(
        encoding="utf-8"
    )
    assert "Read `docs/test-entry-inferences/README.zh.md`" in prompt
    assert "Ignore any existing `task.yaml.test_entry_inferences`" in prompt
    assert "needs_review" in prompt


def test_validation_rejects_incompatible_language_and_empty_entries(
    tmp_path: Path,
) -> None:
    tasks_root = tmp_path / "tasks"
    _task(tasks_root, "from-example/task-one")
    solution = tasks_root / "from-example" / "task-one" / "solution.sh"
    records = [
        {
            "schema_version": 1,
            "task_key": "from-example/task-one",
            "solution_sha256": workflow.applicability_workflow._sha256(solution),
            "decision": "inferred",
            "language": "java",
            "source": "static_test_invocation",
            "entries": [],
            "inspected_files": ["solution.sh", "task.yaml"],
            "evidence_paths": ["pkg/demo.py"],
            "rationale": "Local authority is proven.",
        }
    ]

    errors = workflow.validate_records(
        records,
        tasks_root,
        require_complete=True,
        require_final=True,
    )

    assert any("incompatible" in error for error in errors)
    assert any("requires non-empty entries" in error for error in errors)


@pytest.mark.parametrize(
    ("languages", "language", "path"),
    [
        (["javascript_typescript"], "javascript", "src/App.vue"),
        (["python"], "python", "bin/tool.bin"),
    ],
)
def test_validation_accepts_supported_noncanonical_entry_containers(
    tmp_path: Path,
    languages: list[str],
    language: str,
    path: str,
) -> None:
    tasks_root = tmp_path / "tasks"
    _task(tasks_root, "from-example/task-one", languages=languages)
    solution = tasks_root / "from-example" / "task-one" / "solution.sh"
    task_yaml = tasks_root / "from-example" / "task-one" / "task.yaml"
    records = [
        {
            "schema_version": 1,
            "task_key": "from-example/task-one",
            "solution_sha256": workflow.applicability_workflow._sha256(solution),
            "decision": "inferred",
            "language": language,
            "source": "static_test_invocation",
            "entries": [
                {
                    "test_id": "tests/test_demo.py::test_entry",
                    "path": path,
                    "symbol": f"{path}:entry",
                    "start_line": 1,
                    "end_line": 3,
                }
            ],
            "inspected_files": [
                solution.as_posix(),
                task_yaml.as_posix(),
            ],
            "evidence_paths": [path],
            "rationale": "Trusted tests directly expose the runtime-owned entry.",
        }
    ]

    errors = workflow.validate_records(
        records,
        tasks_root,
        require_complete=True,
        require_final=True,
    )

    assert errors == []


def test_merge_clear_and_apply_project_final_test_entry_inferences(
    tmp_path: Path,
) -> None:
    tasks_root = tmp_path / "tasks"
    _task(
        tasks_root,
        "from-example/task-one",
        task_yaml_prefix=(
            "instruction: test\n"
            "sentinel: true\n"
            "test_entry_inferences:\n"
            "  schema_version: 1\n"
            "  language: python\n"
            "  source: static_test_invocation\n"
            "  entries:\n"
            "    - test_id: tests/test_demo.py::test_old\n"
            "      path: pkg/old.py\n"
            "      symbol: pkg/old.py:old\n"
            "      start_line: 1\n"
            "      end_line: 1\n"
        ),
    )
    task_yaml = tasks_root / "from-example" / "task-one" / "task.yaml"
    solution = tasks_root / "from-example" / "task-one" / "solution.sh"
    batch = tmp_path / "batch.json"
    batch.write_text(
        json.dumps(
            {
                "annotations": [
                    {
                        "schema_version": 1,
                        "task_key": "from-example/task-one",
                        "solution_sha256": workflow.applicability_workflow._sha256(
                            solution
                        ),
                        "decision": "inferred",
                        "language": "python",
                        "source": "static_test_invocation",
                        "entries": [
                            {
                                "test_id": "tests/test_demo.py::test_entry",
                                "path": "pkg/demo.py",
                                "symbol": "pkg/demo.py:entry",
                                "start_line": 3,
                                "end_line": 7,
                            },
                            {
                                "test_id": "tests/test_demo.py::test_entry",
                                "path": "pkg/demo.py",
                                "symbol": "pkg/demo.py:entry",
                                "start_line": 3,
                                "end_line": 7,
                            },
                            {
                                "test_id": "tests/test_demo.py::test_alpha",
                                "path": "pkg/demo.py",
                                "symbol": "pkg/demo.py:alpha",
                                "start_line": 1,
                                "end_line": 2,
                            },
                        ],
                        "inspected_files": [
                            task_yaml.as_posix(),
                            solution.as_posix(),
                            task_yaml.as_posix(),
                        ],
                        "evidence_paths": [
                            "tests/test_demo.py",
                            "pkg/demo.py",
                            "pkg/demo.py",
                        ],
                        "rationale": (
                            "Trusted tests directly call local production entries."
                        ),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "manifest.jsonl"

    assert workflow.merge([batch], output) == 1
    merged = json.loads(output.read_text(encoding="utf-8"))
    assert merged["evidence_paths"] == ["pkg/demo.py", "tests/test_demo.py"]
    assert merged["entries"] == [
        {
            "test_id": "tests/test_demo.py::test_alpha",
            "path": "pkg/demo.py",
            "symbol": "pkg/demo.py:alpha",
            "start_line": 1,
            "end_line": 2,
        },
        {
            "test_id": "tests/test_demo.py::test_entry",
            "path": "pkg/demo.py",
            "symbol": "pkg/demo.py:entry",
            "start_line": 3,
            "end_line": 7,
        },
    ]

    total, cleared = workflow.clear_existing_metadata(tasks_root)
    cleared_yaml = yaml.safe_load(task_yaml.read_text(encoding="utf-8"))
    assert (total, cleared) == (1, 1)
    assert "test_entry_inferences" not in cleared_yaml

    total, written = workflow.apply_manifest(output, tasks_root)
    applied = yaml.safe_load(task_yaml.read_text(encoding="utf-8"))

    assert (total, written) == (1, 1)
    assert applied["sentinel"] is True
    assert applied["test_entry_inferences"] == {
        "schema_version": 1,
        "language": "python",
        "source": "static_test_invocation",
        "entries": [
            {
                "test_id": "tests/test_demo.py::test_alpha",
                "path": "pkg/demo.py",
                "symbol": "pkg/demo.py:alpha",
                "start_line": 1,
                "end_line": 2,
            },
            {
                "test_id": "tests/test_demo.py::test_entry",
                "path": "pkg/demo.py",
                "symbol": "pkg/demo.py:entry",
                "start_line": 3,
                "end_line": 7,
            },
        ],
    }


def test_apply_rejects_non_final_manifest_record(tmp_path: Path) -> None:
    tasks_root = tmp_path / "tasks"
    _task(tasks_root, "from-example/task-one")
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "task_key": "from-example/task-one",
                "solution_sha256": "a" * 64,
                "decision": "needs_review",
                "language": None,
                "source": None,
                "entries": [],
                "inspected_files": [],
                "evidence_paths": ["<unavailable>"],
                "rationale": "Static proof is unavailable.",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(workflow.WorkflowError, match="cannot apply invalid manifest"):
        workflow.apply_manifest(manifest, tasks_root)
