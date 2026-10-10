"""Owned Compose/terminal controls; optionally run a configured model on a synthetic public task.

This is an infrastructure diagnostic, not a hardest500 or official 5+5 score.
"""
import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import time
import uuid

sys.dont_write_bytecode = True
PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from integrations.testprism.bootstrap import verify_runtime

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--bench", type=Path, default=PROJECT / "TestPrism-main")
parser.add_argument("--runtime-copy", type=Path, required=True)
parser.add_argument("--out", type=Path, required=True)
parser.add_argument("--image", required=True, help="Existing image with Python, bash, tmux and asciinema")
parser.add_argument("--model-smoke", action="store_true", help="Paid model test using --config")
parser.add_argument("--config", type=Path, default=PROJECT / "configs/testprism.quickstart.json")
args = parser.parse_args()
args.out = args.out.resolve()
if args.out.exists() or args.out.is_relative_to(args.bench.resolve()):
    raise ValueError("Use a fresh owned output directory")
verify_runtime(args.bench.resolve(), args.runtime_copy.resolve())
sys.path.insert(0, str(args.runtime_copy.resolve()))
from testprism.terminal.terminal import Terminal
from integrations.testprism.native_bridge import NativeBridge
from integrations.testprism.agent import TestHelixAgent

signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
args.out.mkdir(parents=True)
public = args.out / "task/public"; public.mkdir(parents=True)
(public / "intervals.py").write_text('''def coalesce(intervals):
    result = []
    for a, b in sorted(intervals):
        if a == b:
            continue
        if result and a <= result[-1][1]:
            result[-1] = (result[-1][0], max(b, result[-1][1]))
        else:
            result.append((a, b))
    return result
''')
(public / "state_server.py").write_text('''from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        state = Path('/state/value')
        if self.path.startswith('/set/'):
            state.write_text(self.path[5:])
        value = state.read_text() if state.exists() else 'empty'
        self.send_response(200); self.end_headers(); self.wfile.write(value.encode())
HTTPServer(('0.0.0.0', 8765), Handler).serve_forever()
''')
compose = args.out / "task/docker-compose.yaml"
compose.write_text('''services:
  client:
    image: ${T_BENCH_TASK_DOCKER_CLIENT_IMAGE_NAME}
    container_name: ${T_BENCH_TASK_DOCKER_CLIENT_CONTAINER_NAME}
    command: [bash, -lc, "mkdir -p /testhelix-fixture; cp /fixture/intervals.py /testhelix-fixture/; exec sleep infinity"]
    working_dir: /testhelix-fixture
    volumes:
      - ./public:/fixture:ro
      - ${T_BENCH_TASK_LOGS_PATH}:/logs
      - ${T_BENCH_TASK_AGENT_LOGS_PATH}:/agent-logs
  server:
    image: ${T_BENCH_TASK_DOCKER_SERVER_IMAGE_NAME}
    command: [python, /fixture/state_server.py]
    volumes:
      - ./public:/fixture:ro
      - service-state:/state
volumes:
  service-state: {}
''')
instruction = '''Fix coalesce(intervals) in intervals.py. Each input is a pair (start, end) of integers with start <= end, representing a half-open interval. Return a list of tuples sorted by start. Merge intervals with positive overlap, including containment and transitive chains, but keep intervals that merely touch as separate intervals. Drop zero-length intervals. Do not mutate the input or its pairs. Empty input returns an empty list. Handle negative endpoints and duplicates. The input is a finite list, and efficiency should be O(n log n).'''
(args.out / "task/task.yaml").write_text("instruction: " + json.dumps(instruction) + "\nworkdir: /testhelix-fixture\n")
original = Terminal(client_container_name="ctb-testhelix-smoke-" + uuid.uuid4().hex[:14], client_image_name=args.image,
    server_image_name=args.image, docker_compose_path=compose, no_rebuild=True, cleanup=False, disable_recording=True,
    sessions_logs_path=args.out / "original-sessions", agent_logs_path=args.out / "original-logs")
bridge = None
report = {"kind": "synthetic-native-workflow-controls", "officialScore": False, "paidRequests": args.model_smoke}
started = time.monotonic()
try:
    original.start()
    session = original.create_session("original", as_configured_user=True)
    bridge = NativeBridge(args.out / "task", "/testhelix-fixture", original.container, args.out / "native-controls", server_image=args.image)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        a, b = list(pool.map(bridge.fork, ["control-a", "control-b"]))
    def execute(env, text, timeout=10000):
        value = bridge.execute(env["id"], text, timeout)
        if value["code"] != 0 or value["timedOut"]:
            raise RuntimeError("Native control failed: " + json.dumps(value))
        return value["stdout"].strip()
    get = "python -c \"import urllib.request; print(urllib.request.urlopen('http://server:8765%s',timeout=3).read().decode())\""
    assert execute(a, get % "/set/alpha") == "alpha"
    assert execute(b, get % "/") == "empty"
    assert execute(b, get % "/set/beta") == "beta"
    assert execute(a, get % "/") == "alpha"
    execute(a, "echo marker-a > isolation.txt")
    assert execute(b, "test ! -e isolation.txt && printf isolated") == "isolated"
    bridge.interact(a["id"], "start", "shell", "export TESTHELIX_SESSION_VALUE=persistent")
    bridge.interact(a["id"], "send", "shell", "printf 'CHECK:%s\\n' \"$TESTHELIX_SESSION_VALUE\"")
    time.sleep(.2)
    assert "CHECK:persistent" in bridge.interact(a["id"], "read", "shell", "")["output"]
    timeout = bridge.execute(b["id"], "sleep 30", 150)
    assert timeout["timedOut"] and b["id"] not in bridge.environments
    report["controls"] = {"parallelForks": True, "serviceStateIsolation": True, "workspaceIsolation": True,
        "persistentTerminal": True, "timeoutClosesEnvironment": True}
    bridge.close()
    if args.model_smoke:
        from dotenv import load_dotenv
        from integrations.testprism.runner import effective_config
        load_dotenv(PROJECT / ".env.local", override=False)
        os.environ["NODE_USE_ENV_PROXY"] = "1"
        config = effective_config(args.config, "node", os.environ)
        report["model"] = config["model"]["name"]
        agent = TestHelixAgent(project_root=str(PROJECT), config=str(args.config.resolve()),
            model_name=config["model"]["name"], task_id="from-workflow-fixture/half-open-intervals", task_source="from-quixbugs",
            workdir="/testhelix-fixture", native_task_path=args.out / "task", benchmark_root=str(args.bench.resolve()), timeout_sec=840)
        result = agent.perform_task(instruction, session, args.out / "agent-logs")
        report["agentFailureMode"] = str(result.failure_mode)
        run_files = list((args.out / "agent-logs").rglob("result.json"))
        for file in run_files:
            value = json.loads(file.read_text())
            if "taskId" in value:
                report["agent"] = {key: value.get(key) for key in ("status", "reason", "usage")}
        events = []
        for file in (args.out / "agent-logs").rglob("events.jsonl"):
            events.extend(json.loads(line) for line in file.read_text().splitlines())
        report["roles"] = [{key: e["data"].get(key) for key in ("session", "model", "reasoningEffort")}
                           for e in events if e["type"] == "model.requested"]
        report["plannerCompleted"] = sum(e["type"] == "planner.completed" for e in events)
        report["apiErrors"] = sum(e["type"] == "model.error" for e in events)
        # Verify publication at the native boundary before deleting the original.
        # Compare frozen host artifacts with bytes actually present in its container.
        submission = args.out / "agent-logs/testhelix-js/submission"
        expected = {str(p.relative_to(submission)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in submission.rglob("*") if p.is_file()}
        check = original.container.exec_run(["python", "-c", """
import hashlib, json
from pathlib import Path
root = Path('/testhelix-fixture')
print(json.dumps({'tests': {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
    for p in (root / 'agent-tests').rglob('*') if p.is_file()},
    'source': hashlib.sha256((root / 'intervals.py').read_bytes()).hexdigest()}))
"""])
        if check.exit_code:
            raise RuntimeError("Unable to verify native publication")
        exported = json.loads(check.output)
        report["publication"] = {"files": len(expected), "hashesMatch": expected == exported["tests"],
            "originalSourceUnchanged": exported["source"] == hashlib.sha256((public / "intervals.py").read_bytes()).hexdigest()}
        assert report["publication"]["hashesMatch"] and report["publication"]["originalSourceUnchanged"]
finally:
    if bridge:
        bridge.close()
    try:
        original.stop()
    finally:
        original._compose_manager._force_remove_compose_project()
    report["durationSeconds"] = round(time.monotonic() - started, 2)
    report["cleanupRemaining"] = len(original._compose_manager._client.containers.list(all=True,
        filters={"label": "com.docker.compose.project=" + original._compose_manager._client_container_name}))
    (args.out / "summary.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report, indent=2))
