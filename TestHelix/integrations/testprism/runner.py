"""Launch the unmodified external TestPrism scorer with our Python/Node bridge.

This module belongs to TestHelix. No factory registration, installed-agent shim,
scorer replacement, or file installation inside the external checkout is needed.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

PROJECT = Path(__file__).resolve().parents[2]
AGENT_IMPORT_PATH = "integrations.testprism.agent:TestHelixAgent"

HOST_HARD_CONSTRAINTS = [
    "public-task-authoritative", "export-root-agent-tests", "runner-required",
    "no-hidden-benchmark-data", "bounded-artifacts", "source-repairs-internal",
]


def infer_task_contract(instruction: str) -> dict:
    """Infer a small, deterministic contract from public task text.

    This is deliberately conservative: it only blocks a task when the public
    text names a source artifact or source-repair operation that the official
    TestHelix adapter cannot export (the adapter exports agent-tests/).
    """
    text = instruction.strip()
    outputs = []
    for pattern in (r"\b(?:fixed|corrected|patched)[-_][A-Za-z0-9_.-]+\.(?:py|js|ts|java|go|rs|c|cpp|rb|php)\b",
                    r"\b(?:fixed|corrected)\s+[A-Za-z0-9_.-]+\.(?:py|js|ts|java|go|rs|c|cpp)\b"):
        outputs.extend(re.findall(pattern, text, flags=re.I))
    if re.search(r"agent-tests(?:/|\\)", text, flags=re.I):
        outputs.append("agent-tests/")
    if re.search(r"run-tests\.sh", text, flags=re.I):
        outputs.append("agent-tests/run-tests.sh")
    outputs = list(dict.fromkeys(outputs))[:24]
    source_repair = bool(re.search(
        r"\b(?:fixed[-_][\w.-]+\.(?:py|js|ts|java|go|rs|c|cpp|rb|php)|corrected\s+(?:program|version|file)|source\s+(?:repair|modification|change)|"
        r"modify\s+(?:the\s+)?source|change\s+exactly\s+(?:one|1)\s+line)\b", text, flags=re.I))
    test_output = bool(re.search(r"agent-tests(?:/|\\)|run-tests\.sh|regression\s+(?:test|suite)|test\s+cases?", text, flags=re.I))
    mode = "mixed" if source_repair and test_output else "source-repair" if source_repair else "tests-only" if test_output else "unspecified"
    inputs = [re.sub(r"\s+", " ", line.strip())[:300] for line in text.splitlines()
              if re.search(r"\b(?:input|format|constraint|requirement|data|argument|parameter|return)\b", line, flags=re.I)][:16]
    return {"mode": mode, "requiredOutputs": outputs, "inputConstraints": inputs,
            "hardConstraints": HOST_HARD_CONSTRAINTS}


def check_task_contract(dataset: Path, selected: list[str], layout: str, yaml) -> dict:
    from integrations.testprism.staging import source_task
    contracts = []
    incompatible = []
    for key in selected:
        task_file = source_task(dataset, key, layout) / "task.yaml"
        public = yaml(typ="safe").load(task_file.read_text(encoding="utf-8")) or {}
        instruction = public.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError(f"Public task instruction is missing: {key}")
        contract = infer_task_contract(instruction)
        contracts.append({"task": key, **contract})
        if contract["mode"] in ("source-repair", "mixed"):
            required = ", ".join(contract["requiredOutputs"]) or "source changes"
            incompatible.append(
                f"{key}: public task requires source artifacts ({required}), but the official TestHelix adapter exports agent-tests/ only; choose a test-generation task or source/patch score mode"
            )
    if incompatible:
        raise ValueError("; ".join(incompatible))
    return {"tasks": len(contracts), "protocol": "official-tests-only", "contracts": contracts}


def outside(output: Path, roots: list[Path]) -> None:
    resolved = output.resolve()
    if any(resolved == root.resolve() or root.resolve() in resolved.parents for root in roots):
        raise ValueError("Output must be outside the external benchmark, dataset and patch-set directories")


def selection_keys(selection: dict) -> list[str]:
    keys = []
    for source, data in selection["sources"].items():
        for task in data["task_ids"]:
            if not all(isinstance(part, str) and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", part)
                       for part in (source, task)):
                raise ValueError("Selection IDs must be single safe path components")
            keys.append(f"{source}/{task}")
    total = selection.get("total_selected")
    if type(total) is not int or total < 1 or len(keys) != total or len(set(keys)) != total:
        raise ValueError("Selection manifest has an invalid total or duplicate tasks")
    return keys


def resolve_config(value, environment=None):
    environment = os.environ if environment is None else environment
    if isinstance(value, str):
        def replace(match):
            name = match.group(1)
            if name not in environment:
                raise ValueError(f"Missing configuration environment variable: {name}")
            return environment[name]
        return re.sub(r"\$\{([A-Z_][A-Z0-9_]*)\}", replace, value)
    if isinstance(value, list):
        return [resolve_config(item, environment) for item in value]
    if isinstance(value, dict):
        return {key: resolve_config(item, environment) for key, item in value.items()}
    return value


def effective_config(file, node, environment):
    # Parse TOML/JSON and validate once, in the same JS implementation the agent uses.
    module = (PROJECT / "dist/config.js").as_uri()
    script = "const {loadConfig}=await import(process.argv[1]); process.stdout.write(JSON.stringify(await loadConfig(process.argv[2])));"
    result = subprocess.run([node, "--input-type=module", "-e", script, module, str(file.resolve())],
                            env=environment, text=True, capture_output=True, timeout=30)
    if result.returncode:
        raise ValueError("Agent config validation failed: " + result.stderr[-1600:])
    return json.loads(result.stdout)


def missing_external_modules(bench: Path) -> list[str]:
    """Inspect eager factory imports; missing optional agents remain upstream errors."""
    missing = []
    factory = bench / "testprism/agents/agent_factory.py"
    if not factory.is_file():
        return ["testprism.agents.agent_factory"]
    for node in ast.walk(ast.parse(factory.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("testprism."):
            file = bench.joinpath(*node.module.split("."))
            if not file.with_suffix(".py").is_file() and not (file / "__init__.py").is_file():
                missing.append(node.module)
    return sorted(set(missing))


def import_runtime(bench: Path):
    sys.dont_write_bytecode = True
    sys.path[:0] = [str(PROJECT), str(bench)]
    package = importlib.import_module("testprism")
    if (bench / "testprism").resolve() not in [Path(item).resolve() for item in package.__path__]:
        raise ImportError("A different TestPrism installation is imported; use a fresh Python process")
    harness = importlib.import_module("testprism.harness.testbench_harness").TestBenchHarness
    agent = importlib.import_module("integrations.testprism.agent").TestHelixAgent
    base = importlib.import_module("testprism.agents.base_agent").BaseAgent
    if not issubclass(agent, base):
        raise TypeError("Adapter does not implement the installed BaseAgent contract")
    return harness, importlib.import_module("ruamel.yaml").YAML


def public_harness(base, yaml):
    class NativeHarness(base):
        """Inherit scoring verbatim; forward only task.yaml runtime metadata."""
        def _create_agent_for_task(self, task_id):
            agent = super()._create_agent_for_task(task_id)
            task_path = next(p for p in self._dataset.tasks if self._task_id_for_path(p) == task_id)
            public = yaml(typ="safe").load((task_path / "task.yaml").read_text(encoding="utf-8"))
            agent.task_id = task_id
            agent.task_source = task_id.split("/", 1)[0]
            agent.workdir = public.get("workdir") or "/app"
            agent.native_task_path = task_path
            maximum = float(public.get("max_agent_timeout_sec", 600))
            test_timeout = float(public.get("max_test_timeout_sec", 600))
            if not all(math.isfinite(value) and value > 0 for value in (maximum, test_timeout)):
                raise ValueError("Public task timeouts must be finite and positive")
            # Leave bridge cleanup time inside the official agent deadline.
            reserve = min(120, maximum / 4)
            agent.timeout_sec = max(0.1, maximum - reserve)
            agent.cleanup_timeout_sec = max(0.05, min(90, reserve * 0.75))
            agent.test_timeout_sec = test_timeout
            return agent
    return NativeHarness


def agent_revision() -> str:
    files = list((PROJECT / "src").glob("*.ts")) + list((PROJECT / "dist").glob("*.js"))
    files += [p for p in (PROJECT / "skills/testprism").rglob("*")
              if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"]
    files += list((PROJECT / "integrations/testprism").glob("*.py"))
    files += [PROJECT / name for name in ("package-lock.json", "package.json", "scripts/run_testprism.py")]
    files += [PROJECT / name for name in ("scripts/cleanup_testprism.py", "scripts/prepare_testprism_runtime.py",
                                         "integrations/testprism/requirements-native.lock.txt") if (PROJECT / name).is_file()]
    digest = hashlib.sha256()
    for file in sorted(files):
        digest.update(str(file.relative_to(PROJECT)).encode() + b"\0")
        digest.update(hashlib.sha256(file.read_bytes()).digest())
    return digest.hexdigest()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bench", type=Path, required=True, help="External read-only TestPrism checkout")
    parser.add_argument("--check", action="store_true", help="Read-only preflight; no harness run, containers or model requests")
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--dataset-layout", choices=("canonical", "flat"), default="canonical",
                        help="Explicit source layout; tasks retain source/task identity after staging")
    parser.add_argument("--score-mode", choices=("patch-set", "f2p"), default="patch-set",
                        help="Official strict 5+5 (default) or official original single-reference F2P")
    parser.add_argument("--patch-sets", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--model", default=os.environ.get("TESTHELIX_MODEL"))
    parser.add_argument("--node", default="node")
    parser.add_argument("--task-id", action="append")
    parser.add_argument("--selection", type=Path)
    parser.add_argument("--concurrency", type=int)
    parser.add_argument("--patch-concurrency", type=int)
    parser.add_argument("--no-rebuild", action="store_true", help="Reuse prepared official task images via the harness no_rebuild option")
    parser.add_argument("--runtime-copy", type=Path)
    args = parser.parse_args(argv)
    bench = args.bench.resolve()
    errors, checks = [], {}
    runtime = None

    def check(name, action):
        try:
            checks[name] = action()
        except (OSError, ValueError, TypeError, KeyError, AttributeError, ImportError, SyntaxError, subprocess.SubprocessError) as error:
            message = re.sub(r"\bsk-[\w-]{12,}", "[REDACTED]", str(error))
            message = re.sub(r"\bBearer\s+\S+", "Bearer [REDACTED]", message, flags=re.I)
            errors.append({"check": name, "error": message})

    def check_external():
        nonlocal runtime
        if not (bench / "testprism/harness/testbench_harness.py").is_file():
            raise ValueError("External TestPrism TestBenchHarness is missing")
        import_root = bench
        if args.runtime_copy:
            from integrations.testprism.bootstrap import verify_runtime
            outside(args.runtime_copy, [bench])
            verify_runtime(bench, args.runtime_copy.resolve())
            import_root = args.runtime_copy.resolve()
        missing = missing_external_modules(import_root)
        if missing:
            raise ImportError("External AgentFactory imports absent modules: " + ", ".join(missing))
        runtime = import_runtime(import_root)
        return "official harness and external adapter imported; scorer unchanged"

    def check_node():
        if not (PROJECT / "dist/cli.js").is_file():
            raise ValueError(f"Build the agent first: npm run build in {PROJECT}")
        result = subprocess.run([args.node, "--version"], capture_output=True, text=True, timeout=10, check=True)
        if tuple(map(int, result.stdout.strip().lstrip("v").split(".")[:3])) < (22, 19, 0):
            raise ValueError("Node.js 22.19.0 or later is required")
        if not shutil.which("docker"):
            raise ValueError("Docker CLI is missing on the agent host")
        return {"node": result.stdout.strip(), "dockerCLI": True, "daemonAndImagesChecked": False}

    check("externalRuntime", check_external)
    check("hostRuntime", check_node)
    if not args.check:
        required = ["dataset", "out", "run_id"]
        if args.score_mode == "patch-set":
            required.append("patch_sets")
        for field in required:
            if getattr(args, field) is None:
                errors.append({"check": "arguments", "error": f"--{field.replace('_', '-')} is required to run"})
    if args.score_mode == "f2p" and args.patch_sets:
        errors.append({"check": "arguments", "error": "--score-mode f2p cannot be combined with --patch-sets"})
    if not args.check or any(value is not None for value in (args.dataset, args.patch_sets, args.selection, args.model)):
        args.config = args.config or PROJECT / "configs/testprism.quickstart.json"
    if any(value is not None and not 1 <= value <= limit for value, limit in [(args.concurrency, 16), (args.patch_concurrency, 8)]):
        errors.append({"check": "arguments", "error": "Concurrency must be positive"})
    if args.run_id is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}", args.run_id):
        errors.append({"check": "arguments", "error": "run-id must be a safe single path component"})
    if args.out:
        def check_output():
            roots = [bench] + [p for p in (args.dataset, args.patch_sets) if p]
            outside(args.out, roots)
            if args.run_id:
                outside(args.out / args.run_id, roots)
            return "output root and run directory are outside external inputs"
        check("output", check_output)

    selected, selection_path, resolved_config = [], None, None
    if args.selection or args.dataset or not args.check:
        selection_path = (args.selection or bench / "verified_tasks/test-hardest-500/manifest.json").resolve()
        def check_selection():
            nonlocal selected
            expected = selection_keys(json.loads(selection_path.read_text(encoding="utf-8")))
            selected = args.task_id or expected
            if len(set(selected)) != len(selected) or any(key not in expected for key in selected):
                raise ValueError("Requested task IDs are duplicated or outside the frozen selection")
            if args.dataset:
                from integrations.testprism.staging import source_task
                missing = [key for key in selected if not (source_task(args.dataset, key, args.dataset_layout) / "task.yaml").is_file()]
                if missing:
                    raise ValueError(f"Selected {args.dataset_layout} dataset is incomplete: {len(missing)} missing task.yaml files; no implicit layout fallback")
            return {"manifestTasks": len(expected), "selectedTasks": len(selected), "datasetChecked": args.dataset is not None,
                    "datasetLayout": args.dataset_layout}
        check("selection", check_selection)
    elif args.task_id:
        errors.append({"check": "selection", "error": "--task-id requires --selection or --dataset during preflight"})
    if args.dataset and selected and runtime is not None:
        def check_contract():
            _, yaml = runtime
            return check_task_contract(args.dataset, selected, args.dataset_layout, yaml)
        check("taskContract", check_contract)
    if args.score_mode == "f2p" and args.dataset and selected and runtime is not None:
        def check_references():
            from integrations.testprism.staging import source_task
            task_paths = importlib.import_module("testprism.handlers.trial_handler").TaskPaths
            for key in selected:
                paths = task_paths(source_task(args.dataset, key, args.dataset_layout))
                if not paths.solution_path.is_file() or not paths.run_tests_path.is_file():
                    raise ValueError(f"Official reference or test entry point is missing: {key}")
            return {"tasks": len(selected), "validation": "official TaskPaths reference and test entry point presence"}
        check("referenceAssets", check_references)
    if args.patch_sets and args.score_mode == "patch-set":
        def check_patches():
            if not args.patch_sets.is_dir():
                raise ValueError("A frozen schema-v3 5+5 patch-set directory is required")
            if not selected:
                raise ValueError("Patch-set validation requires a valid --selection or --dataset")
            if runtime is None:
                raise ValueError("Official patch-set validation requires a working external runtime")
            loader = importlib.import_module("testprism.patches.models").load_patch_set_manifest
            for key in selected:
                manifest, _ = loader(args.patch_sets, key.rsplit("/", 1)[-1], task_key=key)
                if manifest.status != "ready":
                    raise ValueError(f"Selected patch set is not a complete ready 5+5 set: {key}")
            return {"tasks": len(selected), "validation": "official schema, oracle evidence and referenced solution hashes"}
        check("patchSets", check_patches)
    if args.config:
        def check_config():
            nonlocal resolved_config
            environment = dict(os.environ)
            if args.model:
                environment["TESTHELIX_MODEL"] = args.model
            resolved_config = effective_config(args.config, args.node, environment)
            model = resolved_config["model"]
            if not args.model or model["name"] != args.model:
                raise ValueError("--model must match the resolved agent config model.name")
            required = [model.get("apiKeyEnv", "TESTHELIX_API_KEY"), *model.get("headersEnv", {}).values()]
            missing = [name for name in required if not os.environ.get(name)]
            if missing:
                raise ValueError("Missing credential/header environment variables: " + ", ".join(missing))
            return {"model": args.model, "credentialsPresent": True, "paidRequestSent": False}
        check("config", check_config)
    if args.check or errors:
        print(json.dumps({"ready": not errors, "scope": "supplied inputs only; no model, daemon, image or scoring execution",
                          "scoreMode": args.score_mode, "agentImportPath": AGENT_IMPORT_PATH, "checks": checks, "errors": errors}, indent=2))
        return 0 if not errors else 2

    os.environ["TESTHELIX_MODEL"] = args.model
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    harness_type, yaml = runtime
    from integrations.testprism.staging import stage_dataset
    staged = stage_dataset(args.dataset, args.out.resolve() / (args.run_id + "-inputs") / "dataset", selected, layout=args.dataset_layout)
    harness = public_harness(harness_type, yaml)(
        dataset_path=staged, patch_set_path=args.patch_sets.resolve() if args.patch_sets else None, output_path=args.out.resolve(),
        run_id=args.run_id, model_name=args.model, agent_import_path=AGENT_IMPORT_PATH,
        agent_kwargs={"project_root": str(PROJECT), "config": str(args.config.resolve()), "node": args.node,
                      "model_name": args.model, "benchmark_root": str(bench), "agent_revision": agent_revision(),
                      "config_digest": hashlib.sha256(json.dumps(resolved_config, sort_keys=True).encode()).hexdigest(),
                      "selection_digest": hashlib.sha256(selection_path.read_bytes()).hexdigest()},
        task_ids=selected, n_attempts=1, n_concurrent_trials=args.concurrency or resolved_config.get("scheduler", {}).get("taskConcurrency", 2),
        patch_concurrency=args.patch_concurrency or resolved_config.get("scheduler", {}).get("patchConcurrency", 1),
        no_rebuild=args.no_rebuild, upload_results=False)
    results = harness.run()
    print(json.dumps({"results": str(args.out.resolve() / args.run_id / "results.json"),
                      "scoreMode": args.score_mode, "trials": len(results.results)}, indent=2))
    return 0
