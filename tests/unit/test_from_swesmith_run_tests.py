from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_from_swesmith_run_tests_read_config_from_test_dir():
    run_tests_paths = sorted(
        (REPO_ROOT / "verified_tasks").glob("*/from-swesmith/*/run-tests.sh")
    )

    assert run_tests_paths

    for run_tests_path in run_tests_paths:
        run_tests_text = run_tests_path.read_text(encoding="utf-8")

        assert 'cd "${TEST_DIR:-/tests}"' in run_tests_text
        assert 'with open("config.json", "r") as file:' in run_tests_text
        assert 'with open("tests/config.json", "r") as file:' not in run_tests_text
