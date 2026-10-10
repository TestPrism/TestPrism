import importlib.util
import sys
from pathlib import Path

import pytest
import yaml


ADAPTER_DIR = Path(__file__).resolve().parents[2] / "adapters" / "algotune"
ADAPTER_PATH = ADAPTER_DIR / "adapter.py"


def _load_algotune_adapter_module():
    spec = importlib.util.spec_from_file_location("algotune_adapter", ADAPTER_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_old_format_task(task_dir: Path) -> None:
    (task_dir / "tests").mkdir(parents=True)
    instruction = """**TASK DESCRIPTION:**
Find a correct result for the demo optimization problem.

Below is the reference implementation:
```python
def is_solution(problem, solution):
    return solution == problem["answer"]
```
"""
    (task_dir / "task.yaml").write_text(
        yaml.safe_dump(
            {
                "instruction": instruction,
                "category": "optimization",
                "tags": ["algotune"],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (task_dir / "solver.py").write_text(
        "class Solver:\n    def solve(self, problem, **kwargs):\n        return problem['answer']\n",
        encoding="utf-8",
    )
    (task_dir / "tests" / "test_outputs.py").write_text(
        "PROBLEM_SIZE = 73\n",
        encoding="utf-8",
    )
    (task_dir / "tests" / "evaluator.py").write_text(
        "# legacy evaluator\n",
        encoding="utf-8",
    )
    (task_dir / ".best_solver_speedup.txt").write_text("2.5\n", encoding="utf-8")


def test_convert_task_migrates_old_layout_without_leaking_grading_assets(tmp_path):
    module = _load_algotune_adapter_module()
    task_dir = tmp_path / "algotune-demo"
    _write_old_format_task(task_dir)

    module.convert_task(task_dir, module.read_disk(task_dir))

    task = yaml.safe_load((task_dir / "task.yaml").read_text(encoding="utf-8"))
    instruction = task["instruction"]
    assert "Find a correct result for the demo optimization problem." in instruction
    assert "def is_solution(problem, solution):" in instruction
    assert "Problem size (n)**: 73" in instruction
    assert "Target speedup over baseline**: ~2.50x" in instruction
    assert task["category"] == "performance-testing"
    assert task["tags"] == ["algotune", "test-writing"]

    assert "COPY solver.py ." in (task_dir / "Dockerfile").read_text(encoding="utf-8")
    assert (task_dir / "run-tests.sh").read_text(encoding="utf-8").endswith("exit 0\n")
    assert (task_dir / "run-tests.sh").stat().st_mode & 0o111
    assert not (task_dir / "tests").exists()
    assert not (task_dir / ".best_solver_speedup.txt").exists()


def test_convert_task_is_idempotent_after_legacy_inputs_are_removed(tmp_path):
    module = _load_algotune_adapter_module()
    task_dir = tmp_path / "algotune-demo"
    _write_old_format_task(task_dir)

    module.convert_task(task_dir, module.read_disk(task_dir))
    first_task_yaml = (task_dir / "task.yaml").read_text(encoding="utf-8")

    module.convert_task(task_dir, module.read_disk(task_dir))

    assert (task_dir / "task.yaml").read_text(encoding="utf-8") == first_task_yaml


def test_convert_task_requires_task_yaml(tmp_path):
    module = _load_algotune_adapter_module()

    with pytest.raises(FileNotFoundError, match="missing task.yaml"):
        module.convert_task(
            tmp_path / "missing-task",
            {"task_yaml": None, "test_outputs": None, "speedup": None},
        )
