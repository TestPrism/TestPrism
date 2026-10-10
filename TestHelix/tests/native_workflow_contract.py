"""Deterministic transport/lifecycle fault tests. Real Compose is exercised separately."""
import json
import socket
import signal
import time
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from integrations.testprism.native_bridge import NativeBridge
from integrations.testprism.staging import stage_dataset
from integrations.testprism.cleanup import cleanup_native
from integrations.testprism.native_worker import Environment


from native_worker_fixture import Container, Terminal


class Contracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="testhelix-native-contract-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.task = self.root / "task"
        self.task.mkdir()
        (self.task / "docker-compose.yaml").write_text("services: {}\n")
        (self.task / "fixture.json").write_text(json.dumps({"root": str(self.root)}))
        self.bridge = NativeBridge(self.task, str(self.root), Container(), self.root / "logs", server_image="fixture", terminal_type=Terminal, cleanup_timeout_sec=3)
        self.addCleanup(self.bridge.close)

    def test_native_forks_execute_exact_status_and_independent_interactive_sessions(self):
        a, b = self.bridge.fork("a"), self.bridge.fork("b")
        self.assertNotEqual(a["id"], b["id"])
        kwargs = [env["kwargs"] for env in self.bridge.environments.values()]
        self.assertNotEqual(kwargs[0]["docker_compose_path"], kwargs[1]["docker_compose_path"])
        self.assertTrue(all(not item["cleanup"] and item["no_rebuild"] for item in kwargs))
        self.assertTrue(all(item["client_image_name"] == "sha256:fixture" for item in kwargs))
        result = self.bridge.execute(a["id"], "set -euo pipefail; value=(中文); printf '%s' \"${value[0]}\"; printf err >&2; exit 7", 3000)
        self.assertEqual((result["code"], result["stdout"], result["stderr"]), (7, "中文", "err"))
        self.bridge.interact(a["id"], "start", "server", "a-marker")
        self.bridge.interact(b["id"], "start", "server", "b-marker")
        self.assertNotIn("a-marker", self.bridge.interact(b["id"], "read", "server", "")["output"])
        with self.assertRaises(ValueError):
            self.bridge.interact(a["id"], "start", "server", "")
        with self.assertRaises(ValueError):
            self.bridge.dispatch("score", {})
        self.bridge.close_environments()
        self.assertFalse(list(self.root.glob("*.live")))

    def test_exit_status_is_published_only_after_the_writer_finishes(self):
        class DelayedWriter:
            def send_keys(self, keys, **_):
                # Shell redirection happens before this function runs. Keep the
                # writer paused across multiple real reader polls.
                self.process = subprocess.Popen('printf() { sleep .2; command printf "$@"; }; ' + keys[0], shell=True)
        session = DelayedWriter()
        environment = Environment(SimpleNamespace(container=Container()), str(self.root))
        environment.session = session
        result = environment.execute("printf payload; exit 17", 3000)
        session.process.wait(timeout=3)
        self.assertEqual((result["code"], result["stdout"], result["timedOut"]), (17, "payload", False))

    def wait_for(self, predicate):
        deadline = time.monotonic() + 4
        while not predicate() and time.monotonic() < deadline:
            time.sleep(.025)
        self.assertTrue(predicate())

    def test_close_racing_with_start_terminates_startup_and_its_children(self):
        (self.root / "block-start").touch()
        errors = []
        def start():
            try:
                self.bridge.fork("racing")
            except RuntimeError as error:
                errors.append(str(error))
        thread = threading.Thread(target=start); thread.start()
        self.wait_for(lambda: (self.root / "startup-child.pid").exists())
        self.bridge.close_environments()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertTrue(errors)
        self.assertFalse(self.bridge.environments)
        self.assertFalse(list(self.root.glob("*.live")))
        self.assertFalse(list(self.root.glob("*.started")))
        child = Path("/proc") / (self.root / "startup-child.pid").read_text() / "stat"
        self.wait_for(lambda: not child.exists() or child.read_text().split()[2] == "Z")

    def test_cleanup_silent_failure_retains_environment_for_retry(self):
        value = self.bridge.fork("retry")
        failure = self.root / "cleanup-fails"; failure.touch()
        try:
            with self.assertRaisesRegex(RuntimeError, "resources remain"):
                self.bridge.close_environment(value["id"])
            self.assertIn(value["id"], self.bridge.environments)
            self.assertIn(value["id"], json.loads((self.root / "logs/native-resources.json").read_text())["pending"])
        finally:
            failure.unlink()
        self.bridge.close_environment(value["id"])
        self.assertNotIn(value["id"], self.bridge.environments)
        self.assertFalse(list(self.root.glob("*.live")))

    def test_parent_death_kills_worker_group_and_leaves_recoverable_ownership(self):
        script = '''
import json, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from native_worker_fixture import Container, Terminal
from integrations.testprism.native_bridge import NativeBridge
root = Path(sys.argv[1])
bridge = NativeBridge(root / 'task', str(root), Container(), root / 'orphan-logs', terminal_type=Terminal)
bridge.fork('orphan')
worker = next(iter(bridge.environments.values()))['worker']['process']
(root / 'worker.pid').write_text(str(worker.pid))
time.sleep(60)
'''
        parent = subprocess.Popen([sys.executable, "-B", "-c", script, str(self.root), str(Path(__file__).parent)])
        def stop_parent():
            if parent.poll() is None:
                parent.kill()
            parent.wait(timeout=5)
        self.addCleanup(stop_parent)
        self.wait_for(lambda: (self.root / "worker.pid").exists())
        worker_pid = int((self.root / "worker.pid").read_text())
        self.addCleanup(lambda: self.kill_if_running(worker_pid))
        parent.kill(); parent.wait(timeout=5)
        worker_stat = Path(f"/proc/{worker_pid}/stat")
        self.wait_for(lambda: not worker_stat.exists() or worker_stat.read_text().split()[2] == "Z")
        journal = json.loads((self.root / "orphan-logs/native-resources.json").read_text())
        self.assertEqual(journal["pending"], journal["projects"])
        self.assertEqual(len(journal["pending"]), 1)
        # Killing a host worker cannot remove daemon-owned resources. Recovery
        # must retain its owned journal and independently verify Docker cleanup.
        name = journal["pending"][0]
        self.assertTrue((self.root / (name + ".live")).exists())
        recovered = cleanup_native(self.root / "orphan-logs/native-resources.json",
            manager_type=lambda **kwargs: Terminal(**kwargs)._compose_manager)
        self.assertEqual(recovered, 1)
        self.assertFalse((self.root / (name + ".live")).exists())

    @staticmethod
    def kill_if_running(pid):
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def test_disconnected_fork_is_cancelled_without_late_startup(self):
        (self.root / "block-start").touch()
        client = socket.socket(socket.AF_UNIX)
        client.connect(str(self.bridge.socket))
        client.sendall(json.dumps({"method": "fork", "params": {"label": "disconnect", "operationId": "a" * 32, "timeoutMs": 5000}}).encode() + b"\n")
        self.wait_for(lambda: (self.root / "startup-child.pid").exists())
        client.close()
        self.wait_for(lambda: not self.bridge.environments)
        self.assertFalse(list(self.root.glob("*.live")))
        self.assertFalse(list(self.root.glob("*.started")))
        with self.assertRaisesRegex(ValueError, "already been used"):
            self.bridge.fork("duplicate", operationId="a" * 32)

    def test_fork_deadline_stops_blocking_startup(self):
        (self.root / "block-start").touch()
        start = time.monotonic()
        with self.assertRaisesRegex(TimeoutError, "deadline"):
            self.bridge.fork("deadline", timeoutMs=250)
        self.assertLess(time.monotonic() - start, 3)
        self.assertFalse(self.bridge.environments)
        self.assertFalse(list(self.root.glob("*.live")))

    def test_invalid_timeouts_and_unknown_ids_never_execute(self):
        for timeout in [True, float("nan"), 0, 300001]:
            with self.assertRaises(ValueError):
                self.bridge.execute("missing", "true", timeout)
        with self.assertRaises(ValueError):
            self.bridge.execute("other-owner", "true", 1)

    def test_long_task_lifecycle_starts_node_and_keeps_short_rpc_deadlines(self):
        for seconds in (7080, 7200):
            with self.subTest(seconds=seconds):
                before = time.monotonic()
                bridge = NativeBridge(self.task, str(self.root), Container(), self.root / str(seconds),
                                      server_image="fixture", terminal_type=Terminal,
                                      timeout_sec=seconds, cleanup_timeout_sec=3)
                self.addCleanup(bridge.close)
                self.assertGreaterEqual(bridge.deadline, before + seconds)
                self.assertLessEqual(bridge.deadline, time.monotonic() + seconds)
                observed_timeouts = []
                execute = bridge.execute
                def capture(id, script, timeoutMs, cancelled=None):
                    observed_timeouts.append(timeoutMs)
                    return execute(id, script, timeoutMs, cancelled)
                bridge.execute = capture
                # Start the real Node native adapter with a local fixture worker;
                # no model provider or Docker operation is involved.
                script = """
import assert from 'node:assert/strict';
import {NativeFactory} from './src/native-runtime.ts';
import {configSchema} from './src/types.ts';
const config = configSchema.parse({model: {name: 'fixture', baseURL: 'http://localhost:1'}, budget: {wallMs: 900000}});
assert.equal(Math.min(config.budget.wallMs, (Number(process.argv[3]) - 20) * 1000), 900000);
let remaining = 1000;
const factory = new NativeFactory({kind: 'testprism', socket: process.argv[1], root: process.argv[2], container: 'fixture'}, () => remaining);
const sandbox = await factory.fork('long-lifecycle');
const result = await sandbox.exec('printf local-short-operation', 120000);
assert.equal(result.code, 0); assert.equal(result.stdout, 'local-short-operation');
remaining = 0;
await assert.rejects(sandbox.exec('false', 1000), /deadline exhausted/);
await sandbox.close();
console.log('node-native-complete');
"""
                result = subprocess.run(["node", "--import", "tsx", "--input-type=module", "-e", script,
                                         str(bridge.socket), str(self.root), str(seconds)],
                                        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("node-native-complete", result.stdout)
                self.assertEqual(observed_timeouts, [1000])
                with self.assertRaises(ValueError):
                    bridge.fork("oversized", timeoutMs=3600001)
                with self.assertRaises(ValueError):
                    bridge.execute("missing", "false", 300001)
                with self.assertRaises(ValueError):
                    bridge.interact("missing", "read", "session", "", timeoutMs=300001)
                bridge.close()

    def test_invalid_task_lifecycle_is_rejected_before_startup(self):
        for seconds in (True, float("nan"), float("inf"), float("-inf"), 0, -1, 10 ** 400):
            with self.subTest(seconds=seconds), self.assertRaisesRegex(ValueError, "lifecycle timeout"):
                NativeBridge(self.task, str(self.root), Container(), self.root / "invalid-lifetime",
                             server_image="fixture", terminal_type=Terminal, timeout_sec=seconds)
        self.assertFalse((self.root / "invalid-lifetime").exists())

    def test_staging_is_owned_hash_verified_and_rejects_escaping_symlinks(self):
        task = self.root / "source/s/t"; task.mkdir(parents=True)
        (task / "task.yaml").write_text("instruction: public fixture")
        (task / "self-directory").symlink_to(".", target_is_directory=True)
        target = stage_dataset(self.root / "source", self.root / "staged", ["s/t"])
        self.assertEqual((target / "s/t/task.yaml").read_bytes(), (task / "task.yaml").read_bytes())
        self.assertTrue((target / "s/t/self-directory").is_symlink())
        self.assertEqual(len(list(target.rglob("*"))), 4, "Directory links must not cause recursive copies")
        self.assertEqual(stage_dataset(self.root / "source", target, ["s/t"]), target)
        (target / "s/t/task.yaml").write_text("changed")
        with self.assertRaisesRegex(ValueError, "changed staged input"):
            stage_dataset(self.root / "source", target, ["s/t"])
        (task / "outside").symlink_to(self.root / "task")
        with self.assertRaisesRegex(ValueError, "outside"):
            stage_dataset(self.root / "source", self.root / "next", ["s/t"])

    def test_crash_recovery_rejects_unowned_projects_before_docker_access(self):
        journal = self.root / "journal.json"
        journal.write_text(json.dumps({"schemaVersion": 1, "owner": "ctb-testhelix-" + "a" * 16, "projects": ["unrelated-service"]}))
        with self.assertRaisesRegex(ValueError, "outside its owner"):
            cleanup_native(journal, manager_type=lambda **kw: self.fail("must reject before Docker"))

    def test_staging_resume_preserves_explicit_input_layout(self):
        source = self.root / "layout-source"
        for name in ("s/t", "s__t"):
            task = source / name
            task.mkdir(parents=True)
            (task / "task.yaml").write_text("instruction: public fixture")
        target = stage_dataset(source, self.root / "layout-staged", ["s/t"], layout="flat")
        self.assertEqual(stage_dataset(source, target, ["s/t"], layout="flat"), target)
        with self.assertRaisesRegex(ValueError, "changed staged input selection"):
            stage_dataset(source, target, ["s/t"], layout="canonical")


if __name__ == "__main__":
    unittest.main()
