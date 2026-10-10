"""Stage immutable task inputs outside the read-only benchmark checkout."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import shutil
import re


def validate_task_links(task: Path):
    for file in task.rglob("*"):
        if file.is_symlink():
            if file.readlink().is_absolute() or not file.resolve().is_relative_to(task.resolve()):
                raise ValueError("Task symlink points outside its portable staged boundary: " + str(file.relative_to(task)))


def input_files(task: Path):
    return [p for p in task.rglob("*") if (p.is_file() or p.is_symlink())
            and not any(part == "__pycache__" or part.startswith(".testprism-") for part in p.relative_to(task).parts)]


def source_task(source: Path, key: str, layout: str = "auto") -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*", key):
        raise ValueError("Expected a canonical source/task ID")
    if layout not in ("auto", "canonical", "flat"):
        raise ValueError("Unknown dataset layout")
    direct, flat = source / key, source / key.replace("/", "__")
    if layout == "canonical" or (layout == "auto" and (direct / "task.yaml").is_file()):
        return direct
    return flat


def stage_dataset(source: Path, target: Path, keys: list[str], *, layout: str = "auto") -> Path:
    source, target = source.resolve(), target.resolve()
    if target == source or source in target.parents:
        raise ValueError("Staged dataset must be outside its source")
    selected = []
    if not keys or len(keys) != len(set(keys)) or any(not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*", key) for key in keys):
        raise ValueError("Staging requires unique canonical task IDs")
    for key in keys:
        task = source_task(source, key, layout)
        if not (task / "task.yaml").is_file():
            raise ValueError(f"Selected task is unavailable: {key}")
        validate_task_links(task)
        selected.append((key, task))
    if target.exists():
        audit_file = target.parent / "staging.json"
        if not audit_file.is_file():
            raise ValueError("Existing staging is incomplete; use a fresh run directory")
        audit = json.loads(audit_file.read_text())
        if audit.get("source") != str(source) or audit.get("taskKeys") != keys or audit.get("layout", "auto") != layout:
            raise ValueError("Cannot resume with changed staged input selection")
        records = {item["path"]: item for item in audit["files"]}
        source_files = {key + "/" + p.relative_to(task).as_posix(): p for key, task in selected for p in input_files(task)}
        copied_files = {p.relative_to(target).as_posix(): p for p in input_files(target)}
        if records.keys() != source_files.keys() or records.keys() != copied_files.keys():
            raise ValueError("Cannot resume with changed staged input inventory")
        for name, item in records.items():
            for file in (source_files[name], copied_files[name]):
                if "symlinkTarget" in item:
                    valid = file.is_symlink() and str(file.readlink()) == item["symlinkTarget"]
                else:
                    valid = not file.is_symlink() and hashlib.sha256(file.read_bytes()).hexdigest() == item["sha256"]
                if not valid:
                    raise ValueError("Cannot resume with changed staged input: " + name)
        # The native Harness remains responsible for run-lock/configuration validation
        # and deciding which completed or interrupted trials to reuse.
        return target
    target.mkdir(parents=True)
    records = []
    for key, task in selected:
        destination = target / key
        shutil.copytree(task, destination, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", ".testprism-*"))
        for file in sorted(destination.rglob("*")):
            if file.is_symlink():
                original = task / file.relative_to(destination)
                if file.readlink() != original.readlink():
                    raise RuntimeError("Task symlink changed during staging")
                records.append({"path": file.relative_to(target).as_posix(), "symlinkTarget": str(file.readlink())})
            elif file.is_file():
                original = task / file.relative_to(destination)
                digest = hashlib.sha256(file.read_bytes()).hexdigest()
                if digest != hashlib.sha256(original.read_bytes()).hexdigest():
                    raise RuntimeError("Task changed during staging")
                records.append({"path": file.relative_to(target).as_posix(), "sha256": digest})
    (target.parent / "staging.json").write_text(json.dumps({"source": str(source), "layout": layout, "taskKeys": keys, "files": records}, indent=2))
    return target
