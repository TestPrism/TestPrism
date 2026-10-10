from testprism.handlers.trial_handler import TrialPaths


def test_trial_paths_exposes_durable_test_and_coverage_artifacts(tmp_path):
    paths = TrialPaths(tmp_path, "task-1", "trial-1")

    root = tmp_path / "task-1" / "trial-1"
    assert paths.agent_tests_path == root / "agent-tests"
    assert paths.agent_tests_manifest_path == root / "agent-tests-manifest.json"
    assert paths.coverage_path == root / "coverage"
