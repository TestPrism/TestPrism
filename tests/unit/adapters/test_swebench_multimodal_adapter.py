import importlib.util
import subprocess
import sys
import types
from enum import Enum
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
ADAPTER_DIR = REPO_ROOT / "adapters" / "swe-bench-multimodal"


def _load_parser_template():
    path = ADAPTER_DIR / "template" / "tests" / "parser.py"
    spec = importlib.util.spec_from_file_location("swebench_mm_parser_template", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_adapter(monkeypatch, utils):
    datasets = types.ModuleType("datasets")
    datasets.load_dataset = lambda *_args, **_kwargs: None
    monkeypatch.setitem(sys.modules, "datasets", datasets)
    monkeypatch.setitem(sys.modules, "utils", utils)

    path = ADAPTER_DIR / "adapter.py"
    spec = importlib.util.spec_from_file_location("swebench_mm_adapter_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_utils(monkeypatch):
    constants = types.ModuleType("swebench.harness.constants")
    constants.LATEST = "latest"

    javascript_constants = types.ModuleType(
        "swebench.harness.constants.javascript"
    )
    javascript_constants.MAP_REPO_VERSION_TO_SPECS_JS = {
        "example/react-pdf": {
            "2.0": {
                "test_cmd": "jest --no-color",
                "install": ["npm install"],
            }
        },
        "Automattic/wp-calypso": {
            "1.0": {
                "test_cmd": "npm run test-client",
                "install": ["npm install"],
            }
        },
    }

    javascript_test_spec = types.ModuleType(
        "swebench.harness.test_spec.javascript"
    )
    javascript_test_spec.MAP_REPO_TO_TEST_CMDS = {
        "Automattic/wp-calypso": lambda instance: [
            "./node_modules/.bin/jest "
            f"--verbose {instance['version']} affected.test.js"
        ]
    }

    fake_modules = {
        "swebench": types.ModuleType("swebench"),
        "swebench.harness": types.ModuleType("swebench.harness"),
        "swebench.harness.constants": constants,
        "swebench.harness.constants.javascript": javascript_constants,
        "swebench.harness.test_spec": types.ModuleType(
            "swebench.harness.test_spec"
        ),
        "swebench.harness.test_spec.javascript": javascript_test_spec,
    }
    for name, module in fake_modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    path = ADAPTER_DIR / "utils.py"
    spec = importlib.util.spec_from_file_location("swebench_mm_utils_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_legacy_one_argument_parser_is_adapted_without_masking_parser_errors():
    parser_template = _load_parser_template()
    sentinel_spec = object()

    def legacy_parser(log):
        return {"received": log}

    adapted = parser_template.adapt_log_parser(legacy_parser)
    assert adapted("test output", sentinel_spec) == {"received": "test output"}

    def modern_parser(log, test_spec):
        assert test_spec is sentinel_spec
        raise TypeError("error raised inside parser")

    unchanged = parser_template.adapt_log_parser(modern_parser)
    assert unchanged is modern_parser
    try:
        unchanged("test output", sentinel_spec)
    except TypeError as error:
        assert str(error) == "error raised inside parser"
    else:
        raise AssertionError("internal parser TypeError must remain visible")


def test_empty_or_failed_parser_output_is_an_infrastructure_error():
    parser_template = _load_parser_template()

    with pytest.raises(RuntimeError, match="no parseable SWE-bench"):
        parser_template.ensure_parse_result(
            {},
            has_oracle_tests=True,
            fail_only=False,
            test_exit_code=0,
        )
    with pytest.raises(RuntimeError, match=r"exit code 1"):
        parser_template.ensure_parse_result(
            {},
            has_oracle_tests=True,
            fail_only=True,
            test_exit_code=1,
        )

    # Failure-only parsers report only failures, so an empty map plus a clean
    # runner exit is their valid representation of an all-passing suite.
    parser_template.ensure_parse_result(
        {},
        has_oracle_tests=True,
        fail_only=True,
        test_exit_code=0,
    )


def test_upstream_full_suite_command_is_not_narrowed_to_patch_files(monkeypatch):
    utils = _load_utils(monkeypatch)
    test_patch = (
        "diff --git a/affected.test.js b/affected.test.js\n"
        "--- a/affected.test.js\n"
        "+++ b/affected.test.js\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )

    script = utils.get_test_commands(
        test_patch, "example/react-pdf", "2.0", "deadbeef"
    )

    assert "\n        jest --no-color\n" in script
    assert "jest --no-color affected.test.js" not in script
    assert "npm install" in script
    assert "set -euo pipefail" in script
    assert "git apply --check /tmp/test_patch.diff" in script
    assert "git apply --check /tmp/test_patch.diff 2>/dev/null || true" not in script
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)


def test_patch_aware_upstream_command_builder_defines_executed_scope(monkeypatch):
    utils = _load_utils(monkeypatch)
    test_patch = (
        "diff --git a/affected.test.js b/affected.test.js\n"
        "--- a/affected.test.js\n"
        "+++ b/affected.test.js\n"
        "@@ -1 +1 @@\n-old\n+new\n"
    )

    script = utils.get_test_commands(
        test_patch, "Automattic/wp-calypso", "1.0", "deadbeef"
    )

    assert "./node_modules/.bin/jest --verbose 1.0 affected.test.js" in script
    assert (
        '--json --outputFile="$CALYPSO_JEST_JSON_DIR/result-0.json"' in script
    )
    assert "CALYPSO_JEST_JSON_DIR=$(mktemp -d)" in script
    assert "npm run test-client affected.test.js" not in script
    assert "TEST_COMMAND_EXIT_CODE=$?" in script
    assert "export TEST_COMMAND_EXIT_CODE" in script


def test_new_test_files_are_reset_without_hiding_checkout_failures(monkeypatch):
    utils = _load_utils(monkeypatch)
    test_patch = (
        "diff --git a/new.test.js b/new.test.js\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/new.test.js\n"
        "@@ -0,0 +1 @@\n+test('new', () => {});\n"
    )

    script = utils.get_test_commands(
        test_patch, "example/react-pdf", "2.0", "deadbeef"
    )

    assert 'git cat-file -e "${BASE_COMMIT}:${test_file}"' in script
    assert 'rm -f -- "$test_file"' in script
    assert "reset_test_files new.test.js" in script
    assert "git checkout deadbeef new.test.js 2>/dev/null || true" not in script


def test_install_commands_clean_node_modules_and_use_lockfiles(monkeypatch):
    utils = _load_utils(monkeypatch)

    npm = utils._render_install_commands(
        ["npm install --unsafe-perm", "node ./scripts/build.js"]
    )
    assert "rm -rf node_modules" in npm
    assert "if [ -f package-lock.json ]" in npm
    assert "npm ci --unsafe-perm" in npm
    assert "node ./scripts/build.js" in npm

    yarn = utils._render_install_commands(["npm i -g yarn", "yarn install"])
    assert "npm install --global yarn@1.22.22" in yarn
    assert "npm i -g yarn\n" not in yarn
    assert "if [ -f yarn.lock ]" in yarn
    assert "yarn install --frozen-lockfile" in yarn
    assert yarn.count("rm -rf node_modules") == 1


@pytest.mark.parametrize(
    ("task_name", "expected_count"),
    [
        ("Automattic__wp-calypso-34597", 30),
        ("Automattic__wp-calypso-25160", 295),
    ],
)
def test_calypso_json_names_cover_real_f2p_and_p2p(
    tmp_path, task_name, expected_count
):
    parser_template = _load_parser_template()
    config_path = (
        REPO_ROOT
        / "verified_tasks"
        / "hardest-500-patch-oriented"
        / "from-swe-bench-multimodal"
        / task_name
        / "tests"
        / "config.json"
    )
    config = __import__("json").loads(config_path.read_text(encoding="utf-8"))
    fail_to_pass = __import__("json").loads(config["FAIL_TO_PASS"])
    pass_to_pass = __import__("json").loads(config["PASS_TO_PASS"])
    references = [*fail_to_pass, *pass_to_pass]
    assert len(references) == expected_count

    # Jest JSON fullName separates ancestor suites with spaces; SWE-bench's
    # Calypso parser adds " - ". The parser must align both representations.
    assertions = [
        {"fullName": reference.replace(" - ", " "), "status": "passed"}
        for reference in references
    ]
    (tmp_path / "result-0.json").write_text(
        __import__("json").dumps(
            {"testResults": [{"assertionResults": assertions}]}
        ),
        encoding="utf-8",
    )

    results = parser_template.load_calypso_jest_results(
        str(tmp_path), references
    )
    assert len(results) == expected_count
    assert set(results.values()) == {"PASSED"}


def test_calypso_json_requires_complete_reference_coverage(tmp_path):
    parser_template = _load_parser_template()
    (tmp_path / "result-0.json").write_text(
        '{"testResults":[{"assertionResults":['
        '{"fullName":"suite fixed","status":"passed"}]}]}',
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="did not cover.*regression"):
        parser_template.load_calypso_jest_results(
            str(tmp_path), ["suite - fixed", "suite - regression"]
        )


def test_calypso_main_scores_json_without_using_fragile_log_parser(
    monkeypatch, tmp_path, capsys
):
    parser_template = _load_parser_template()

    class EvalType(Enum):
        FAIL_ONLY = "fail_only"
        PASS_AND_FAIL = "pass_and_fail"

    class ResolvedStatus(Enum):
        FULL = "FULL"

    constants = types.ModuleType("swebench.harness.constants")
    constants.END_TEST_OUTPUT = "END"
    constants.START_TEST_OUTPUT = "START"
    constants.FAIL_ONLY_REPOS = set()
    constants.FAIL_TO_PASS = "FAIL_TO_PASS"
    constants.PASS_TO_PASS = "PASS_TO_PASS"
    constants.KEY_INSTANCE_ID = "instance_id"
    constants.EvalType = EvalType
    constants.ResolvedStatus = ResolvedStatus

    grading = types.ModuleType("swebench.harness.grading")
    grading.MAP_REPO_TO_PARSER = {}
    grading.get_logs_eval = lambda *_args: (_ for _ in ()).throw(
        AssertionError("Calypso must use Jest JSON, not parse_log_calypso")
    )
    grading.get_eval_tests_report = (
        lambda status_map, _reference, eval_type: status_map
    )
    grading.get_resolution_status = lambda report: (
        "FULL" if report and set(report.values()) == {"PASSED"} else "NO"
    )

    test_spec_module = types.ModuleType(
        "swebench.harness.test_spec.test_spec"
    )
    test_spec_module.make_test_spec = lambda datum: SimpleNamespace(
        repo=datum["repo"],
        instance_id=datum["instance_id"],
        FAIL_TO_PASS=datum["FAIL_TO_PASS"],
        PASS_TO_PASS=datum["PASS_TO_PASS"],
    )
    for name, module in {
        "swebench": types.ModuleType("swebench"),
        "swebench.harness": types.ModuleType("swebench.harness"),
        "swebench.harness.constants": constants,
        "swebench.harness.grading": grading,
        "swebench.harness.test_spec": types.ModuleType(
            "swebench.harness.test_spec"
        ),
        "swebench.harness.test_spec.test_spec": test_spec_module,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    config_path = tmp_path / "config.json"
    config_path.write_text(
        '{"repo":"Automattic/wp-calypso","instance_id":"calypso-1",'
        '"FAIL_TO_PASS":["suite - fixed"],'
        '"PASS_TO_PASS":["suite - stable"]}',
        encoding="utf-8",
    )
    log_path = tmp_path / "test.log"
    log_path.write_text("human-readable Jest output", encoding="utf-8")
    json_dir = tmp_path / "json"
    json_dir.mkdir()
    (json_dir / "result-0.json").write_text(
        '{"testResults":[{"assertionResults":['
        '{"fullName":"suite fixed","status":"passed"},'
        '{"fullName":"suite stable","status":"passed"}]}]}',
        encoding="utf-8",
    )
    monkeypatch.setenv("TESTPRISM_CONFIG_FILE", str(config_path))
    monkeypatch.setenv("LOG_FILE", str(log_path))
    monkeypatch.setenv("CALYPSO_JEST_JSON_DIR", str(json_dir))
    monkeypatch.setenv("TEST_COMMAND_EXIT_CODE", "0")

    assert parser_template.main() == 0
    assert "PASSED" in capsys.readouterr().out


def test_generated_task_installs_parser_under_runtime_tests_mount(
    monkeypatch, tmp_path
):
    utils = _load_utils(monkeypatch)
    adapter_module = _load_adapter(monkeypatch, utils)
    task = SimpleNamespace(
        instance_id="example__react-pdf-1",
        problem_statement="Fix the renderer.",
        difficulty="hard",
        test_patch=(
            "diff --git a/affected.test.js b/affected.test.js\n"
            "--- a/affected.test.js\n"
            "+++ b/affected.test.js\n"
            "@@ -1 +1 @@\n-old\n+new\n"
        ),
        repo="example/react-pdf",
        version="2.0",
        base_commit="deadbeef",
        patch="diff --git a/source.js b/source.js\n",
    )
    adapter = adapter_module.SWEBenchAdapter.__new__(
        adapter_module.SWEBenchAdapter
    )
    adapter.id_to_docker_image = {
        task.instance_id: "example/swebench-runtime:latest"
    }
    output_dir = tmp_path / "generated-task"

    adapter._prepare_task_from_template(task, output_dir)

    assert (output_dir / "tests" / "parser.py").is_file()
    run_tests = (output_dir / "run-tests.sh").read_text(encoding="utf-8")
    assert "uv run /tests/parser.py" in run_tests
    assert not (output_dir / "parser.py").exists()
