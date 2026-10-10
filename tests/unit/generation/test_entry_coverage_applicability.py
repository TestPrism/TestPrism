from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from scripts.generation import entry_coverage_applicability as workflow


def _task(
    root: Path,
    key: str,
    *,
    task_yaml_text: str = "instruction: test\n",
    solution: str = "echo ok\n",
    run_tests: str = "pytest -q\n",
) -> tuple[Path, Path, Path, Path]:
    task_dir = root / key
    task_dir.mkdir(parents=True)
    task_yaml = task_dir / "task.yaml"
    task_yaml.write_text(task_yaml_text, encoding="utf-8")
    solution_sh = task_dir / "solution.sh"
    solution_sh.write_text(solution, encoding="utf-8")
    run_tests_sh = task_dir / "run-tests.sh"
    run_tests_sh.write_text(run_tests, encoding="utf-8")
    return task_dir, task_yaml, solution_sh, run_tests_sh


def test_queue_uses_local_repo_root_and_discovers_checked_in_tests(
    tmp_path: Path,
) -> None:
    tasks_root = tmp_path / "tasks"
    task_dir, task_yaml, solution_sh, run_tests_sh = _task(
        tasks_root,
        "from-example/task-one",
        solution="""#!/bin/bash
cat <<'PATCH' >/tmp/fix.diff
diff --git a/workspace/pkg/main.py b/workspace/pkg/main.py
--- a/workspace/pkg/main.py
+++ b/workspace/pkg/main.py
PATCH
""",
    )
    (task_dir / "workspace" / "pkg").mkdir(parents=True)
    (task_dir / "workspace" / "pkg" / "main.py").write_text(
        "def run():\n    return 1\n",
        encoding="utf-8",
    )
    (task_dir / "tests").mkdir()
    (task_dir / "tests" / "test_main.py").write_text(
        (
            "from workspace.pkg.main import run\n\n\n"
            "def test_run():\n"
            "    assert run() == 1\n"
        ),
        encoding="utf-8",
    )

    record = workflow.build_queue_record(
        tasks_root,
        "from-example/task-one",
        task_dir,
        task_yaml,
        solution_sh,
        run_tests_sh,
        None,
        repo_cache_dir=tmp_path / "mirrors",
        checkout_root=tmp_path / "checkouts",
        materialize_missing_repos=False,
    )

    assert record["review_repo_status"] == "task_local"
    assert record["review_repo_root"] == task_dir.as_posix()
    assert record["trusted_test_paths"] == ["tests/test_main.py"]
    assert record["language_hints"] == ["python"]
    assert record["static_hint"] == "inspect_trusted_test_surface"


def test_prepare_creates_small_independent_prompt_batches(tmp_path: Path) -> None:
    tasks_root = tmp_path / "tasks"
    for index in range(3):
        _task(tasks_root, f"from-example/task-{index}")

    task_count, batch_count = workflow.prepare(
        tasks_root,
        tmp_path / "work",
        batch_size=2,
        repo_cache_dir=tmp_path / "mirrors",
        checkout_root=tmp_path / "checkouts",
        materialize_missing_repos=False,
    )

    assert (task_count, batch_count) == (3, 2)
    queue_lines = (
        tmp_path / "work" / "review-queue.jsonl"
    ).read_text(encoding="utf-8").splitlines()
    assert len(queue_lines) == 3
    prompts = sorted((tmp_path / "work" / "prompts").glob("batch-*.md"))
    assert len(prompts) == 2
    prompt = prompts[0].read_text(encoding="utf-8")
    assert "Read `docs/entry-coverage-applicability/REPORT.zh.md`" in prompt
    assert "CLI, subprocess, service, HTTP" in prompt
    assert "routing aids only. Never make a" in prompt


def test_validation_rejects_stale_hash_and_needs_review_in_final(
    tmp_path: Path,
) -> None:
    tasks_root = tmp_path / "tasks"
    _task(tasks_root, "from-example/task-one")
    record = {
        "schema_version": 1,
        "task_key": "from-example/task-one",
        "solution_sha256": "0" * 64,
        "decision": "needs_review",
        "reason_code": "post_fix_tree_required",
        "languages": [],
        "inspected_files": [],
        "evidence_paths": ["<unavailable>"],
        "rationale": "The exact post-fix repository is required.",
    }

    errors = workflow.validate_records(
        [record], tasks_root, require_complete=True, require_final=True
    )

    assert any("stale or invalid" in error for error in errors)
    assert any("needs_review is not allowed" in error for error in errors)


def test_validation_accepts_task_local_required_inspection_aliases(
    tmp_path: Path,
) -> None:
    tasks_root = tmp_path / "tasks"
    _task(tasks_root, "from-example/task-one")
    solution = tasks_root / "from-example" / "task-one" / "solution.sh"
    record = {
        "schema_version": 1,
        "task_key": "from-example/task-one",
        "solution_sha256": workflow._sha256(solution),
        "decision": "not_applicable",
        "reason_code": "artifact_only_verifier",
        "languages": [],
        "inspected_files": ["solution.sh", "task.yaml"],
        "evidence_paths": ["answer.txt"],
        "rationale": "Trusted verification inspects only the final artifact.",
    }

    errors = workflow.validate_records(
        [record], tasks_root, require_complete=True, require_final=True
    )

    assert errors == []


def test_merge_and_apply_project_final_field_into_task_yaml(tmp_path: Path) -> None:
    tasks_root = tmp_path / "tasks"
    task_dir, task_yaml, solution_sh, _run_tests_sh = _task(
        tasks_root,
        "from-example/task-one",
        task_yaml_text="instruction: test\nsentinel: true\n",
    )
    batch = tmp_path / "batch.json"
    batch.write_text(
        json.dumps(
            {
                "annotations": [
                    {
                        "schema_version": 1,
                        "task_key": "from-example/task-one",
                        "solution_sha256": workflow._sha256(solution_sh),
                        "decision": "applicable",
                        "reason_code": "direct_local_entry_surface",
                        "languages": ["python", "python"],
                        "inspected_files": [
                            task_yaml.as_posix(),
                            solution_sh.as_posix(),
                            task_yaml.as_posix(),
                        ],
                        "evidence_paths": [
                            "pkg/api.py",
                            "tests/test_api.py",
                            "pkg/api.py",
                        ],
                        "rationale": (
                            "Trusted tests directly exercise a local Python entry."
                        ),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "manifest.jsonl"

    assert workflow.merge([batch], output) == 1
    record = json.loads(output.read_text(encoding="utf-8"))
    assert record["languages"] == ["python"]
    assert record["evidence_paths"] == ["pkg/api.py", "tests/test_api.py"]

    total, written = workflow.apply_manifest(output, tasks_root)
    applied = yaml.safe_load(task_yaml.read_text(encoding="utf-8"))

    assert (total, written) == (1, 1)
    assert applied["sentinel"] is True
    assert applied["entry_coverage_applicability"] == {
        "schema_version": 1,
        "solution_sha256": workflow._sha256(solution_sh),
        "decision": "applicable",
        "reason_code": "direct_local_entry_surface",
        "languages": ["python"],
        "evidence_paths": ["pkg/api.py", "tests/test_api.py"],
    }
    assert task_dir.exists()


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
                "reason_code": "post_fix_tree_required",
                "languages": [],
                "inspected_files": [],
                "evidence_paths": ["<unavailable>"],
                "rationale": "The exact post-fix repository is required.",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(workflow.WorkflowError, match="cannot apply invalid manifest"):
        workflow.apply_manifest(manifest, tasks_root)


def test_prepare_can_filter_to_a_task_list_and_audit_writes_rerun_lists(
    tmp_path: Path,
) -> None:
    tasks_root = tmp_path / "tasks"
    _task(tasks_root, "from-example/task-one")
    _task(tasks_root, "from-example/task-two")
    task_list = tmp_path / "needs-review.tasklist"
    task_list.write_text("from-example/task-two\n", encoding="utf-8")

    task_count, batch_count = workflow.prepare(
        tasks_root,
        tmp_path / "work",
        batch_size=20,
        repo_cache_dir=tmp_path / "mirrors",
        checkout_root=tmp_path / "checkouts",
        materialize_missing_repos=False,
        selected_task_keys=workflow._selected_task_keys([], task_list),
    )

    assert (task_count, batch_count) == (1, 1)
    queue = [
        json.loads(line)
        for line in (
            tmp_path / "work" / "review-queue.jsonl"
        ).read_text(encoding="utf-8").splitlines()
    ]
    assert [record["task_key"] for record in queue] == ["from-example/task-two"]

    solution = tasks_root / "from-example" / "task-two" / "solution.sh"
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "task_key": "from-example/task-two",
                "solution_sha256": workflow._sha256(solution),
                "decision": "needs_review",
                "reason_code": "post_fix_tree_required",
                "languages": [],
                "inspected_files": ["task.yaml", "solution.sh"],
                "evidence_paths": ["<unavailable>"],
                "rationale": "The exact post-fix repository is required.",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    summary = workflow.audit_manifest(
        manifest,
        tasks_root,
        tmp_path / "audit",
        require_complete=True,
        require_final=True,
    )

    assert summary["needs_review_count"] == 1
    assert summary["missing_from_manifest_count"] == 1
    assert (tmp_path / "audit" / "needs_review.tasklist").read_text(
        encoding="utf-8"
    ) == "from-example/task-two\n"
    assert (tmp_path / "audit" / "rerun.tasklist").read_text(
        encoding="utf-8"
    ) == "from-example/task-one\nfrom-example/task-two\n"
