"""Package the staged anonymous source tree for handoff.

Ignored files, generated output and untracked development material are excluded.
Recipients install dependencies and build the agent as described in README.md.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile

PROJECT = Path(__file__).resolve().parents[1]
FILES = {"README.md", "TESTPRISM_QUICKSTART.md", "package.json", "package-lock.json", "tsconfig.json", ".gitignore", ".env.example"}
DIRECTORIES = {"src", "tests", "scripts", "skills", "configs", "integrations", "examples"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=PROJECT / ".runs/handoff/testhelix-handoff.tar.gz")
    args = parser.parse_args()
    output = args.out.resolve()
    if output.suffixes[-2:] != [".tar", ".gz"]:
        parser.error("--out must end in .tar.gz")
    for name in (*DIRECTORIES, "TestPrism-main", "TestPrism", "docs", "legacy", ".git", "node_modules"):
        if output.is_relative_to(PROJECT / name):
            parser.error("Archive output must be outside source and external input directories")
    if subprocess.run(["git", "diff", "--quiet"], cwd=PROJECT, check=False).returncode:
        parser.error("Stage all source changes before packaging")
    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=PROJECT, check=True, capture_output=True).stdout
    paths = []
    for raw in tracked.split(b"\0"):
        if not raw:
            continue
        relative = Path(os.fsdecode(raw))
        if relative.is_absolute() or ".." in relative.parts or not (
            relative.as_posix() in FILES or relative.parts[0] in DIRECTORIES
        ):
            raise ValueError(f"File outside anonymous source allowlist: {relative}")
        paths.append(PROJECT / relative)
    payloads = {}
    entries = []
    for file in sorted(set(paths)):
        relative = file.relative_to(PROJECT)
        if file.suffix in {".pyc", ".pyo", ".tsbuildinfo"} or ".local." in file.name:
            raise ValueError(f"Generated or local file in index: {relative}")
        if file.is_symlink():
            raise ValueError(f"Refusing linked input: {relative}")
        data = file.read_bytes()
        if re.search(rb"\bsk-[A-Za-z0-9_-]{24,}\b|-----BEGIN [A-Z ]*PRIVATE KEY-----", data):
            # Report only the filename, never a credential match.
            raise ValueError(f"Possible credential in packaged input: {relative}")
        mode = 0o755 if file.stat().st_mode & 0o111 else 0o644
        payloads[relative.as_posix()] = (data, mode)
        entries.append({"path": relative.as_posix(), "bytes": len(data), "mode": oct(mode),
                        "sha256": hashlib.sha256(data).hexdigest()})
    manifest = {"schemaVersion": 1, "scope": "staged Git files matching the working tree; manifest excludes itself",
                "excluded": ["credentials", "development docs", "external benchmark", "generated builds", "node_modules", "legacy", "run history", "git metadata"],
                "files": entries}
    payloads["HANDOFF_MANIFEST.json"] = ((json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode(), 0o644)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as staged:
        temporary = Path(staged.name)
    try:
        with temporary.open("wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", filename="", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                for name, (data, mode) in sorted(payloads.items()):
                    info = tarfile.TarInfo("TestHelix/" + name)
                    info.size = len(data)
                    info.mode = mode
                    archive.addfile(info, io.BytesIO(data))
        digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    output.with_name(output.name + ".sha256").write_text(f"{digest}  {output.name}\n")
    print(json.dumps({"archive": str(output), "files": len(entries), "bytes": output.stat().st_size, "sha256": digest}))


if __name__ == "__main__":
    main()
