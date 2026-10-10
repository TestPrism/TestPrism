#!/usr/bin/env python3
"""Validate the anonymous TestPrism code artifact without benchmark data."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TESTHELIX = ROOT / "TestHelix"
MAX_REPOSITORY_BYTES = 95 * 1024 * 1024
ALLOWED_RELEASE_REMOTES = {
    "https://github.com/TestPrism/TestPrism",
    "https://github.com/TestPrism/TestPrism.git",
    "git@github.com:TestPrism/TestPrism.git",
}
FORBIDDEN_TOP_LEVEL = ("verified_tasks", "patch_sets")
FORBIDDEN_PATH_PARTS = {
    "patchgen",
    "attempts.jsonl",
    ".env",
    ".DS_Store",
    "__pycache__",
}


def hidden_literal(hex_value: str) -> re.Pattern[str]:
    """Build a leak detector without reproducing the sensitive value in source."""
    return re.compile(re.escape(bytes.fromhex(hex_value).decode()), re.I)


FORBIDDEN_TEXT = {
    "development GitHub account": hidden_literal(
        "6769746875622e636f6d2f7363687765726c69"
    ),
    "development email": hidden_literal("313939313831313536354071712e636f6d"),
    "developer home path": hidden_literal("2f55736572732f6a61792f"),
    "development datastore path": hidden_literal(
        "2f6d6e742f6461746173746f72652f436f64655462656e63682f"
    ),
    "private workspace path": hidden_literal("436f64655442656e63682d50726f2f"),
}
SECRET_PATTERNS = {
    "OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "GitHub token": re.compile(r"\bgh[opsu]_[A-Za-z0-9]{20,}\b"),
    "AWS access key": re.compile(r"\bAKIA(?!IOSFODNN7EXAMPLE)[0-9A-Z]{16}\b"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}


def fail(errors: list[str], message: str) -> None:
    errors.append(message)


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def main() -> int:
    errors: list[str] = []

    for name in FORBIDDEN_TOP_LEVEL:
        if (ROOT / name).exists():
            fail(errors, f"external data must not be bundled in code repository: {name}/")
    if (ROOT / "benchmark300.json").exists():
        fail(errors, "benchmark300.json belongs in the external patch-set repository")
    if (ROOT / "testprism" / "patchgen").exists():
        fail(errors, "unfinished testprism/patchgen producer is present")

    required_testhelix_files = (
        "package.json",
        "package-lock.json",
        "src/agent.ts",
        "src/cli.ts",
        "integrations/testprism/agent.py",
        "skills/testprism/artifact-workflow/SKILL.md",
        "tests/testprism_integration_contract.py",
    )
    for relative_path in required_testhelix_files:
        if not (TESTHELIX / relative_path).is_file():
            fail(errors, f"missing TestHelix artifact file: {relative_path}")
    if (TESTHELIX / "package.json").is_file():
        package = json.loads(
            (TESTHELIX / "package.json").read_text(encoding="utf-8")
        )
        if package.get("name") != "testhelix" or not package.get("version"):
            fail(errors, "invalid TestHelix package metadata")

    git_dir = ROOT / ".git"
    if git_dir.exists():
        tracked_files = [Path(item) for item in git("ls-files").splitlines()]
    else:
        tracked_files = [
            path.relative_to(ROOT)
            for path in ROOT.rglob("*")
            if path.is_file()
        ]
    tracked_set = set(tracked_files)
    if git_dir.exists():
        for relative_path in required_testhelix_files:
            tracked_path = Path("TestHelix") / relative_path
            if tracked_path not in tracked_set:
                fail(errors, f"untracked TestHelix artifact file: {relative_path}")
    for relative_path in tracked_files:
        if any(part in FORBIDDEN_PATH_PARTS for part in relative_path.parts):
            fail(errors, f"forbidden release path: {relative_path}")
        path = ROOT / relative_path
        if not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for label, pattern in {**FORBIDDEN_TEXT, **SECRET_PATTERNS}.items():
            if pattern.search(text):
                fail(errors, f"{label} found in {relative_path}")

    git_bytes: int | None = None
    if git_dir.exists():
        commit_count = int(git("rev-list", "--count", "HEAD").strip())
        if commit_count != 1:
            fail(errors, f"expected exactly one Git commit, found {commit_count}")
        identities = set(
            git("log", "--format=%an <%ae>%n%cn <%ce>").splitlines()
        )
        if identities != {"Anonymous <anonymous@invalid>"}:
            fail(errors, f"non-anonymous Git identity found: {sorted(identities)}")
        for remote in git("remote").splitlines():
            urls = {
                url
                for url in git("remote", "get-url", "--all", remote).splitlines()
                if url
            }
            if remote != "origin" or not urls or not urls <= ALLOWED_RELEASE_REMOTES:
                fail(
                    errors,
                    f"unexpected Git remote {remote!r}: {sorted(urls)}",
                )

        git_bytes = sum(
            path.stat().st_size for path in git_dir.rglob("*") if path.is_file()
        )
        if git_bytes >= MAX_REPOSITORY_BYTES:
            fail(
                errors,
                f"Git repository is {git_bytes / 1024 / 1024:.2f} MiB; "
                "must remain below the 95 MiB safety threshold",
            )

    if errors:
        print(f"FAIL: {len(errors)} artifact validation error(s)", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    if git_bytes is None:
        print("PASS: code-only source archive, TestHelix present; hygiene checks passed")
    else:
        print(
            f"PASS: code-only artifact, TestHelix present, one anonymous commit, "
            f"Git storage {git_bytes / 1024 / 1024:.2f} MiB"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
