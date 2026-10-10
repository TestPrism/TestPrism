"""Audited private runtime copy: defer unavailable optional agents, keep scoring methods intact."""
from __future__ import annotations
import ast
import hashlib
import json
from pathlib import Path
import shutil


def prepare_runtime(bench: Path, output: Path) -> Path:
    bench, output = bench.resolve(), output.resolve()
    if output == bench or bench in output.parents or output.exists():
        raise ValueError("Runtime copy requires a fresh directory outside the external checkout")
    source = bench / "testprism"
    shutil.copytree(source, output / "testprism", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    changed = {}
    # Empty package initializers avoid loading unrelated agents; explicitly
    # imported implementation modules retain their original behavior.
    for package in ("agents", "harness", "terminal"):
        relative = f"{package}/__init__.py"
        (output / "testprism" / relative).write_text('"""Private runtime: use explicit submodule imports."""\n')
        changed[relative] = "Defer eager package imports; explicit submodule APIs used by this adapter"
    factory = source / "agents/agent_factory.py"
    body = factory.read_text()
    definition = next(node for node in ast.parse(body).body if isinstance(node, ast.ClassDef) and node.name == "AgentFactory")
    methods = []
    for node in definition.body:
        if isinstance(node, ast.FunctionDef):
            start = min([node.lineno, *[d.lineno for d in node.decorator_list]]) - 1
            methods.append("\n".join(body.splitlines()[start:node.end_lineno]))
    (output / "testprism/agents/agent_factory.py").write_text(
        '"""Private runtime: original dynamic import methods; optional named-agent registry omitted."""\n'
        'import importlib\nfrom testprism.agents.agent_name import AgentName\nfrom testprism.agents.base_agent import BaseAgent\n'
        'class AgentFactory:\n    AGENT_NAME_TO_CLASS = {}\n' + "\n\n".join(methods) + "\n")
    changed["agents/agent_factory.py"] = "Retain original factory methods verbatim; use explicit import_path instead of eagerly loading unavailable named agents"
    harness = output / "testprism/harness/testbench_harness.py"
    text = harness.read_text()
    optional = "from testprism.agents.installed_agents.mini_swe_agent import MiniSweAgent"
    if text.count(optional) != 1:
        raise ValueError("Unexpected optional-agent import; inspect runtime before adaptation")
    text = text.replace(optional, 'try:\n    ' + optional + '\nexcept ImportError as error:\n'
        '    if error.name not in {"testprism.agents.installed_agents.mini_swe_agent", "testprism.agents.installed_agents.mini_swe_agent.mini_swe_agent"}:\n'
        '        raise\n    MiniSweAgent = ()  # Empty isinstance target; no replacement agent implementation.\n')
    harness.write_text(text)
    changed["harness/testbench_harness.py"] = "Guard only absent optional MiniSweAgent import; every class and function body preserved"
    before = ast.parse((source / "harness/testbench_harness.py").read_text())
    after = ast.parse(text)
    definitions = lambda tree: [ast.dump(node, include_attributes=False) for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef))]
    if definitions(before) != definitions(after):
        raise ValueError("Harness definitions changed during optional-import adaptation")
    records = []
    for original in sorted(source.rglob("*")):
        if not original.is_file() or "__pycache__" in original.parts or original.suffix == ".pyc":
            continue
        relative = original.relative_to(source).as_posix()
        old = hashlib.sha256(original.read_bytes()).hexdigest()
        new = hashlib.sha256((output / "testprism" / relative).read_bytes()).hexdigest()
        if old != new and relative not in changed:
            raise ValueError("Unexpected runtime copy change: " + relative)
        records.append({"path": relative, "sourceSha256": old, "runtimeSha256": new})
    (output / "runtime-audit.json").write_text(json.dumps({"source": str(bench), "changes": changed,
        "harnessDefinitionsUnchanged": True, "files": records}, indent=2))
    return output


def verify_runtime(bench: Path, runtime: Path):
    audit = json.loads((runtime / "runtime-audit.json").read_text())
    if Path(audit["source"]).resolve() != bench.resolve() or not audit["harnessDefinitionsUnchanged"]:
        raise ValueError("Runtime audit does not match the selected benchmark")
    expected = {item["path"] for item in audit["files"]}
    if len(expected) != len(audit["files"]):
        raise ValueError("Duplicate runtime audit paths")
    for root in (bench, runtime):
        actual = {p.relative_to(root / "testprism").as_posix() for p in (root / "testprism").rglob("*")
                  if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"}
        if actual != expected:
            raise ValueError("Runtime audit file inventory changed")
    for item in audit["files"]:
        relative = Path(item["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Invalid runtime audit path")
        for root, key in ((bench, "sourceSha256"), (runtime, "runtimeSha256")):
            if hashlib.sha256((root / "testprism" / relative).read_bytes()).hexdigest() != item[key]:
                raise ValueError("Runtime/source digest changed: " + str(relative))
    definitions = lambda root: [ast.dump(node, include_attributes=False) for node in ast.parse(
        (root / "testprism/harness/testbench_harness.py").read_text()).body if isinstance(node, (ast.ClassDef, ast.FunctionDef))]
    if definitions(bench) != definitions(runtime):
        raise ValueError("Runtime harness definitions differ from the external scorer")
    return audit
