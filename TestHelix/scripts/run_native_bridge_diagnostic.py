"""Run public tasks through native TestPrism classes and our adapter.

This is a component integration diagnostic, not TestBenchHarness or official 5+5
scoring. An owned runtime copy changes only three eager package initializers to
lazy exports; every other official runtime file remains byte-identical. References
are never read here. Evaluate the entire frozen batch separately afterward.
Development mode excludes hardest500; explicit hardest500 sampling is evaluation-only.
"""

from __future__ import annotations

import argparse
import ast
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import inspect
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import time

sys.dont_write_bytecode = True
PROJECT = Path(__file__).resolve().parents[1]
ENVIRONMENT = {"PATH": "/opt/miniconda3/envs/testbed/bin:/opt/miniconda3/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"}
LAZY_PACKAGES = ("agents", "harness", "terminal")


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def lazy_initializer(source):
    """Keep the same exports, delaying only their imports. Reject other code."""
    exports = {}
    names = None
    for statement in ast.parse(source).body:
        if isinstance(statement, ast.ImportFrom) and statement.level == 0:
            for name in statement.names:
                if name.name == "*":
                    raise ValueError("Cannot rewrite wildcard import")
                exports[name.asname or name.name] = (statement.module, name.name)
        elif (isinstance(statement, ast.Assign) and len(statement.targets) == 1
              and isinstance(statement.targets[0], ast.Name) and statement.targets[0].id == "__all__"):
            names = ast.literal_eval(statement.value)
        elif isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str):
            continue
        else:
            raise ValueError("Unexpected package initialization code; cannot safely make it lazy")
    if names is None or any(name not in exports for name in names):
        raise ValueError("Unresolved native package exports")
    return ('"""Diagnostic copy: defer optional imports; native implementations are unchanged."""\n'
            'from importlib import import_module\n'
            f'__all__ = {names!r}\n_EXPORTS = {exports!r}\n'
            'def __getattr__(name):\n'
            '    if name not in _EXPORTS:\n        raise AttributeError(name)\n'
            '    module, attribute = _EXPORTS[name]\n'
            '    value = getattr(import_module(module), attribute)\n'
            '    globals()[name] = value\n    return value\n')


def copy_runtime(bench, out):
    runtime = out / "native-runtime"
    source = bench / "testprism"
    shutil.copytree(source, runtime / "testprism", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    changed = {f"{package}/__init__.py" for package in LAZY_PACKAGES}
    for relative in changed:
        target = runtime / "testprism" / relative
        target.write_text(lazy_initializer(target.read_text()))
    files = []
    for file in sorted((runtime / "testprism").rglob("*")):
        if not file.is_file():
            continue
        relative = file.relative_to(runtime / "testprism").as_posix()
        original, copied = digest((source / relative).read_bytes()), digest(file.read_bytes())
        if relative not in changed and original != copied:
            raise ValueError("Native runtime copy differs: " + relative)
        files.append({"path": relative, "originalSha256": original, "copiedSha256": copied})
    write_json(out / "native-runtime-audit.json", {
        "source": str(source), "copiedAt": now(), "officialHarnessUsed": False,
        "initializerChanges": sorted(changed), "classesAndMethodsChanged": False, "files": files,
    })
    sys.path[:0] = [str(runtime), str(PROJECT)]
    return runtime


def resolve_agent_project(requested, bench):
    agent_project = requested.resolve()
    if not agent_project.is_relative_to(PROJECT) or agent_project.is_relative_to(bench):
        raise ValueError("Agent checkout must be owned by our project, outside the external benchmark")
    # The launcher imports the current Python adapter. A frozen Node runtime
    # must use the same bridge files for this comparison to be well defined.
    for relative in ('agent.py', 'runner.py', '__init__.py'):
        current = PROJECT / 'integrations/testprism' / relative
        frozen = agent_project / 'integrations/testprism' / relative
        if current.read_bytes() != frozen.read_bytes():
            raise ValueError("Frozen adapter differs; use its own diagnostic launcher: " + relative)
    return agent_project


def validate_task_split(key, holdout, existing, sample_split):
    if sample_split not in {"development", "hardest500"}:
        raise ValueError("Unknown diagnostic split")
    if not re.fullmatch(r"from-swe-bench/[a-zA-Z0-9_.-]+", key) or key in existing:
        raise ValueError("Invalid or duplicate task: " + key)
    if (key in holdout) != (sample_split == "hardest500"):
        raise ValueError("Task does not belong to the explicitly selected diagnostic split: " + key)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bench", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--images", type=Path, required=True, help="JSON mapping canonical task IDs to prepared Docker images")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--agent-project", type=Path, default=PROJECT,
                        help="Owned frozen agent checkout to build/run; the native diagnostic stays in this project")
    parser.add_argument("--prepare-only", action="store_true", help="Exercise native imports and real sessions without model calls")
    parser.add_argument("--sample-split", choices=("development", "hardest500"), default="development",
                        help="Explicit hardest500 sampling is evaluation-only; default development excludes the holdout")
    args = parser.parse_args()
    bench, out = args.bench.resolve(), args.out.resolve()
    agent_project = resolve_agent_project(args.agent_project, bench)
    if not out.is_relative_to(PROJECT) or out.is_relative_to(bench):
        raise ValueError("Diagnostic output must be in our project, outside the external benchmark")
    out.mkdir(parents=True, exist_ok=False)
    runtime = copy_runtime(bench, out)
    import docker
    import yaml
    from testprism.agents.base_agent import AgentResult, BaseAgent, SystemPromptMode
    from testprism.harness.testbench_system_prompt import TESTBENCH_SYSTEM_PROMPT
    from testprism.terminal.tmux_session import TmuxSession
    from integrations.testprism.agent import TestHelixAgent

    assert issubclass(TestHelixAgent, BaseAgent)
    native_classes = {}
    for cls in (BaseAgent, AgentResult, TmuxSession):
        file = Path(inspect.getfile(cls)).resolve()
        relative = file.relative_to(runtime / "testprism")
        assert file.read_bytes() == (bench / "testprism" / relative).read_bytes()
        native_classes[cls.__name__] = {"module": cls.__module__, "file": str(file), "sha256": digest(file.read_bytes())}
    write_json(out / "native-imports.json", {"classes": native_classes, "dockerSdk": docker.__version__,
        "systemPromptSha256": digest(TESTBENCH_SYSTEM_PROMPT.encode()), "adapterIsNativeSubclass": True,
        "agentProject": str(agent_project), "launcherSha256": digest(Path(__file__).read_bytes()),
        "adapterSha256": digest(Path(inspect.getfile(TestHelixAgent)).read_bytes())})
    config = json.loads(args.config.read_text())
    write_json(out / "config.json", config)
    for key in [config["model"]["apiKeyEnv"], *config["model"].get("headersEnv", {}).values()]:
        if not args.prepare_only and not os.environ.get(key):
            raise ValueError("Missing provider environment variable: " + key)
    os.environ.setdefault("NODE_USE_ENV_PROXY", "1" if any(os.environ.get(k) for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")) else "0")
    holdout_bytes = (bench / "verified_tasks/test-hardest-500/manifest.json").read_bytes()
    holdout = {f"{source}/{task}" for source, group in json.loads(holdout_bytes)["sources"].items() for task in group["task_ids"]}
    images = json.loads(args.images.read_text())
    tasks = []
    for entry in json.loads(args.plan.read_text())["tasks"]:
        key = entry["key"]
        validate_task_split(key, holdout, {t["id"] for t in tasks}, args.sample_split)
        file = Path(entry["publicTaskPath"]).resolve()
        if not file.is_relative_to(bench):
            raise ValueError("Public task file must belong to the supplied benchmark")
        public = yaml.safe_load(file.read_text())
        if (file.parent.name != key.replace("/", "__") or public["difficulty"] != "hard" or public["workdir"] != "/testbed"
                or digest(public["instruction"].encode()) != entry["instructionDigest"]):
            raise ValueError("Frozen public task metadata changed")
        if float(public["max_agent_timeout_sec"]) < config["budget"]["wallMs"] / 1000 + 110:
            raise ValueError("Diagnostic and cleanup budget exceed public task timeout")
        tasks.append({"id": key, "instance": key.split("/")[1], "taskDirectory": str(file.parent),
            "instruction": public["instruction"], "root": public["workdir"], "image": images[key],
            "instructionDigest": entry["instructionDigest"], "difficulty": "hard"})
    if not tasks or len(tasks) > 2:
        raise ValueError("This bounded diagnostic accepts one or two tasks")
    write_json(out / "cohort.json", {"frozenAt": now(), "eligibleForEvolutionValidation": False,
        "officialSplit": False, "officialHarnessUsed": False, "referenceRead": False,
        "sampleSplit": args.sample_split, "datasetLayout": "flat-fallback", "selectedSnapshotEquivalenceVerified": False,
        "holdoutManifestDigest": digest(holdout_bytes), "config": config,
        "tasks": [{k: v for k, v in task.items() if k != "instruction"} for task in tasks]})
    subprocess.run(["npm", "run", "build"], cwd=agent_project, check=True)
    for relative in ("src", "dist", "skills", "integrations", "scripts", "package.json", "package-lock.json", "tsconfig.json"):
        source, target = agent_project / relative, out / "implementation" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        else:
            shutil.copyfile(source, target)
    if not args.prepare_only:
        from urllib.parse import urlsplit
        url = urlsplit(config["model"]["baseURL"])
        transport = subprocess.run(["node", "--input-type=module", "-e",
            'const r=await fetch(process.argv[1],{signal:AbortSignal.timeout(20000)});if(r.status>=500)process.exit(1);console.log(r.status)',
            f"{url.scheme}://{url.netloc}"], cwd=PROJECT, capture_output=True, text=True, timeout=25)
        write_json(out / "transport.json", {"checkedAt": now(), "code": transport.returncode,
            "status": transport.stdout.strip(), "envProxyEnabled": os.environ["NODE_USE_ENV_PROXY"] == "1"})
        if transport.returncode:
            raise RuntimeError("Provider transport preflight failed before model calls")

    client = docker.from_env(timeout=120)
    containers, sessions, prepared = [], [], []
    started = time.monotonic()
    try:
        # Finish all setup before submitting either paid task.
        for task in tasks:
            directory = out / task["instance"]
            directory.mkdir()
            image = client.images.get(task["image"])
            container = client.containers.run(image.id, ["-c", "sleep infinity"], entrypoint="/bin/bash",
                detach=True, network_mode="none", environment=ENVIRONMENT,
                labels={"testhelix.diagnostic": "native-bridge", "testhelix.batch": str(out)})
            containers.append(container)
            cleaned = container.exec_run(["bash", "-c", "find /testbed -name .git -prune -exec rm -rf {} +; rm -rf /testbed/agent-tests /testbed/solution.sh /testbed/patch-sets /testbed/.patch-generation-state; mkdir -p /logs"])
            if cleaned.exit_code:
                raise RuntimeError("Task container sanitization failed")
            # Keep this exact sanitized baseline for subsequent frozen host evaluation.
            baseline = container.commit(conf={"Labels": {"testhelix.diagnostic": "native-frozen-baseline", "testhelix.batch": str(out)}})
            session = TmuxSession(session_name="testhelix-native", container=container, disable_recording=True)
            sessions.append(session)
            session.start()
            write_json(directory / "provenance.json", {"schemaVersion": 1, "preparedAt": now(),
                "kind": "native-component-development-diagnostic", "eligibleForEvolutionValidation": False,
                "excludesHardest500": args.sample_split == "development", "sampleSplit": args.sample_split,
                "taskDirectory": task["taskDirectory"], "image": task["image"],
                "imageId": baseline.id, "originalImageId": image.id, "containerId": container.id,
                "root": task["root"], "env": ENVIRONMENT, "configDigest": digest((out / "config.json").read_bytes()),
                "nativeSessionStarted": session.is_session_alive(), "recordingEnabled": False})
            prepared.append((task, directory, session))
            print("Native session ready: " + task["id"], flush=True)
        if args.prepare_only:
            write_json(out / "preparation.json", {"complete": True, "paidCalls": 0, "tasks": len(tasks)})
            return

        def run_one(item):
            task, directory, session = item
            task_start = time.monotonic()
            row = {"taskId": task["id"], "startedAt": now()}
            try:
                agent = TestHelixAgent(project_root=str(agent_project), config=str(out / "config.json"),
                    benchmark_root=str(bench), task_id=task["id"], task_source="from-swe-bench",
                    workdir=task["root"], system_prompt=TESTBENCH_SYSTEM_PROMPT,
                    system_prompt_mode=SystemPromptMode.REPLACE, test_timeout_sec=120,
                    timeout_sec=config["budget"]["wallMs"] / 1000 + 20, cleanup_timeout_sec=90)
                result = agent.perform_task(task["instruction"], session, directory / "logs")
                assert isinstance(result, AgentResult)
                row["agentResult"] = result.model_dump(mode="json")
                artifact_hashes = {}
                try:
                    stream, _ = session.container.get_archive(task["root"] + "/agent-tests")
                    with tarfile.open(fileobj=io.BytesIO(b"".join(stream))) as archive:
                        for member in archive.getmembers():
                            if member.isfile():
                                artifact_hashes[member.name] = digest(archive.extractfile(member).read())
                except docker.errors.NotFound:
                    pass
                row["exportedContainerArtifactSha256"] = artifact_hashes
                submission = directory / "logs/testhelix-js/submission"
                expected = {file.relative_to(submission).as_posix(): digest(file.read_bytes())
                    for file in sorted((submission / "agent-tests").rglob("*")) if file.is_file()}
                row["exportMatchesFrozenSubmission"] = bool(expected) and artifact_hashes == expected
            except Exception as error:
                # Do not serialize arbitrary exception bodies containing provider details.
                row["integrationError"] = type(error).__name__
            finally:
                descriptor = directory / "logs/testhelix-public-task.json"
                if descriptor.is_file():
                    shutil.copyfile(descriptor, directory / "task.json")
                (directory / "run").symlink_to("logs/testhelix-js", target_is_directory=True)
                row["elapsedMs"] = round((time.monotonic() - task_start) * 1000)
                write_json(directory / "native-result.json", row)
            print("Native task completed: " + task["id"], flush=True)
            return row

        with ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(pool.map(run_one, prepared))
        frozen = []
        for task in tasks:
            result_file = out / task["instance"] / "run/result.json"
            frozen.append({"taskId": task["id"], "resultDigest": digest(result_file.read_bytes()) if result_file.is_file() else None})
        write_json(out / "generation-freeze.json", {"frozenAt": now(), "referenceRead": False, "runs": frozen})
        write_json(out / "native-summary.json", {"completedAt": now(), "elapsedMs": round((time.monotonic() - started) * 1000),
            "officialHarnessUsed": False, "officialScore": None, "nativeClassesUnchanged": True,
            "eligibleForEvolutionValidation": False, "rows": rows})
    finally:
        cleanup_errors = []
        for session in sessions:
            try:
                session.stop()
            except Exception as error:
                cleanup_errors.append({"resource": "session", "error": type(error).__name__})
        for container in containers:
            try:
                container.remove(force=True)
            except Exception as error:
                cleanup_errors.append({"resource": container.id, "error": type(error).__name__})
        client.close()
        write_json(out / "cleanup.json", {"completedAt": now(), "errors": cleanup_errors,
            "baselineImagesRetainedForFrozenEvaluation": True})


if __name__ == "__main__":
    main()
