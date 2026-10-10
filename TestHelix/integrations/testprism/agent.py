"""Native TestPrism adapter; all generation, tools and budgets live in Node.

External agent import path: integrations.testprism.agent:TestHelixAgent
Requires a complete TestPrism installation on PYTHONPATH and `npm run build`.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import tempfile
from pathlib import Path

from testprism.agents.base_agent import AgentResult, BaseAgent, SystemPromptMode
from testprism.agents.failure_mode import FailureMode


class TestHelixAgent(BaseAgent):
    supports_profile_environment = True
    native_system_prompt_modes = frozenset({SystemPromptMode.REPLACE, SystemPromptMode.APPEND})

    @staticmethod
    def name() -> str:
        return "testhelix-js"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.project = Path(kwargs.get("project_root", Path(__file__).resolve().parents[2])).resolve()
        self.config = Path(kwargs.get("config", self.project / "configs/testprism.quickstart.json")).resolve()
        self.node = str(kwargs.get("node", "node"))
        self.model_name = kwargs.get("model_name")
        self.task_id = kwargs.get("task_id")
        self.benchmark_root = Path(kwargs["benchmark_root"]).resolve() if kwargs.get("benchmark_root") else None
        self.workdir = kwargs.get("workdir")
        self.task_source = str(kwargs.get("task_source", "testprism"))
        self.test_timeout_sec = kwargs.get("test_timeout_sec")
        self.native_task_path = kwargs.get("native_task_path")
        self._native_bridge = None
        self.timeout_sec = float(kwargs.get("timeout_sec", 960))
        self.cleanup_timeout_sec = float(kwargs.get("cleanup_timeout_sec", 90))
        if not all(math.isfinite(value) and value > 0 for value in (self.timeout_sec, self.cleanup_timeout_sec)):
            raise ValueError("Agent and cleanup timeouts must be finite and positive")

    def perform_task(self, instruction, session, logging_dir=None) -> AgentResult:
        try:
            return self._perform_task(instruction, session, logging_dir)
        finally:
            if self._native_bridge is not None:
                self._native_bridge.close()
                self._native_bridge = None

    def _perform_task(self, instruction, session, logging_dir=None) -> AgentResult:
        entry = self.project / "dist/cli.js"
        if not entry.is_file():
            raise RuntimeError(f"Build the Node agent first: npm run build (in {self.project})")
        root = Path(logging_dir).resolve() if logging_dir is not None else Path(tempfile.mkdtemp(prefix="testhelix-bridge-"))
        if self.benchmark_root and (root == self.benchmark_root or self.benchmark_root in root.parents):
            raise ValueError("Agent logging_dir must be outside the external TestPrism checkout")
        for name in ("testhelix-js", "testhelix-public-task.json", "testhelix-node.log"):
            if (root / name).is_symlink():
                raise ValueError(f"Bridge output must not be a symlink: {name}")
        root.mkdir(parents=True, exist_ok=True)
        run_dir = root / "testhelix-js"
        if (run_dir / "budget.json").exists() or (run_dir / "result.json").exists():
            raise RuntimeError("Agent logging directory already contains a run; use a fresh harness trial directory")
        container = session.container
        container.reload()
        workdir = self.workdir or container.attrs.get("Config", {}).get("WorkingDir") or "/app"
        task = {
            "id": self.task_id or hashlib.sha256(instruction.encode()).hexdigest()[:24],
            "source": self.task_source,
            "instruction": instruction,
            "systemPrompt": self.system_prompt or "",
            "systemPromptMode": self.system_prompt_mode.value,
            "runtime": {"kind": "docker", "container": container.id, "root": workdir},
        }
        if self.native_task_path:
            from integrations.testprism.native_bridge import NativeBridge
            native_path = Path(self.native_task_path).resolve()
            if self.benchmark_root and (native_path == self.benchmark_root or self.benchmark_root in native_path.parents):
                raise ValueError("Native task runtime must use an owned staged copy outside the benchmark")
            self._native_bridge = NativeBridge(native_path, workdir, container, root / "native",
                timeout_sec=self.timeout_sec, cleanup_timeout_sec=self.cleanup_timeout_sec)
            task["runtime"].update({"kind": "testprism", "socket": str(self._native_bridge.socket)})
        if self.test_timeout_sec is not None:
            timeout = float(self.test_timeout_sec)
            if not math.isfinite(timeout) or timeout <= 0:
                raise ValueError("test_timeout_sec must be finite and positive")
            task["testTimeoutMs"] = max(1, int(timeout * 1000))
        task_file = root / "testhelix-public-task.json"
        task_file.write_text(json.dumps(task, ensure_ascii=False), encoding="utf-8")
        task_file.chmod(0o600)
        environment = os.environ.copy()
        environment.update({str(k): str(v) for k, v in self.profile_env.items()})
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        if self.model_name:
            environment["TESTHELIX_MODEL"] = str(self.model_name)
        args = [self.node, str(entry), "run", "--task", str(task_file),
                "--config", str(self.config), "--out", str(run_dir),
                "--wall-ms", str(max(1000, int((self.timeout_sec - 20) * 1000)))]
        failure = FailureMode.NONE
        with (root / "testhelix-node.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen(args, cwd=self.project, env=environment, stdout=log,
                                       stderr=subprocess.STDOUT)
            try:
                code = process.wait(timeout=self.timeout_sec)
                if code != 0:
                    failure = FailureMode.AGENT_TEST_MISSING if code == 2 else FailureMode.UNKNOWN_AGENT_ERROR
            except subprocess.TimeoutExpired:
                failure = FailureMode.AGENT_TIMEOUT
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            finally:
                # A killed Node process cannot run its finally block. Delete only
                # resources whose Docker ownership label matches this run's lease.
                try:
                    cleanup = subprocess.run([self.node, str(entry), "cleanup", "--out", str(run_dir)],
                                             cwd=self.project, env=environment, stdout=log, stderr=log,
                                             timeout=self.cleanup_timeout_sec, check=False)
                    if cleanup.returncode != 0 and failure == FailureMode.NONE:
                        failure = FailureMode.UNKNOWN_AGENT_ERROR
                except subprocess.TimeoutExpired:
                    log.write("Owned-resource cleanup exceeded its deadline; rerun the cleanup command.\n")
                    if failure == FailureMode.NONE:
                        failure = FailureMode.UNKNOWN_AGENT_ERROR
                except OSError as error:
                    log.write(f"Owned-resource cleanup could not start ({type(error).__name__}); rerun the cleanup command.\n")
                    if failure == FailureMode.NONE:
                        failure = FailureMode.UNKNOWN_AGENT_ERROR
        result_file = run_dir / "result.json"
        budget_file = run_dir / "budget.json"
        usage = None
        for file in (result_file, budget_file):
            try:
                if not file.is_file():
                    continue
                record = json.loads(file.read_text(encoding="utf-8"))
                if not isinstance(record, dict):
                    raise ValueError("Expected a JSON object")
                if file == result_file and record.get("status") not in {"validated", "provisional", "no_submit"}:
                    raise ValueError("Invalid run status")
                candidate_usage = record.get("usage") if file == result_file else record
                if not isinstance(candidate_usage, dict) or not all(
                    type(candidate_usage.get(key)) is int and candidate_usage[key] >= 0 for key in ("input", "output")
                ):
                    raise ValueError("Invalid usage record")
                usage = candidate_usage
                if failure in (FailureMode.NONE, FailureMode.AGENT_TEST_MISSING):
                    if file == budget_file:
                        failure = FailureMode.UNKNOWN_AGENT_ERROR
                    elif record["status"] == "no_submit":
                        failure = FailureMode.AGENT_TEST_MISSING
                        events_file = run_dir / "events.jsonl"
                        if events_file.is_file():
                            events = [json.loads(line) for line in events_file.read_text().splitlines() if line]
                            errors = [event for event in events if event.get("type") == "model.error"]
                            responded = any(event.get("type") == "model.latency" for event in events)
                            if errors and not responded:
                                # An unavailable provider is an agent infrastructure failure,
                                # rather than evidence that a responding model could not write tests.
                                failure = FailureMode.UNKNOWN_AGENT_ERROR
                                if all(event.get("data", {}).get("status") in (401, 403) for event in errors):
                                    failure = getattr(FailureMode, "LLM_AUTHENTICATION_ERROR", failure)
                break
            except (OSError, ValueError, TypeError):
                if failure == FailureMode.NONE:
                    failure = FailureMode.UNKNOWN_AGENT_ERROR
                with (root / "testhelix-node.log").open("a", encoding="utf-8") as log:
                    log.write(f"Invalid or unreadable {file.name}; trying the remaining usage ledger.\n")
        if usage is None:
            usage = {"input": 0, "output": 0}
            if failure == FailureMode.NONE:
                failure = FailureMode.UNKNOWN_AGENT_ERROR
        return AgentResult(total_input_tokens=usage["input"],
                           total_output_tokens=usage["output"], failure_mode=failure)
