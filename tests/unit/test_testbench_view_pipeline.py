import importlib.util
import sys
from pathlib import Path

import yaml

CONVERTERS_DIR = Path(__file__).resolve().parents[2] / "converters"
TB_TO_TESTBENCH_PATH = CONVERTERS_DIR / "tb_to_testbench.py"
FILTER_COMPATIBLE_PATH = CONVERTERS_DIR / "filter_compatible.py"


def _load_module(module_name: str, module_path: Path):
    if str(CONVERTERS_DIR) not in sys.path:
        sys.path.insert(0, str(CONVERTERS_DIR))

    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_runtime_task(tmp_path: Path, *, workdir: str = "/workspace/repo") -> Path:
    task_dir = tmp_path / "demo-task"
    task_dir.mkdir()
    (task_dir / "tests").mkdir()
    (task_dir / "task.yaml").write_text(
        f"""
instruction: |
  Fix the failing demo behavior.
difficulty: medium
parser_name: swebench
workdir: {workdir}
""".strip()
        + "\n"
    )
    (task_dir / "Dockerfile").write_text("FROM ubuntu:24.04\nWORKDIR /workspace/repo\n")
    (task_dir / "docker-compose.yaml").write_text(
        """
services:
  client:
    image: ${T_BENCH_TASK_DOCKER_CLIENT_IMAGE_NAME}
""".strip()
        + "\n"
    )
    (task_dir / "run-tests.sh").write_text("#!/bin/bash\nexit 1\n")
    (task_dir / "solution.sh").write_text("#!/bin/bash\nexit 0\n")
    return task_dir


def test_tb_to_testbench_preserves_instruction(tmp_path):
    module = _load_module("tb_to_testbench_module", TB_TO_TESTBENCH_PATH)
    task_dir = _write_runtime_task(tmp_path, workdir="/workspace/repo")
    output_dir = tmp_path / "converted"

    converted = module.convert_task(task_dir, output_dir)

    assert converted is True
    task_yaml = yaml.safe_load((output_dir / "demo-task" / "task.yaml").read_text())
    assert task_yaml["tags"] == ["testbench"]
    assert task_yaml["instruction"] == "Fix the failing demo behavior.\n"


def test_filter_compatible_reports_capabilities_from_runtime_contract(tmp_path):
    module = _load_module("filter_compatible_module", FILTER_COMPATIBLE_PATH)
    task_dir = _write_runtime_task(tmp_path, workdir="/testbed")

    report = module.inspect_testbench_compatibility(task_dir)

    assert report["compatible"] is True
    assert report["parser_name"] == "swebench"
    assert report["workdir"] == "/testbed"
    assert report["capabilities"]["has_shell_solution"] is True
    assert report["capabilities"]["supports_parser"] is True
    assert report["capabilities"]["declares_absolute_workdir"] is True
    assert report["capabilities"]["can_write_agent_tests_under_workdir"] is True
