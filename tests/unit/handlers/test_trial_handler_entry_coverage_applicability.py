from __future__ import annotations

import pytest
from pydantic import ValidationError

from testprism.handlers.trial_handler import Task


def test_task_accepts_final_entry_coverage_applicability() -> None:
    task = Task.model_validate(
        {
            "instruction": "demo",
            "difficulty": "easy",
            "entry_coverage_applicability": {
                "schema_version": 1,
                "solution_sha256": "a" * 64,
                "decision": "applicable",
                "reason_code": "direct_local_entry_surface",
                "languages": ["python"],
                "evidence_paths": ["pkg/demo.py", "tests/test_demo.py"],
            },
        }
    )

    assert task.entry_coverage_applicability is not None
    assert task.entry_coverage_applicability.decision == "applicable"
    assert task.entry_coverage_applicability.languages == ["python"]


def test_task_rejects_not_applicable_languages() -> None:
    with pytest.raises(ValidationError, match="must not declare languages"):
        Task.model_validate(
            {
                "instruction": "demo",
                "difficulty": "easy",
                "entry_coverage_applicability": {
                    "schema_version": 1,
                    "solution_sha256": "a" * 64,
                    "decision": "not_applicable",
                    "reason_code": "artifact_only_verifier",
                    "languages": ["python"],
                    "evidence_paths": ["answer.txt"],
                },
            }
        )


@pytest.mark.parametrize(
    "language",
    ["python", "javascript", "typescript", "java", "cpp", "go", "rust"],
)
def test_task_accepts_supported_test_entry_inference_languages(language: str) -> None:
    task = Task.model_validate(
        {
            "instruction": "demo",
            "difficulty": "easy",
            "test_entry_inferences": {
                "schema_version": 1,
                "language": language,
                "source": "static_test_invocation",
                "entries": [
                    {
                        "test_id": "tests/test_demo.py::test_entry",
                        "path": "pkg/demo.py",
                        "symbol": "pkg.demo:entry",
                        "start_line": 3,
                        "end_line": 7,
                    }
                ],
            },
        }
    )

    assert task.test_entry_inferences is not None
    assert task.test_entry_inferences.language == language
    assert task.test_entry_inferences.entries[0].path == "pkg/demo.py"


def test_task_test_entry_inferences_remains_optional_for_historical_tasks() -> None:
    task = Task.model_validate(
        {
            "instruction": "demo",
            "difficulty": "easy",
        }
    )

    assert task.test_entry_inferences is None


def test_task_test_entry_inferences_preserve_path_and_span_validation() -> None:
    with pytest.raises(
        ValidationError, match=r"path must not contain empty, '\.' or '\.\.' segments"
    ):
        Task.model_validate(
            {
                "instruction": "demo",
                "difficulty": "easy",
                "test_entry_inferences": {
                    "schema_version": 1,
                    "language": "go",
                    "source": "static_test_invocation",
                    "entries": [
                        {
                            "test_id": "tests/test_demo.py::test_entry",
                            "path": "../pkg/demo.go",
                            "symbol": "pkg.demo:entry",
                            "start_line": 3,
                            "end_line": 7,
                        }
                    ],
                },
            }
        )

    with pytest.raises(
        ValidationError, match="greater than or equal to start_line"
    ):
        Task.model_validate(
            {
                "instruction": "demo",
                "difficulty": "easy",
                "test_entry_inferences": {
                    "schema_version": 1,
                    "language": "rust",
                    "source": "static_test_invocation",
                    "entries": [
                        {
                            "test_id": "tests/test_demo.py::test_entry",
                            "path": "pkg/demo.rs",
                            "symbol": "pkg.demo:entry",
                            "start_line": 7,
                            "end_line": 3,
                        }
                    ],
                },
            }
        )
