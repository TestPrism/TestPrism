import importlib.util
import sys
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[3]
    / "adapters"
    / "swebench-pro"
    / "run_adapter.py"
)


def _load_run_adapter_module():
    spec = importlib.util.spec_from_file_location("swebench_pro_run_adapter", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_build_task_uses_git_fallback_and_shell_array_for_selected_tests(tmp_path, monkeypatch):
    module = _load_run_adapter_module()

    template_dir = tmp_path / "template"
    template_dir.mkdir()
    (template_dir / "task.yaml").write_text("instruction: |\n{problem_statement}\n")
    (template_dir / "Dockerfile").write_text("FROM {docker_image}\n")
    (template_dir / "solution.sh").write_text("#!/bin/bash\n{patch}\n")
    monkeypatch.setattr(module, "TEMPLATE_DIR", template_dir)

    sweap_dir = tmp_path / "sweap"
    run_script_dir = sweap_dir / "run_scripts" / "instance_protonmail__webclients-demo"
    run_script_dir.mkdir(parents=True)
    (run_script_dir / "run_script.sh").write_text(
        "#!/bin/bash\n"
        "run_all_tests() {\n"
        "  echo all\n"
        "}\n"
        "run_selected_tests() {\n"
        "  printf '%s\\n' \"$@\"\n"
        "}\n"
    )

    output_dir = tmp_path / "out"
    record = {
        "instance_id": "instance_protonmail__webclients-demo",
        "problem_statement": "demo task",
        "dockerhub_tag": "demo",
        "patch": "echo demo",
        "before_repo_set_cmd": "git reset --hard deadbeef",
        "test_patch": "diff --git a/foo b/foo",
        "selected_test_files_to_run": [
            "applications/mail/src/app/helpers/recipients.test.ts",
            "applications/mail/src/app/helpers/elements.test.ts | should be an element from Proton",
        ],
    }

    assert module.build_task(record, sweap_dir, output_dir) is True

    run_tests = (
        output_dir / "instance_protonmail__webclients-demo" / "run-tests.sh"
    ).read_text()

    assert "cd /testbed" in run_tests
    assert "ensure_git_repo()" in run_tests
    assert "apply_patch_file()" in run_tests
    assert 'echo "No git metadata in $(pwd); skipping before_repo_set_cmd"' in run_tests
    assert "ensure_git_repo || true" in run_tests
    assert "apply_patch_file /tests/test.patch" in run_tests
    assert "apply_patch_file /tmp/agent_patch.diff" in run_tests
    assert "SELECTED_TESTS=(" in run_tests
    assert "applications/mail/src/app/helpers/recipients.test.ts" in run_tests
    assert (
        "'applications/mail/src/app/helpers/elements.test.ts | should be an element from Proton'"
        in run_tests
    )
    assert 'run_selected_tests "${SELECTED_TESTS[@]}"' in run_tests
    assert 'run_selected_tests "${SELECTED_TESTS[@]}" 2>&1 | tee /tmp/_test_output.log' in run_tests
    assert "^Test failed:" in run_tests


def test_build_task_preserves_selected_test_arguments_and_go_runtime(tmp_path):
    module = _load_run_adapter_module()
    sweap_dir = tmp_path / "sweap"
    output_dir = tmp_path / "output"
    instance_id = "instance_flipt-io__flipt-demo"

    run_script_dir = sweap_dir / "run_scripts" / instance_id
    run_script_dir.mkdir(parents=True, exist_ok=True)
    (run_script_dir / "run_script.sh").write_text(
        "#!/bin/bash\n"
        "run_selected_tests() {\n"
        "  printf 'selected:%s\\n' \"$@\"\n"
        "}\n"
        "run_all_tests() {\n"
        "  echo all\n"
        "}\n"
    )

    record = {
        "instance_id": instance_id,
        "problem_statement": "demo",
        "dockerhub_tag": "flipt/demo",
        "patch": "",
        "test_patch": "",
        "before_repo_set_cmd": "",
        "selected_test_files_to_run": ["TestOne", "TestTwo"],
    }

    assert module.build_task(record, sweap_dir, output_dir) is True

    task_dir = output_dir / instance_id
    run_tests_text = (task_dir / "run-tests.sh").read_text()
    dockerfile_text = (task_dir / "Dockerfile").read_text()

    assert "SELECTED_TESTS=(TestOne TestTwo)" in run_tests_text
    assert 'run_selected_tests "${SELECTED_TESTS[@]}"' in run_tests_text
    assert "^Test failed:" in run_tests_text
    assert "golang-go" in dockerfile_text
