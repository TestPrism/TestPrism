# ruff: noqa: E501

"""Offline GCC/Clang gcov coverage backend for C and C++ targets.

The native backend intentionally relies only on compiler and gcov-compatible
tools already present in a task image.  Build flags are injected through the
runner environment, while runtime counters are redirected to a namespace owned
by one ``(state, suite)`` collection.  A small stdlib-only finalizer converts
gcov text into the common ``executed_lines``/``missing_lines`` report.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping

from testprism.harness.coverage_backends import (
    CoverageBackend,
    normalize_target_path,
)

NATIVE_SOURCE_SUFFIXES = (
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".h",
    ".hh",
    ".hpp",
    ".hxx",
)
GCOV_SOURCE_SUFFIXES = NATIVE_SOURCE_SUFFIXES
C_CPP_SOURCE_SUFFIXES = NATIVE_SOURCE_SUFFIXES
NATIVE_COVERAGE_ROOT = "/tmp/testbench/coverage"
GCOV_COVERAGE_ROOT = NATIVE_COVERAGE_ROOT
NATIVE_COVERAGE_FLAGS = "--coverage"


@dataclass(frozen=True)
class NativeCoverageRuntimeFiles:
    """Host-side payload and namespace paths for one native collection."""

    root: Path
    namespace: str
    container_root: str
    coverage_dir: str
    counters_dir: str
    raw_dir: str
    json_path: str
    report_path: str
    targets_path: str
    normalizer_path: str
    probe_dir: str

    @property
    def counter_dir(self) -> str:
        """Singular compatibility alias for the redirected ``.gcda`` root."""

        return self.counters_dir

    @property
    def gcda_dir(self) -> str:
        """Descriptive alias used by native-runtime callers."""

        return self.counters_dir

    @property
    def gcov_dir(self) -> str:
        """Alias for the raw gcov text namespace."""

        return self.raw_dir

    @property
    def gcda_path(self) -> str:
        """Alias for callers that treat the counter root as a path."""

        return self.counters_dir

    @property
    def gcov_path(self) -> str:
        """Alias for callers that treat the raw report root as a path."""

        return self.raw_dir


def _validate_namespace(namespace: str) -> str:
    if not isinstance(namespace, str) or not namespace:
        raise ValueError("native coverage namespace must be non-empty")
    if not all(character.isalnum() or character in "_-" for character in namespace):
        raise ValueError(f"invalid native coverage namespace: {namespace!r}")
    return namespace


def native_coverage_namespace(namespace: str = "generated") -> dict[str, str]:
    """Return deterministic, disjoint native paths for one suite."""

    namespace = _validate_namespace(namespace)
    root = f"{NATIVE_COVERAGE_ROOT}/{namespace}/native"
    json_path = f"{root}/coverage.json"
    return {
        "root": root,
        "coverage_dir": root,
        "counters_dir": f"{root}/counters",
        "raw_dir": f"{root}/raw",
        "json_path": json_path,
        "report_path": json_path,
        "targets_path": f"{root}/targets.json",
        "normalizer_path": f"{root}/normalize_gcov_coverage.py",
        "probe_dir": f"{root}/probe",
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
        raise ValueError("native coverage requires at least one target path")
    return workdir_path, dict(sorted(targets.items()))


def _source_line_universe(source: bytes) -> set[int]:
    """Return conservative executable-looking C/C++ source lines."""

    try:
        text = source.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("native source is not valid UTF-8") from exc

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
            if character in {'"', "'"}:
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
        if (
            content
            and not content.startswith("#")
            and not re.fullmatch(r"[{}\[\]();,]+", compact)
        ):
            result.add(line_number)
    return result


def _read_source_lines(path: Path) -> set[int]:
    try:
        return _source_line_universe(path.read_bytes())
    except FileNotFoundError:
        return set()
    except OSError as exc:
        raise ValueError(f"cannot read native target source: {path}") from exc


def _normalise_report_path(
    value: str,
    *,
    workdir: Path,
    targets: Mapping[str, Path],
) -> list[str]:
    """Map one gcov source spelling to requested in-workdir targets."""

    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("malformed gcov source path")
    rendered = value.replace("\\", "/").strip()
    candidate = Path(rendered)
    if candidate.is_absolute():
        absolute = candidate.resolve(strict=False)
        try:
            relative = normalize_target_path(absolute.relative_to(workdir).as_posix())
        except ValueError:
            # gcov routinely includes system and dependency headers.  They are
            # valid input but cannot contribute to an in-workdir report.
            return []
        return [relative] if relative in targets else []
    try:
        relative = normalize_target_path(rendered)
    except ValueError:
        return []
    if relative in targets:
        return [relative]
    suffix = "/" + relative
    matches = sorted(path for path in targets if path.endswith(suffix))
    if matches:
        return matches
    if "/" not in relative:
        basename = PurePosixPath(relative).name
        return sorted(
            path for path in targets if PurePosixPath(path).name == basename
        )
    return []


_GCOV_SOURCE_RE = re.compile(r"^\s*-\s*:\s*0:Source:(.+?)\s*$")
_GCOV_LINE_RE = re.compile(r"^\s*([^:]+):\s*(\d+):")
_GCOV_INTERMEDIATE_FILE_RE = re.compile(r"^file:(.*)$")
_GCOV_INTERMEDIATE_LINE_RE = re.compile(r"^lcount:(\d+),([^\s]+)$")


def _count_value(value: str) -> tuple[bool, bool]:
    """Return ``(is_statement, executed)`` for one gcov count token."""

    rendered = value.strip()
    if rendered in {"-", ""}:
        return False, False
    if rendered in {"#####", "=====", "#######"}:
        return True, False
    # gcov can annotate a count with ``*`` for a hot line in branch output.
    rendered = rendered.rstrip("*")
    if not rendered.isdigit():
        raise ValueError(f"malformed gcov execution count: {value!r}")
    return True, int(rendered) > 0


def _parse_gcov_text(
    text: str,
    *,
    workdir: Path,
    targets: Mapping[str, Path],
    line_universe: dict[str, set[int]],
    executed: dict[str, set[int]],
) -> None:
    current: list[str] = []
    saw_source: set[str] = set()
    for raw_line in text.splitlines():
        source_match = _GCOV_SOURCE_RE.match(raw_line)
        if source_match:
            current = _normalise_report_path(
                source_match.group(1), workdir=workdir, targets=targets
            )
            saw_source.update(current)
            continue
        intermediate_file = _GCOV_INTERMEDIATE_FILE_RE.match(raw_line)
        if intermediate_file:
            current = _normalise_report_path(
                intermediate_file.group(1), workdir=workdir, targets=targets
            )
            saw_source.update(current)
            continue
        intermediate_line = _GCOV_INTERMEDIATE_LINE_RE.match(raw_line.strip())
        if intermediate_line:
            number = int(intermediate_line.group(1))
            if number <= 0:
                raise ValueError("gcov line number must be positive")
            statement, covered = _count_value(intermediate_line.group(2))
        else:
            line_match = _GCOV_LINE_RE.match(raw_line)
            if not line_match:
                continue
            count_value, rendered_number = line_match.groups()
            number = int(rendered_number)
            statement, covered = _count_value(count_value)
            if number <= 0:
                # gcov metadata records (Source/Graph/Data/Runs) use line 0
                # and a ``-`` count; they are not source statements.
                if not statement:
                    continue
                raise ValueError("gcov line number must be positive")
        if not statement:
            continue
        for target in current:
            line_universe[target].add(number)
            if covered:
                executed[target].add(number)
    # ``saw_source`` is intentionally only diagnostic state.  A source block
    # with no statement records falls back to source-derived lines below.
    del saw_source


def _coerce_gcov_input(raw: object) -> list[str]:
    if isinstance(raw, Path):
        try:
            return [raw.read_text(encoding="utf-8")]
        except (OSError, UnicodeError) as exc:
            raise ValueError("cannot read gcov report") from exc
    if isinstance(raw, bytes):
        try:
            return [raw.decode("utf-8")]
        except UnicodeDecodeError as exc:
            raise ValueError("gcov report is not valid UTF-8") from exc
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, Mapping):
        # Accept a useful hand-authored shape for callers/tests while keeping
        # the runtime collector's canonical input as text files.
        if "files" in raw:
            return [json.dumps(raw)]
        return [json.dumps(raw)]
    if isinstance(raw, Iterable):
        values: list[str] = []
        for item in raw:
            values.extend(_coerce_gcov_input(item))
        return values
    raise ValueError("gcov report must be text, bytes, a path, or an iterable")


def _normalise_json_report(
    value: Mapping[str, object],
    *,
    workdir: Path,
    targets: Mapping[str, Path],
    line_universe: dict[str, set[int]],
    executed: dict[str, set[int]],
) -> None:
    files = value.get("files")
    if isinstance(files, list):
        for entry in files:
            if not isinstance(entry, Mapping):
                raise ValueError("malformed gcov JSON file entry")
            source_name = entry.get("file", entry.get("filename"))
            if not isinstance(source_name, str):
                raise ValueError("malformed gcov JSON file entry")
            matched = _normalise_report_path(
                source_name, workdir=workdir, targets=targets
            )
            lines = entry.get("lines")
            if not isinstance(lines, list):
                continue
            for record in lines:
                if not isinstance(record, Mapping):
                    raise ValueError("malformed gcov JSON line entry")
                number = record.get("line_number", record.get("line"))
                count = record.get("count", record.get("execution_count", 0))
                if (
                    isinstance(number, bool)
                    or not isinstance(number, int)
                    or number <= 0
                ):
                    raise ValueError("gcov JSON line number must be positive")
                if isinstance(count, bool) or not isinstance(count, (int, float)):
                    raise ValueError("gcov JSON execution count must be numeric")
                for target in matched:
                    line_universe[target].add(number)
                    if count > 0:
                        executed[target].add(number)
        return
    if isinstance(files, Mapping):
        for source_name, entry in files.items():
            if not isinstance(source_name, str) or not isinstance(entry, Mapping):
                raise ValueError("malformed gcov JSON file entry")
            matched = _normalise_report_path(
                source_name, workdir=workdir, targets=targets
            )
            lines = entry.get("lines")
            if not isinstance(lines, list):
                continue
            for record in lines:
                if not isinstance(record, Mapping):
                    raise ValueError("malformed gcov JSON line entry")
                number = record.get("line_number", record.get("line"))
                count = record.get("count", record.get("execution_count", 0))
                if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
                    raise ValueError("gcov JSON line number must be positive")
                if isinstance(count, bool) or not isinstance(count, (int, float)):
                    raise ValueError("gcov JSON execution count must be numeric")
                for target in matched:
                    line_universe[target].add(number)
                    if count > 0:
                        executed[target].add(number)
        return
    raise ValueError("malformed gcov JSON report")


def normalize_gcov_reports(
    reports: object,
    *,
    workdir: str | Path,
    target_paths: Iterable[str | Path],
) -> dict[str, object]:
    """Normalize gcov text/JSON into the common line report contract."""

    workdir_path, targets = _target_map(workdir, target_paths)
    source_lines = {
        relative: _read_source_lines(absolute) for relative, absolute in targets.items()
    }
    line_universe = {relative: set() for relative in targets}
    executed = {relative: set() for relative in targets}
    for raw in _coerce_gcov_input(reports):
        stripped = raw.lstrip()
        if stripped.startswith("{"):
            try:
                value = json.loads(raw)
            except (json.JSONDecodeError, UnicodeError) as exc:
                raise ValueError("malformed gcov JSON report") from exc
            if not isinstance(value, Mapping):
                raise ValueError("malformed gcov JSON report")
            _normalise_json_report(
                value,
                workdir=workdir_path,
                targets=targets,
                line_universe=line_universe,
                executed=executed,
            )
        else:
            _parse_gcov_text(
                raw,
                workdir=workdir_path,
                targets=targets,
                line_universe=line_universe,
                executed=executed,
            )

    files: dict[str, dict[str, list[int]]] = {}
    for relative in sorted(targets):
        universe = line_universe[relative] or source_lines[relative]
        covered = sorted(executed[relative] & universe)
        missing = sorted(universe - set(covered))
        files[relative] = {
            "executed_lines": covered,
            "missing_lines": missing,
        }
    return {"files": files}


normalize_gcov_report = normalize_gcov_reports
normalize_gcov_coverage = normalize_gcov_reports
normalize_gcov_text = normalize_gcov_reports
normalise_gcov_reports = normalize_gcov_reports
normalise_gcov_report = normalize_gcov_reports


NATIVE_NORMALIZER_SOURCE = r'''#!/usr/bin/env python3
"""Container-side dependency-free gcov text normalizer."""
import json
import os
import re
from pathlib import Path, PurePosixPath


def fail(message):
    raise ValueError(message)


def normalize(value):
    value = value.replace("\\", "/").strip()
    if not value or "\x00" in value:
        fail("malformed gcov source path")
    return value


def source_lines(source):
    text = source.decode("utf-8", errors="strict")
    result = set()
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
            if char in ('"', "'"):
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
        if content and not content.startswith("#") and not re.fullmatch(r"[{}\[\]();,]+", compact):
            result.add(number)
    return result


def targets_for(value, workdir, targets):
    value = normalize(value)
    if value.startswith("/"):
        absolute = Path(value).resolve()
        try:
            relative = absolute.relative_to(workdir).as_posix()
        except ValueError:
            return []
    else:
        relative = value.lstrip("./")
    if relative in targets:
        return [relative]
    suffix = "/" + relative
    matches = sorted(path for path in targets if path.endswith(suffix))
    if matches:
        return matches
    if "/" not in relative:
        name = PurePosixPath(relative).name
        return sorted(path for path in targets if PurePosixPath(path).name == name)
    return []


def count(value):
    value = value.strip()
    if value in ("", "-"):
        return False, False
    if value in ("#####", "=====", "#######"):
        return True, False
    value = value.rstrip("*")
    if not value.isdigit():
        fail("malformed gcov execution count")
    return True, int(value) > 0


def parse(text, workdir, targets, universes, executed):
    current = []
    source_re = re.compile(r"^\s*-\s*:\s*0:Source:(.+?)\s*$")
    line_re = re.compile(r"^\s*([^:]+):\s*(\d+):")
    file_re = re.compile(r"^file:(.*)$")
    lcount_re = re.compile(r"^lcount:(\d+),([^\s]+)$")
    for raw in text.splitlines():
        match = source_re.match(raw)
        if match:
            current = targets_for(match.group(1), workdir, targets)
            continue
        match = file_re.match(raw)
        if match:
            current = targets_for(match.group(1), workdir, targets)
            continue
        match = lcount_re.match(raw.strip())
        if match:
            number = int(match.group(1))
            statement, covered = count(match.group(2))
        else:
            match = line_re.match(raw)
            if not match:
                continue
            statement, covered = count(match.group(1))
            number = int(match.group(2))
        if number <= 0:
            if not statement:
                continue
            fail("gcov line number must be positive")
        if not statement:
            continue
        for target in current:
            universes[target].add(number)
            if covered:
                executed[target].add(number)


def main():
    manifest = json.loads(Path(os.environ["TESTPRISM_NATIVE_TARGETS"]).read_text())
    if not isinstance(manifest, dict) or not isinstance(manifest.get("workdir"), str) or not isinstance(manifest.get("targets"), list):
        fail("malformed native target manifest")
    workdir = Path(manifest["workdir"]).resolve()
    targets = {}
    for entry in manifest["targets"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("relative"), str) or not isinstance(entry.get("absolute"), str):
            fail("malformed native target manifest entry")
        absolute = Path(entry["absolute"]).resolve()
        try:
            absolute.relative_to(workdir)
        except ValueError:
            fail("coverage target escapes workdir")
        targets[entry["relative"]] = absolute
    if not targets:
        fail("native coverage requires at least one target path")
    universes = {relative: set() for relative in targets}
    executed = {relative: set() for relative in targets}
    for raw_path in sorted(Path(os.environ["TESTPRISM_NATIVE_RAW"]).glob("*.gcov")):
        parse(raw_path.read_text(), workdir, targets, universes, executed)
    for relative, absolute in targets.items():
        if not universes[relative]:
            try:
                universes[relative] = source_lines(absolute.read_bytes())
            except FileNotFoundError:
                universes[relative] = set()
    files = {}
    for relative in sorted(targets):
        covered = sorted(executed[relative] & universes[relative])
        files[relative] = {"executed_lines": covered, "missing_lines": sorted(universes[relative] - set(covered))}
    Path(os.environ["TESTPRISM_NATIVE_REPORT"]).write_text(json.dumps({"files": files}))


try:
    main()
except (OSError, UnicodeError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
    print("native gcov normalization failed: %s" % error)
    raise SystemExit(1)
'''


def write_native_runtime_files(
    root: Path,
    *,
    workdir: str,
    target_paths: list[str],
    namespace: str = "generated",
) -> NativeCoverageRuntimeFiles:
    """Create target manifest and the dependency-free gcov finalizer."""

    paths = native_coverage_namespace(namespace)
    workdir_path, targets = _target_map(workdir, target_paths)
    root.mkdir(parents=True, exist_ok=True)
    (root / "normalize_gcov_coverage.py").write_text(
        NATIVE_NORMALIZER_SOURCE, encoding="utf-8"
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
    return NativeCoverageRuntimeFiles(
        root=root,
        namespace=_validate_namespace(namespace),
        container_root=paths["root"],
        coverage_dir=paths["coverage_dir"],
        counters_dir=paths["counters_dir"],
        raw_dir=paths["raw_dir"],
        json_path=paths["json_path"],
        report_path=paths["report_path"],
        targets_path=paths["targets_path"],
        normalizer_path=paths["normalizer_path"],
        probe_dir=paths["probe_dir"],
    )


def _native_flag_value(key: str) -> str:
    return f"${{{key}:+${{{key}}} }}{NATIVE_COVERAGE_FLAGS}"


class NativeCoverageBackend(CoverageBackend):
    """GCC/Clang gcov-compatible collector for C and C++ sources."""

    language = "native"
    source_suffixes = NATIVE_SOURCE_SUFFIXES

    def write_runtime_files(
        self,
        root: Path,
        *,
        workdir: str,
        target_paths: list[str],
        namespace: str = "generated",
    ) -> NativeCoverageRuntimeFiles:
        return write_native_runtime_files(
            root,
            workdir=workdir,
            target_paths=target_paths,
            namespace=namespace,
        )

    def setup_command(self, namespace: str = "generated") -> str:
        paths = native_coverage_namespace(namespace)
        root = shlex.quote(paths["root"])
        counters = shlex.quote(paths["counters_dir"])
        probe = shlex.quote(paths["probe_dir"])
        gcov_path = shlex.quote(f"{paths['root']}/gcov.path")
        probe_source = f"{paths['probe_dir']}/probe.c"
        probe_binary = f"{paths['probe_dir']}/probe"
        probe_counters = f"{paths['probe_dir']}/counters"
        return (
            f"mkdir -p {root}; "
            f"rm -rf {counters} {probe}; : > {gcov_path}; mkdir -p {probe} {shlex.quote(probe_counters)}; "
            "__ctb_cc=$(command -v gcc || command -v clang || command -v cc || true); "
            "__ctb_gcov=$(command -v gcov || true); "
            "__ctb_cc_name=${__ctb_cc##*/}; "
            "case \"$__ctb_cc_name\" in gcc*|cc) "
            "__ctb_cc_major=$(\"$__ctb_cc\" -dumpversion 2>/dev/null | cut -d. -f1 || true); "
            "if [ -n \"$__ctb_cc_major\" ] && command -v \"gcov-$__ctb_cc_major\" >/dev/null 2>&1; then "
            "__ctb_gcov=$(command -v \"gcov-$__ctb_cc_major\"); fi ;; "
            "clang*) __ctb_gcov= ;; esac; "
            "__ctb_llvm_cov=$(command -v llvm-cov || true); "
            "__ctb_py=$(command -v python3 || command -v python || true); "
            "__ctb_gcov_ready=0; "
            "if [ -n \"$__ctb_gcov\" ] || "
            "( [ -n \"$__ctb_llvm_cov\" ] && \"$__ctb_llvm_cov\" gcov --help >/dev/null 2>&1 ); then "
            "__ctb_gcov_ready=1; fi; "
            "if [ -z \"$__ctb_cc\" ] || [ -z \"$__ctb_py\" ] || "
            "[ \"$__ctb_gcov_ready\" -ne 1 ]; then "
            "echo __TESTPRISM_COVERAGE_SETUP__=unsupported; "
            f"elif printf '%s\\n' 'int main(void){{return 0;}}' > {shlex.quote(probe_source)} "
            f"&& \"$__ctb_cc\" --coverage -O0 {shlex.quote(probe_source)} -o {shlex.quote(probe_binary)} "
            f"&& GCOV_PREFIX={shlex.quote(probe_counters)} GCOV_PREFIX_STRIP=0 {shlex.quote(probe_binary)} >/dev/null 2>&1; then "
            f"if find {shlex.quote(probe_counters)} -type f -name '*.gcda' -print -quit 2>/dev/null | grep -q .; then "
            f"printf '%s\\n' \"$__ctb_gcov\" > {gcov_path}; "
            "echo __TESTPRISM_COVERAGE_SETUP__=ready; "
            "else echo __TESTPRISM_COVERAGE_SETUP__=unsupported; fi; "
            "else echo __TESTPRISM_COVERAGE_SETUP__=unsupported; fi"
        )

    def environment(
        self, workdir: str, namespace: str = "generated"
    ) -> dict[str, str]:
        del workdir
        paths = native_coverage_namespace(namespace)
        return {
            "CFLAGS": _native_flag_value("CFLAGS"),
            "CXXFLAGS": _native_flag_value("CXXFLAGS"),
            "LDFLAGS": _native_flag_value("LDFLAGS"),
            "GCOV_PREFIX": paths["counters_dir"],
            "GCOV_PREFIX_STRIP": "0",
        }

    def collect_command(self, namespace: str = "generated") -> str:
        paths = native_coverage_namespace(namespace)
        root = shlex.quote(paths["root"])
        counters = shlex.quote(paths["counters_dir"])
        raw = shlex.quote(paths["raw_dir"])
        targets = shlex.quote(paths["targets_path"])
        report = shlex.quote(paths["json_path"])
        normalizer = shlex.quote(paths["normalizer_path"])
        gcov_path = shlex.quote(f"{paths['root']}/gcov.path")
        # Copy each redirected counter back to its original object directory
        # only for the duration of gcov.  Existing task counters are backed up
        # and restored; source, task tests, and agent tests are never removed.
        copy_and_run = (
            "__ctb_rel=${__ctb_gcda#\"$__ctb_counters/\"}; "
            "__ctb_orig=\"/${__ctb_rel#/}\"; "
            "case \"$__ctb_orig\" in \"$PWD\"/*) ;; *) continue ;; esac; "
            "__ctb_backup=\"$__ctb_root/backups/${__ctb_rel#/}\"; "
            "mkdir -p \"$(dirname \"$__ctb_backup\")\"; "
            "__ctb_had=0; if [ -f \"$__ctb_orig\" ]; then cp -p \"$__ctb_orig\" \"$__ctb_backup\"; __ctb_had=1; fi; "
            "mkdir -p \"$(dirname \"$__ctb_orig\")\"; cp \"$__ctb_gcda\" \"$__ctb_orig\"; "
            "if [ -n \"$__ctb_gcov\" ]; then \"$__ctb_gcov\" -t -o \"$__ctb_orig\" \"$PWD\"; "
            "else \"$__ctb_llvm_cov\" gcov -t -o \"$__ctb_orig\" \"$PWD\"; fi >> \"$__ctb_raw/gcov-stdout.gcov\" 2>> \"$__ctb_raw/gcov-stderr.log\"; "
            "__ctb_status=$?; if [ \"$__ctb_had\" = 1 ]; then mv \"$__ctb_backup\" \"$__ctb_orig\"; else rm -f \"$__ctb_orig\"; fi; "
            "[ \"$__ctb_status\" -eq 0 ] || __ctb_failed=1; "
        )
        return (
            "__ctb_gcov=$(command -v gcov || true); "
            "__ctb_llvm_cov=$(command -v llvm-cov || true); "
            "__ctb_python=$(command -v python3 || command -v python || true); "
            f"__ctb_root={root}; __ctb_counters={counters}; __ctb_raw={raw}; "
            f"if [ -e {gcov_path} ]; then __ctb_gcov=$(cat {gcov_path}); fi; "
            f"mkdir -p \"$__ctb_raw\"; rm -f \"$__ctb_raw\"/*.gcov \"$__ctb_raw\"/*.log {report}; "
            'if [ -z "$__ctb_python" ] || ( [ -z "$__ctb_gcov" ] && [ -z "$__ctb_llvm_cov" ] ); then '
            "echo __TESTPRISM_COVERAGE_COLLECT__=unsupported; "
            'elif ! find "$__ctb_counters" -type f -name \'*.gcda\' -print -quit 2>/dev/null | grep -q .; then '
            "echo __TESTPRISM_COVERAGE_COLLECT__=unsupported; "
            "else __ctb_seen=0; __ctb_failed=0; "
            'while IFS= read -r __ctb_gcda; do [ -n "$__ctb_gcda" ] || continue; __ctb_seen=1; '
            + copy_and_run
            + "done <<__TESTPRISM_NATIVE_GCDA__\n"
            + '$(find "$__ctb_counters" -type f -name \'*.gcda\' -print 2>/dev/null)\n'
            + "__TESTPRISM_NATIVE_GCDA__\n"
            + "if [ \"$__ctb_seen\" -eq 0 ]; then echo __TESTPRISM_COVERAGE_COLLECT__=unsupported; "
            + "elif [ \"$__ctb_failed\" -ne 0 ] && [ ! -s \"$__ctb_raw/gcov-stdout.gcov\" ]; then echo __TESTPRISM_COVERAGE_COLLECT__=failed; "
            + f"elif TESTPRISM_NATIVE_TARGETS={targets} TESTPRISM_NATIVE_RAW=\"$__ctb_raw\" TESTPRISM_NATIVE_REPORT={report} \"$__ctb_python\" {normalizer}; then echo __TESTPRISM_COVERAGE_COLLECT__=collected; "
            + "else echo __TESTPRISM_COVERAGE_COLLECT__=failed; fi; fi"
        )

    def normalized_report_path(self, namespace: str = "generated") -> str:
        return native_coverage_namespace(namespace)["json_path"]


NATIVE_COVERAGE_BACKEND = NativeCoverageBackend()
GCOV_COVERAGE_BACKEND = NATIVE_COVERAGE_BACKEND
CPP_COVERAGE_BACKEND = NATIVE_COVERAGE_BACKEND
C_CPP_COVERAGE_BACKEND = NATIVE_COVERAGE_BACKEND


__all__ = [
    "CPP_COVERAGE_BACKEND",
    "C_CPP_COVERAGE_BACKEND",
    "C_CPP_SOURCE_SUFFIXES",
    "GCOV_COVERAGE_BACKEND",
    "GCOV_SOURCE_SUFFIXES",
    "NATIVE_COVERAGE_BACKEND",
    "NATIVE_COVERAGE_FLAGS",
    "NATIVE_COVERAGE_ROOT",
    "NATIVE_NORMALIZER_SOURCE",
    "NATIVE_SOURCE_SUFFIXES",
    "NativeCoverageBackend",
    "NativeCoverageRuntimeFiles",
    "native_coverage_namespace",
    "normalise_gcov_report",
    "normalise_gcov_reports",
    "normalize_gcov_coverage",
    "normalize_gcov_report",
    "normalize_gcov_reports",
    "normalize_gcov_text",
    "write_native_runtime_files",
]
