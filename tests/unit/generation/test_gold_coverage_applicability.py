from __future__ import annotations

import json
from pathlib import Path

from scripts.generation import gold_coverage_applicability as workflow


SCHEMA_PATH = (
    workflow.REPOSITORY_ROOT
    / "docs"
    / "gold-coverage-applicability"
    / "review-output.schema.json"
)


def _task(root: Path, key: str, solution: str) -> tuple[Path, Path]:
    task_dir = root / key
    task_dir.mkdir(parents=True)
    task_yaml = task_dir / "task.yaml"
    task_yaml.write_text("instruction: test\n", encoding="utf-8")
    solution_sh = task_dir / "solution.sh"
    solution_sh.write_text(solution, encoding="utf-8")
    return task_yaml, solution_sh


def test_queue_extracts_supported_production_paths_and_excludes_tests(
    tmp_path: Path,
) -> None:
    tasks_root = tmp_path / "tasks"
    task_yaml, solution = _task(
        tasks_root,
        "from-example/task-one",
        """#!/bin/bash
cat <<'PATCH' >/tmp/fix.diff
diff --git a/pkg/main.py b/pkg/main.py
--- a/pkg/main.py
+++ b/pkg/main.py
diff --git a/tests/test_main.py b/tests/test_main.py
--- a/tests/test_main.py
+++ b/tests/test_main.py
PATCH
""",
    )

    record = workflow.build_queue_record(
        tasks_root, "from-example/task-one", task_yaml, solution
    )

    assert record["embedded_diff_paths"] == ["pkg/main.py", "tests/test_main.py"]
    assert record["source_path_hints"] == ["pkg/main.py"]
    assert record["language_hints"] == ["python"]
    assert record["static_hint"] == "likely_applicable"


def test_prepare_creates_small_independent_prompt_batches(tmp_path: Path) -> None:
    tasks_root = tmp_path / "tasks"
    for index in range(3):
        _task(tasks_root, f"from-example/task-{index}", "echo answer > /app/out.txt\n")

    task_count, batch_count = workflow.prepare(
        tasks_root, tmp_path / "work", batch_size=2
    )

    assert (task_count, batch_count) == (3, 2)
    queue_lines = (
        tmp_path / "work" / "review-queue.jsonl"
    ).read_text().splitlines()
    assert len(queue_lines) == 3
    prompts = sorted((tmp_path / "work" / "prompts").glob("batch-*.md"))
    assert len(prompts) == 2
    prompt = prompts[0].read_text()
    assert "Do not execute the gold" in prompt
    assert "Never infer a decision from `task_key`" in prompt
    assert "If either required file cannot be read" in prompt


def test_prompt_record_caps_long_path_evidence() -> None:
    record = {
        "embedded_diff_paths": [f"pkg/file-{index}.py" for index in range(25)],
        "source_path_hints": [f"pkg/source-{index}.py" for index in range(21)],
    }

    compact = workflow._prompt_record(record)

    assert len(compact["embedded_diff_paths"]) == 20
    assert compact["embedded_diff_paths_count"] == 25
    assert len(compact["source_path_hints"]) == 20
    assert compact["source_path_hints_count"] == 21


def test_validation_rejects_stale_hash_and_needs_review_in_final(
    tmp_path: Path,
) -> None:
    tasks_root = tmp_path / "tasks"
    _task(tasks_root, "from-example/task-one", "echo ok\n")
    record = {
        "schema_version": 1,
        "task_key": "from-example/task-one",
        "solution_sha256": "0" * 64,
        "decision": "needs_review",
        "reason_code": "canonical_diff_required",
        "languages": [],
        "inspected_files": [],
        "evidence_paths": ["/app/output"],
        "rationale": "The static solution does not reveal the final effect.",
    }

    errors = workflow.validate_records(
        [record], tasks_root, require_complete=True, require_final=True
    )

    assert any("stale or invalid" in error for error in errors)
    assert any("needs_review is not allowed" in error for error in errors)


def test_merge_accepts_schema_constrained_codex_batch_output(tmp_path: Path) -> None:
    batch = tmp_path / "batch.json"
    batch.write_text(
        json.dumps(
            {
                "annotations": [
                    {
                        "schema_version": 1,
                        "task_key": "from-example/task-one",
                        "solution_sha256": "a" * 64,
                        "decision": "applicable",
                        "reason_code": "production_source_only",
                        "languages": ["python", "c_cpp", "python"],
                        "inspected_files": [
                            "from-example/task-one/task.yaml",
                            "from-example/task-one/solution.sh",
                            "from-example/task-one/task.yaml",
                        ],
                        "evidence_paths": ["pkg/z.py", "pkg/main.py", "pkg/z.py"],
                        "rationale": (
                            "The gold patch changes the production implementation."
                        ),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "manifest.jsonl"

    assert workflow.merge([batch], output) == 1
    merged = json.loads(output.read_text())
    assert merged["decision"] == "applicable"
    assert merged["languages"] == ["c_cpp", "python"]
    assert merged["inspected_files"] == [
        "from-example/task-one/solution.sh",
        "from-example/task-one/task.yaml",
    ]
    assert merged["evidence_paths"] == ["pkg/main.py", "pkg/z.py"]


def test_merge_clears_languages_for_non_applicable_annotation(tmp_path: Path) -> None:
    batch = tmp_path / "batch.json"
    batch.write_text(
        json.dumps(
            {
                "annotations": [
                    {
                        "schema_version": 1,
                        "task_key": "from-example/task-one",
                        "solution_sha256": "a" * 64,
                        "decision": "not_applicable",
                        "reason_code": "essential_non_source_change",
                        "languages": ["python"],
                        "inspected_files": [],
                        "evidence_paths": ["build.sh"],
                        "rationale": "The build script is essential to the gold effect.",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "manifest.jsonl"

    workflow.merge([batch], output)

    assert json.loads(output.read_text())["languages"] == []


def test_merge_applies_explicit_manual_override(tmp_path: Path) -> None:
    base = tmp_path / "base.json"
    override = tmp_path / "override.json"
    common = {
        "schema_version": 1,
        "task_key": "from-example/task-one",
        "solution_sha256": "a" * 64,
        "languages": [],
        "inspected_files": [],
        "evidence_paths": ["<unavailable>"],
    }
    base.write_text(
        json.dumps(
            {
                "annotations": [
                    {
                        **common,
                        "decision": "needs_review",
                        "reason_code": "canonical_diff_required",
                        "rationale": "The canonical diff is required.",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    override.write_text(
        json.dumps(
            {
                "annotations": [
                    {
                        **common,
                        "decision": "applicable",
                        "reason_code": "production_source_only",
                        "languages": ["javascript_typescript"],
                        "evidence_paths": ["src/main.ts"],
                        "rationale": "The canonical patch changes TypeScript source.",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    output = tmp_path / "manifest.jsonl"

    workflow.merge([base], output, overrides=[override])

    merged = json.loads(output.read_text())
    assert merged["decision"] == "applicable"
    assert merged["evidence_paths"] == ["src/main.ts"]


def test_validation_requires_both_input_files_for_final_decision(
    tmp_path: Path,
) -> None:
    tasks_root = tmp_path / "tasks"
    _task(tasks_root, "from-example/task-one", "echo ok\n")
    solution = tasks_root / "from-example/task-one/solution.sh"
    record = {
        "schema_version": 1,
        "task_key": "from-example/task-one",
        "solution_sha256": workflow._sha256(solution),
        "decision": "not_applicable",
        "reason_code": "artifact_or_answer",
        "languages": [],
        "inspected_files": [],
        "evidence_paths": ["/app/answer.txt"],
        "rationale": "The gold writes an answer artifact rather than source code.",
    }

    errors = workflow.validate_records(
        [record], tasks_root, require_complete=True, require_final=True
    )

    assert any("must attest inspection" in error for error in errors)


def test_validation_allows_additional_inspected_evidence(tmp_path: Path) -> None:
    tasks_root = tmp_path / "tasks"
    task_yaml, solution = _task(
        tasks_root, "from-example/task-one", "echo answer > /app/answer.txt\n"
    )
    record = {
        "schema_version": 1,
        "task_key": "from-example/task-one",
        "solution_sha256": workflow._sha256(solution),
        "decision": "not_applicable",
        "reason_code": "artifact_or_answer",
        "languages": [],
        "inspected_files": sorted(
            [
                workflow._display_path(task_yaml),
                workflow._display_path(solution),
                "/extra/tests/test_outputs.py",
            ]
        ),
        "evidence_paths": ["/app/answer.txt"],
        "rationale": "The gold writes an answer artifact rather than source code.",
    }

    errors = workflow.validate_records(
        [record], tasks_root, require_complete=True, require_final=True
    )

    assert errors == []


def test_every_structured_output_property_has_an_explicit_type() -> None:
    """OpenAI's structured-output subset rejects implicit const/enum types."""

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    def visit(node: object, location: str = "$") -> None:
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                for name, property_schema in properties.items():
                    assert isinstance(property_schema, dict)
                    assert "type" in property_schema, f"{location}.{name} lacks type"
            for name, child in node.items():
                visit(child, f"{location}.{name}")
        elif isinstance(node, list):
            for index, child in enumerate(node):
                visit(child, f"{location}[{index}]")

    visit(schema)

    # Keep the CLI schema within OpenAI's conservative Structured Outputs
    # subset. Stronger uniqueness and non-empty checks remain in our local
    # deterministic validator.
    rendered = json.dumps(schema)
    for unsupported_keyword in ("$schema", "const", "uniqueItems", "minLength"):
        assert f'"{unsupported_keyword}"' not in rendered
