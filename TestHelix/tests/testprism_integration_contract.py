"""Launcher contracts against a temporary API fixture, not official scoring."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

PROJECT = Path(__file__).resolve().parents[1]


class IntegrationContract(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="testhelix-external-integration-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        # Exercise the real owned integration from a temporary agent bundle so
        # npm run check does not depend on an earlier build in the user's tree.
        self.agent_project = self.root / "agent"
        for file in [PROJECT / "integrations/__init__.py", *sorted((PROJECT / "integrations/testprism").glob("*.py")),
                     PROJECT / "scripts/run_testprism.py", PROJECT / "package.json", PROJECT / "package-lock.json"]:
            target = self.agent_project / file.relative_to(PROJECT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(file, target)
        (self.agent_project / "dist").mkdir()
        (self.agent_project / "dist/cli.js").write_text('throw new Error("Preflight must never execute the model CLI");\n')
        (self.agent_project / "dist/config.js").write_text('import {readFile} from "node:fs/promises"; export async function loadConfig(p) {return JSON.parse((await readFile(p,"utf8")).replace(/\\$\\{([A-Z_][A-Z0-9_]*)\\}/g, (_, k) => process.env[k]));}\n')
        self.bench = self.root / "external-bench"
        self.dataset = self.bench / "dataset"
        self.patches = self.bench / "patches"
        self.patches.mkdir(parents=True)
        modules = {
            "testprism/__init__.py": "",
            "testprism/agents/__init__.py": "",
            "testprism/harness/__init__.py": "",
            "testprism/agents/base_agent.py": '''from enum import Enum
class SystemPromptMode(str, Enum):
    REPLACE = "replace"
    APPEND = "append"
class BaseAgent:
    def __init__(self, **kwargs):
        self.system_prompt = kwargs.get("system_prompt", "")
        self.system_prompt_mode = SystemPromptMode(kwargs.get("system_prompt_mode", "replace"))
        self.profile_env = kwargs.get("profile_env", {})
class AgentResult:
    pass
''',
            "testprism/agents/failure_mode.py": '''from enum import Enum
class FailureMode(str, Enum):
    NONE="none"
    AGENT_TIMEOUT="agent_timeout"
    AGENT_TEST_MISSING="agent_test_missing"
    UNKNOWN_AGENT_ERROR="unknown_agent_error"
''',
            "testprism/agents/agent_factory.py": "from testprism.agents.base_agent import BaseAgent\n",
            "testprism/handlers/__init__.py": "",
            "testprism/handlers/trial_handler.py": '''class TaskPaths:
    def __init__(self, path): self.path = path
    @property
    def solution_path(self):
        script = self.path / "solution.sh"
        return script if script.exists() else self.path / "solution.yaml"
    @property
    def run_tests_path(self): return self.path / "run-tests.sh"
''',
            "testprism/patches/__init__.py": "",
            "testprism/patches/models.py": '''import os
from types import SimpleNamespace
def load_patch_set_manifest(root, task_id, task_key=None):
    assert task_key == "from-fixture/" + task_id
    assert root.is_dir()
    if os.environ.get("FIXTURE_PATCH_ERROR"):
        raise ValueError("Fixture official loader: referenced solution hash mismatch")
    return SimpleNamespace(status="ready"), root / task_key
''',
            "ruamel/__init__.py": "",
            "ruamel/yaml/__init__.py": '''import json
class YAML:
    def __init__(self, **kwargs): pass
    def load(self, text): return json.loads(text)
''',
            "testprism/harness/testbench_harness.py": '''import importlib, json
from pathlib import Path
from types import SimpleNamespace
class TestBenchHarness:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self._dataset = SimpleNamespace(tasks=[kwargs["dataset_path"] / key for key in kwargs["task_ids"]])
    def _task_id_for_path(self, path):
        return str(path.relative_to(self.kwargs["dataset_path"]))
    def _create_agent_for_task(self, task_id):
        module, name = self.kwargs["agent_import_path"].split(":")
        return getattr(importlib.import_module(module), name)(**self.kwargs["agent_kwargs"], system_prompt="OFFICIAL FIXTURE PROMPT")
    def score(self): return "UNCHANGED_SCORER"
    def run(self):
        assert type(self).score is TestBenchHarness.score
        assert self.kwargs["n_attempts"] == 1 and self.kwargs["upload_results"] is False
        agents = [self._create_agent_for_task(key) for key in self.kwargs["task_ids"]]
        assert all(a.task_id == key for a, key in zip(agents, self.kwargs["task_ids"]))
        assert all(a.workdir == "/testbed" and a.system_prompt == "OFFICIAL FIXTURE PROMPT" for a in agents)
        assert all(a.timeout_sec + a.cleanup_timeout_sec + 8 <= 600 for a in agents)
        assert all(a.test_timeout_sec == 45 for a in agents)
        out = self.kwargs["output_path"] / self.kwargs["run_id"]
        out.mkdir(parents=True)
        (out / "contract.json").write_text(json.dumps(self.kwargs, default=str))
        return SimpleNamespace(results=[{} for a in agents])
''',
        }
        for name, content in modules.items():
            file = self.bench / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(content)
        for task in ["one", "two"]:
            file = self.dataset / "from-fixture" / task / "task.yaml"
            file.parent.mkdir(parents=True)
            file.write_text(json.dumps({"instruction": "Public fixture", "workdir": "/testbed",
                                        "max_agent_timeout_sec": 600, "max_test_timeout_sec": 45}))
            (file.parent / "solution.sh").write_text("# fixture reference; never executed\n")
            (file.parent / "run-tests.sh").write_text("# fixture entry point; never executed\n")
        self.selection = self.root / "selection.json"
        self.selection.write_text(json.dumps({"total_selected": 2, "sources": {"from-fixture": {"task_ids": ["one", "two"]}}}))
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"model": {"name": "${TESTHELIX_MODEL}", "apiKeyEnv": "FIXTURE_KEY"}}))
        binary = self.root / "bin"
        binary.mkdir()
        docker = binary / "docker"
        docker.write_text("#!/bin/sh\nexit 19\n")
        docker.chmod(0o755)
        self.env = dict(os.environ, PATH=str(binary) + os.pathsep + os.environ["PATH"],
                        PYTHONPATH=str(self.agent_project), FIXTURE_KEY="fixture-secret-never-print")
        self.env.pop("TESTHELIX_MODEL", None)
        self.before = self.snapshot()

    def snapshot(self):
        return [(str(p.relative_to(self.bench)), p.lstat().st_mtime_ns,
                 hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None)
                for p in sorted(self.bench.rglob("*"))]

    def invoke(self, extra, script=False):
        entry = [str(self.agent_project / "scripts/run_testprism.py")] if script else ["-m", "integrations.testprism"]
        result = subprocess.run([sys.executable, "-B", *entry, "--bench", str(self.bench), *extra],
                                cwd=self.agent_project, env=self.env, text=True, capture_output=True, timeout=15)
        self.assertNotIn("fixture-secret-never-print", result.stdout + result.stderr)
        return result

    def inputs(self):
        return ["--dataset", str(self.dataset), "--patch-sets", str(self.patches), "--selection", str(self.selection),
                "--config", str(self.config), "--model", "fixture", "--out", str(self.root / "output"), "--run-id", "contract"]

    def test_preflight_is_read_only_and_both_entry_points_agree(self):
        reports = []
        for script in [False, True]:
            result = self.invoke(["--check", *self.inputs()], script)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            reports.append(json.loads(result.stdout))
        self.assertEqual(reports[0], reports[1])
        self.assertTrue(reports[0]["ready"])
        self.assertFalse((self.root / "output").exists())
        self.assertEqual(self.snapshot(), self.before)

    def test_task_contract_blocks_source_artifact_tasks_before_model_requests(self):
        task = self.dataset / "from-fixture/one/task.yaml"
        original = task.read_text()
        task.write_text(json.dumps({"instruction": "Fix the bug and save the corrected program as fixed_rpn_eval.py. Input format is one expression.",
                                    "workdir": "/testbed", "max_agent_timeout_sec": 600, "max_test_timeout_sec": 45}))
        try:
            result = self.invoke(["--check", *self.inputs(), "--task-id", "from-fixture/one"])
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("taskContract", result.stdout)
            self.assertIn("source artifacts", result.stdout)
            self.assertFalse((self.root / "output").exists())
        finally:
            task.write_text(original)

    def test_launch_preserves_scoring_selection_and_metadata_without_registry_edits(self):
        result = self.invoke([*self.inputs(), "--task-id", "from-fixture/two", "--concurrency", "2", "--patch-concurrency", "3"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["trials"], 1)
        record = json.loads((self.root / "output/contract/contract.json").read_text())
        self.assertEqual(record["task_ids"], ["from-fixture/two"])
        self.assertEqual(record["n_concurrent_trials"], 2)
        self.assertEqual(record["patch_concurrency"], 3)
        self.assertFalse(record["no_rebuild"])
        self.assertEqual(record["agent_import_path"], "integrations.testprism.agent:TestHelixAgent")
        self.assertNotIn("patch_set_path", record["agent_kwargs"])
        self.assertEqual(len(record["agent_kwargs"]["agent_revision"]), 64)
        self.assertEqual(self.snapshot(), self.before)

    def test_prepared_images_can_be_reused_without_changing_scoring_inputs(self):
        result = self.invoke([*self.inputs(), "--no-rebuild"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = json.loads((self.root / "output/contract/contract.json").read_text())
        self.assertTrue(record["no_rebuild"])
        self.assertEqual(record["patch_set_path"], str(self.patches))
        self.assertEqual(record["task_ids"], ["from-fixture/one", "from-fixture/two"])
        self.assertEqual(self.snapshot(), self.before)

    def test_launcher_default_matches_the_node_and_adapter_hardest500_config(self):
        default = self.agent_project / "configs/testprism.quickstart.json"
        default.parent.mkdir()
        # The fixture's loadConfig stub reads JSON; exercise path selection here.
        shutil.copyfile(self.config, default)
        args = self.inputs()
        index = args.index("--config")
        del args[index:index + 2]
        preflight = self.invoke(["--check", *args])
        self.assertEqual(preflight.returncode, 0, preflight.stdout + preflight.stderr)
        self.assertIn("config", json.loads(preflight.stdout)["checks"])
        result = self.invoke(args)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = json.loads((self.root / "output/contract/contract.json").read_text())
        self.assertEqual(record["agent_kwargs"]["config"], str(default))

    def test_node_version_floor_matches_the_dependency_requirement(self):
        node = self.root / "bin/node-old"
        node.write_text("#!/bin/sh\necho v22.18.0\n")
        node.chmod(0o755)
        result = self.invoke(["--check", "--node", str(node)])
        self.assertEqual(result.returncode, 2)
        self.assertIn("22.19.0", result.stdout)

    def test_rejects_duplicate_and_unselected_tasks_without_running(self):
        for tasks in [["from-fixture/one", "from-fixture/one"], ["from-fixture/absent"]]:
            result = self.invoke([*self.inputs(), *[item for task in tasks for item in ("--task-id", task)]])
            self.assertEqual(result.returncode, 2)
            self.assertIn("outside the frozen selection", result.stdout)
            self.assertFalse((self.root / "output").exists())
        self.assertEqual(self.snapshot(), self.before)

    def test_rejects_symlink_output_inside_external_input_roots(self):
        alias = self.root / "output-alias"
        alias.symlink_to(self.bench, target_is_directory=True)
        for output in [self.bench / "new", self.dataset / "new", self.patches / "new", alias / "new"]:
            result = self.invoke(["--check", *self.inputs(), "--out", str(output)])
            self.assertEqual(result.returncode, 2)
            self.assertIn("Output must be outside", result.stdout)
            self.assertFalse(output.exists())
        self.assertEqual(self.snapshot(), self.before)

    def test_missing_external_imports_are_reported_without_creating_shims(self):
        factory = self.bench / "testprism/agents/agent_factory.py"
        factory.write_text("from testprism.agents.some_other_agent import Agent\n")
        before = self.snapshot()
        result = self.invoke(["--check"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("testprism.agents.some_other_agent", result.stdout)
        self.assertEqual(self.snapshot(), before)

    def test_run_id_symlink_cannot_escape_an_otherwise_safe_output_root(self):
        output = self.root / "output"
        output.mkdir()
        (output / "contract").symlink_to(self.bench, target_is_directory=True)
        result = self.invoke(["--check", *self.inputs()])
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("Output must be outside", result.stdout)
        self.assertEqual(self.snapshot(), self.before)

    def test_official_patch_validation_failure_blocks_before_harness_execution(self):
        self.env["FIXTURE_PATCH_ERROR"] = "1"
        result = self.invoke(self.inputs())
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertIn("referenced solution hash mismatch", result.stdout)
        self.assertFalse((self.root / "output").exists())
        self.assertEqual(self.snapshot(), self.before)

    def test_official_f2p_uses_existing_flat_inputs_without_patch_sets(self):
        for task in ("one", "two"):
            (self.dataset / "from-fixture" / task).rename(self.dataset / ("from-fixture__" + task))
        before = self.snapshot()
        args = self.inputs()
        index = args.index("--patch-sets")
        del args[index:index + 2]
        args += ["--score-mode", "f2p", "--dataset-layout", "flat", "--task-id", "from-fixture/two"]
        implicit = list(args)
        index = implicit.index("--dataset-layout")
        del implicit[index:index + 2]
        rejected = self.invoke(["--check", *implicit])
        self.assertEqual(rejected.returncode, 2)
        self.assertIn("no implicit layout fallback", rejected.stdout)
        self.env["FIXTURE_PATCH_ERROR"] = "1"
        preflight = self.invoke(["--check", *args])
        self.assertEqual(preflight.returncode, 0, preflight.stdout + preflight.stderr)
        checks = json.loads(preflight.stdout)["checks"]
        self.assertIn("referenceAssets", checks)
        self.assertNotIn("patchSets", checks)
        result = self.invoke(args)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["scoreMode"], "f2p")
        record = json.loads((self.root / "output/contract/contract.json").read_text())
        self.assertIsNone(record["patch_set_path"])
        self.assertEqual(record["task_ids"], ["from-fixture/two"])
        staged = Path(record["dataset_path"]) / "from-fixture/two"
        original = self.dataset / "from-fixture__two"
        for file in original.iterdir():
            self.assertEqual((staged / file.name).read_bytes(), file.read_bytes())
        self.assertEqual(self.snapshot(), before)

    def test_score_modes_cannot_silently_ignore_or_drop_patch_sets(self):
        conflicting = self.invoke(["--check", *self.inputs(), "--score-mode", "f2p"])
        self.assertEqual(conflicting.returncode, 2)
        self.assertIn("cannot be combined", conflicting.stdout)
        args = self.inputs()
        index = args.index("--patch-sets")
        del args[index:index + 2]
        strict = self.invoke(args)
        self.assertEqual(strict.returncode, 2)
        self.assertIn("--patch-sets is required", strict.stdout)
        self.assertFalse((self.root / "output").exists())

    def test_f2p_requires_reference_assets_before_launch(self):
        (self.dataset / "from-fixture/one/solution.sh").unlink()
        args = self.inputs()
        index = args.index("--patch-sets")
        del args[index:index + 2]
        result = self.invoke([*args, "--score-mode", "f2p"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("referenceAssets", result.stdout)
        self.assertFalse((self.root / "output").exists())

    def test_explicit_flat_layout_is_preserved_when_both_layouts_exist(self):
        flat = self.dataset / "from-fixture__two"
        shutil.copytree(self.dataset / "from-fixture/two", flat)
        (flat / "layout-marker").write_text("flat")
        result = self.invoke([*self.inputs(), "--dataset-layout", "flat", "--task-id", "from-fixture/two"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = json.loads((self.root / "output/contract/contract.json").read_text())
        self.assertEqual((Path(record["dataset_path"]) / "from-fixture/two/layout-marker").read_text(), "flat")


if __name__ == "__main__":
    unittest.main()
