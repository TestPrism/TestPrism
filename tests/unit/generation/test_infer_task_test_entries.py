from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

MODULE_PATH = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "generation"
    / "infer_task_test_entries.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "infer_task_test_entries",
        MODULE_PATH,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_literal_solution_extractor_preserves_exact_payload():
    module = _load_module()
    script = """#!/bin/bash
# literal writer
cat > /app/solution.py << 'EOL'
def answer():
    return "$HOME"
EOL
"""

    assert module.extract_literal_solution(script) == (
        'def answer():\n    return "$HOME"\n'
    )


def test_literal_solution_extractor_rejects_unsafe_shell_forms():
    module = _load_module()
    cases = [
        "cat > /app/solution.py << EOL\ndef answer(): pass\nEOL\n",
        'cat > /app/solution.py << "EOL"\ndef answer(): pass\nEOL\n',
        "cat > /app/../solution.py << 'EOL'\ndef answer(): pass\nEOL\n",
        "cat > /app/solution.py << 'EOL'\ndef answer(): pass\n",
        (
            "cat > /app/solution.py << 'EOL'\ndef one(): pass\nEOL\n"
            "cat > /app/solution.py << 'EOL'\ndef two(): pass\nEOL\n"
        ),
        (
            "cat > /app/solution.py << 'EOL'\ndef answer(): pass\nEOL\n"
            "python /app/solution.py\n"
        ),
    ]

    assert all(module.extract_literal_solution(script) is None for script in cases)


def _write_literal_solution_task(
    task_dir: Path,
    *,
    solution: str,
    prompt: str,
) -> None:
    (task_dir / "tests").mkdir(parents=True)
    (task_dir / "task.yaml").write_text("sentinel: true\n", encoding="utf-8")
    (task_dir / "solution.sh").write_text(
        "#!/bin/bash\ncat > /app/solution.py << 'EOL'\n" + solution + "EOL\n",
        encoding="utf-8",
    )
    (task_dir / "tests" / "test_outputs.py").write_text(
        "def test_solution_correct():\n"
        f"    task_data = {json.dumps({'prompt': prompt})}\n"
        "    problem_text = task_data['prompt']\n"
        "    function_name = None\n"
        "    for line in reversed(problem_text.strip().split('\\n')):\n"
        "        if line.strip().startswith('def '):\n"
        "            function_name = line.split('def ')[1].split('(')[0].strip()\n"
        "            break\n"
        "    solution_function = getattr(solution, function_name)\n"
        "    helper(1)\n"
        "    solution_function(1)\n",
        encoding="utf-8",
    )


def test_process_literal_solution_resolves_prompt_alias_and_excludes_helper(tmp_path):
    module = _load_module()
    task_dir = tmp_path / "from-evoeval" / "32"
    solution = (
        "def helper(value):\n"
        "    return value + 1\n\n"
        "def target(value):\n"
        "    return helper(value)\n"
    )
    prompt = (
        "def helper(value):\n    return value\n\ndef target(value):\n    return value\n"
    )
    _write_literal_solution_task(task_dir, solution=solution, prompt=prompt)

    written = module.process_task(
        task_dir,
        repo_cache_dir=tmp_path / "mirrors",
        checkout_root=tmp_path / "checkouts",
        check=False,
        skip_existing=False,
    )
    initial_bytes = (task_dir / "task.yaml").read_bytes()
    checked = module.process_task(
        task_dir,
        repo_cache_dir=tmp_path / "mirrors",
        checkout_root=tmp_path / "checkouts",
        check=True,
        skip_existing=False,
    )
    metadata = yaml.safe_load(initial_bytes)["test_entry_inferences"]

    assert written == ("written", 0)
    assert checked == ("checked", 0)
    assert (task_dir / "task.yaml").read_bytes() == initial_bytes
    assert metadata["language"] == "python"
    assert metadata["entries"] == [
        {
            "test_id": "tests/test_outputs.py::test_solution_correct",
            "path": "solution.py",
            "symbol": "solution.py:target",
            "start_line": 4,
            "end_line": 5,
        }
    ]


def test_literal_solution_rejects_ambiguous_payload_target(tmp_path):
    module = _load_module()
    task_dir = tmp_path / "from-evoeval" / "ambiguous"
    _write_literal_solution_task(
        task_dir,
        solution="def target():\n    return 1\ndef target():\n    return 2\n",
        prompt="def target():\n    return 1\n",
    )

    assert module.literal_solution_sources(task_dir) is None
    assert module.process_task(
        task_dir,
        repo_cache_dir=tmp_path / "mirrors",
        checkout_root=tmp_path / "checkouts",
        check=False,
        skip_existing=False,
    ) == ("skipped-unsupported-language", 0)


def test_literal_solution_rejects_nonliteral_prompt(tmp_path):
    module = _load_module()
    task_dir = tmp_path / "from-evoeval" / "dynamic-prompt"
    _write_literal_solution_task(
        task_dir,
        solution="def target(value):\n    return value\n",
        prompt="def target(value):\n    return value\n",
    )
    test_path = task_dir / "tests" / "test_outputs.py"
    test_path.write_text(
        test_path.read_text(encoding="utf-8").replace(
            'task_data = {"prompt": "def target(value):\\n    return value\\n"}',
            "prompt = 'def target(value):\\n    return value\\n'\n"
            "    task_data = {'prompt': prompt}",
        ),
        encoding="utf-8",
    )

    assert module.literal_solution_sources(task_dir) is None


def _write_quixbugs_task(task_dir: Path) -> None:
    (task_dir / "tests").mkdir(parents=True)
    (task_dir / "task.yaml").write_text("sentinel: true\n", encoding="utf-8")
    (task_dir / "solution.sh").write_text(
        "#!/bin/bash\n"
        "set -e\n\n"
        "# literal fixed program\n"
        "cat > /app/fixed_shunting_yard.py << 'EOL'\n"
        "def shunting_yard(tokens):\n"
        "    return list(reversed(tokens))\n"
        "EOL\n\n"
        'echo "Solution written to fixed_shunting_yard.py"\n',
        encoding="utf-8",
    )
    (task_dir / "tests" / "test_outputs.py").write_text(
        "from test_utils import import_program\n"
        "from diff_helper import count_diff_lines\n\n"
        "program_under_test = import_program(\n"
        "    'fixed_shunting_yard', fallback_program_name='shunting_yard'\n"
        ")\n\n"
        "def test_one_line_change():\n"
        "    count_diff_lines('old', 'new')\n\n"
        "def test_shunting_yard():\n"
        "    assert program_under_test([1, 2]) == [2, 1]\n",
        encoding="utf-8",
    )
    (task_dir / "tests" / "test_utils.py").write_text(
        "import importlib\n\n"
        "def import_program(program_name, fallback_program_name=None):\n"
        "    module_name = program_name\n"
        "    try:\n"
        "        module = importlib.import_module(program_name)\n"
        "    except ModuleNotFoundError:\n"
        "        if fallback_program_name is None:\n"
        "            raise\n"
        "        module_name = fallback_program_name\n"
        "        module = importlib.import_module(fallback_program_name)\n"
        "    func_name = module_name.replace('fixed_', '')\n"
        "    return getattr(module, func_name)\n",
        encoding="utf-8",
    )


def test_process_quixbugs_resolves_only_selected_generated_entry(tmp_path):
    module = _load_module()
    task_dir = tmp_path / "from-quixbugs" / "quixbugs-python-shunting_yard"
    _write_quixbugs_task(task_dir)

    assert module.process_task(
        task_dir,
        repo_cache_dir=tmp_path / "mirrors",
        checkout_root=tmp_path / "checkouts",
        check=False,
        skip_existing=False,
    ) == ("written", 0)
    initial_bytes = (task_dir / "task.yaml").read_bytes()
    assert module.process_task(
        task_dir,
        repo_cache_dir=tmp_path / "mirrors",
        checkout_root=tmp_path / "checkouts",
        check=True,
        skip_existing=False,
    ) == ("checked", 0)
    assert (task_dir / "task.yaml").read_bytes() == initial_bytes
    assert yaml.safe_load(initial_bytes)["test_entry_inferences"]["entries"] == [
        {
            "test_id": "tests/test_outputs.py::test_shunting_yard",
            "path": "fixed_shunting_yard.py",
            "symbol": "fixed_shunting_yard.py:shunting_yard",
            "start_line": 1,
            "end_line": 2,
        }
    ]


def test_quixbugs_rejects_path_mismatch_extra_command_and_ambiguous_alias(tmp_path):
    module = _load_module()
    task_dir = tmp_path / "task"
    _write_quixbugs_task(task_dir)
    script_path = task_dir / "solution.sh"
    original_script = script_path.read_text(encoding="utf-8")
    test_path = task_dir / "tests" / "test_outputs.py"
    original_test = test_path.read_text(encoding="utf-8")

    script_path.write_text(
        original_script.replace("fixed_shunting_yard.py", "fixed_other.py", 1),
        encoding="utf-8",
    )
    assert module.quixbugs_sources(task_dir) is None

    script_path.write_text(
        original_script + "python fixed_shunting_yard.py\n", encoding="utf-8"
    )
    assert module.quixbugs_sources(task_dir) is None

    script_path.write_text(
        original_script.replace(
            'echo "Solution written to fixed_shunting_yard.py"',
            'echo "$HOME/fixed_shunting_yard.py"',
        ),
        encoding="utf-8",
    )
    assert module.quixbugs_sources(task_dir) is None

    script_path.write_text(
        original_script
        + "cat > /app/fixed_shunting_yard.py << 'EOL'\n"
        + "def shunting_yard(tokens):\n    return tokens\nEOL\n",
        encoding="utf-8",
    )
    assert module.quixbugs_sources(task_dir) is None

    script_path.write_text(original_script, encoding="utf-8")
    test_path.write_text(
        original_test.replace(
            "def test_one_line_change():",
            "program_under_test = import_program(\n"
            "    'fixed_shunting_yard', fallback_program_name='shunting_yard'\n"
            ")\n\n"
            "def test_one_line_change():",
        ),
        encoding="utf-8",
    )
    assert module.quixbugs_sources(task_dir) is None


def test_quixbugs_rejects_unproven_import_helper(tmp_path):
    module = _load_module()
    task_dir = tmp_path / "task"
    _write_quixbugs_task(task_dir)
    helper_path = task_dir / "tests" / "test_utils.py"
    helper_path.write_text(
        helper_path.read_text(encoding="utf-8").replace(
            "except ModuleNotFoundError:", "except Exception:"
        ),
        encoding="utf-8",
    )

    assert module.quixbugs_sources(task_dir) is None


def test_quixbugs_rejects_fallback_name_assignment_after_try(tmp_path):
    module = _load_module()
    task_dir = tmp_path / "task"
    _write_quixbugs_task(task_dir)
    helper_path = task_dir / "tests" / "test_utils.py"
    helper_path.write_text(
        helper_path.read_text(encoding="utf-8").replace(
            "        module_name = fallback_program_name\n"
            "        module = importlib.import_module(fallback_program_name)\n"
            "    func_name = module_name.replace('fixed_', '')\n",
            "        module = importlib.import_module(fallback_program_name)\n"
            "    module_name = fallback_program_name\n"
            "    func_name = module_name.replace('fixed_', '')\n",
        ),
        encoding="utf-8",
    )

    assert module.quixbugs_sources(task_dir) is None


def _write_literal_module_task(task_dir: Path, *, solution: str) -> None:
    (task_dir / "tests" / "judges").mkdir(parents=True)
    (task_dir / "task.yaml").write_text("sentinel: true\n", encoding="utf-8")
    (task_dir / "solution.sh").write_text(
        "#!/bin/bash\ncat > /app/solution.py << 'EOL'\n" + solution + "EOL\n",
        encoding="utf-8",
    )
    (task_dir / "tests" / "test_outputs.py").write_text(
        "from . import evaluate_solution_sets\n\n"
        "def test_single_task(workdir):\n"
        "    with open('/app/solution.py', 'r') as f:\n"
        "        completion = f.read()\n"
        "    task_id = 'task'\n"
        "    solution_dict = {task_id: [completion]}\n"
        "    result_sets = evaluate_solution_sets(\n"
        "        workdir, solution_dict, {'task': {}}, mode='fail_fast'\n"
        "    )\n",
        encoding="utf-8",
    )
    (task_dir / "tests" / "evaluate.py").write_text(
        "from .judges.usaco_batch_judge import USACOBatchJudge\n\n"
        "def evaluate_solution_sets(workdir, solution_dict, problem_dict, **kwargs):\n"
        "    judge = USACOBatchJudge()\n"
        "    return judge.judge(workdir, solution_dict, problem_dict, **kwargs)\n",
        encoding="utf-8",
    )
    (task_dir / "tests" / "judges" / "usaco_batch_judge.py").write_text(
        "from .usaco_utils import check_correctness\n\n"
        "class USACOBatchJudge:\n"
        "    def judge(self, workdir, solution_dict, problem_dict, **kwargs):\n"
        "        for problem_id, solution_set in solution_dict.items():\n"
        "            for solution in solution_set:\n"
        "                args = (workdir, problem_dict[problem_id], solution)\n"
        "                executor.submit(usaco_judge, *args, **kwargs)\n\n"
        "def usaco_judge(workdir, problem, solution_code):\n"
        "    return usaco_python3_judge(workdir, problem['cp_id'], solution_code)\n\n"
        "def usaco_python3_judge(workdir, problem_id, solution_code):\n"
        "    return check_correctness(solution_code, 1, 1, '', '', '')\n",
        encoding="utf-8",
    )
    (task_dir / "tests" / "judges" / "usaco_utils.py").write_text(
        "import multiprocessing\n\n"
        "def check_correctness(program):\n"
        "    def unsafe_execute():\n"
        "        prefix_program = ''\n"
        "        suffix_program = ''\n"
        "        check_program = prefix_program + program + suffix_program\n"
        "        exec(check_program, {'__name__': '__main__'})\n"
        "    p = multiprocessing.Process(target=unsafe_execute)\n"
        "    p.start()\n",
        encoding="utf-8",
    )


def test_executable_module_span_excludes_imports_and_docstring():
    module = _load_module()

    assert module.executable_module_span(
        '"""module docs"""\n'
        "import sys\n"
        "from pathlib import Path\n\n"
        "input_path = Path(sys.stdin.name)\n"
        "if input_path.suffix != '.in':\n"
        "    raise SystemExit\n"
        "output_path = input_path.with_suffix('.out')\n"
        "sys.stdout.write(output_path.read_text())\n"
    ) == (5, 9)
    assert module.executable_module_span("import sys\n") is None
    assert module.executable_module_span("if True print('bad')\n") is None


def test_process_literal_module_requires_proven_execution_chain(tmp_path):
    module = _load_module()
    task_dir = tmp_path / "arbitrary-family" / "script"
    solution = (
        "import sys\n"
        "from pathlib import Path\n\n"
        "input_path = Path(sys.stdin.name)\n"
        "if input_path.suffix != '.in':\n"
        "    raise SystemExit\n"
        "output_path = input_path.with_suffix('.out')\n"
        "sys.stdout.write(output_path.read_text())\n"
    )
    _write_literal_module_task(task_dir, solution=solution)

    assert module.proven_usaco_evaluator(task_dir) is True
    assert module.process_task(
        task_dir,
        repo_cache_dir=tmp_path / "mirrors",
        checkout_root=tmp_path / "checkouts",
        check=False,
        skip_existing=False,
    ) == ("written", 0)
    initial_bytes = (task_dir / "task.yaml").read_bytes()
    assert module.process_task(
        task_dir,
        repo_cache_dir=tmp_path / "mirrors",
        checkout_root=tmp_path / "checkouts",
        check=True,
        skip_existing=False,
    ) == ("checked", 0)
    assert (task_dir / "task.yaml").read_bytes() == initial_bytes
    assert yaml.safe_load(initial_bytes)["test_entry_inferences"]["entries"] == [
        {
            "test_id": "tests/test_outputs.py::test_single_task",
            "path": "solution.py",
            "symbol": "solution.py:<module>",
            "start_line": 4,
            "end_line": 8,
        }
    ]


def test_literal_module_rejects_mere_read_and_ambiguous_chain(tmp_path):
    module = _load_module()
    task_dir = tmp_path / "task"
    _write_literal_module_task(task_dir, solution="print('ok')\n")
    test_path = task_dir / "tests" / "test_outputs.py"
    original = test_path.read_text(encoding="utf-8")

    test_path.write_text(
        "from pathlib import Path\n\n"
        "def test_single_task():\n"
        "    assert Path('/app/solution.py').exists()\n",
        encoding="utf-8",
    )
    assert module.proven_usaco_evaluator(task_dir) is False

    test_path.write_text(
        "def test_single_task():\n"
        "    with open('/app/solution.py', 'r') as f:\n"
        "        completion = f.read()\n",
        encoding="utf-8",
    )
    assert module.proven_usaco_evaluator(task_dir) is False

    test_path.write_text(original, encoding="utf-8")
    utils_path = task_dir / "tests" / "judges" / "usaco_utils.py"
    utils_path.write_text(
        utils_path.read_text(encoding="utf-8").replace(
            "    p.start()\n",
            "    p.start()\n"
            "    alternate = multiprocessing.Process(target=unsafe_execute)\n"
            "    alternate.start()\n",
        ),
        encoding="utf-8",
    )
    assert module.proven_usaco_evaluator(task_dir) is False

    test_path.write_text(
        original.replace(
            "    result_sets = evaluate_solution_sets(\n",
            "    evaluate_solution_sets(workdir, solution_dict, {})\n"
            "    result_sets = evaluate_solution_sets(\n",
        ),
        encoding="utf-8",
    )
    assert module.proven_usaco_evaluator(task_dir) is False


def test_literal_module_rejects_mixed_payload(tmp_path):
    module = _load_module()
    task_dir = tmp_path / "task"
    _write_literal_module_task(
        task_dir,
        solution="value = 1\ndef helper():\n    return value\nprint(value)\n",
    )

    assert (
        module.executable_module_span(
            "value = 1\ndef helper():\n    return value\nprint(value)\n"
        )
        is None
    )
    assert module.literal_module_sources(task_dir) is None


def test_literal_module_rejects_broken_formal_and_loop_data_flow(tmp_path):
    module = _load_module()
    task_dir = tmp_path / "task"
    _write_literal_module_task(task_dir, solution="print('ok')\n")
    batch_path = task_dir / "tests" / "judges" / "usaco_batch_judge.py"
    original_batch = batch_path.read_text(encoding="utf-8")

    batch_path.write_text(
        original_batch.replace(
            "for solution in solution_set:",
            "for solution in other_solutions:",
        ),
        encoding="utf-8",
    )
    assert module.proven_usaco_evaluator(task_dir) is False

    batch_path.write_text(original_batch, encoding="utf-8")
    utils_path = task_dir / "tests" / "judges" / "usaco_utils.py"
    utils_path.write_text(
        utils_path.read_text(encoding="utf-8").replace(
            "def check_correctness(program):",
            "def check_correctness(payload):",
        ),
        encoding="utf-8",
    )
    assert module.proven_usaco_evaluator(task_dir) is False


def test_selection_prefers_exact_changed_test_functions(tmp_path):
    module = _load_module()
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "api.py").write_text(
        "def alpha():\n    return 1\n\ndef beta():\n    return 2\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_api.py").write_text(
        "from pkg.api import alpha, beta\n\n"
        "def test_alpha():\n"
        "    alpha()\n\n"
        "def test_beta():\n"
        "    beta()\n",
        encoding="utf-8",
    )

    record = {
        "test_patch": """diff --git a/tests/test_api.py b/tests/test_api.py
--- a/tests/test_api.py
+++ b/tests/test_api.py
@@ -1,4 +1,7 @@
 from pkg.api import alpha, beta
 
 def test_alpha():
     alpha()
+
+def test_beta():
+    beta()
""",
        "FAIL_TO_PASS": ["tests/test_api.py::test_alpha"],
    }

    selection = module.discover_trusted_selection(tmp_path, record)

    assert selection.test_ids == ("tests/test_api.py::test_beta",)
    assert selection.test_paths == ()
    assert selection.helper_only_changed is False


def test_selection_recognizes_changed_suffix_test_module(tmp_path):
    module = _load_module()
    (tmp_path / "pkg").mkdir()
    test_path = tmp_path / "pkg" / "numpy_test.py"
    test_path.write_text(
        "from pkg.api import alpha\ndef test_alpha():\n    alpha()\n",
        encoding="utf-8",
    )
    record = {
        "test_patch": """diff --git a/pkg/numpy_test.py b/pkg/numpy_test.py
--- a/pkg/numpy_test.py
+++ b/pkg/numpy_test.py
@@ -1,3 +1,4 @@
 from pkg.api import alpha
 def test_alpha():
+    alpha()
"""
    }

    selection = module.discover_trusted_selection(tmp_path, record)

    assert selection.test_ids == ("pkg/numpy_test.py::test_alpha",)
    assert selection.test_paths == ()
    assert selection.helper_only_changed is False


def test_selection_uses_suffix_test_file_boundary_when_no_function_matches(
    tmp_path,
):
    module = _load_module()
    (tmp_path / "pkg").mkdir()
    test_path = tmp_path / "pkg" / "numpy_test.py"
    test_path.write_text(
        "from pkg.api import alpha\nHELPER = alpha\n",
        encoding="utf-8",
    )
    record = {
        "test_patch": """diff --git a/pkg/numpy_test.py b/pkg/numpy_test.py
--- a/pkg/numpy_test.py
+++ b/pkg/numpy_test.py
@@ -1,2 +1,3 @@
 from pkg.api import alpha
 HELPER = alpha
+VALUE = 1
"""
    }

    selection = module.discover_trusted_selection(tmp_path, record)

    assert selection.test_ids == ()
    assert selection.test_paths == ("pkg/numpy_test.py",)
    assert selection.helper_only_changed is True


def test_selection_keeps_changed_resources_and_explicit_metadata_selectors(tmp_path):
    module = _load_module()
    (tmp_path / "tests" / "validators").mkdir(parents=True)
    (tmp_path / "tests" / "validators" / "tests.py").write_text(
        "with open('invalid_urls.txt') as stream:\n    DATA = list(stream)\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "validators" / "invalid_urls.txt").write_text(
        "http://example.com\n",
        encoding="utf-8",
    )

    record = {
        "test_patch": """\
diff --git a/tests/validators/invalid_urls.txt b/tests/validators/invalid_urls.txt
--- a/tests/validators/invalid_urls.txt
+++ b/tests/validators/invalid_urls.txt
@@ -1 +1,2 @@
 http://example.com
+http://bad@example.com
""",
        "FAIL_TO_PASS": [
            "test_URLValidator_raises_error_352 (validators.tests.TestSimpleValidators)"
        ],
    }

    selection = module.discover_trusted_selection(tmp_path, record)

    assert selection.test_ids == (
        "test_URLValidator_raises_error_352 (validators.tests.TestSimpleValidators)",
    )
    assert selection.test_paths == ("tests/validators/invalid_urls.txt",)
    assert selection.helper_only_changed is False


def test_selection_keeps_resource_scope_without_selector_authority(tmp_path):
    module = _load_module()
    resource = tmp_path / "tests" / "fixtures" / "expected.json"
    resource.parent.mkdir(parents=True)
    resource.write_text("{}\n", encoding="utf-8")
    record = {
        "test_patch": """\
diff --git a/tests/fixtures/expected.json b/tests/fixtures/expected.json
--- a/tests/fixtures/expected.json
+++ b/tests/fixtures/expected.json
@@ -1 +1 @@
-{}
+{"value": 1}
""",
    }

    selection = module.discover_trusted_selection(tmp_path, record)

    assert selection.test_ids == ()
    assert selection.test_paths == ("tests/fixtures/expected.json",)
    assert selection.helper_only_changed is False


def test_selection_falls_back_to_metadata_for_helper_only_changes(tmp_path):
    module = _load_module()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "api.py").write_text(
        "def alpha():\n    return 1\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "conftest.py").write_text(
        "VALUE = 1\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_api.py").write_text(
        "from pkg.api import alpha\n\ndef test_alpha():\n    alpha()\n",
        encoding="utf-8",
    )

    record = {
        "test_patch": """diff --git a/tests/conftest.py b/tests/conftest.py
--- a/tests/conftest.py
+++ b/tests/conftest.py
@@ -1 +1,2 @@
 VALUE = 1
+OTHER = 2
""",
        "FAIL_TO_PASS": ["tests/test_api.py::test_alpha"],
    }

    selection = module.discover_trusted_selection(tmp_path, record)

    assert selection.test_ids == ("tests/test_api.py::test_alpha",)
    assert selection.test_paths == ()
    assert selection.helper_only_changed is True


def test_selection_decodes_flat_python_literal_selector_and_path_sequences(tmp_path):
    module = _load_module()
    expected = module.discover_trusted_selection(
        tmp_path,
        {
            "fail_to_pass": [
                " test/units/test_z.py::test_z ",
                "test/units/test_a.py::test_a",
            ],
            "selected_test_files_to_run": [
                " test/units/test_z.py ",
                "test/units/test_a.py",
            ],
        },
    )

    single_quoted_list = module.discover_trusted_selection(
        tmp_path,
        {
            "fail_to_pass": (
                "[' test/units/test_z.py::test_z ', 'test/units/test_a.py::test_a']"
            ),
            "selected_test_files_to_run": (
                "[' test/units/test_z.py ', 'test/units/test_a.py']"
            ),
        },
    )
    double_quoted_tuple = module.discover_trusted_selection(
        tmp_path,
        {
            "fail_to_pass": (
                '(" test/units/test_z.py::test_z ", "test/units/test_a.py::test_a")'
            ),
            "selected_test_files_to_run": (
                '(" test/units/test_z.py ", "test/units/test_a.py")'
            ),
        },
    )

    assert single_quoted_list == expected
    assert double_quoted_tuple == expected
    assert expected.test_ids == (
        "test/units/test_a.py::test_a",
        "test/units/test_z.py::test_z",
    )
    assert expected.test_paths == (
        "test/units/test_a.py",
        "test/units/test_z.py",
    )


def test_jsonish_items_rejects_non_flat_or_executable_python_literals():
    module = _load_module()
    unsafe_values = (
        "'test/units/test_api.py::test_alpha'",
        "[['test/units/test_api.py::test_alpha']]",
        "{'test/units/test_api.py::test_alpha'}",
        "{'selector': 'test/units/test_api.py::test_alpha'}",
        "['test/units/test_api.py::test_alpha', 1]",
        "[value for value in selectors]",
        "list(('test/units/test_api.py::test_alpha',))",
        "selectors",
        "['test/units/test_api.py::test_alpha'",
    )

    for value in unsafe_values:
        assert module._jsonish_items(value) == [value]
        selection = module.discover_trusted_selection(
            Path("."), {"fail_to_pass": value}
        )
        assert "test/units/test_api.py::test_alpha" not in selection.test_ids
        path_selection = module.discover_trusted_selection(
            Path("."), {"selected_test_files_to_run": value}
        )
        assert "test/units/test_api.py" not in path_selection.test_paths


def test_jsonish_items_preserves_json_and_line_string_fallbacks():
    module = _load_module()

    assert module._jsonish_items('[" second ", "first"]') == ["second", "first"]
    assert module._jsonish_items(" second\n\n first ") == ["second", "first"]


def _write_mypy_suggestion_fixture(tmp_path: Path) -> tuple[Path, object, str]:
    module = _load_module()
    (tmp_path / "mypy" / "test").mkdir(parents=True, exist_ok=True)
    (tmp_path / "test-data" / "unit").mkdir(parents=True, exist_ok=True)
    (tmp_path / "mypy" / "test" / "testfinegrained.py").write_text(
        "from mypy.dmypy_server import Server\n"
        "from mypy.test.helpers import find_test_files\n\n"
        "class FineGrainedSuite:\n"
        "    files = find_test_files(\n"
        "        pattern='fine-grained*.test',\n"
        "        exclude=['fine-grained-cache-incremental.test'],\n"
        "    )\n\n"
        "    def run_case(self, testcase):\n"
        "        self.maybe_suggest(\n"
        "            1, testcase.server, testcase.src, testcase.tmpdir\n"
        "        )\n\n"
        "    def maybe_suggest(\n"
        "        self, step: int, server: Server, src: str, tmp_dir: str\n"
        "    ):\n"
        "        return server.cmd_suggest('foo.F.__new__', False)\n",
        encoding="utf-8",
    )
    (tmp_path / "mypy" / "dmypy_server.py").write_text(
        "class Server:\n"
        "    def cmd_suggest(self, function: str, callsites: bool):\n"
        "        return {'out': function}\n",
        encoding="utf-8",
    )
    resource = (
        "[case testSuggestNewInit]\n"
        "# suggest: foo.F.__init__\n"
        "# suggest: foo.F.__new__\n"
        "[out]\n"
    )
    (tmp_path / "test-data" / "unit" / "fine-grained-suggest.test").write_text(
        resource, encoding="utf-8"
    )
    selection = module.TrustedSelection(
        test_ids=(
            "mypy/test/testfinegrained.py::FineGrainedSuite::"
            "fine-grained-suggest.test::testSuggestNewInit",
        ),
        test_paths=("test-data/unit/fine-grained-suggest.test",),
        helper_only_changed=False,
    )
    patch = (
        "diff --git a/test-data/unit/fine-grained-suggest.test "
        "b/test-data/unit/fine-grained-suggest.test\n"
        "--- a/test-data/unit/fine-grained-suggest.test\n"
        "+++ b/test-data/unit/fine-grained-suggest.test\n"
        "@@ -0,0 +1,4 @@\n"
        "+[case testSuggestNewInit]\n"
        "+# suggest: foo.F.__init__\n"
        "+# suggest: foo.F.__new__\n"
        "+[out]\n"
    )
    return tmp_path, selection, patch


def test_mypy_suggestion_adapter_emits_canonical_selector_and_ast_method_span(
    tmp_path,
):
    module = _load_module()
    repo_root, selection, patch = _write_mypy_suggestion_fixture(tmp_path)

    result = module.infer_mypy_suggestion_entry(repo_root, selection, patch)

    assert result is not None
    assert [entry.model_dump() for entry in result.entries] == [
        {
            "test_id": (
                "mypy/test/testfinegrained.py::FineGrainedSuite."
                "fine-grained-suggest.test.testSuggestNewInit"
            ),
            "path": "mypy/dmypy_server.py",
            "symbol": "mypy/dmypy_server.py:Server.cmd_suggest",
            "start_line": 2,
            "end_line": 3,
        }
    ]
    assert (
        module.quality_gate_reason(repo_root, result, test_ids=selection.test_ids)
        is None
    )


def test_mypy_suggestion_adapter_derives_patched_resource_from_selector(tmp_path):
    module = _load_module()
    repo_root, selection, patch = _write_mypy_suggestion_fixture(tmp_path)
    selector_only = module.TrustedSelection(
        test_ids=selection.test_ids,
        test_paths=(),
        helper_only_changed=False,
    )

    result = module.infer_mypy_suggestion_entry(repo_root, selector_only, patch)

    assert result is not None
    assert result.entries[0].test_id.endswith("testSuggestNewInit")


@pytest.mark.parametrize(
    ("selector_mutation", "resource_replacement", "patch_replacement"),
    [
        ("", None, None),
        ("suite", None, None),
        ("file", None, None),
        ("case", None, None),
        (None, "# # suggest:", None),
        (None, "# suggest2:", None),
        (None, "#  suggest:", None),
        (None, "[case testSuggestNewInit]", "[case testOtherCase]"),
    ],
)
def test_mypy_suggestion_adapter_rejects_mismatched_or_variant_case_authority(
    tmp_path, selector_mutation, resource_replacement, patch_replacement
):
    module = _load_module()
    repo_root, selection, patch = _write_mypy_suggestion_fixture(tmp_path)
    if selector_mutation == "":
        selection = module.TrustedSelection(
            test_ids=(),
            test_paths=selection.test_paths,
            helper_only_changed=False,
        )
    elif selector_mutation is not None:
        replacements = {
            "suite": ("FineGrainedSuite", "WrongSuite"),
            "file": ("fine-grained-suggest.test", "fine-grained-other.test"),
            "case": ("testSuggestNewInit", "testOtherCase"),
        }
        old, new = replacements[selector_mutation]
        selection = module.TrustedSelection(
            test_ids=(selection.test_ids[0].replace(old, new),),
            test_paths=selection.test_paths,
            helper_only_changed=False,
        )
    resource_path = repo_root / selection.test_paths[0]
    if resource_replacement is not None:
        resource_path.write_text(
            resource_path.read_text(encoding="utf-8").replace(
                "# suggest:", resource_replacement
            ),
            encoding="utf-8",
        )
    if patch_replacement is not None:
        patch = patch.replace("[case testSuggestNewInit]", patch_replacement, 1)

    assert module.infer_mypy_suggestion_entry(repo_root, selection, patch) is None


def test_mypy_suggestion_adapter_rejects_duplicate_or_multi_case_authority(tmp_path):
    module = _load_module()
    repo_root, selection, patch = _write_mypy_suggestion_fixture(tmp_path)
    resource_path = repo_root / selection.test_paths[0]
    resource_path.write_text(
        resource_path.read_text(encoding="utf-8")
        + "[case testSuggestNewInit]\n# suggest: foo.F.__new__\n",
        encoding="utf-8",
    )
    assert module.infer_mypy_suggestion_entry(repo_root, selection, patch) is None

    _repo_root, selection, patch = _write_mypy_suggestion_fixture(tmp_path)
    multi = module.TrustedSelection(
        test_ids=(
            *selection.test_ids,
            selection.test_ids[0].replace("NewInit", "Other"),
        ),
        test_paths=selection.test_paths,
        helper_only_changed=False,
    )
    assert module.infer_mypy_suggestion_entry(repo_root, multi, patch) is None


def test_mypy_suggestion_adapter_does_not_pair_directive_from_another_hunk(
    tmp_path,
):
    module = _load_module()
    repo_root, selection, _patch = _write_mypy_suggestion_fixture(tmp_path)
    separated_patch = (
        "diff --git a/test-data/unit/fine-grained-suggest.test "
        "b/test-data/unit/fine-grained-suggest.test\n"
        "--- a/test-data/unit/fine-grained-suggest.test\n"
        "+++ b/test-data/unit/fine-grained-suggest.test\n"
        "@@ -1 +1,2 @@\n"
        "+[case testSuggestNewInit]\n"
        " [out]\n"
        "@@ -20,2 +21,3 @@\n"
        " [case testOtherCase]\n"
        "+# suggest: other.target\n"
        " [out]\n"
    )

    assert (
        module.infer_mypy_suggestion_entry(repo_root, selection, separated_patch)
        is None
    )


@pytest.mark.parametrize(
    ("path", "old", "new"),
    [
        ("mypy/test/testfinegrained.py", "fine-grained*.test", "other*.test"),
        ("mypy/test/testfinegrained.py", "self.maybe_suggest", "self.maybe_inspect"),
        ("mypy/test/testfinegrained.py", "server.cmd_suggest", "server.check"),
        ("mypy/test/testfinegrained.py", "server: Server", "server: object"),
        ("mypy/dmypy_server.py", "class Server:", "class OtherServer:"),
    ],
)
def test_mypy_suggestion_adapter_rejects_altered_runner_links(tmp_path, path, old, new):
    module = _load_module()
    repo_root, selection, patch = _write_mypy_suggestion_fixture(tmp_path)
    source_path = repo_root / path
    source_path.write_text(
        source_path.read_text(encoding="utf-8").replace(old, new),
        encoding="utf-8",
    )

    assert module.infer_mypy_suggestion_entry(repo_root, selection, patch) is None


def test_mypy_suggestion_adapter_rejects_duplicate_production_method(tmp_path):
    module = _load_module()
    repo_root, selection, patch = _write_mypy_suggestion_fixture(tmp_path)
    with (repo_root / "mypy" / "dmypy_server.py").open("a", encoding="utf-8") as stream:
        stream.write(
            "    def cmd_suggest(self, function, callsites):\n        return {}\n"
        )

    assert module.infer_mypy_suggestion_entry(repo_root, selection, patch) is None


@pytest.mark.parametrize("nested_link", ["run_case", "maybe_suggest"])
def test_mypy_suggestion_adapter_rejects_nested_only_runner_calls(
    tmp_path, nested_link
):
    module = _load_module()
    repo_root, selection, patch = _write_mypy_suggestion_fixture(tmp_path)
    runner_path = repo_root / "mypy" / "test" / "testfinegrained.py"
    source = runner_path.read_text(encoding="utf-8")
    if nested_link == "run_case":
        source = source.replace(
            "        self.maybe_suggest(\n"
            "            1, testcase.server, testcase.src, testcase.tmpdir\n"
            "        )\n",
            "        def deferred():\n"
            "            self.maybe_suggest(\n"
            "                1, testcase.server, testcase.src, testcase.tmpdir\n"
            "            )\n"
            "        return deferred\n",
        )
    else:
        source = source.replace(
            "        return server.cmd_suggest('foo.F.__new__', False)\n",
            "        deferred = lambda: server.cmd_suggest(\n"
            "            'foo.F.__new__', False\n"
            "        )\n"
            "        return deferred\n",
        )
    runner_path.write_text(source, encoding="utf-8")

    assert module.infer_mypy_suggestion_entry(repo_root, selection, patch) is None


def test_mypy_suggestion_adapter_rejects_rebound_server_parameter(tmp_path):
    module = _load_module()
    repo_root, selection, patch = _write_mypy_suggestion_fixture(tmp_path)
    runner_path = repo_root / "mypy" / "test" / "testfinegrained.py"
    source = runner_path.read_text(encoding="utf-8").replace(
        "        return server.cmd_suggest('foo.F.__new__', False)\n",
        "        server = replacement\n"
        "        return server.cmd_suggest('foo.F.__new__', False)\n",
    )
    runner_path.write_text(source, encoding="utf-8")

    assert module.infer_mypy_suggestion_entry(repo_root, selection, patch) is None


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            "from mypy.dmypy_server import Server",
            "from mypy.dmypy_server import Server as ImportedServer",
        ),
        (
            "from mypy.test.helpers import find_test_files",
            "from mypy.test.helpers import find_test_files as collect_test_files",
        ),
    ],
)
def test_mypy_suggestion_adapter_requires_unaliased_runner_imports(tmp_path, old, new):
    module = _load_module()
    repo_root, selection, patch = _write_mypy_suggestion_fixture(tmp_path)
    runner_path = repo_root / "mypy" / "test" / "testfinegrained.py"
    runner_path.write_text(
        runner_path.read_text(encoding="utf-8").replace(old, new),
        encoding="utf-8",
    )

    assert module.infer_mypy_suggestion_entry(repo_root, selection, patch) is None


@pytest.mark.parametrize(
    "competing_binding",
    [
        "Server = replacement",
        "from other import Server",
        "find_test_files = replacement",
        "from other import find_test_files",
    ],
)
def test_mypy_suggestion_adapter_rejects_competing_runner_bindings(
    tmp_path, competing_binding
):
    module = _load_module()
    repo_root, selection, patch = _write_mypy_suggestion_fixture(tmp_path)
    runner_path = repo_root / "mypy" / "test" / "testfinegrained.py"
    source = runner_path.read_text(encoding="utf-8").replace(
        "from mypy.test.helpers import find_test_files\n",
        f"from mypy.test.helpers import find_test_files\n{competing_binding}\n",
    )
    runner_path.write_text(source, encoding="utf-8")

    assert module.infer_mypy_suggestion_entry(repo_root, selection, patch) is None


def test_mypy_suggestion_adapter_rejects_rebound_self_parameter(tmp_path):
    module = _load_module()
    repo_root, selection, patch = _write_mypy_suggestion_fixture(tmp_path)
    runner_path = repo_root / "mypy" / "test" / "testfinegrained.py"
    source = runner_path.read_text(encoding="utf-8").replace(
        "    def run_case(self, testcase):\n",
        "    def run_case(self, testcase):\n        self = replacement\n",
    )
    runner_path.write_text(source, encoding="utf-8")

    assert module.infer_mypy_suggestion_entry(repo_root, selection, patch) is None


def test_language_classification_ignores_parentheses_in_javascript_tests(tmp_path):
    module = _load_module()

    record = {
        "repo_language": "javascript",
        "FAIL_TO_PASS": ["renders (components.Widget)"],
        "test_patch": "diff --git a/test/widget.test.js b/test/widget.test.js\n",
    }

    assert module._looks_like_python_task(record, tmp_path) is False


def test_language_classification_accepts_local_python_task_yaml(tmp_path):
    module = _load_module()

    assert (
        module._looks_like_python_task(
            {"FAIL_TO_PASS": ["test(foo)"]},
            tmp_path,
            {"language": "python"},
        )
        is True
    )


def test_task_language_normalizes_java_and_cpp_declarations(tmp_path):
    module = _load_module()

    assert module._task_language({}, tmp_path, {"language": "Java"}) == "java"
    assert module._task_language({}, tmp_path, {"language": "C++"}) == "cpp"


def test_task_language_normalizes_javascript_and_typescript_declarations(tmp_path):
    module = _load_module()

    assert (
        module._task_language({}, tmp_path, {"language": "JavaScript"}) == "javascript"
    )
    assert (
        module._task_language({}, tmp_path, {"language": "TypeScript"}) == "typescript"
    )


def test_task_language_uses_unambiguous_local_java_evidence(tmp_path):
    module = _load_module()
    (tmp_path / "tests").mkdir()
    (tmp_path / "Program.java").write_text("class Program {}\n", encoding="utf-8")
    (tmp_path / "tests" / "ProgramTest.java").write_text(
        "class ProgramTest {}\n", encoding="utf-8"
    )

    assert module._task_language({}, tmp_path, {}) == "java"


def test_task_language_does_not_treat_python_only_verifier_as_project(tmp_path):
    module = _load_module()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_outputs.py").write_text(
        "def test_output(): pass\n", encoding="utf-8"
    )

    assert module._task_language({}, tmp_path, {}) == ""


def test_changed_java_test_method_becomes_an_exact_selector(tmp_path):
    module = _load_module()
    test = tmp_path / "src" / "test" / "java" / "WidgetTest.java"
    test.parent.mkdir(parents=True)
    test.write_text(
        "import org.junit.jupiter.api.Test;\n"
        "class WidgetTest {\n"
        "  @Test void first() { new Widget().run(); }\n"
        "  @Test void second() { new Widget().stop(); }\n"
        "}\n",
        encoding="utf-8",
    )
    record = {
        "test_patch": """\
diff --git a/src/test/java/WidgetTest.java b/src/test/java/WidgetTest.java
--- a/src/test/java/WidgetTest.java
+++ b/src/test/java/WidgetTest.java
@@ -2,2 +2,3 @@
 class WidgetTest {
+  @Test void first() { new Widget().run(); }
""",
        "FAIL_TO_PASS": ["src/test/java/WidgetTest.java::WidgetTest.second"],
    }

    selection = module.discover_trusted_selection(tmp_path, record, "java")

    assert selection.test_ids == ("src/test/java/WidgetTest.java::WidgetTest.first",)


def test_changed_javascript_test_becomes_an_exact_selector(tmp_path):
    module = _load_module()
    test = tmp_path / "tests" / "widget.test.js"
    test.parent.mkdir(parents=True)
    test.write_text(
        "describe('Widget', () => {\n"
        "  test('first case', () => callOne());\n"
        "  test('second case', () => callTwo());\n"
        "});\n",
        encoding="utf-8",
    )
    record = {
        "test_patch": """\
diff --git a/tests/widget.test.js b/tests/widget.test.js
--- a/tests/widget.test.js
+++ b/tests/widget.test.js
@@ -1,2 +1,3 @@
 describe('Widget', () => {
+  test('first case', () => callOne());
 """,
        "FAIL_TO_PASS": ["Widget second case"],
    }

    selection = module.discover_trusted_selection(tmp_path, record, "javascript")

    assert selection.test_ids == ("tests/widget.test.js::Widget:first case",)


def test_javascript_metadata_selectors_preserve_suite_titles(tmp_path):
    module = _load_module()

    selection = module.discover_trusted_selection(
        tmp_path,
        {
            "FAIL_TO_PASS": [
                "MediaActions - #add() - should accept a single upload",
                "Files:p5.prototype.loadStrings:should include empty strings",
            ]
        },
        "javascript",
    )

    assert selection.test_ids == (
        "Files:p5.prototype.loadStrings:should include empty strings",
        "MediaActions - #add() - should accept a single upload",
    )


def test_polyglot_java_inference_resolves_receiver_and_overload(tmp_path):
    from testprism.polyglot_entry_inference import infer_polyglot_test_entries

    production = tmp_path / "src" / "main" / "java" / "demo" / "Widget.java"
    test = tmp_path / "src" / "test" / "java" / "demo" / "WidgetTest.java"
    production.parent.mkdir(parents=True)
    test.parent.mkdir(parents=True)
    production.write_text(
        "package demo;\npublic class Widget {\n"
        "  public Widget() {}\n"
        "  public int size(String value) { return value.length(); }\n"
        "  public int size(Integer value) { return value; }\n"
        "}\n",
        encoding="utf-8",
    )
    test.write_text(
        "package demo;\nimport org.junit.jupiter.api.Test;\n"
        "class WidgetTest {\n  @Test void reportsSize() {\n"
        "    Widget widget = new Widget();\n"
        "    String value = null;\n    widget.size(value);\n  }\n}\n",
        encoding="utf-8",
    )

    result = infer_polyglot_test_entries(tmp_path, language="java")

    assert [entry.symbol for entry in result.entries] == [
        "src/main/java/demo/Widget.java:Widget.Widget",
        "src/main/java/demo/Widget.java:Widget.size",
    ]
    assert result.language == "java"


def test_polyglot_cpp_inference_handles_gtest_and_template_receiver(tmp_path):
    from testprism.polyglot_entry_inference import infer_polyglot_test_entries

    header = tmp_path / "include" / "stack.hpp"
    test = tmp_path / "unit_tests" / "test_stack.cpp"
    header.parent.mkdir()
    test.parent.mkdir()
    header.write_text(
        "template <class T>\nclass Stack {\npublic:\n"
        "  void push(const T &value) { (void)value; }\n"
        "  bool empty() { return true; }\n};\n",
        encoding="utf-8",
    )
    test.write_text(
        '#include <gtest/gtest.h>\n#include "../include/stack.hpp"\n'
        "TEST(StackTest, Push) { Stack<int> stack; stack.push(1); }\n",
        encoding="utf-8",
    )

    result = infer_polyglot_test_entries(tmp_path, language="cpp")

    assert [entry.symbol for entry in result.entries] == [
        "include/stack.hpp:Stack.push"
    ]
    assert result.language == "cpp"


def test_polyglot_javascript_inference_resolves_default_import_call(tmp_path):
    from testprism.polyglot_entry_inference import infer_polyglot_test_entries

    production = tmp_path / "src" / "resolve.js"
    test = tmp_path / "tests" / "resolve.test.js"
    production.parent.mkdir(parents=True)
    test.parent.mkdir(parents=True)
    production.write_text(
        "const resolve = (glyphs = []) => {\n"
        "  return glyphs;\n"
        "};\n"
        "export default resolve;\n",
        encoding="utf-8",
    )
    test.write_text(
        "import resolve from '../src/resolve';\n"
        "describe('indices resolve operator', () => {\n"
        "  test('returns values', () => {\n"
        "    resolve([]);\n"
        "  });\n"
        "});\n",
        encoding="utf-8",
    )

    result = infer_polyglot_test_entries(tmp_path, language="javascript")

    assert [(entry.test_id, entry.symbol) for entry in result.entries] == [
        (
            "tests/resolve.test.js::indices resolve operator:returns values",
            "src/resolve.js:resolve",
        )
    ]


def test_polyglot_javascript_inference_resolves_default_namespace_methods(tmp_path):
    from testprism.polyglot_entry_inference import infer_polyglot_test_entries

    production = tmp_path / "src" / "actions.js"
    test = tmp_path / "test" / "actions.js"
    production.parent.mkdir(parents=True)
    test.parent.mkdir(parents=True)
    production.write_text(
        "const MediaActions = { _fetching: {} };\n"
        "MediaActions.fetch = function(siteId) {\n"
        "  return siteId;\n"
        "};\n"
        "MediaActions.add = function(site, files) {\n"
        "  return files;\n"
        "};\n"
        "export default MediaActions;\n",
        encoding="utf-8",
    )
    test.write_text(
        "const MediaActions = require('../src/actions');\n"
        "describe('MediaActions', () => {\n"
        "  describe('#add()', () => {\n"
        "    test('should accept a single upload', () => {\n"
        "      MediaActions.add('site', 'file');\n"
        "    });\n"
        "  });\n"
        "  describe('#fetch()', () => {\n"
        "    test('should fetch an item', () => {\n"
        "      MediaActions.fetch(1);\n"
        "    });\n"
        "  });\n"
        "});\n",
        encoding="utf-8",
    )

    result = infer_polyglot_test_entries(
        tmp_path,
        language="javascript",
        test_ids=["MediaActions - #add() - should accept a single upload"],
    )

    assert [(entry.test_id, entry.symbol) for entry in result.entries] == [
        (
            "test/actions.js::MediaActions:#add():should accept a single upload",
            "src/actions.js:MediaActions.add",
        )
    ]


def test_polyglot_javascript_inference_resolves_constructor_mixin_method(tmp_path):
    from testprism.polyglot_entry_inference import infer_polyglot_test_entries

    module_dir = tmp_path / "src"
    test_dir = tmp_path / "tests"
    module_dir.mkdir(parents=True)
    test_dir.mkdir(parents=True)
    (module_dir / "methods.js").write_text(
        "module.exports = {\n  compileMethods() {\n    return true;\n  },\n};\n",
        encoding="utf-8",
    )
    (module_dir / "index.js").write_text(
        "const compileMethods = require('./methods');\n"
        "class AwsCompileApigEvents {\n"
        "  constructor() {\n"
        "    Object.assign(this, compileMethods);\n"
        "  }\n"
        "}\n"
        "module.exports = AwsCompileApigEvents;\n",
        encoding="utf-8",
    )
    (test_dir / "methods.js").write_text(
        "const AwsCompileApigEvents = require('../src');\n"
        "describe('#compileMethods()', () => {\n"
        "  test('should create method resources when http events given', () => {\n"
        "    const instance = new AwsCompileApigEvents();\n"
        "    instance.compileMethods();\n"
        "  });\n"
        "});\n",
        encoding="utf-8",
    )

    result = infer_polyglot_test_entries(
        tmp_path,
        language="javascript",
        test_ids=[
            "#compileMethods() should create method resources when http events given"
        ],
    )

    assert [(entry.test_id, entry.symbol) for entry in result.entries] == [
        (
            "tests/methods.js::#compileMethods():"
            "should create method resources when http events given",
            "src/index.js:AwsCompileApigEvents.constructor",
        ),
        (
            "tests/methods.js::#compileMethods():"
            "should create method resources when http events given",
            "src/methods.js:module.exports.compileMethods",
        ),
    ]


def test_polyglot_javascript_inference_seeds_p5_callback_receiver(tmp_path):
    from testprism.polyglot_entry_inference import infer_polyglot_test_entries

    production = tmp_path / "src" / "files.js"
    test = tmp_path / "test" / "files_input.js"
    production.parent.mkdir(parents=True)
    test.parent.mkdir(parents=True)
    production.write_text(
        "p5.prototype.loadStrings = function() {\n"
        "  return [];\n"
        "};\n"
        "p5.prototype.httpDo = function() {\n"
        "  return Promise.resolve();\n"
        "};\n",
        encoding="utf-8",
    )
    test.write_text(
        "suite('Files', function() {\n"
        "  var myp5;\n"
        "  setup(function() {\n"
        "    new p5(function(p) {\n"
        "      p.setup = function() {\n"
        "        myp5 = p;\n"
        "      };\n"
        "    });\n"
        "  });\n"
        "  suite('p5.prototype.loadStrings', function() {\n"
        "    test('should include empty strings', function() {\n"
        "      myp5.loadStrings('fixture.txt');\n"
        "    });\n"
        "  });\n"
        "});\n",
        encoding="utf-8",
    )

    result = infer_polyglot_test_entries(
        tmp_path,
        language="javascript",
        test_ids=["Files:p5.prototype.loadStrings:should include empty strings"],
    )

    assert [(entry.test_id, entry.symbol) for entry in result.entries] == [
        (
            "test/files_input.js::Files:p5.prototype.loadStrings:"
            "should include empty strings",
            "src/files.js:p5.prototype.loadStrings",
        )
    ]


def test_polyglot_test_path_predicate_accepts_exact_suffix_and_conventions():
    from testprism.polyglot_entry_inference import _looks_like_test

    assert _looks_like_test("src/unit_test.cpp", "cpp") is True
    assert _looks_like_test("tests/test_stack.cpp", "cpp") is True
    assert _looks_like_test("src/contest_utils.cpp", "cpp") is False
    assert _looks_like_test("src/latest.cpp", "cpp") is False
    assert _looks_like_test("src/test_stack.cpp", "cpp") is True
    assert _looks_like_test("src/WidgetTest.java", "java") is True
    assert _looks_like_test("src/unit_test.java", "java") is True
    assert _looks_like_test("src/acceptanceTest/java/Widget.java", "java") is True
    assert _looks_like_test("src/latest.java", "java") is False
    assert _looks_like_test("tests/actions.js", "javascript") is True
    assert _looks_like_test("src/resolve.test.js", "javascript") is True
    assert _looks_like_test("src/resolve.spec.ts", "typescript") is True
    assert _looks_like_test("src/latest.ts", "typescript") is False


def test_polyglot_selector_free_discovery_accepts_suffix_tests_only(tmp_path):
    from testprism.polyglot_entry_inference import discover_test_paths

    paths = [
        tmp_path / "src" / "unit_test.cpp",
        tmp_path / "tests" / "test_stack.cpp",
        tmp_path / "src" / "WidgetTest.java",
        tmp_path / "src" / "resolve.test.js",
        tmp_path / "tests" / "actions.js",
        tmp_path / "src" / "resolve.spec.ts",
        tmp_path / "src" / "contest_utils.cpp",
        tmp_path / "src" / "latest.cpp",
        tmp_path / "src" / "latest.java",
        tmp_path / "src" / "latest.ts",
        tmp_path / "tests" / "f2p.py",
    ]
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    cpp_paths = [
        path.relative_to(tmp_path).as_posix()
        for path in discover_test_paths(tmp_path, "cpp")
    ]
    assert cpp_paths == [
        "src/unit_test.cpp",
        "tests/test_stack.cpp",
    ]
    java_paths = [
        path.relative_to(tmp_path).as_posix()
        for path in discover_test_paths(tmp_path, "java")
    ]
    assert java_paths == [
        "src/WidgetTest.java",
    ]
    javascript_paths = [
        path.relative_to(tmp_path).as_posix()
        for path in discover_test_paths(tmp_path, "javascript")
    ]
    assert javascript_paths == [
        "src/resolve.test.js",
        "tests/actions.js",
    ]
    typescript_paths = [
        path.relative_to(tmp_path).as_posix()
        for path in discover_test_paths(tmp_path, "typescript")
    ]
    assert typescript_paths == [
        "src/resolve.spec.ts",
    ]


def test_polyglot_cpp_namespace_call_handles_multiline_and_global_ambiguity(
    tmp_path,
):
    from testprism.polyglot_entry_inference import infer_polyglot_test_entries

    header = tmp_path / "include" / "functions.hpp"
    test = tmp_path / "tests" / "test_functions.cpp"
    header.parent.mkdir(parents=True)
    test.parent.mkdir(parents=True)
    header.write_text(
        "namespace q {\n"
        "inline int fn(\n"
        "    int value\n"
        ") {\n"
        "    return value;\n"
        "}\n"
        "}\n"
        "inline int fn(int value) { return value + 1; }\n",
        encoding="utf-8",
    )
    test.write_text(
        '#include "../include/functions.hpp"\n'
        "TEST(QTest, Qualified) {\n"
        "    EXPECT_EQ(q::fn(1), 1);\n"
        "}\n"
        "TEST(QTest, Unqualified) {\n"
        "    EXPECT_EQ(fn(1), 2);\n"
        "}\n",
        encoding="utf-8",
    )

    result = infer_polyglot_test_entries(tmp_path, language="cpp")

    assert [
        (entry.test_id, entry.symbol, entry.start_line, entry.end_line)
        for entry in result.entries
    ] == [
        (
            "tests/test_functions.cpp::QTest.Qualified",
            "include/functions.hpp:q.fn",
            2,
            6,
        )
    ]


def test_polyglot_selector_free_discovery_excludes_dependency_and_build_trees(
    tmp_path,
):
    from testprism.polyglot_entry_inference import infer_polyglot_test_entries

    source = tmp_path / "src" / "widget.cpp"
    test = tmp_path / "tests" / "test_widget.cpp"
    vendor_source = tmp_path / "third_party" / "src" / "widget.cpp"
    vendor_test = tmp_path / "third_party" / "tests" / "test_widget.cpp"
    generated_source = tmp_path / "build" / "generated" / "widget.cpp"
    for path in (source, test, vendor_source, vendor_test, generated_source):
        path.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "class Widget {\npublic:\n  int value() { return 1; }\n};\n",
        encoding="utf-8",
    )
    test.write_text(
        "TEST(WidgetTest, Value) { Widget widget; widget.value(); }\n",
        encoding="utf-8",
    )
    vendor_source.write_text(
        "class VendorWidget {\npublic:\n  int value() { return 2; }\n};\n",
        encoding="utf-8",
    )
    vendor_test.write_text(
        "TEST(VendorWidgetTest, Value) { VendorWidget widget; widget.value(); }\n",
        encoding="utf-8",
    )
    generated_source.write_text(
        "class GeneratedWidget {\npublic:\n  int value() { return 3; }\n};\n",
        encoding="utf-8",
    )

    result = infer_polyglot_test_entries(tmp_path, language="cpp")

    assert [(entry.test_id, entry.path) for entry in result.entries] == [
        ("tests/test_widget.cpp::WidgetTest.Value", "src/widget.cpp")
    ]


def test_quality_gate_rejects_excluded_metadata_without_writing_yaml(
    tmp_path, monkeypatch
):
    module = _load_module()
    from testprism.entry_inference import InferenceResult
    from testprism.handlers.trial_handler import TestEntry, TestEntryInferences

    task_dir = tmp_path / "task"
    repo = task_dir / "deveval_source_repo"
    test = repo / "tests" / "test_widget.cpp"
    test.parent.mkdir(parents=True)
    test.write_text(
        "TEST(WidgetTest, Value) { Widget widget; widget.value(); }\n",
        encoding="utf-8",
    )
    task_yaml = task_dir / "task.yaml"
    original = "language: cpp\nsentinel: true\n"
    task_yaml.write_text(original, encoding="utf-8")
    polluted = InferenceResult(
        metadata=TestEntryInferences(
            language="cpp",
            entries=[
                TestEntry(
                    test_id="third_party/tests/test_widget.cpp::WidgetTest.Value",
                    path="third_party/src/widget.cpp",
                    symbol="third_party/src/widget.cpp:Widget.value",
                    start_line=1,
                    end_line=1,
                )
            ],
        )
    )
    monkeypatch.setattr(module, "infer_polyglot_test_entries", lambda *a, **k: polluted)

    status, failures = module.process_task(
        task_dir,
        repo_cache_dir=tmp_path / "cache",
        checkout_root=tmp_path / "checkouts",
        check=False,
        skip_existing=False,
    )

    assert (status, failures) == ("excluded-path-leakage", 0)
    assert task_yaml.read_text(encoding="utf-8") == original


def test_cli_accumulates_process_task_failures(tmp_path, monkeypatch, capsys):
    module = _load_module()
    first = tmp_path / "first"
    second = tmp_path / "second"

    def fake_process(task_dir, **_kwargs):
        if task_dir == first.resolve():
            return "failed-inference", 1
        return "written", 0

    monkeypatch.setattr(module, "process_task", fake_process)

    exit_code = module.main(["--task-dir", str(first), "--task-dir", str(second)])

    assert exit_code == 1
    assert "processed=2 failures=1" in capsys.readouterr().out


def test_quality_gate_rejects_empty_and_class_spans_before_write(tmp_path):
    module = _load_module()
    from testprism.entry_inference import InferenceResult
    from testprism.handlers.trial_handler import TestEntry, TestEntryInferences

    empty = InferenceResult(
        metadata=TestEntryInferences(entries=[]),
    )
    failure = module.quality_gate_reason(tmp_path, empty)
    assert failure is not None
    assert failure.status == "empty-inference"

    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "api.py").write_text(
        "class Widget:\n    def method(self):\n        return 1\n",
        encoding="utf-8",
    )
    polluted = InferenceResult(
        metadata=TestEntryInferences(
            entries=[
                TestEntry(
                    test_id="tests/test_api.py::test_widget",
                    path="pkg/api.py",
                    symbol="pkg/api.py:Widget",
                    start_line=1,
                    end_line=3,
                )
            ]
        ),
    )
    failure = module.quality_gate_reason(tmp_path, polluted)
    assert failure is not None
    assert failure.status == "class-definition-span"


def test_cached_checkout_is_refused_for_duplicate_task_leaf(tmp_path):
    module = _load_module()
    tasks_root = tmp_path / "verified_tasks" / "test-hardest-500"
    first = tasks_root / "family-a" / "duplicate"
    second = tasks_root / "family-b" / "duplicate"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    (first / "task.yaml").write_text("language: python\n", encoding="utf-8")
    (second / "task.yaml").write_text("language: python\n", encoding="utf-8")
    checkout = tmp_path / ".cache" / "entry-checkouts" / "duplicate"
    checkout.mkdir(parents=True)
    (checkout / "module.py").write_text("VALUE = 1\n", encoding="utf-8")

    assert module._cached_repo_root(first, checkout.parent, {}) is None


def test_apply_patch_treats_already_applied_diff_as_success(tmp_path):
    module = _load_module()
    (tmp_path / "demo.txt").write_text("new\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "add", "demo.txt"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    patch_text = """\
diff --git a/demo.txt b/demo.txt
--- a/demo.txt
+++ b/demo.txt
@@ -1 +1 @@
-old
+new
"""

    module._apply_patch(tmp_path, patch_text)
    assert (tmp_path / "demo.txt").read_text(encoding="utf-8") == "new\n"


def test_apply_patch_rejects_fuzzy_only_state_without_invoking_patch_tool(
    tmp_path, monkeypatch
):
    module = _load_module()
    calls: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(
            cmd,
            1,
            stdout="",
            stderr="context does not match exactly",
        )

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    try:
        module._apply_patch(tmp_path, "fuzzy candidate\n")
    except module.UnprovablePatchState as exc:
        assert "forward: context does not match exactly" in str(exc)
        assert "reverse: context does not match exactly" in str(exc)
    else:  # pragma: no cover - this fixture must be rejected
        raise AssertionError("fuzzy-only patch unexpectedly accepted")

    assert [command[:3] for command in calls] == [
        ["git", "apply", "--whitespace=nowarn"],
        ["git", "apply", "--reverse"],
    ]
    assert all(command[0] != "patch" for command in calls)


def test_repository_slug_uses_org_when_repo_has_no_owner():
    module = _load_module()

    assert module._repository_slug({"repo": "pydata/xarray"}) == "pydata/xarray"
    assert module._repository_slug({"org": "astropy", "repo": "astropy"}) == (
        "astropy/astropy"
    )


def test_repo_materialization_uses_provenance_keyed_clone_target(tmp_path, monkeypatch):
    module = _load_module()
    calls: list[tuple[list[str], Path]] = []

    def fake_run(cmd, *, cwd):
        calls.append((list(cmd), cwd))
        if cmd[:3] == ["git", "clone", "--mirror"]:
            (cwd / cmd[-1]).mkdir(parents=True, exist_ok=True)
        elif cmd[:2] == ["git", "clone"]:
            target = cwd / cmd[-1]
            target.mkdir(parents=True, exist_ok=True)
            (target / ".git").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(module, "_run", fake_run)
    monkeypatch.setattr(module, "_apply_patch", lambda repo_root, patch_text: None)
    monkeypatch.setattr(
        module, "_exact_checkout_proof_failure", lambda *args, **kwargs: None
    )

    task_dir = tmp_path / "verified_tasks" / "sample-task"
    task_dir.mkdir(parents=True)
    repo_cache_dir = tmp_path / ".cache" / "entry-repo-mirrors"
    checkout_root = tmp_path / ".cache" / "entry-checkouts"
    record = {"repo": "pydata/xarray", "base_commit": "abc1234"}

    checkout_path = module.materialize_repo_for_task(
        task_dir,
        record,
        repo_cache_dir=repo_cache_dir,
        checkout_root=checkout_root,
    )

    clone_calls = [call for call in calls if call[0][:2] == ["git", "clone"]]
    mirror_clone, checkout_clone = clone_calls

    assert mirror_clone == (
        [
            "git",
            "clone",
            "--mirror",
            "https://github.com/pydata/xarray.git",
            "pydata__xarray.git",
        ],
        repo_cache_dir,
    )
    assert checkout_clone[0] == [
        "git",
        "clone",
        str((repo_cache_dir / "pydata__xarray.git").resolve()),
        "checkout",
    ]
    assert checkout_clone[1].parent == checkout_root
    assert checkout_clone[1].name.startswith(".sample-task--")
    assert checkout_path.parent == checkout_root
    assert checkout_path.name.startswith("sample-task--")
    assert len(checkout_path.name.rsplit("--", 1)[1]) == 24


def _init_git_repo(path: Path, *, filename: str = "module.py") -> str:
    path.mkdir(parents=True, exist_ok=True)
    (path / filename).write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "add", filename], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "base"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _task_and_checkout_paths(tmp_path: Path) -> tuple[Path, Path]:
    task_dir = tmp_path / "verified_tasks" / "family" / "sample-task"
    task_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text("language: python\n", encoding="utf-8")
    checkout_root = tmp_path / ".cache" / "entry-checkouts"
    checkout_root.mkdir(parents=True)
    return task_dir, checkout_root


def test_cached_checkout_rejects_wrong_head_and_unapplied_patch(tmp_path):
    module = _load_module()
    task_dir, checkout_root = _task_and_checkout_paths(tmp_path)
    source = tmp_path / "source"
    base_commit = _init_git_repo(source)
    (source / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    patch_text = subprocess.run(
        ["git", "diff", "--", "module.py"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    subprocess.run(
        ["git", "add", "module.py"], cwd=source, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "second"],
        cwd=source,
        check=True,
        capture_output=True,
    )
    candidate = checkout_root / task_dir.name
    subprocess.run(
        ["git", "clone", str(source), str(candidate)],
        check=True,
        capture_output=True,
    )
    record = {"base_commit": base_commit, "patch": patch_text}

    # The checkout has a valid git worktree and Python source, but its HEAD is
    # not the task base commit.
    assert module._cached_repo_root(task_dir, checkout_root, record) is None

    # Rewind to the task base: the patch is now simply unapplied and must not
    # be mistaken for a post-fix checkout.
    subprocess.run(
        ["git", "checkout", "--force", base_commit],
        cwd=candidate,
        check=True,
        capture_output=True,
    )
    assert module._cached_repo_root(task_dir, checkout_root, record) is None


def test_cached_checkout_rejects_partially_applied_patch(tmp_path):
    module = _load_module()
    task_dir, checkout_root = _task_and_checkout_paths(tmp_path)
    source = tmp_path / "source"
    base_commit = _init_git_repo(source)
    (source / "other.py").write_text("OTHER = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "other.py"], cwd=source, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "--amend", "-m", "base-with-other"],
        cwd=source,
        check=True,
        capture_output=True,
    )
    base_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    # Build a two-file patch from the original base, then put only one hunk in
    # the candidate.  Reverse-check must reject the incomplete application.
    (source / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    (source / "other.py").write_text("OTHER = 2\n", encoding="utf-8")
    patch_text = subprocess.run(
        ["git", "diff", "--", "module.py", "other.py"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    candidate = checkout_root / task_dir.name
    subprocess.run(
        ["git", "clone", str(source), str(candidate)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "checkout", "--force", base_commit],
        cwd=candidate,
        check=True,
        capture_output=True,
    )
    (candidate / "module.py").write_text("VALUE = 2\n", encoding="utf-8")

    assert (
        module._cached_repo_root(
            task_dir,
            checkout_root,
            {"base_commit": base_commit, "patch": patch_text},
        )
        is None
    )


def test_cached_checkout_accepts_complete_base_and_gold_test_patches(tmp_path):
    module = _load_module()
    task_dir, checkout_root = _task_and_checkout_paths(tmp_path)
    source = tmp_path / "source"
    base_commit = _init_git_repo(source)
    (source / "tests.py").write_text(
        "def test_value():\n    assert False\n", encoding="utf-8"
    )
    subprocess.run(
        ["git", "add", "tests.py"], cwd=source, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "--amend", "-m", "base-with-tests"],
        cwd=source,
        check=True,
        capture_output=True,
    )
    base_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (source / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    gold_patch = subprocess.run(
        ["git", "diff", "--", "module.py"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert gold_patch.strip()
    subprocess.run(
        ["git", "restore", "module.py"], cwd=source, check=True, capture_output=True
    )
    (source / "tests.py").write_text(
        "def test_value():\n    assert True\n", encoding="utf-8"
    )
    test_patch = subprocess.run(
        ["git", "diff", "--", "tests.py"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert test_patch.strip()
    candidate = checkout_root / task_dir.name
    subprocess.run(
        ["git", "clone", str(source), str(candidate)],
        check=True,
        capture_output=True,
    )
    module._apply_patch(candidate, gold_patch)
    module._apply_patch(candidate, test_patch)

    assert (
        module._cached_repo_root(
            task_dir,
            checkout_root,
            {
                "base_commit": base_commit,
                "patch": gold_patch,
                "test_patch": test_patch,
            },
        )
        == candidate
    )


def test_cached_checkout_uses_fix_patch_and_requires_both_effective_patches(tmp_path):
    module = _load_module()
    task_dir, checkout_root = _task_and_checkout_paths(tmp_path)
    source = tmp_path / "source"
    base_commit = _init_git_repo(source)
    (source / "tests.py").write_text(
        "def test_value():\n    assert False\n", encoding="utf-8"
    )
    subprocess.run(
        ["git", "add", "tests.py"], cwd=source, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "--amend", "-m", "base-with-tests"],
        cwd=source,
        check=True,
        capture_output=True,
    )
    base_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    (source / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    fix_patch = subprocess.run(
        ["git", "diff", "--", "module.py"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    subprocess.run(
        ["git", "restore", "module.py"], cwd=source, check=True, capture_output=True
    )
    (source / "module.py").write_text("VALUE = 3\n", encoding="utf-8")
    backup_patch = subprocess.run(
        ["git", "diff", "--", "module.py"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    subprocess.run(
        ["git", "restore", "module.py"], cwd=source, check=True, capture_output=True
    )
    (source / "tests.py").write_text(
        "def test_value():\n    assert True\n", encoding="utf-8"
    )
    test_patch = subprocess.run(
        ["git", "diff", "--", "tests.py"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert fix_patch.strip()
    assert backup_patch.strip()
    assert test_patch.strip()

    candidate = checkout_root / task_dir.name
    subprocess.run(
        ["git", "clone", str(source), str(candidate)], check=True, capture_output=True
    )
    record = {
        "base_commit": base_commit,
        "fix_patch": fix_patch,
        "patch": backup_patch,
        "test_patch": test_patch,
    }

    # The effective gold patch alone is incomplete without the test patch.
    module._apply_patch(candidate, fix_patch)
    assert module._cached_repo_root(task_dir, checkout_root, record) is None

    # The test patch alone is incomplete without the effective gold patch.
    subprocess.run(
        ["git", "checkout", "--force", base_commit],
        cwd=candidate,
        check=True,
        capture_output=True,
    )
    module._apply_patch(candidate, test_patch)
    assert module._cached_repo_root(task_dir, checkout_root, record) is None

    # Both applied patches prove the post-fix checkout; the unused backup
    # patch is intentionally absent and must not invalidate provenance.
    subprocess.run(
        ["git", "checkout", "--force", base_commit],
        cwd=candidate,
        check=True,
        capture_output=True,
    )
    module._apply_patch(candidate, fix_patch)
    module._apply_patch(candidate, test_patch)
    assert module._cached_repo_root(task_dir, checkout_root, record) == candidate


def test_materialize_failure_removes_only_new_checkout(tmp_path, monkeypatch):
    module = _load_module()
    task_dir = tmp_path / "verified_tasks" / "family" / "sample-task"
    task_dir.mkdir(parents=True)
    checkout_root = tmp_path / ".cache" / "entry-checkouts"
    checkout_root.mkdir(parents=True)
    sentinel = checkout_root / "keep-me"
    sentinel.mkdir()
    mirror = tmp_path / ".cache" / "mirror.git"
    mirror.mkdir(parents=True)
    record = {
        "repo": "org/repo",
        "base_commit": "abc1234",
        "patch": "not a patch",
    }
    prior_cache = checkout_root / module._checkout_key(task_dir, record)
    prior_cache.mkdir()
    (prior_cache / "marker").write_text("keep\n", encoding="utf-8")

    def fake_run(cmd, *, cwd):
        if cmd[:3] == ["git", "clone", "--mirror"]:
            (cwd / cmd[-1]).mkdir(parents=True, exist_ok=True)
        elif cmd[:2] == ["git", "clone"]:
            target = cwd / cmd[-1]
            target.mkdir(parents=True, exist_ok=True)
            (target / ".git").mkdir()

    monkeypatch.setattr(module, "_ensure_repo_mirror", lambda repo, cache: mirror)
    monkeypatch.setattr(module, "_run", fake_run)
    monkeypatch.setattr(
        module,
        "_apply_patch",
        lambda repo_root, patch_text: (_ for _ in ()).throw(RuntimeError("bad patch")),
    )

    try:
        module.materialize_repo_for_task(
            task_dir,
            record,
            repo_cache_dir=tmp_path / ".cache" / "mirrors",
            checkout_root=checkout_root,
        )
    except RuntimeError as exc:
        assert str(exc) == "bad patch"
    else:  # pragma: no cover - the setup must fail for this fixture
        raise AssertionError("materialization unexpectedly succeeded")

    assert not any(
        path.name.startswith(f".{task_dir.name}--") for path in checkout_root.iterdir()
    )
    assert sentinel.is_dir()
    assert (prior_cache / "marker").read_text(encoding="utf-8") == "keep\n"


def _build_materialization_fixture(tmp_path: Path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "pkg").mkdir()
    (source / "tests").mkdir()
    (source / "pkg" / "api.py").write_text(
        "def value():\n    return 1\n", encoding="utf-8"
    )
    (source / "tests" / "test_api.py").write_text(
        "from pkg.api import value\n\ndef test_value():\n    assert value() == 1\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init"], cwd=source, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=source,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=source,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "add", "."], cwd=source, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "base"], cwd=source, check=True, capture_output=True
    )
    base_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    (source / "pkg" / "api.py").write_text(
        "def value():\n    return 2\n", encoding="utf-8"
    )
    gold_patch = subprocess.run(
        ["git", "diff", "--", "pkg/api.py"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    subprocess.run(
        ["git", "restore", "pkg/api.py"], cwd=source, check=True, capture_output=True
    )
    (source / "tests" / "test_api.py").write_text(
        "from pkg.api import value\n\ndef test_value():\n    assert value() == 2\n",
        encoding="utf-8",
    )
    test_patch = subprocess.run(
        ["git", "diff", "--", "tests/test_api.py"],
        cwd=source,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    subprocess.run(
        ["git", "restore", "tests/test_api.py"],
        cwd=source,
        check=True,
        capture_output=True,
    )
    return source, base_commit, gold_patch, test_patch


def test_process_task_materializes_once_then_reuses_proven_cache(tmp_path, monkeypatch):
    module = _load_module()
    source, base_commit, gold_patch, test_patch = _build_materialization_fixture(
        tmp_path
    )
    task_dir = tmp_path / "verified_tasks" / "family" / "sample-task"
    (task_dir / "tests").mkdir(parents=True)
    (task_dir / "task.yaml").write_text("language: python\n", encoding="utf-8")
    record = {
        "repo": "example/project",
        "base_commit": base_commit,
        "patch": gold_patch,
        "test_patch": test_patch,
        "FAIL_TO_PASS": ["tests/test_api.py::test_value"],
    }
    (task_dir / "tests" / "config.json").write_text(
        json.dumps(record),
        encoding="utf-8",
    )
    mirror_calls = 0

    def fake_mirror(repo, cache):
        nonlocal mirror_calls
        mirror_calls += 1
        return source

    monkeypatch.setattr(module, "_ensure_repo_mirror", fake_mirror)
    checkout_root = tmp_path / ".cache" / "entry-checkouts"

    first = module.process_task(
        task_dir,
        repo_cache_dir=tmp_path / ".cache" / "mirrors",
        checkout_root=checkout_root,
        check=False,
        skip_existing=False,
    )
    second = module.process_task(
        task_dir,
        repo_cache_dir=tmp_path / ".cache" / "mirrors",
        checkout_root=checkout_root,
        check=True,
        skip_existing=False,
    )

    assert first == ("written", 0)
    assert second == ("checked", 0)
    assert mirror_calls == 1
    checkout = checkout_root / module._checkout_key(task_dir, record)
    assert (
        (checkout / "pkg" / "api.py").read_text(encoding="utf-8").endswith("return 2\n")
    )
    assert (
        (checkout / "tests" / "test_api.py")
        .read_text(encoding="utf-8")
        .endswith("assert value() == 2\n")
    )
    metadata = yaml.safe_load((task_dir / "task.yaml").read_text(encoding="utf-8"))[
        "test_entry_inferences"
    ]
    assert [(entry["path"], entry["test_id"]) for entry in metadata["entries"]] == [
        ("pkg/api.py", "tests/test_api.py::test_value")
    ]


def test_process_task_rejects_fresh_checkout_without_exact_patch_proof(
    tmp_path, monkeypatch
):
    module = _load_module()
    source, base_commit, gold_patch, test_patch = _build_materialization_fixture(
        tmp_path
    )
    task_dir = tmp_path / "verified_tasks" / "family" / "unproven-task"
    (task_dir / "tests").mkdir(parents=True)
    task_yaml = task_dir / "task.yaml"
    task_yaml.write_text("language: python\nsentinel: true\n", encoding="utf-8")
    record = {
        "repo": "example/project",
        "base_commit": base_commit,
        "patch": gold_patch,
        "test_patch": test_patch,
        "FAIL_TO_PASS": ["tests/test_api.py::test_value"],
    }
    (task_dir / "tests" / "config.json").write_text(
        json.dumps(record), encoding="utf-8"
    )
    monkeypatch.setattr(module, "_ensure_repo_mirror", lambda repo, cache: source)
    monkeypatch.setattr(module, "_apply_patch", lambda repo, patch: None)

    status, failures = module.process_task(
        task_dir,
        repo_cache_dir=tmp_path / "mirrors",
        checkout_root=tmp_path / "checkouts",
        check=False,
        skip_existing=False,
    )

    assert failures == 0
    assert status.startswith("unverifiable-post-fix-checkout: post-materialization")
    assert "gold patch is not exactly reverse-applicable" in status
    assert task_yaml.read_text(encoding="utf-8") == (
        "language: python\nsentinel: true\n"
    )
    assert not any((tmp_path / "checkouts").iterdir())


def test_cli_reports_patch_conflict_and_continues_without_failure(
    tmp_path, monkeypatch, capsys
):
    module = _load_module()
    first = tmp_path / "openlibrary"
    second = tmp_path / "next-task"

    def fake_process(task_dir, **_kwargs):
        if task_dir == first.resolve():
            return (
                "unverifiable-post-fix-checkout: test patch conflict: "
                "forward and reverse exact checks failed",
                0,
            )
        return "written", 0

    monkeypatch.setattr(module, "process_task", fake_process)

    exit_code = module.main(["--task-dir", str(first), "--task-dir", str(second)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "unverifiable-post-fix-checkout: test patch conflict" in output
    assert f"written: {second.resolve()}" in output
    assert "processed=2 failures=0" in output


def test_same_leaf_with_different_provenance_uses_isolated_checkout_keys(
    tmp_path, monkeypatch
):
    module = _load_module()
    checkout_root = tmp_path / "checkouts"
    mirror = tmp_path / "mirror.git"
    mirror.mkdir()

    def fake_run(cmd, *, cwd):
        if cmd[:2] == ["git", "clone"]:
            (cwd / cmd[-1]).mkdir()

    monkeypatch.setattr(module, "_ensure_repo_mirror", lambda repo, cache: mirror)
    monkeypatch.setattr(module, "_run", fake_run)
    monkeypatch.setattr(module, "_apply_patch", lambda repo, patch: None)
    monkeypatch.setattr(
        module, "_exact_checkout_proof_failure", lambda *args, **kwargs: None
    )
    first_task = tmp_path / "tasks" / "family-a" / "same"
    second_task = tmp_path / "tasks" / "family-b" / "same"
    first_task.mkdir(parents=True)
    second_task.mkdir(parents=True)
    first = module.materialize_repo_for_task(
        first_task,
        {"repo": "org/project", "base_commit": "a" * 40, "patch": "one"},
        repo_cache_dir=tmp_path / "mirrors",
        checkout_root=checkout_root,
    )
    second = module.materialize_repo_for_task(
        second_task,
        {"repo": "org/project", "base_commit": "b" * 40, "patch": "two"},
        repo_cache_dir=tmp_path / "mirrors",
        checkout_root=checkout_root,
    )

    assert first != second
    assert first.is_dir()
    assert second.is_dir()


def test_checkout_key_changes_when_before_repo_setup_changes(tmp_path):
    module = _load_module()
    task_dir = tmp_path / "tasks" / "family" / "same"
    common = {
        "repo": "org/project",
        "base_commit": "a" * 40,
        "patch": "same patch",
    }

    first = module._checkout_key(
        task_dir,
        {**common, "before_repo_set_cmd": "\n git checkout abc1234 -- tests/a.py \n"},
    )
    equivalent = module._checkout_key(
        task_dir,
        {**common, "before_repo_set_cmd": "git checkout abc1234 -- tests/a.py"},
    )
    second = module._checkout_key(
        task_dir,
        {**common, "before_repo_set_cmd": "git checkout def5678 -- tests/a.py"},
    )

    assert first == equivalent
    assert first != second


def test_process_task_missing_or_malformed_provenance_does_not_clone(
    tmp_path, monkeypatch
):
    module = _load_module()
    task_dir = tmp_path / "verified_tasks" / "family" / "sample-task"
    (task_dir / "tests").mkdir(parents=True)
    (task_dir / "task.yaml").write_text("language: python\n", encoding="utf-8")
    (task_dir / "tests" / "config.json").write_text(
        '{"repo": "https://example.invalid/repo", "base_commit": "main"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        module,
        "materialize_repo_for_task",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("cloned")),
    )

    assert module.process_task(
        task_dir,
        repo_cache_dir=tmp_path / "mirrors",
        checkout_root=tmp_path / "checkouts",
        check=False,
        skip_existing=False,
    ) == ("missing-post-fix-checkout", 0)


def test_before_repo_setup_rejects_non_git_commands(tmp_path, monkeypatch):
    module = _load_module()
    calls = []
    monkeypatch.setattr(module, "_run", lambda cmd, *, cwd: calls.append(cmd))

    try:
        module._run_before_repo_setup(tmp_path, "python setup.py\ngit status")
    except ValueError as exc:
        assert "unsupported before_repo_set_cmd" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("arbitrary setup unexpectedly allowed")
    assert calls == []
