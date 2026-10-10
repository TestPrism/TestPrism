# ruff: noqa: E501

"""Offline Go coverage backend for Go source targets.

The backend keeps all instrumentation in harness-owned files. A small wrapper
scopes ``GOFLAGS`` to ``go test`` only so helper subcommands such as
``go list`` remain untouched, while ``GOCOVERDIR`` and ``go tool covdata
textfmt`` provide the raw coverage profile consumed by a stdlib-only
normalizer.
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

GO_SOURCE_SUFFIXES = (".go",)
GO_COVERAGE_ROOT = "/tmp/testbench/coverage"
GO_COVERAGE_FLAGS = "-cover -covermode=set"


@dataclass(frozen=True)
class GoCoverageRuntimeFiles:
    """Host-side payload and namespace paths for one Go collection."""

    root: Path
    namespace: str
    container_root: str
    coverage_dir: str
    covdata_dir: str
    profile_path: str
    json_path: str
    report_path: str
    targets_path: str
    packages_path: str
    normalizer_path: str
    wrapper_dir: str
    wrapper_path: str
    real_go_path: str


def _validate_namespace(namespace: str) -> str:
    if not isinstance(namespace, str) or not namespace:
        raise ValueError("Go coverage namespace must be non-empty")
    if not all(character.isalnum() or character in "_-" for character in namespace):
        raise ValueError(f"invalid Go coverage namespace: {namespace!r}")
    return namespace


def go_coverage_namespace(namespace: str = "generated") -> dict[str, str]:
    """Return deterministic, disjoint Go paths for one suite."""

    namespace = _validate_namespace(namespace)
    root = f"{GO_COVERAGE_ROOT}/{namespace}/go"
    json_path = f"{root}/coverage.json"
    return {
        "root": root,
        "coverage_dir": root,
        "covdata_dir": f"{root}/covdata",
        "profile_path": f"{root}/coverage.out",
        "json_path": json_path,
        "report_path": json_path,
        "targets_path": f"{root}/targets.json",
        "packages_path": f"{root}/packages.txt",
        "normalizer_path": f"{root}/normalize_go_coverage.go",
        "wrapper_dir": f"{root}/bin",
        "wrapper_path": f"{root}/bin/go",
        "real_go_path": f"{root}/real-go.path",
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
        raise ValueError("Go coverage requires at least one target path")
    return workdir_path, dict(sorted(targets.items()))


def _coverage_package_patterns(targets: Mapping[str, Path]) -> tuple[str, ...]:
    patterns = {
        "."
        if PurePosixPath(relative).parent.as_posix() in {"", "."}
        else f"./{PurePosixPath(relative).parent.as_posix()}"
        for relative in targets
    }
    return tuple(sorted(patterns))


def _source_line_universe(source: bytes) -> set[int]:
    """Return conservative executable-looking Go source lines."""

    try:
        text = source.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("Go source is not valid UTF-8") from exc

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
                visible.append(" ")
                if quote == "`":
                    if character == "`":
                        quote = None
                elif escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == quote:
                    quote = None
                index += 1
                continue
            if line.startswith("//", index):
                break
            if line.startswith("/*", index):
                in_block_comment = True
                index += 2
                continue
            if character in {'"', "'", "`"}:
                quote = character
                visible.append(" ")
                index += 1
                continue
            visible.append(character)
            index += 1
        content = "".join(visible).strip()
        compact = re.sub(r"\s+", "", content)
        if not content:
            continue
        if re.match(r"^(package|import)\b", content):
            continue
        if re.fullmatch(r"[{}\[\]();,]+", compact):
            continue
        result.add(line_number)
    return result


def _read_source_lines(path: Path) -> set[int]:
    try:
        return _source_line_universe(path.read_bytes())
    except FileNotFoundError:
        return set()
    except OSError as exc:
        raise ValueError(f"cannot read Go target source: {path}") from exc


def _normalise_report_path(
    value: str,
    *,
    workdir: Path,
    targets: Mapping[str, Path],
) -> list[str]:
    """Map one Go coverage path spelling to one requested target."""

    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("malformed Go coverage file path")
    rendered = value.replace("\\", "/").strip()
    if not rendered:
        raise ValueError("malformed Go coverage file path")
    candidate = Path(rendered)
    if candidate.is_absolute() or (
        len(rendered) >= 2 and rendered[1] == ":"
    ):
        if not candidate.is_absolute():
            raise ValueError(f"Go coverage file path escapes workdir: {value!r}")
        absolute = candidate.resolve(strict=False)
        try:
            relative = normalize_target_path(absolute.relative_to(workdir).as_posix())
        except ValueError as exc:
            raise ValueError(
                f"Go coverage file path escapes workdir: {value!r}"
            ) from exc
        return [relative] if relative in targets else []
    relative = normalize_target_path(rendered)
    if relative in targets:
        return [relative]
    target_suffix_matches = sorted(
        path for path in targets if relative.endswith("/" + path)
    )
    if len(target_suffix_matches) == 1:
        return target_suffix_matches
    if len(target_suffix_matches) > 1:
        return []
    suffix_matches = sorted(
        path for path in targets if path.endswith("/" + relative)
    )
    if len(suffix_matches) == 1:
        return suffix_matches
    if len(suffix_matches) > 1:
        return []
    if "/" not in relative:
        basename = PurePosixPath(relative).name
        basename_matches = sorted(
            path for path in targets if PurePosixPath(path).name == basename
        )
        if len(basename_matches) == 1:
            return basename_matches
    return []


def _profile_block_lines(
    start_line: int,
    start_col: int,
    end_line: int,
    end_col: int,
) -> set[int]:
    if (
        isinstance(start_line, bool)
        or isinstance(start_col, bool)
        or isinstance(end_line, bool)
        or isinstance(end_col, bool)
        or not isinstance(start_line, int)
        or not isinstance(start_col, int)
        or not isinstance(end_line, int)
        or not isinstance(end_col, int)
    ):
        raise ValueError("Go coverage block positions must be integers")
    if (
        start_line <= 0
        or start_col <= 0
        or end_line <= 0
        or end_col <= 0
        or end_line < start_line
        or (end_line == start_line and end_col <= start_col)
    ):
        raise ValueError("Go coverage block range is invalid")
    last_line = end_line if end_col > 1 else end_line - 1
    if last_line < start_line:
        raise ValueError("Go coverage block range is invalid")
    return set(range(start_line, last_line + 1))


_GO_MODE_RE = re.compile(r"^mode:\s*([A-Za-z0-9_]+)\s*$")
_GO_PROFILE_LINE_RE = re.compile(
    r"^(?P<path>.+):(?P<start_line>\d+)\.(?P<start_col>\d+),"
    r"(?P<end_line>\d+)\.(?P<end_col>\d+)\s+"
    r"(?P<num_stmt>\d+)\s+(?P<count>\d+)\s*$"
)


def _parse_go_profile_text(
    text: str,
    *,
    workdir: Path,
    targets: Mapping[str, Path],
    line_universe: dict[str, set[int]],
    executed: dict[str, set[int]],
    expected_mode: str | None,
) -> str:
    mode: str | None = None
    for raw_line in text.splitlines():
        rendered = raw_line.strip()
        if mode is None:
            if not rendered:
                continue
            mode_match = _GO_MODE_RE.match(rendered)
            if mode_match is None:
                raise ValueError("Go coverage profile is missing mode header")
            mode = mode_match.group(1)
            if expected_mode is not None and mode != expected_mode:
                raise ValueError("Go coverage profile mode mismatch")
            continue
        if not rendered:
            continue
        line_match = _GO_PROFILE_LINE_RE.match(raw_line)
        if line_match is None:
            raise ValueError("malformed Go coverage profile line")
        num_stmt = int(line_match.group("num_stmt"))
        if num_stmt <= 0:
            raise ValueError("Go coverage block statement count must be positive")
        block_lines = _profile_block_lines(
            int(line_match.group("start_line")),
            int(line_match.group("start_col")),
            int(line_match.group("end_line")),
            int(line_match.group("end_col")),
        )
        matched = _normalise_report_path(
            line_match.group("path"),
            workdir=workdir,
            targets=targets,
        )
        if not matched:
            continue
        count = int(line_match.group("count"))
        for target in matched:
            line_universe[target].update(block_lines)
            if count > 0:
                executed[target].update(block_lines)
    if mode is None:
        raise ValueError("Go coverage profile is missing mode header")
    return mode


def _coerce_go_profile_input(raw: object) -> list[str]:
    if isinstance(raw, Path):
        try:
            return [raw.read_text(encoding="utf-8")]
        except (OSError, UnicodeError) as exc:
            raise ValueError("cannot read Go coverage profile") from exc
    if isinstance(raw, bytes):
        try:
            return [raw.decode("utf-8")]
        except UnicodeDecodeError as exc:
            raise ValueError("Go coverage profile is not valid UTF-8") from exc
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, Mapping):
        raise ValueError("Go coverage profile must be text, bytes, a path, or an iterable")
    if isinstance(raw, Iterable):
        values: list[str] = []
        for item in raw:
            values.extend(_coerce_go_profile_input(item))
        return values
    raise ValueError("Go coverage profile must be text, bytes, a path, or an iterable")


def normalize_go_coverage_profiles(
    profile: object,
    *,
    workdir: str | Path,
    target_paths: Iterable[str | Path],
) -> dict[str, object]:
    """Normalize one or more Go text profiles to the common line contract."""

    workdir_path, targets = _target_map(workdir, target_paths)
    source_lines = {
        relative: _read_source_lines(absolute) for relative, absolute in targets.items()
    }
    line_universe = {relative: set() for relative in targets}
    executed = {relative: set() for relative in targets}
    expected_mode: str | None = None
    for raw in _coerce_go_profile_input(profile):
        expected_mode = _parse_go_profile_text(
            raw,
            workdir=workdir_path,
            targets=targets,
            line_universe=line_universe,
            executed=executed,
            expected_mode=expected_mode,
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


normalize_go_coverage_profile = normalize_go_coverage_profiles
normalize_go_coverage = normalize_go_coverage_profiles
normalise_go_coverage = normalize_go_coverage_profiles
normalise_go_coverage_profile = normalize_go_coverage_profiles


GO_WRAPPER_SOURCE = """#!/bin/sh
set -eu

__ctb_bin=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
__ctb_root=$(CDPATH= cd -- "$__ctb_bin/.." && pwd -P)
__ctb_real_file="$__ctb_root/real-go.path"

if [ ! -f "$__ctb_real_file" ]; then
    printf '%s\\n' 'testprism go wrapper missing real-go.path' >&2
    exit 127
fi

__ctb_go=$(cat "$__ctb_real_file")
if [ -z "$__ctb_go" ] || [ ! -x "$__ctb_go" ]; then
    printf '%s\\n' 'testprism go wrapper has invalid real go tool' >&2
    exit 127
fi

if [ "$#" -eq 0 ]; then
    exec "$__ctb_go"
fi

__ctb_command=$1
shift

if [ "$__ctb_command" = "test" ]; then
    __ctb_coverpkg=
    if [ -f "$__ctb_root/packages.txt" ]; then
        while IFS= read -r __ctb_package; do
            [ -n "$__ctb_package" ] || continue
            if [ -n "$__ctb_coverpkg" ]; then
                __ctb_coverpkg="$__ctb_coverpkg,$__ctb_package"
            else
                __ctb_coverpkg="$__ctb_package"
            fi
        done < "$__ctb_root/packages.txt"
    fi

    __ctb_skip=0
    case " ${GOFLAGS-} " in
        *" -cover "*|*" -cover="*|*" -covermode="*|*" -coverpkg="*|*" -coverprofile="*)
            __ctb_skip=1
            ;;
    esac
    for __ctb_arg in "$@"; do
        case "$__ctb_arg" in
            -cover|-cover=*|-covermode=*|-coverpkg=*|-coverprofile=*)
                __ctb_skip=1
                ;;
        esac
    done

    if [ "$__ctb_skip" -eq 0 ] && [ -n "$__ctb_coverpkg" ]; then
        __ctb_flags='-cover -covermode=set'
        if [ -n "${GOFLAGS-}" ]; then
            GOFLAGS="${GOFLAGS} ${__ctb_flags} -coverpkg=${__ctb_coverpkg}" exec "$__ctb_go" test "$@"
        fi
        GOFLAGS="${__ctb_flags} -coverpkg=${__ctb_coverpkg}" exec "$__ctb_go" test "$@"
    fi
    exec "$__ctb_go" test "$@"
fi

exec "$__ctb_go" "$__ctb_command" "$@"
"""


GO_NORMALIZER_SOURCE = r'''package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
)

type targetEntry struct {
	Relative string `json:"relative"`
	Absolute string `json:"absolute"`
}

type targetManifest struct {
	Workdir string        `json:"workdir"`
	Targets []targetEntry `json:"targets"`
}

type lineReport struct {
	ExecutedLines []int `json:"executed_lines"`
	MissingLines  []int `json:"missing_lines"`
}

type coverageReport struct {
	Files map[string]lineReport `json:"files"`
}

var (
	modeRE        = regexp.MustCompile(`^mode:\s*([A-Za-z0-9_]+)\s*$`)
	profileLineRE = regexp.MustCompile(`^(.+):([0-9]+)\.([0-9]+),([0-9]+)\.([0-9]+)\s+([0-9]+)\s+([0-9]+)\s*$`)
	wordSpaceRE   = regexp.MustCompile(`\s+`)
)

func fail(message string) error {
	return fmt.Errorf(message)
}

func cleanTargetPath(value string) (string, error) {
	rendered := strings.TrimSpace(strings.ReplaceAll(value, "\\", "/"))
	if rendered == "" || strings.ContainsRune(rendered, '\x00') {
		return "", fail("malformed Go coverage file path")
	}
	for strings.HasPrefix(rendered, "./") {
		rendered = rendered[2:]
	}
	if strings.HasPrefix(rendered, "a/") || strings.HasPrefix(rendered, "b/") {
		rendered = rendered[2:]
	}
	if rendered == "" || rendered == "." || strings.HasPrefix(rendered, "/") {
		return "", fail("Go coverage file path escapes workdir")
	}
	if len(rendered) >= 2 && rendered[1] == ':' {
		return "", fail("Go coverage file path escapes workdir")
	}
	parts := strings.Split(rendered, "/")
	for _, part := range parts {
		if part == ".." {
			return "", fail("Go coverage file path escapes workdir")
		}
	}
	normalized := path.Clean(rendered)
	if normalized == "" || normalized == "." || normalized == ".." || strings.HasPrefix(normalized, "../") {
		return "", fail("Go coverage file path escapes workdir")
	}
	return normalized, nil
}

func isWithin(root string, candidate string) bool {
	relative, err := filepath.Rel(root, candidate)
	if err != nil {
		return false
	}
	relative = filepath.ToSlash(relative)
	return relative == "." || (relative != ".." && !strings.HasPrefix(relative, "../"))
}

func normalizeProfilePath(value string, workdir string, targets map[string]string) ([]string, error) {
	if value == "" || strings.ContainsRune(value, '\x00') {
		return nil, fail("malformed Go coverage file path")
	}
	rendered := strings.TrimSpace(strings.ReplaceAll(value, "\\", "/"))
	if rendered == "" {
		return nil, fail("malformed Go coverage file path")
	}
	if strings.HasPrefix(rendered, "/") || (len(rendered) >= 2 && rendered[1] == ':') {
		if !filepath.IsAbs(rendered) {
			return nil, fail("Go coverage file path escapes workdir")
		}
		absolute := filepath.Clean(rendered)
		if !isWithin(workdir, absolute) {
			return nil, fail("Go coverage file path escapes workdir")
		}
		relative, err := filepath.Rel(workdir, absolute)
		if err != nil {
			return nil, fail("Go coverage file path escapes workdir")
		}
		normalized, err := cleanTargetPath(filepath.ToSlash(relative))
		if err != nil {
			return nil, err
		}
		if _, ok := targets[normalized]; ok {
			return []string{normalized}, nil
		}
		return nil, nil
	}
	normalized, err := cleanTargetPath(rendered)
	if err != nil {
		return nil, err
	}
	if _, ok := targets[normalized]; ok {
		return []string{normalized}, nil
	}
	targetSuffixMatches := make([]string, 0, 1)
	for target := range targets {
		if strings.HasSuffix(normalized, "/"+target) {
			targetSuffixMatches = append(targetSuffixMatches, target)
		}
	}
	sort.Strings(targetSuffixMatches)
	if len(targetSuffixMatches) == 1 {
		return targetSuffixMatches, nil
	}
	if len(targetSuffixMatches) > 1 {
		return nil, nil
	}
	suffixMatches := make([]string, 0, 1)
	for target := range targets {
		if strings.HasSuffix(target, "/"+normalized) {
			suffixMatches = append(suffixMatches, target)
		}
	}
	sort.Strings(suffixMatches)
	if len(suffixMatches) == 1 {
		return suffixMatches, nil
	}
	if len(suffixMatches) > 1 {
		return nil, nil
	}
	if !strings.Contains(normalized, "/") {
		basename := path.Base(normalized)
		basenameMatches := make([]string, 0, 1)
		for target := range targets {
			if path.Base(target) == basename {
				basenameMatches = append(basenameMatches, target)
			}
		}
		sort.Strings(basenameMatches)
		if len(basenameMatches) == 1 {
			return basenameMatches, nil
		}
	}
	return nil, nil
}

func blockLines(startLine int, startCol int, endLine int, endCol int) ([]int, error) {
	if startLine <= 0 || startCol <= 0 || endLine <= 0 || endCol <= 0 || endLine < startLine || (endLine == startLine && endCol <= startCol) {
		return nil, fail("Go coverage block range is invalid")
	}
	lastLine := endLine
	if endCol == 1 {
		lastLine = endLine - 1
	}
	if lastLine < startLine {
		return nil, fail("Go coverage block range is invalid")
	}
	lines := make([]int, 0, lastLine-startLine+1)
	for line := startLine; line <= lastLine; line++ {
		lines = append(lines, line)
	}
	return lines, nil
}

func sourceLines(source []byte) (map[int]struct{}, error) {
	text := string(source)
	result := map[int]struct{}{}
	blockComment := false
	var quote rune
	escaped := false
	for index, line := range strings.Split(strings.ReplaceAll(text, "\r\n", "\n"), "\n") {
		visible := strings.Builder{}
		for cursor := 0; cursor < len(line); {
			character := rune(line[cursor])
			if blockComment {
				end := strings.Index(line[cursor:], "*/")
				if end < 0 {
					cursor = len(line)
					break
				}
				blockComment = false
				cursor += end + 2
				continue
			}
			if quote != 0 {
				visible.WriteByte(' ')
				if quote == '`' {
					if character == '`' {
						quote = 0
					}
				} else if escaped {
					escaped = false
				} else if character == '\\' {
					escaped = true
				} else if character == quote {
					quote = 0
				}
				cursor++
				continue
			}
			if strings.HasPrefix(line[cursor:], "//") {
				break
			}
			if strings.HasPrefix(line[cursor:], "/*") {
				blockComment = true
				cursor += 2
				continue
			}
			if character == '"' || character == '\'' || character == '`' {
				quote = character
				visible.WriteByte(' ')
				cursor++
				continue
			}
			visible.WriteByte(line[cursor])
			cursor++
		}
		content := strings.TrimSpace(visible.String())
		if content == "" {
			continue
		}
		compact := wordSpaceRE.ReplaceAllString(content, "")
		if strings.HasPrefix(content, "package ") || strings.HasPrefix(content, "import ") || compact == "import(" {
			continue
		}
		if matched, _ := regexp.MatchString(`^[{}\[\]();,]+$`, compact); matched {
			continue
		}
		result[index+1] = struct{}{}
	}
	return result, nil
}

func sortedInts(values map[int]struct{}) []int {
	result := make([]int, 0, len(values))
	for value := range values {
		result = append(result, value)
	}
	sort.Ints(result)
	return result
}

func difference(all map[int]struct{}, covered map[int]struct{}) []int {
	result := make([]int, 0, len(all))
	for value := range all {
		if _, ok := covered[value]; ok {
			continue
		}
		result = append(result, value)
	}
	sort.Ints(result)
	return result
}

func main() {
	manifestPath := os.Getenv("TESTPRISM_GO_TARGETS")
	profilePath := os.Getenv("TESTPRISM_GO_PROFILE")
	reportPath := os.Getenv("TESTPRISM_GO_REPORT")
	if manifestPath == "" || profilePath == "" || reportPath == "" {
		panic("missing Go coverage normalizer environment")
	}

	var manifest targetManifest
	if err := json.Unmarshal(mustRead(manifestPath), &manifest); err != nil {
		panic(err)
	}
	if manifest.Workdir == "" || len(manifest.Targets) == 0 {
		panic("malformed Go target manifest")
	}
	workdir := filepath.Clean(manifest.Workdir)
	targets := map[string]string{}
	sourceUniverses := map[string]map[int]struct{}{}
	for _, entry := range manifest.Targets {
		if entry.Relative == "" || entry.Absolute == "" {
			panic("malformed Go target manifest entry")
		}
		absolute := filepath.Clean(entry.Absolute)
		if !isWithin(workdir, absolute) {
			panic("coverage target escapes workdir")
		}
		targets[entry.Relative] = absolute
		source, err := sourceLines(mustRead(absolute))
		if err != nil {
			panic(err)
		}
		sourceUniverses[entry.Relative] = source
	}

	lineUniverses := map[string]map[int]struct{}{}
	executed := map[string]map[int]struct{}{}
	for relative := range targets {
		lineUniverses[relative] = map[int]struct{}{}
		executed[relative] = map[int]struct{}{}
	}

	file, err := os.Open(profilePath)
	if err != nil {
		panic(err)
	}
	defer file.Close()

	mode := ""
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		rawLine := scanner.Text()
		trimmed := strings.TrimSpace(rawLine)
		if mode == "" {
			if trimmed == "" {
				continue
			}
			match := modeRE.FindStringSubmatch(trimmed)
			if match == nil {
				panic("Go coverage profile is missing mode header")
			}
			mode = match[1]
			continue
		}
		if trimmed == "" {
			continue
		}
		match := profileLineRE.FindStringSubmatch(rawLine)
		if match == nil {
			panic("malformed Go coverage profile line")
		}
		lines, err := blockLines(atoi(match[2]), atoi(match[3]), atoi(match[4]), atoi(match[5]))
		if err != nil {
			panic(err)
		}
		if atoi(match[6]) <= 0 {
			panic("Go coverage block statement count must be positive")
		}
		matched, err := normalizeProfilePath(match[1], workdir, targets)
		if err != nil {
			panic(err)
		}
		if len(matched) == 0 {
			continue
		}
		count := atoi(match[7])
		for _, relative := range matched {
			for _, line := range lines {
				lineUniverses[relative][line] = struct{}{}
				if count > 0 {
					executed[relative][line] = struct{}{}
				}
			}
		}
	}
	if err := scanner.Err(); err != nil {
		panic(err)
	}
	if mode == "" {
		panic("Go coverage profile is missing mode header")
	}

	report := coverageReport{Files: map[string]lineReport{}}
	for relative := range targets {
		universe := lineUniverses[relative]
		if len(universe) == 0 {
			universe = sourceUniverses[relative]
		}
		report.Files[relative] = lineReport{
			ExecutedLines: sortedInts(executed[relative]),
			MissingLines:  difference(universe, executed[relative]),
		}
	}
	data, err := json.Marshal(report)
	if err != nil {
		panic(err)
	}
	if err := os.WriteFile(reportPath, data, 0o644); err != nil {
		panic(err)
	}
}

func mustRead(path string) []byte {
	data, err := os.ReadFile(path)
	if err != nil {
		panic(err)
	}
	return data
}

func atoi(value string) int {
	result := 0
	for _, character := range value {
		result = result*10 + int(character-'0')
	}
	return result
}
'''


def write_go_runtime_files(
    root: Path,
    *,
    workdir: str,
    target_paths: list[str],
    namespace: str = "generated",
) -> GoCoverageRuntimeFiles:
    """Create a target manifest, wrapper, and stdlib-only Go finalizer."""

    paths = go_coverage_namespace(namespace)
    workdir_path, targets = _target_map(workdir, target_paths)
    packages = _coverage_package_patterns(targets)
    root.mkdir(parents=True, exist_ok=True)
    (root / "bin").mkdir(parents=True, exist_ok=True)
    (root / "normalize_go_coverage.go").write_text(
        GO_NORMALIZER_SOURCE, encoding="utf-8"
    )
    wrapper_path = root / "bin" / "go"
    wrapper_path.write_text(GO_WRAPPER_SOURCE, encoding="utf-8")
    wrapper_path.chmod(0o755)
    packages_text = "\n".join(packages)
    if packages_text:
        packages_text += "\n"
    (root / "packages.txt").write_text(packages_text, encoding="utf-8")
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
    return GoCoverageRuntimeFiles(
        root=root,
        namespace=_validate_namespace(namespace),
        container_root=paths["root"],
        coverage_dir=paths["coverage_dir"],
        covdata_dir=paths["covdata_dir"],
        profile_path=paths["profile_path"],
        json_path=paths["json_path"],
        report_path=paths["report_path"],
        targets_path=paths["targets_path"],
        packages_path=paths["packages_path"],
        normalizer_path=paths["normalizer_path"],
        wrapper_dir=paths["wrapper_dir"],
        wrapper_path=paths["wrapper_path"],
        real_go_path=paths["real_go_path"],
    )


class GoCoverageBackend(CoverageBackend):
    """Go ``covdata`` collector for ``.go`` production sources."""

    language = "go"
    source_suffixes = GO_SOURCE_SUFFIXES

    def write_runtime_files(
        self,
        root: Path,
        *,
        workdir: str,
        target_paths: list[str],
        namespace: str = "generated",
    ) -> GoCoverageRuntimeFiles:
        return write_go_runtime_files(
            root,
            workdir=workdir,
            target_paths=target_paths,
            namespace=namespace,
        )

    def setup_command(self, namespace: str = "generated") -> str:
        paths = go_coverage_namespace(namespace)
        root = shlex.quote(paths["root"])
        wrapper_dir = shlex.quote(paths["wrapper_dir"])
        covdata = shlex.quote(paths["covdata_dir"])
        profile = shlex.quote(paths["profile_path"])
        report = shlex.quote(paths["json_path"])
        real_go = shlex.quote(paths["real_go_path"])
        wrapper = shlex.quote(paths["wrapper_path"])
        return (
            f"mkdir -p {root} {wrapper_dir}; "
            f"rm -rf {covdata}; mkdir -p {covdata}; "
            f"rm -f {profile} {report} {real_go}; "
            "__ctb_go=$(command -v go || true); "
            'if [ -z "$__ctb_go" ]; then '
            "echo __TESTPRISM_COVERAGE_SETUP__=unsupported; "
            'else "$__ctb_go" tool covdata textfmt -h >/dev/null 2>&1; '
            "__ctb_covdata_status=$?; "
            'if [ "$__ctb_covdata_status" -eq 0 ] || [ "$__ctb_covdata_status" -eq 2 ]; then '
            f"printf '%s\\n' \"$__ctb_go\" > {real_go}; "
            f"chmod 755 {wrapper} >/dev/null 2>&1 || true; "
            "echo __TESTPRISM_COVERAGE_SETUP__=ready; "
            "else echo __TESTPRISM_COVERAGE_SETUP__=unsupported; fi; fi"
        )

    def environment(self, workdir: str, namespace: str = "generated") -> dict[str, str]:
        del workdir
        paths = go_coverage_namespace(namespace)
        return {
            "PATH": f"{paths['wrapper_dir']}${{PATH:+:${{PATH}}}}",
            "GOCOVERDIR": paths["covdata_dir"],
        }

    def collect_command(self, namespace: str = "generated") -> str:
        paths = go_coverage_namespace(namespace)
        covdata = shlex.quote(paths["covdata_dir"])
        profile = shlex.quote(paths["profile_path"])
        report = shlex.quote(paths["json_path"])
        targets = shlex.quote(paths["targets_path"])
        normalizer = shlex.quote(paths["normalizer_path"])
        real_go = shlex.quote(paths["real_go_path"])
        return (
            f"__ctb_covdata={covdata}; __ctb_profile={profile}; "
            f"__ctb_report={report}; __ctb_targets={targets}; "
            f"__ctb_normalizer={normalizer}; __ctb_real_go_file={real_go}; "
            'rm -f "$__ctb_profile" "$__ctb_report"; '
            '__ctb_go=""; '
            'if [ -f "$__ctb_real_go_file" ] && [ ! -L "$__ctb_real_go_file" ]; then '
            '__ctb_go=$(cat "$__ctb_real_go_file"); fi; '
            'if [ -z "$__ctb_go" ] || [ ! -x "$__ctb_go" ]; then '
            "echo __TESTPRISM_COVERAGE_COLLECT__=unsupported; "
            'else env -u GOFLAGS -u GOCOVERDIR "$__ctb_go" tool covdata textfmt -h >/dev/null 2>&1; '
            "__ctb_covdata_status=$?; "
            'if [ "$__ctb_covdata_status" -ne 0 ] && [ "$__ctb_covdata_status" -ne 2 ]; then '
            "echo __TESTPRISM_COVERAGE_COLLECT__=unsupported; "
            'elif ! find "$__ctb_covdata" -type f -print -quit 2>/dev/null | grep -q .; then '
            "echo __TESTPRISM_COVERAGE_COLLECT__=unsupported; "
            'elif env -u GOFLAGS -u GOCOVERDIR "$__ctb_go" tool covdata textfmt '
            '-i "$__ctb_covdata" -o "$__ctb_profile" >/dev/null 2>&1 && '
            'TESTPRISM_GO_TARGETS="$__ctb_targets" '
            'TESTPRISM_GO_PROFILE="$__ctb_profile" '
            'TESTPRISM_GO_REPORT="$__ctb_report" '
            'env -u GOFLAGS -u GOCOVERDIR "$__ctb_go" run "$__ctb_normalizer"; '
            "then echo __TESTPRISM_COVERAGE_COLLECT__=collected; "
            "else echo __TESTPRISM_COVERAGE_COLLECT__=failed; fi; fi; fi"
        )

    def normalized_report_path(self, namespace: str = "generated") -> str:
        return go_coverage_namespace(namespace)["json_path"]


GO_COVERAGE_BACKEND = GoCoverageBackend()


__all__ = [
    "GO_COVERAGE_BACKEND",
    "GO_COVERAGE_FLAGS",
    "GO_COVERAGE_ROOT",
    "GO_NORMALIZER_SOURCE",
    "GO_SOURCE_SUFFIXES",
    "GO_WRAPPER_SOURCE",
    "GoCoverageBackend",
    "GoCoverageRuntimeFiles",
    "go_coverage_namespace",
    "normalize_go_coverage",
    "normalize_go_coverage_profile",
    "normalize_go_coverage_profiles",
    "normalise_go_coverage",
    "normalise_go_coverage_profile",
    "write_go_runtime_files",
]
