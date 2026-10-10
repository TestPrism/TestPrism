# ruff: noqa: E501

"""Offline JaCoCo coverage backend for Java source targets.

The backend deliberately does not install or modify a task build.  The host
copies the harness-vendored JaCoCo agent and dependency-complete CLI into the
namespace owned by this run; setup verifies the host integrity root through a
namespace-local checksum manifest before injecting the agent through
``JAVA_TOOL_OPTIONS``.  Evaluation performs no task/JVM cache discovery or
network access.  Collection re-verifies the copied payload, invokes the CLI,
and uses a dependency-free Python normalizer to produce the common line-report
contract.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping

from testprism.harness.coverage_backends import (
    CoverageBackend,
    normalize_target_path,
)

JAVA_SOURCE_SUFFIXES = (".java",)
# Descriptive aliases make the JaCoCo terminology discoverable to callers.
JACOCO_SOURCE_SUFFIXES = JAVA_SOURCE_SUFFIXES
JAVA_COVERAGE_ROOT = "/tmp/testbench/coverage"
JACOCO_COVERAGE_ROOT = JAVA_COVERAGE_ROOT

# JaCoCo is a release payload owned by this harness.  The jars are copied to
# each suite namespace by ``write_java_runtime_files`` and are never located
# through a task/JVM cache.  Keep the expected values in code as a second
# integrity root: a modified jar, manifest, or checksum file cannot make the
# container accept a modified payload.
JACOCO_VENDOR_ROOT = Path(__file__).resolve().parent / "_vendor" / "jacoco"
JACOCO_VENDOR_MANIFEST = JACOCO_VENDOR_ROOT / "manifest.json"
JACOCO_VENDOR_LICENSE = JACOCO_VENDOR_ROOT / "LICENSE.txt"
JACOCO_VENDOR_NOTICE = JACOCO_VENDOR_ROOT / "NOTICE.txt"
JACOCO_VENDOR_ARTIFACTS = {
    "agent": {
        "filename": "org.jacoco.agent-0.8.15-runtime.jar",
        "sha256": "fb5b0036a0899ea97edfa0fc2c7985b55f3f7c5695028163e5e60d4f3cf6075d",
    },
    "cli": {
        "filename": "org.jacoco.cli-0.8.15-nodeps.jar",
        "sha256": "d2b74b20b415163c1f53261e7c5ef4445b788327094208ff821e0c2baf9bc8f1",
    },
}
JACOCO_VENDOR_MANIFEST_SHA256 = (
    "a6f069d238d3d1ec7ecb79e10214c9f9427d55631477e0dad0d0351f8282700b"
)
# Retained as an empty compatibility symbol for callers that imported the old
# discovery allowlist.  An empty tuple is intentional: T06 forbids cache or
# filesystem discovery during evaluation.
JACOCO_DISCOVERY_ROOTS: tuple[str, ...] = ()
GRADLE_USER_HOME_DIRNAME = "gradle-user-home"
GRADLE_INIT_SCRIPT_NAME = "testprism-disable-project-jacoco.init.gradle"
GRADLE_DISABLE_PROJECT_JACOCO_INIT_SCRIPT = r"""gradle.projectsLoaded {
    gradle.rootProject.allprojects { project ->
        project.pluginManager.withPlugin('jacoco') {
            project.tasks.withType(org.gradle.api.tasks.testing.Test).configureEach { testTask ->
                def jacocoExtension = testTask.extensions.findByName('jacoco')
                if (jacocoExtension != null) {
                    jacocoExtension.enabled = false
                }
            }
        }
    }
}
"""


def _class_root_discovery_command(
    trusted_test_root: str = "/tests",
    declaration_path: str | None = None,
) -> str:
    """Render bounded class-root discovery for the collection container.

    ``trusted_test_root`` is the existing fixed discovery root.  A runner may
    optionally persist additional roots one-per-line in ``declaration_path``;
    those entries are treated as data (never sourced/evaluated), canonicalized
    with ``pwd -P``, and accepted only when they resolve to an existing,
    non-symlink directory below the task workdir.
    """

    rendered_test_root = str(trusted_test_root).replace("\\", "/").rstrip("/") or "/"
    test_root = PurePosixPath(rendered_test_root)
    if (
        not test_root.is_absolute()
        or test_root == PurePosixPath("/")
        or ".." in test_root.parts
    ):
        raise ValueError(
            f"trusted Java test root must be an absolute bounded path: {trusted_test_root!r}"
        )
    quoted_test_root = shlex.quote(test_root.as_posix())
    declaration = ""
    if declaration_path is not None:
        quoted_declaration = shlex.quote(str(declaration_path))
        declaration = (
            f"if [ -f {quoted_declaration} ] && [ ! -L {quoted_declaration} ]; then "
            "__ctb_workdir_real=$(pwd -P 2>/dev/null) || __ctb_workdir_real=; "
            "while IFS= read -r __ctb_declared; do "
            '[ -n "$__ctb_declared" ] || continue; '
            'case "$__ctb_declared" in '
            '/*) __ctb_candidate=$__ctb_declared;; '
            '*) __ctb_candidate=$PWD/$__ctb_declared;; '
            "esac; "
            '[ -d "$__ctb_candidate" ] || continue; '
            '[ ! -L "$__ctb_candidate" ] || continue; '
            '__ctb_real=$(cd -P "$__ctb_candidate" 2>/dev/null && pwd -P) || continue; '
            'case "$__ctb_real" in '
            '"$__ctb_workdir_real"|"$__ctb_workdir_real"/*) '
            'printf "%s\\n" "$__ctb_real";; '
            "esac; "
            f"done < {quoted_declaration}; "
            "fi; "
        )
    return (
        "__ctb_classes=$({ "
        "__ctb_workdir_real=$(pwd -P 2>/dev/null) || __ctb_workdir_real=; "
        "find -P \"$PWD\" -type d \\( "
        "-path '*/build/classes/java/main' -o "
        "-path '*/target/classes' -o "
        "-path \"$PWD/agent-tests/.build/classes\" \\) -print; "
        f"find -P {quoted_test_root} -type d \\( "
        "-path '*/build/classes/java/main' -o "
        "-path '*/target/classes' \\) -print; "
        + declaration
        + "} 2>/dev/null | LC_ALL=C sort -u); "
    )


@dataclass(frozen=True)
class JavaCoverageRuntimeFiles:
    """Host-side payload and namespace paths for one Java collection."""

    root: Path
    namespace: str
    container_root: str
    coverage_dir: str
    exec_path: str
    xml_path: str
    json_path: str
    report_path: str
    targets_path: str
    class_roots_path: str
    gradle_user_home: str
    gradle_init_script_path: str
    normalizer_path: str
    agent_path: str
    cli_path: str
    agent_probe_path: str
    payload_manifest_path: str = ""
    payload_checksum_path: str = ""


def _validate_namespace(namespace: str) -> str:
    if not isinstance(namespace, str) or not namespace:
        raise ValueError("Java coverage namespace must be non-empty")
    if not all(character.isalnum() or character in "_-" for character in namespace):
        raise ValueError(f"invalid Java coverage namespace: {namespace!r}")
    return namespace


def java_coverage_namespace(namespace: str = "generated") -> dict[str, str]:
    """Return deterministic, disjoint JaCoCo paths for one suite."""

    namespace = _validate_namespace(namespace)
    root = f"{JAVA_COVERAGE_ROOT}/{namespace}/java"
    json_path = f"{root}/coverage.json"
    gradle_user_home = f"{root}/{GRADLE_USER_HOME_DIRNAME}"
    gradle_init_dir = f"{gradle_user_home}/init.d"
    return {
        "root": root,
        "coverage_dir": root,
        "exec_path": f"{root}/jacoco.exec",
        "xml_path": f"{root}/jacoco.xml",
        "json_path": json_path,
        "report_path": json_path,
        "targets_path": f"{root}/targets.json",
        "class_roots_path": f"{root}/class-roots.txt",
        "gradle_user_home": gradle_user_home,
        "gradle_init_script_path": f"{gradle_init_dir}/{GRADLE_INIT_SCRIPT_NAME}",
        "normalizer_path": f"{root}/normalize_jacoco_coverage.py",
        "agent_path": f"{root}/jacocoagent.jar",
        "cli_path": f"{root}/jacococli.jar",
        "agent_probe_path": f"{root}/agent-probe.exec",
        "payload_manifest_path": f"{root}/jacoco-manifest.json",
        "payload_checksum_path": f"{root}/jacoco-payload.sha256",
    }


def _workdir_path(workdir: str | Path) -> Path:
    rendered = str(workdir).replace("\\", "/").rstrip("/") or "/"
    path = Path(rendered)
    if not path.is_absolute():
        raise ValueError(f"coverage workdir must be absolute: {workdir!r}")
    return path.resolve(strict=False)


def _target_absolute(workdir: Path, target: str | Path) -> tuple[str, Path]:
    rendered = str(target).replace("\\", "/")
    candidate = Path(rendered)
    if candidate.is_absolute():
        if ".." in PurePosixPath(rendered).parts:
            raise ValueError(f"coverage target escapes workdir: {target}")
        absolute = candidate
    else:
        relative = normalize_target_path(rendered)
        absolute = workdir / PurePosixPath(relative)
    absolute = absolute.resolve(strict=False)
    try:
        relative_path = absolute.relative_to(workdir)
    except ValueError as exc:
        raise ValueError(f"coverage target escapes workdir: {target}") from exc
    relative = normalize_target_path(relative_path.as_posix())
    return relative, absolute


def _target_map(
    workdir: str | Path,
    target_paths: Iterable[str | Path],
) -> tuple[Path, dict[str, Path]]:
    workdir_path = _workdir_path(workdir)
    targets: dict[str, Path] = {}
    for target in target_paths:
        relative, absolute = _target_absolute(workdir_path, target)
        targets[relative] = absolute
    if not targets:
        raise ValueError("Java coverage requires at least one target path")
    return workdir_path, dict(sorted(targets.items()))


def _source_line_universe(source: bytes) -> set[int]:
    """Return executable-looking positive source lines for absent XML files."""

    try:
        text = source.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("Java source is not valid UTF-8") from exc

    result: set[int] = set()
    in_block_comment = False
    quote: str | None = None
    escaped = False
    for line_number, line in enumerate(text.splitlines(), 1):
        visible: list[str] = []
        index = 0
        while index < len(line):
            character = line[index]
            if in_block_comment:
                end = line.find("*/", index)
                if end < 0:
                    index = len(line)
                    break
                in_block_comment = False
                index = end + 2
                continue
            if quote is not None:
                visible.append(character)
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == quote:
                    quote = None
                index += 1
                continue
            if character in {"\"", "'"}:
                quote = character
                visible.append(character)
                index += 1
                continue
            if line.startswith("//", index):
                break
            if line.startswith("/*", index):
                in_block_comment = True
                index += 2
                continue
            visible.append(character)
            index += 1
        content = "".join(visible).strip()
        compact = re.sub(r"\s+", "", content)
        if content and not re.fullmatch(r"[{}\[\]();,]+", compact):
            result.add(line_number)
    return result


def _read_source_lines(path: Path) -> set[int]:
    try:
        return _source_line_universe(path.read_bytes())
    except FileNotFoundError:
        return set()
    except OSError as exc:
        raise ValueError(f"cannot read Java target source: {path}") from exc


def _parse_xml(value: object) -> ET.Element:
    try:
        if isinstance(value, ET.Element):
            return value
        if isinstance(value, (bytes, bytearray)):
            return ET.fromstring(value)
        if isinstance(value, Path):
            return ET.parse(value).getroot()
        if isinstance(value, str):
            if value.lstrip().startswith("<"):
                return ET.fromstring(value.encode("utf-8"))
            return ET.parse(value).getroot()
    except (ET.ParseError, OSError, UnicodeError, ValueError) as exc:
        raise ValueError("malformed JaCoCo XML") from exc
    raise ValueError("JaCoCo XML must be XML text, bytes, path, or an element")


def _xml_local_name(tag: object) -> str:
    if not isinstance(tag, str):
        raise ValueError("malformed JaCoCo XML element")
    return tag.rsplit("}", 1)[-1]


def _required_xml_attr(element: ET.Element, name: str) -> str:
    value = element.attrib.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"JaCoCo XML {name} attribute is missing")
    if "\x00" in value:
        raise ValueError("JaCoCo XML attribute contains NUL")
    return value


def _counter(value: str, name: str, *, positive: bool = False) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"\d+", value):
        raise ValueError(f"JaCoCo XML {name} counter must be a non-negative integer")
    parsed = int(value)
    if positive and parsed <= 0:
        raise ValueError(f"JaCoCo XML {name} must be positive")
    return parsed


def _normalise_package(package: str) -> str:
    rendered = package.replace("\\", "/")
    if rendered.startswith("/") or (len(rendered) >= 2 and rendered[1] == ":"):
        raise ValueError("JaCoCo package path escapes workdir")
    rendered = rendered.strip("/")
    if rendered in {".", ".."} or "\x00" in rendered:
        raise ValueError("malformed JaCoCo package path")
    if rendered.startswith("/") or ".." in PurePosixPath(rendered).parts:
        raise ValueError("JaCoCo package path escapes workdir")
    # JaCoCo normally uses slash-separated package names; accepting dotted
    # names keeps hand-authored reports and older producers interoperable.
    if "/" not in rendered and "." in rendered:
        rendered = rendered.replace(".", "/")
    return rendered


def _source_candidate(package: str, sourcefile: str) -> str:
    rendered = sourcefile.replace("\\", "/")
    if rendered.startswith("/") or (len(rendered) >= 2 and rendered[1] == ":"):
        raise ValueError("JaCoCo sourcefile path escapes workdir")
    rendered = rendered.strip("/")
    if not rendered or "\x00" in rendered:
        raise ValueError("malformed JaCoCo sourcefile path")
    if rendered.startswith("/") or ".." in PurePosixPath(rendered).parts:
        raise ValueError("JaCoCo sourcefile path escapes workdir")
    package = _normalise_package(package)
    return normalize_target_path(f"{package}/{rendered}" if package else rendered)


_JAVA_IDENTIFIER = r"(?:[^\W\d]|[$_])(?:\w|[$])*"
_JAVA_PACKAGE_NAME = rf"{_JAVA_IDENTIFIER}(?:\s*\.\s*{_JAVA_IDENTIFIER})*"
_JAVA_TYPE_DECLARATION = re.compile(r"\b(?:class|interface|enum|record)\b")


def _java_code_without_comments_or_literals(source: bytes) -> str:
    """Decode Java and blank comments/literals while preserving token spacing."""

    try:
        text = source.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("Java source is not valid UTF-8") from exc

    result: list[str] = []
    index = 0
    state = "code"
    quote = ""
    while index < len(text):
        character = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if state == "line-comment":
            if character in "\r\n":
                state = "code"
                result.append(character)
            else:
                result.append(" ")
            index += 1
            continue
        if state == "block-comment":
            if character == "*" and following == "/":
                result.extend((" ", " "))
                index += 2
                state = "code"
            else:
                result.append(character if character in "\r\n" else " ")
                index += 1
            continue
        if state == "literal":
            result.append(character if character in "\r\n" else " ")
            if character == "\\":
                if following:
                    result.append(following if following in "\r\n" else " ")
                    index += 2
                else:
                    index += 1
            elif character == quote:
                state = "code"
                index += 1
            else:
                index += 1
            continue
        if character == "/" and following == "/":
            result.extend((" ", " "))
            index += 2
            state = "line-comment"
        elif character == "/" and following == "*":
            result.extend((" ", " "))
            index += 2
            state = "block-comment"
        elif character in {"\"", "'"}:
            result.append(" ")
            quote = character
            state = "literal"
            index += 1
        else:
            result.append(character)
            index += 1
    return "".join(result)


def _declared_java_package(source: bytes) -> str | None:
    """Return one valid package before the first type, otherwise no alias."""

    code = _java_code_without_comments_or_literals(source)
    type_declaration = _JAVA_TYPE_DECLARATION.search(code)
    prefix = code[: type_declaration.start()] if type_declaration else code
    package_keywords = list(re.finditer(r"\bpackage\b", prefix))
    if len(package_keywords) != 1:
        return None
    declaration = re.match(
        rf"package\s+({_JAVA_PACKAGE_NAME})\s*;",
        prefix[package_keywords[0].start() :],
    )
    if declaration is None:
        return None
    return re.sub(r"\s+", "", declaration.group(1))


def _target_package_aliases(targets: Mapping[str, Path]) -> dict[str, list[str]]:
    aliases: dict[str, list[str]] = {}
    for relative, absolute in targets.items():
        try:
            source = absolute.read_bytes()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ValueError(f"cannot read Java target source: {absolute}") from exc
        package = _declared_java_package(source)
        if package is None:
            continue
        alias = f"{package.replace('.', '/')}/{PurePosixPath(relative).name}"
        aliases.setdefault(alias, []).append(relative)
    return aliases


def _matching_targets(
    candidate: str,
    targets: Mapping[str, Path],
    package_aliases: Mapping[str, list[str]],
) -> list[str]:
    exact = [candidate] if candidate in targets else []
    if exact:
        return exact
    suffix = f"/{candidate}"
    matches = [relative for relative in targets if relative.endswith(suffix)]
    if matches:
        return sorted(matches)
    aliases = package_aliases.get(candidate, [])
    if len(aliases) == 1:
        return aliases
    return []


def _normalise_reports(
    xml: object,
    *,
    workdir: str | Path,
    target_paths: Iterable[str | Path],
) -> dict[str, object]:
    workdir_path, targets = _target_map(workdir, target_paths)
    root = _parse_xml(xml)
    if _xml_local_name(root.tag) != "report":
        raise ValueError("JaCoCo XML root must be report")

    source_lines = {
        relative: _read_source_lines(absolute)
        for relative, absolute in targets.items()
    }
    package_aliases = _target_package_aliases(targets)
    xml_lines: dict[str, set[int]] = {relative: set() for relative in targets}
    executed: dict[str, set[int]] = {relative: set() for relative in targets}
    saw_sourcefile: set[str] = set()

    for package in root.iter():
        if _xml_local_name(package.tag) != "package":
            continue
        package_name = package.attrib.get("name", "")
        if not isinstance(package_name, str):
            raise ValueError("JaCoCo package name must be a string")
        for sourcefile in package:
            if _xml_local_name(sourcefile.tag) != "sourcefile":
                continue
            source_name = _required_xml_attr(sourcefile, "name")
            candidate = _source_candidate(package_name, source_name)
            matched = _matching_targets(candidate, targets, package_aliases)
            for line in sourcefile:
                if _xml_local_name(line.tag) != "line":
                    continue
                number = _counter(_required_xml_attr(line, "nr"), "nr", positive=True)
                missed = _counter(_required_xml_attr(line, "mi"), "mi")
                covered = _counter(_required_xml_attr(line, "ci"), "ci")
                if not matched:
                    # Reports include dependencies and generated classes.  A
                    # valid external entry is ignored only after validating
                    # its counters, so malformed collection is never hidden.
                    continue
                if missed == 0 and covered == 0:
                    continue
                for target in matched:
                    saw_sourcefile.add(target)
                    xml_lines[target].add(number)
                    if covered > 0:
                        executed[target].add(number)

    files: dict[str, dict[str, list[int]]] = {}
    for relative in sorted(targets):
        universe = xml_lines[relative] if relative in saw_sourcefile else source_lines[relative]
        covered = sorted(executed[relative] & universe)
        missing = sorted(universe - set(covered))
        files[relative] = {
            "executed_lines": covered,
            "missing_lines": missing,
        }
    del workdir_path  # Path validation above is intentional; XML has no host paths.
    return {"files": files}


def normalize_jacoco_xml(
    xml: object,
    *,
    workdir: str | Path,
    target_paths: Iterable[str | Path],
) -> dict[str, object]:
    """Normalize JaCoCo XML counters to the common line report contract."""

    return _normalise_reports(xml, workdir=workdir, target_paths=target_paths)


normalize_jacoco_coverage = normalize_jacoco_xml
normalize_jacoco_report = normalize_jacoco_xml
normalise_jacoco_xml = normalize_jacoco_xml


JACOCO_NORMALIZER_SOURCE = r'''#!/usr/bin/env python3
"""Container-side JaCoCo XML normalizer (stdlib only)."""
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath


def fail(message):
    raise ValueError(message)


def local(tag):
    if not isinstance(tag, str):
        fail("malformed JaCoCo XML element")
    return tag.rsplit("}", 1)[-1]


def source_lines(source):
    text = source.decode("utf-8", errors="strict")
    lines = set()
    block = False
    quote = None
    escaped = False
    for number, line in enumerate(text.splitlines(), 1):
        visible = []
        index = 0
        while index < len(line):
            char = line[index]
            if block:
                end = line.find("*/", index)
                if end < 0:
                    index = len(line)
                    break
                block = False
                index = end + 2
                continue
            if quote is not None:
                visible.append(char)
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
                index += 1
                continue
            if char in ("\"", "'"):
                quote = char
                visible.append(char)
                index += 1
                continue
            if line.startswith("//", index):
                break
            if line.startswith("/*", index):
                block = True
                index += 2
                continue
            visible.append(char)
            index += 1
        content = "".join(visible).strip()
        compact = re.sub(r"\s+", "", content)
        if content and not re.fullmatch(r"[{}\[\]();,]+", compact):
            lines.add(number)
    return lines


def integer(value, name, positive=False):
    if not isinstance(value, str) or not re.fullmatch(r"\d+", value):
        fail("JaCoCo XML %s counter must be a non-negative integer" % name)
    result = int(value)
    if positive and result <= 0:
        fail("JaCoCo XML %s must be positive" % name)
    return result


def normalize_package(value):
    rendered = value.replace("\\", "/")
    if rendered.startswith("/") or (len(rendered) >= 2 and rendered[1] == ":"):
        fail("JaCoCo package path escapes workdir")
    rendered = rendered.strip("/")
    if rendered in (".", "..") or "\x00" in rendered:
        fail("malformed JaCoCo package path")
    if rendered.startswith("/") or ".." in PurePosixPath(rendered).parts:
        fail("JaCoCo package path escapes workdir")
    if "/" not in rendered and "." in rendered:
        rendered = rendered.replace(".", "/")
    return rendered


def candidate(package, sourcefile):
    rendered = sourcefile.replace("\\", "/")
    if rendered.startswith("/") or (len(rendered) >= 2 and rendered[1] == ":"):
        fail("JaCoCo sourcefile path escapes workdir")
    rendered = rendered.strip("/")
    if not rendered or rendered.startswith("/") or ".." in PurePosixPath(rendered).parts:
        fail("JaCoCo sourcefile path escapes workdir")
    package = normalize_package(package)
    value = (package + "/" + rendered) if package else rendered
    value = value.replace("//", "/").strip("/")
    if not value or ".." in PurePosixPath(value).parts:
        fail("JaCoCo sourcefile path escapes workdir")
    return value


JAVA_IDENTIFIER = r"(?:[^\W\d]|[$_])(?:\w|[$])*"
JAVA_PACKAGE_NAME = JAVA_IDENTIFIER + r"(?:\s*\.\s*" + JAVA_IDENTIFIER + r")*"
JAVA_TYPE_DECLARATION = re.compile(r"\b(?:class|interface|enum|record)\b")


def code_without_comments_or_literals(source):
    text = source.decode("utf-8", errors="strict")
    result = []
    index = 0
    state = "code"
    quote = ""
    while index < len(text):
        char = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if state == "line-comment":
            if char in "\r\n":
                state = "code"
                result.append(char)
            else:
                result.append(" ")
            index += 1
            continue
        if state == "block-comment":
            if char == "*" and following == "/":
                result.extend((" ", " "))
                index += 2
                state = "code"
            else:
                result.append(char if char in "\r\n" else " ")
                index += 1
            continue
        if state == "literal":
            result.append(char if char in "\r\n" else " ")
            if char == "\\":
                if following:
                    result.append(following if following in "\r\n" else " ")
                    index += 2
                else:
                    index += 1
            elif char == quote:
                state = "code"
                index += 1
            else:
                index += 1
            continue
        if char == "/" and following == "/":
            result.extend((" ", " "))
            index += 2
            state = "line-comment"
        elif char == "/" and following == "*":
            result.extend((" ", " "))
            index += 2
            state = "block-comment"
        elif char in ("\"", "'"):
            result.append(" ")
            quote = char
            state = "literal"
            index += 1
        else:
            result.append(char)
            index += 1
    return "".join(result)


def declared_package(source):
    code = code_without_comments_or_literals(source)
    type_declaration = JAVA_TYPE_DECLARATION.search(code)
    prefix = code[:type_declaration.start()] if type_declaration else code
    package_keywords = list(re.finditer(r"\bpackage\b", prefix))
    if len(package_keywords) != 1:
        return None
    declaration = re.match(
        r"package\s+(" + JAVA_PACKAGE_NAME + r")\s*;",
        prefix[package_keywords[0].start():],
    )
    if declaration is None:
        return None
    return re.sub(r"\s+", "", declaration.group(1))


def package_aliases(targets):
    aliases = {}
    for relative, absolute in targets.items():
        try:
            package = declared_package(absolute.read_bytes())
        except FileNotFoundError:
            continue
        if package is None:
            continue
        alias = package.replace(".", "/") + "/" + PurePosixPath(relative).name
        aliases.setdefault(alias, []).append(relative)
    return aliases


def matches(value, targets, aliases):
    if value in targets:
        return [value]
    suffix = "/" + value
    found = sorted(path for path in targets if path.endswith(suffix))
    if found:
        return found
    found = aliases.get(value, [])
    if len(found) == 1:
        return found
    return []


def main():
    environment = __import__("os").environ
    manifest = json.loads(Path(environment["TESTPRISM_JACOCO_TARGETS"]).read_text())
    if not isinstance(manifest, dict) or not isinstance(manifest.get("workdir"), str) or not isinstance(manifest.get("targets"), list):
        fail("malformed JaCoCo target manifest")
    workdir = Path(manifest["workdir"]).resolve()
    targets = {}
    for entry in manifest["targets"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("relative"), str) or not isinstance(entry.get("absolute"), str):
            fail("malformed JaCoCo target manifest entry")
        absolute = Path(entry["absolute"]).resolve()
        try:
            absolute.relative_to(workdir)
        except ValueError:
            fail("coverage target escapes workdir")
        targets[entry["relative"]] = absolute
    if not targets:
        fail("Java coverage requires at least one target path")
    source = {}
    for relative, absolute in targets.items():
        try:
            source[relative] = source_lines(absolute.read_bytes())
        except FileNotFoundError:
            source[relative] = set()
    aliases = package_aliases(targets)
    if environment.get("TESTPRISM_JACOCO_ZERO_EXECUTION") == "1":
        files = {
            relative: {
                "executed_lines": [],
                "missing_lines": sorted(source[relative]),
            }
            for relative in sorted(targets)
        }
        Path(environment["TESTPRISM_JACOCO_REPORT"]).write_text(json.dumps({"files": files}))
        return
    xml_lines = {relative: set() for relative in targets}
    executed = {relative: set() for relative in targets}
    saw = set()
    root = ET.parse(environment["TESTPRISM_JACOCO_XML"]).getroot()
    if local(root.tag) != "report":
        fail("JaCoCo XML root must be report")
    for package in root.iter():
        if local(package.tag) != "package":
            continue
        package_name = package.attrib.get("name", "")
        if not isinstance(package_name, str):
            fail("JaCoCo package name must be a string")
        for sourcefile in package:
            if local(sourcefile.tag) != "sourcefile":
                continue
            if not isinstance(sourcefile.attrib.get("name"), str) or not sourcefile.attrib["name"]:
                fail("JaCoCo XML sourcefile name is missing")
            value = candidate(package_name, sourcefile.attrib["name"])
            found = matches(value, targets, aliases)
            for line in sourcefile:
                if local(line.tag) != "line":
                    continue
                number = integer(line.attrib.get("nr"), "nr", True)
                missed = integer(line.attrib.get("mi"), "mi")
                covered = integer(line.attrib.get("ci"), "ci")
                if not found or (missed == 0 and covered == 0):
                    continue
                for relative in found:
                    saw.add(relative)
                    xml_lines[relative].add(number)
                    if covered > 0:
                        executed[relative].add(number)
    files = {}
    for relative in sorted(targets):
        universe = xml_lines[relative] if relative in saw else source[relative]
        covered = sorted(executed[relative] & universe)
        files[relative] = {"executed_lines": covered, "missing_lines": sorted(universe - set(covered))}
    Path(environment["TESTPRISM_JACOCO_REPORT"]).write_text(json.dumps({"files": files}))


try:
    main()
except (OSError, UnicodeError, ET.ParseError, ValueError, KeyError, TypeError) as error:
    print("JaCoCo normalization failed: %s" % error, file=sys.stderr)
    sys.exit(1)
'''


def _copy_if_present(source: Path, destination: Path) -> None:
    """Copy one trusted payload file without making setup fail on absence.

    Missing payloads are deliberately represented in the namespace so the
    container-side setup command can emit an explicit ``unsupported`` marker;
    setup failures must remain observational and fail-soft.
    """

    try:
        destination.unlink(missing_ok=True)
        if source.is_file():
            shutil.copyfile(source, destination)
    except OSError:
        # A partially copied/missing payload is checked by the namespace
        # verifier and reported as unsupported, never as an implicit zero.
        return


def _write_payload_files(root: Path) -> None:
    """Copy the pinned jars and write a namespace-local checksum file."""

    _copy_if_present(
        JACOCO_VENDOR_ROOT / JACOCO_VENDOR_ARTIFACTS["agent"]["filename"],
        root / "jacocoagent.jar",
    )
    _copy_if_present(
        JACOCO_VENDOR_ROOT / JACOCO_VENDOR_ARTIFACTS["cli"]["filename"],
        root / "jacococli.jar",
    )
    _copy_if_present(JACOCO_VENDOR_MANIFEST, root / "jacoco-manifest.json")
    # The expected values are generated from the checked-in provenance, not
    # from bytes copied into the task container.  Include the manifest itself
    # so tampering with provenance is also fail-soft.
    (root / "jacoco-payload.sha256").write_text(
        "\n".join(
            (
                f'{JACOCO_VENDOR_ARTIFACTS["agent"]["sha256"]}  jacocoagent.jar',
                f'{JACOCO_VENDOR_ARTIFACTS["cli"]["sha256"]}  jacococli.jar',
                f"{JACOCO_VENDOR_MANIFEST_SHA256}  jacoco-manifest.json",
            )
        )
        + "\n",
        encoding="utf-8",
    )


def write_java_runtime_files(
    root: Path,
    *,
    workdir: str,
    target_paths: list[str],
    namespace: str = "generated",
) -> JavaCoverageRuntimeFiles:
    """Create a target manifest and the dependency-free XML finalizer."""

    paths = java_coverage_namespace(namespace)
    workdir_path, targets = _target_map(workdir, target_paths)
    root.mkdir(parents=True, exist_ok=True)
    (root / "normalize_jacoco_coverage.py").write_text(
        JACOCO_NORMALIZER_SOURCE,
        encoding="utf-8",
    )
    gradle_user_home = root / GRADLE_USER_HOME_DIRNAME
    gradle_init_dir = gradle_user_home / "init.d"
    gradle_init_dir.mkdir(parents=True, exist_ok=True)
    (gradle_init_dir / GRADLE_INIT_SCRIPT_NAME).write_text(
        GRADLE_DISABLE_PROJECT_JACOCO_INIT_SCRIPT,
        encoding="utf-8",
    )
    (root / "targets.json").write_text(
        json.dumps(
            {
                "workdir": workdir_path.as_posix(),
                "targets": [
                    {"relative": relative, "absolute": absolute.as_posix()}
                    for relative, absolute in targets.items()
                ],
            }
        ),
        encoding="utf-8",
    )
    _write_payload_files(root)
    return JavaCoverageRuntimeFiles(
        root=root,
        namespace=_validate_namespace(namespace),
        container_root=paths["root"],
        coverage_dir=paths["coverage_dir"],
        exec_path=paths["exec_path"],
        xml_path=paths["xml_path"],
        json_path=paths["json_path"],
        report_path=paths["report_path"],
        targets_path=paths["targets_path"],
        class_roots_path=paths["class_roots_path"],
        gradle_user_home=paths["gradle_user_home"],
        gradle_init_script_path=paths["gradle_init_script_path"],
        normalizer_path=paths["normalizer_path"],
        agent_path=paths["agent_path"],
        cli_path=paths["cli_path"],
        agent_probe_path=paths["agent_probe_path"],
        payload_manifest_path=paths["payload_manifest_path"],
        payload_checksum_path=paths["payload_checksum_path"],
    )


class JavaCoverageBackend(CoverageBackend):
    """JaCoCo agent/CLI collector for ``.java`` changed targets."""

    language = "java"
    source_suffixes = JAVA_SOURCE_SUFFIXES

    def write_runtime_files(
        self,
        root: Path,
        *,
        workdir: str,
        target_paths: list[str],
        namespace: str = "generated",
    ) -> JavaCoverageRuntimeFiles:
        return write_java_runtime_files(
            root,
            workdir=workdir,
            target_paths=target_paths,
            namespace=namespace,
        )

    def setup_command(self, namespace: str = "generated") -> str:
        paths = java_coverage_namespace(namespace)
        root = shlex.quote(paths["root"])
        agent = shlex.quote(paths["agent_path"])
        cli = shlex.quote(paths["cli_path"])
        manifest = shlex.quote(paths["payload_manifest_path"])
        checksums = shlex.quote(paths["payload_checksum_path"])
        probe = shlex.quote(paths["agent_probe_path"])
        # The payload is already copied by ``write_runtime_files``.  This
        # command intentionally has no cache search or network-capable tool;
        # it verifies only those namespace-local bytes before probing Java.
        verify_payload = (
            "__ctb_verify_payload() { "
            f"[ -s {manifest} ] && [ -s {agent} ] && [ -s {cli} ] "
            f"&& [ -s {checksums} ] || return 1; "
            "__ctb_hash_tool=$(command -v sha256sum || command -v shasum || true); "
            '[ -n "$__ctb_hash_tool" ] || return 1; '
            'case "$__ctb_hash_tool" in */shasum) '
            f"(cd {root} && \"$__ctb_hash_tool\" -a 256 -c {checksums} >/dev/null 2>&1);; "
            f"*) (cd {root} && \"$__ctb_hash_tool\" -c {checksums} >/dev/null 2>&1);; esac; "
            "}; "
        )
        return (
            f"mkdir -p {root}; "
            f"rm -f {shlex.quote(paths['exec_path'])} "
            f"{shlex.quote(paths['xml_path'])} {shlex.quote(paths['json_path'])} "
            f"{shlex.quote(paths['class_roots_path'])} {probe}; "
            f"{verify_payload}"
            "__ctb_java=$(command -v java || true); "
            f'if [ -z "$__ctb_java" ] || [ ! -s {agent} ] || [ ! -s {cli} ] || [ ! -s {manifest} ]; then '
            "echo __TESTPRISM_COVERAGE_SETUP__=unsupported; "
            "echo __TESTPRISM_COVERAGE_SETUP_REASON__=vendored-payload-missing; "
            f"elif ! __ctb_verify_payload; then "
            "echo __TESTPRISM_COVERAGE_SETUP__=unsupported; "
            "echo __TESTPRISM_COVERAGE_SETUP_REASON__=vendored-payload-integrity; "
            f"elif ! \"$__ctb_java\" -jar {cli} --help >/dev/null 2>&1; then "
            "echo __TESTPRISM_COVERAGE_SETUP__=unsupported; "
            "echo __TESTPRISM_COVERAGE_SETUP_REASON__=vendored-cli-probe; "
            f"elif ! \"$__ctb_java\" \"-javaagent:{agent}=destfile={probe},"
            "append=false,output=file\" -version >/dev/null 2>&1; then "
            "echo __TESTPRISM_COVERAGE_SETUP__=unsupported; "
            "echo __TESTPRISM_COVERAGE_SETUP_REASON__=vendored-agent-probe; "
            f"elif rm -f {probe}; then "
            "echo __TESTPRISM_COVERAGE_SETUP__=ready; "
            "else echo __TESTPRISM_COVERAGE_SETUP__=failed; "
            "echo __TESTPRISM_COVERAGE_SETUP_REASON__=namespace-preparation; fi"
        )

    def environment(
        self,
        workdir: str,
        namespace: str = "generated",
    ) -> dict[str, str]:
        del workdir
        paths = java_coverage_namespace(namespace)
        return {
            "TESTPRISM_JACOCO_CLASS_ROOTS": paths["class_roots_path"],
            "GRADLE_USER_HOME": paths["gradle_user_home"],
            "JAVA_TOOL_OPTIONS": (
                "${JAVA_TOOL_OPTIONS:+${JAVA_TOOL_OPTIONS} }"
                "-Dorg.gradle.daemon=false "
                f"-javaagent:{paths['agent_path']}="
                f"destfile={paths['exec_path']},append=true,output=file"
            )
        }

    def collect_command(self, namespace: str = "generated") -> str:
        paths = java_coverage_namespace(namespace)
        root = shlex.quote(paths["root"])
        agent = shlex.quote(paths["agent_path"])
        cli = shlex.quote(paths["cli_path"])
        manifest = shlex.quote(paths["payload_manifest_path"])
        checksums = shlex.quote(paths["payload_checksum_path"])
        verify_payload = (
            "__ctb_verify_payload() { "
            f"[ -s {manifest} ] && [ -s {agent} ] && [ -s {cli} ] "
            f"&& [ -s {checksums} ] || return 1; "
            "__ctb_hash_tool=$(command -v sha256sum || command -v shasum || true); "
            '[ -n "$__ctb_hash_tool" ] || return 1; '
            'case "$__ctb_hash_tool" in */shasum) '
            f"(cd {root} && \"$__ctb_hash_tool\" -a 256 -c {checksums} >/dev/null 2>&1);; "
            f"*) (cd {root} && \"$__ctb_hash_tool\" -c {checksums} >/dev/null 2>&1);; esac; "
            "}; "
        )
        zero_execution = ""
        if namespace == "generated":
            zero_execution = (
                f"TESTPRISM_JACOCO_ZERO_EXECUTION=1 "
                f"TESTPRISM_JACOCO_TARGETS={shlex.quote(paths['targets_path'])} "
                f"TESTPRISM_JACOCO_REPORT={shlex.quote(paths['json_path'])} "
                f'"$__ctb_python" {shlex.quote(paths["normalizer_path"])} '
                "&& echo __TESTPRISM_COVERAGE_COLLECT__=collected "
                "|| echo __TESTPRISM_COVERAGE_COLLECT__=failed; "
            )
        else:
            zero_execution = "echo __TESTPRISM_COVERAGE_COLLECT__=failed; "
        return (
            "__ctb_java=$(command -v java || true); "
            "__ctb_python=$(command -v python3 || command -v python || true); "
            f"{verify_payload}"
            f'if [ -z "$__ctb_java" ] || [ -z "$__ctb_python" ] || [ ! -s {agent} ] || [ ! -s {cli} ] || [ ! -s {manifest} ] || [ ! -s {checksums} ]; then '
            "echo __TESTPRISM_COVERAGE_COLLECT__=unsupported; "
            "echo __TESTPRISM_COVERAGE_COLLECT_REASON__=vendored-payload-missing; "
            f"elif ! __ctb_verify_payload; then "
            "echo __TESTPRISM_COVERAGE_COLLECT__=unsupported; "
            "echo __TESTPRISM_COVERAGE_COLLECT_REASON__=vendored-payload-integrity; "
            f"elif [ -L {shlex.quote(paths['exec_path'])} ]; then "
            "echo __TESTPRISM_COVERAGE_COLLECT__=failed; "
            f"elif [ ! -e {shlex.quote(paths['exec_path'])} ]; then "
            f"{zero_execution}"
            f"elif [ ! -s {shlex.quote(paths['exec_path'])} ]; then "
            "echo __TESTPRISM_COVERAGE_COLLECT__=failed; "
            "else "
            f"{_class_root_discovery_command(declaration_path=paths['class_roots_path'])}"
            "set --; while IFS= read -r __ctb_dir; do "
            '[ -n "$__ctb_dir" ] && set -- "$@" --classfiles "$__ctb_dir"; '
            "done <<__TESTPRISM_CLASSES__\n$__ctb_classes\n__TESTPRISM_CLASSES__\n"
            f"if [ \"$#\" -eq 0 ]; then echo __TESTPRISM_COVERAGE_COLLECT__=failed; "
            "else "
            f"if env -u JAVA_TOOL_OPTIONS \"$__ctb_java\" -jar "
            f"{shlex.quote(paths['cli_path'])} report {shlex.quote(paths['exec_path'])} "
            '"$@" --sourcefiles "$PWD" --xml '
            f"{shlex.quote(paths['xml_path'])} "
            f"&& TESTPRISM_JACOCO_XML={shlex.quote(paths['xml_path'])} "
            f"TESTPRISM_JACOCO_TARGETS={shlex.quote(paths['targets_path'])} "
            f"TESTPRISM_JACOCO_REPORT={shlex.quote(paths['json_path'])} "
            f"\"$__ctb_python\" {shlex.quote(paths['normalizer_path'])}; then "
            "echo __TESTPRISM_COVERAGE_COLLECT__=collected; "
            "else echo __TESTPRISM_COVERAGE_COLLECT__=failed; fi; fi; fi"
        )

    def normalized_report_path(self, namespace: str = "generated") -> str:
        return java_coverage_namespace(namespace)["json_path"]


JACOCO_COVERAGE_BACKEND = JavaCoverageBackend()
JAVA_COVERAGE_BACKEND = JACOCO_COVERAGE_BACKEND
JaCoCoCoverageBackend = JavaCoverageBackend
JaCoCoCoverageRuntimeFiles = JavaCoverageRuntimeFiles
jacoco_coverage_namespace = java_coverage_namespace
write_jacoco_runtime_files = write_java_runtime_files


__all__ = [
    "JAVA_COVERAGE_BACKEND",
    "JAVA_SOURCE_SUFFIXES",
    "JACOCO_COVERAGE_BACKEND",
    "JACOCO_COVERAGE_ROOT",
    "JACOCO_DISCOVERY_ROOTS",
    "JACOCO_VENDOR_ARTIFACTS",
    "JACOCO_VENDOR_LICENSE",
    "JACOCO_VENDOR_MANIFEST",
    "JACOCO_VENDOR_MANIFEST_SHA256",
    "JACOCO_VENDOR_NOTICE",
    "JACOCO_VENDOR_ROOT",
    "JACOCO_NORMALIZER_SOURCE",
    "JACOCO_SOURCE_SUFFIXES",
    "JaCoCoCoverageBackend",
    "JaCoCoCoverageRuntimeFiles",
    "JavaCoverageBackend",
    "JavaCoverageRuntimeFiles",
    "jacoco_coverage_namespace",
    "java_coverage_namespace",
    "normalize_jacoco_coverage",
    "normalize_jacoco_report",
    "normalize_jacoco_xml",
    "normalise_jacoco_xml",
    "write_jacoco_runtime_files",
    "write_java_runtime_files",
]
