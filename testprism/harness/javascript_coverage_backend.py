# ruff: noqa: E501

"""Node V8 coverage backend for JavaScript and TypeScript targets.

Node writes one JSON document per process when ``NODE_V8_COVERAGE`` is set.
This module keeps that raw process data in a namespace owned by the harness and
normalizes the V8 function ranges to the line-oriented report consumed by the
patch metric.  The normalizer is deliberately stdlib-only so the runtime can
be copied into a disposable task container without installing npm packages.
"""

from __future__ import annotations

import bisect
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping
from urllib.parse import unquote, urlsplit

from testprism.harness.coverage_backends import (
    CoverageBackend,
    normalize_target_path,
)

JAVASCRIPT_SOURCE_SUFFIXES = (
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".mts",
    ".cts",
    ".vue",
)

JAVASCRIPT_COVERAGE_ROOT = "/tmp/testbench/coverage"


@dataclass(frozen=True)
class JavaScriptCoverageRuntimeFiles:
    """Host-side files copied into one task-container V8 namespace."""

    root: Path
    namespace: str
    container_root: str
    coverage_dir: str
    targets_path: str
    normalizer_path: str
    json_path: str


def _validate_namespace(namespace: str) -> str:
    if not isinstance(namespace, str) or not namespace:
        raise ValueError("JavaScript coverage namespace must be non-empty")
    if not all(character.isalnum() or character in "_-" for character in namespace):
        raise ValueError(f"invalid JavaScript coverage namespace: {namespace!r}")
    return namespace


def javascript_coverage_namespace(namespace: str = "generated") -> dict[str, str]:
    """Return deterministic container paths for one generated/reference suite."""

    namespace = _validate_namespace(namespace)
    root = f"{JAVASCRIPT_COVERAGE_ROOT}/{namespace}/javascript"
    return {
        "root": root,
        "coverage_dir": f"{root}/v8",
        "targets_path": f"{root}/targets.json",
        "normalizer_path": f"{root}/normalize_v8_coverage.js",
        "json_path": f"{root}/coverage.json",
    }


def _workdir_path(workdir: str | Path) -> Path:
    rendered = str(workdir).replace("\\", "/").rstrip("/") or "/"
    path = Path(rendered)
    if not path.is_absolute():
        raise ValueError(f"coverage workdir must be absolute: {workdir!r}")
    return path.resolve(strict=False)


def _target_absolute(workdir: Path, target: str | Path) -> tuple[str, Path]:
    """Validate and return a target's repo-relative spelling and absolute path."""

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
        raise ValueError("JavaScript coverage requires at least one target path")
    return workdir_path, dict(sorted(targets.items()))


def _line_universe(source: bytes) -> set[int]:
    """Return positive, non-empty source lines.

    V8 reports executable ranges, but an unexecuted file has no report entry at
    all.  A small lexical filter gives those files a complete and deterministic
    line universe without introducing a parser/npm dependency.  Comment-only
    lines are excluded while all other non-empty lines remain auditable.
    """

    text = source.decode("utf-8", errors="strict")
    result: set[int] = set()
    in_block_comment = False
    for line_number, line in enumerate(text.splitlines(), 1):
        rendered = line
        pieces: list[str] = []
        index = 0
        while index < len(rendered):
            if in_block_comment:
                end = rendered.find("*/", index)
                if end < 0:
                    index = len(rendered)
                    break
                in_block_comment = False
                index = end + 2
                continue
            start = rendered.find("/*", index)
            line_comment = rendered.find("//", index)
            if line_comment >= 0 and (start < 0 or line_comment < start):
                pieces.append(rendered[index:line_comment])
                index = len(rendered)
                break
            if start < 0:
                pieces.append(rendered[index:])
                index = len(rendered)
                break
            pieces.append(rendered[index:start])
            end = rendered.find("*/", start + 2)
            if end < 0:
                in_block_comment = True
                index = len(rendered)
            else:
                index = end + 2
        content = "".join(pieces).strip()
        compact = re.sub(r"\s+", "", content)
        if content and not re.fullmatch(r"[{}\[\]();,]+", compact):
            result.add(line_number)
    return result


def _line_starts(source: bytes) -> list[int]:
    starts = [0]
    starts.extend(index + 1 for index, value in enumerate(source) if value == 10)
    return starts


def _utf16_line_starts(source: bytes) -> tuple[list[int], int]:
    """Return line starts in V8's UTF-16 source-offset coordinate system."""

    text = source.decode("utf-8", errors="strict")
    encoded = text.encode("utf-16-le")
    starts = [0]
    for index in range(0, len(encoded), 2):
        if encoded[index] == 10 and encoded[index + 1] == 0:
            starts.append(index // 2 + 1)
    return starts, len(encoded) // 2


def _range_lines(
    source: bytes,
    start_offset: int,
    end_offset: int,
) -> set[int]:
    if isinstance(start_offset, bool) or isinstance(end_offset, bool):
        raise ValueError("V8 range offsets must be integers")
    if not isinstance(start_offset, int) or not isinstance(end_offset, int):
        raise ValueError("V8 range offsets must be integers")
    if start_offset < 0 or end_offset < start_offset:
        raise ValueError("V8 range offsets are outside the source bytes")
    if start_offset == end_offset:
        return set()
    # Node's inspector emits offsets in UTF-16 code units.  Accept byte
    # offsets as well for hand-authored reports and older V8 producers when
    # the endpoint cannot be represented in the UTF-16 coordinate system.
    utf16_starts, utf16_length = _utf16_line_starts(source)
    if end_offset <= utf16_length:
        starts = utf16_starts
    elif end_offset <= len(source):
        starts = _line_starts(source)
    else:
        raise ValueError("V8 range offsets are outside the source bytes")
    first = bisect.bisect_right(starts, start_offset) - 1
    last = bisect.bisect_right(starts, end_offset - 1) - 1
    return set(range(first + 1, last + 2))


def _file_url_path(url: str, workdir: Path) -> tuple[str, Path] | None:
    if not isinstance(url, str):
        raise ValueError("V8 script url must be a string")
    if not url:
        return None
    parsed = urlsplit(url)
    if parsed.scheme in {"node", "data", "blob"}:
        return None
    # V8 uses bare identifiers for a number of runtime scripts, including
    # ``evalmachine.<anonymous>`` and ``internal/per_context/primordials.js``.
    # URL-shaped non-file scripts (for example ``node:``/``data:`` above) are
    # likewise valid coverage entries, but none can map to a repository target.
    # Keep validating the enclosing report and range structure while ignoring
    # these unrelated scripts.
    if parsed.scheme != "file":
        return None
    if parsed.query or parsed.fragment or parsed.netloc.lower() not in {"", "localhost"}:
        raise ValueError(f"malformed V8 file url: {url!r}")
    if re.search(r"%(?:2f|5c)", parsed.path, flags=re.IGNORECASE):
        raise ValueError(f"malformed V8 file url: {url!r}")
    rendered = unquote(parsed.path)
    if not rendered.startswith("/"):
        raise ValueError(f"malformed V8 file url: {url!r}")
    # Node 18 emits this synthetic startup script whenever V8 coverage is
    # enabled. It is not task source and has no repository-relative target.
    if rendered == "/proc/.reset":
        return None
    absolute = Path(rendered).resolve(strict=False)
    try:
        relative_path = absolute.relative_to(workdir)
    except ValueError:
        # Global npm files and other runtime-owned scripts are expected in V8
        # reports. They are not task targets and must not abort normalization.
        return None
    if ".." in PurePosixPath(rendered).parts:
        # Retain the strict path contract for a target-shaped URL that resolves
        # inside the workdir, while allowing an explicitly escaping runtime
        # path to be ignored as an unrelated script.
        raise ValueError(f"V8 script url escapes workdir: {url!r}")
    relative = normalize_target_path(relative_path.as_posix())
    return relative, absolute


def _validate_range_entry(range_entry: object) -> tuple[int, int, int | float]:
    """Validate V8 range shape without requiring a source buffer.

    Non-target scripts still need a structurally valid report, but their
    offsets cannot be checked against repository source that we intentionally
    ignore. Target ranges are passed through ``_range_lines`` for the stricter
    source-bound check after this validation.
    """

    if not isinstance(range_entry, Mapping):
        raise ValueError("V8 range entry must be an object")
    if "startOffset" not in range_entry or "endOffset" not in range_entry:
        raise ValueError("V8 range is missing offsets")
    start_offset = range_entry["startOffset"]
    end_offset = range_entry["endOffset"]
    if (
        isinstance(start_offset, bool)
        or isinstance(end_offset, bool)
        or not isinstance(start_offset, int)
        or not isinstance(end_offset, int)
    ):
        raise ValueError("V8 range offsets must be integers")
    if start_offset < 0 or end_offset < start_offset:
        raise ValueError("V8 range offsets are outside the source bytes")
    count = range_entry.get("count")
    if (
        isinstance(count, bool)
        or not isinstance(count, (int, float))
        or not math.isfinite(count)
    ):
        raise ValueError("V8 range count must be numeric")
    return start_offset, end_offset, count


def _validate_report(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, Mapping):
        raise ValueError("V8 coverage report must be a JSON object")
    scripts = value.get("result")
    if not isinstance(scripts, list):
        raise ValueError("V8 coverage report is missing a result list")
    return scripts


def _normalise_reports(
    reports: Iterable[Mapping[str, object]],
    *,
    workdir: str | Path,
    target_paths: Iterable[str | Path],
) -> dict[str, object]:
    workdir_path, targets = _target_map(workdir, target_paths)
    target_lines: dict[str, set[int]] = {}
    for relative, absolute in targets.items():
        try:
            source = absolute.read_bytes()
        except FileNotFoundError:
            source = b""
        target_lines[relative] = _line_universe(source)

    executed: dict[str, set[int]] = {relative: set() for relative in targets}
    for report in reports:
        for script in _validate_report(report):
            if not isinstance(script, Mapping):
                raise ValueError("V8 script entry must be an object")
            if "url" not in script:
                raise ValueError("V8 script entry is missing url")
            location = _file_url_path(script["url"], workdir_path)
            functions = script.get("functions")
            if not isinstance(functions, list):
                raise ValueError("V8 script entry is missing functions")
            for function in functions:
                if not isinstance(function, Mapping):
                    raise ValueError("V8 function entry must be an object")
                ranges = function.get("ranges")
                if not isinstance(ranges, list):
                    raise ValueError("V8 function entry is missing ranges")
                if location is None:
                    # Non-file scripts (node internals, eval/data URLs, global
                    # npm files, and any other path outside the workdir) are
                    # valid V8 output but cannot map to repository targets.
                    # Validate each range's shape without applying
                    # source-bound checks.
                    for range_entry in ranges:
                        _validate_range_entry(range_entry)
                    continue
                relative, absolute = location
                is_target = relative in executed
                source = (
                    absolute.read_bytes()
                    if is_target and absolute.is_file()
                    else b""
                )
                for range_entry in ranges:
                    start_offset, end_offset, count = _validate_range_entry(
                        range_entry
                    )
                    # Node can emit file URLs for the eval script used to
                    # bootstrap a runner. Those scripts may live below the
                    # task workdir while not being requested source targets;
                    # validate their shape but never map their offsets against
                    # an empty source buffer.
                    if not is_target:
                        continue
                    lines = _range_lines(source, start_offset, end_offset)
                    if relative in executed and count > 0:
                        executed[relative].update(lines)

    files: dict[str, dict[str, list[int]]] = {}
    for relative in sorted(targets):
        covered = sorted(executed[relative] & target_lines[relative])
        missing = sorted(target_lines[relative] - set(covered))
        files[relative] = {
            "executed_lines": covered,
            "missing_lines": missing,
        }
    return {"files": files}


def normalize_v8_coverage(
    report: Mapping[str, object] | Iterable[Mapping[str, object]],
    *,
    workdir: str | Path,
    target_paths: Iterable[str | Path],
) -> dict[str, object]:
    """Normalize one or more V8 JSON reports to the common line contract."""

    if isinstance(report, Mapping):
        reports: Iterable[Mapping[str, object]] = (report,)
    else:
        reports = report
    return _normalise_reports(reports, workdir=workdir, target_paths=target_paths)


def normalize_v8_coverage_directory(
    coverage_dir: str | Path,
    *,
    workdir: str | Path,
    target_paths: Iterable[str | Path],
) -> dict[str, object]:
    """Read all process reports from a ``NODE_V8_COVERAGE`` directory."""

    root = Path(coverage_dir)
    reports: list[Mapping[str, object]] = []
    if root.exists():
        if not root.is_dir():
            raise ValueError("NODE_V8_COVERAGE path is not a directory")
        for report_path in sorted(root.glob("*.json")):
            try:
                value = json.loads(report_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"malformed V8 coverage file: {report_path}") from exc
            if not isinstance(value, Mapping):
                raise ValueError(f"malformed V8 coverage file: {report_path}")
            reports.append(value)
    return normalize_v8_coverage(
        reports,
        workdir=workdir,
        target_paths=target_paths,
    )


# British spelling and descriptive aliases make the normalizer discoverable to
# callers while keeping one implementation and one report contract.
normalise_v8_coverage = normalize_v8_coverage
normalize_v8_report = normalize_v8_coverage


JAVASCRIPT_NORMALIZER_SOURCE = r'''#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const { fileURLToPath } = require("url");

function fail(message) {
  throw new Error(message);
}

function isWithin(root, candidate) {
  const relative = path.relative(root, candidate);
  return relative === "" || (relative !== ".." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative));
}

function resolvedPath(value) {
  const absolute = path.resolve(value);
  try { return fs.realpathSync(absolute); } catch (_error) { return absolute; }
}

function lineUniverse(source) {
  const lines = new Set();
  let inBlock = false;
  source.toString("utf8").split(/\r?\n/).forEach((line, index) => {
    let rendered = line;
    let pieces = [];
    let cursor = 0;
    while (cursor < rendered.length) {
      if (inBlock) {
        const end = rendered.indexOf("*/", cursor);
        if (end < 0) { cursor = rendered.length; break; }
        inBlock = false; cursor = end + 2; continue;
      }
      const start = rendered.indexOf("/*", cursor);
      const comment = rendered.indexOf("//", cursor);
      if (comment >= 0 && (start < 0 || comment < start)) {
        pieces.push(rendered.slice(cursor, comment)); cursor = rendered.length; break;
      }
      if (start < 0) { pieces.push(rendered.slice(cursor)); cursor = rendered.length; break; }
      pieces.push(rendered.slice(cursor, start));
      const end = rendered.indexOf("*/", start + 2);
      if (end < 0) { inBlock = true; cursor = rendered.length; }
      else cursor = end + 2;
    }
    const content = pieces.join("").trim();
    const compact = content.replace(/\s+/g, "");
    if (content && !/^[{}\[\]();,]+$/.test(compact)) lines.add(index + 1);
  });
  return lines;
}

function rangeLines(source, start, end) {
  if (!Number.isInteger(start) || !Number.isInteger(end) || start < 0 || end < start) {
    fail("V8 range offsets are outside the source bytes");
  }
  if (start === end) return new Set();
  const text = source.toString("utf8");
  const utf16 = Buffer.from(text, "utf16le");
  const utf16Length = utf16.length / 2;
  const useUtf16 = end <= utf16Length;
  if (!useUtf16 && end > source.length) fail("V8 range offsets are outside the source bytes");
  const starts = [0];
  if (useUtf16) {
    for (let index = 0; index < utf16.length; index += 2) {
      if (utf16[index] === 10 && utf16[index + 1] === 0) starts.push(index / 2 + 1);
    }
  } else {
    for (let index = 0; index < source.length; index += 1) if (source[index] === 10) starts.push(index + 1);
  }
  let first = 0;
  while (first + 1 < starts.length && starts[first + 1] <= start) first += 1;
  let last = 0;
  const endOffset = end - 1;
  while (last + 1 < starts.length && starts[last + 1] <= endOffset) last += 1;
  const lines = new Set();
  for (let line = first + 1; line <= last + 1; line += 1) lines.add(line);
  return lines;
}

function scriptPath(url, workdir) {
  if (typeof url !== "string") fail("V8 script url must be a string");
  if (!url) return null;
  if (/^(node|data|blob):/.test(url)) return null;
  let parsed;
  try { parsed = new URL(url); } catch (_error) {
    // Node reports bare runtime identifiers such as
    // ``evalmachine.<anonymous>`` and ``internal/per_context/primordials.js``
    // instead of URL strings. They are valid non-target coverage metadata.
    if (!/^[A-Za-z][A-Za-z0-9+.-]*:/.test(url)) return null;
    fail(`malformed V8 file url: ${url}`);
  }
  if (parsed.protocol !== "file:") return null;
  if (parsed.search || parsed.hash || (parsed.hostname && parsed.hostname !== "localhost")) {
    fail(`malformed V8 file url: ${url}`);
  }
  if (/%(?:2f|5c)/i.test(parsed.pathname)) fail(`malformed V8 file url: ${url}`);
  let absolute;
  let renderedPath;
  try { renderedPath = fileURLToPath(parsed); } catch (_error) { fail(`malformed V8 file url: ${url}`); }
  // Node 18 emits this synthetic startup script whenever V8 coverage is
  // enabled. It is not task source and has no repository-relative target.
  if (renderedPath === "/proc/.reset") return null;
  absolute = resolvedPath(renderedPath);
  if (!isWithin(workdir, absolute)) return null;
  if (renderedPath.split(/[\\/]+/).includes("..")) fail(`V8 script url escapes workdir: ${url}`);
  return { relative: path.relative(workdir, absolute).split(path.sep).join("/"), absolute };
}

function normalizeTargetPath(value) {
  if (typeof value !== "string") fail("malformed V8 target manifest entry");
  let rendered = value.replace(/\\/g, "/").trim();
  if (rendered.includes("\u0000")) fail("malformed V8 target manifest entry");
  while (rendered.startsWith("./")) rendered = rendered.slice(2);
  if (!rendered || rendered === "." || rendered.startsWith("/") || /^[A-Za-z]:/.test(rendered)) {
    fail(`coverage target escapes workdir: ${value}`);
  }
  if (rendered.startsWith("a/") || rendered.startsWith("b/")) rendered = rendered.slice(2);
  const parts = rendered.split("/");
  if (parts.includes("..")) fail(`coverage target escapes workdir: ${value}`);
  const normalized = path.posix.normalize(rendered);
  if (!normalized || normalized === "." || normalized.startsWith("../") || normalized === "..") {
    fail(`coverage target escapes workdir: ${value}`);
  }
  return normalized;
}

function validateRange(range) {
  if (!range || typeof range !== "object" || !("startOffset" in range) || !("endOffset" in range) || typeof range.count !== "number" || !Number.isFinite(range.count)) {
    fail("malformed V8 range entry");
  }
  if (!Number.isInteger(range.startOffset) || !Number.isInteger(range.endOffset) || range.startOffset < 0 || range.endOffset < range.startOffset) {
    fail("V8 range offsets are outside the source bytes");
  }
  return range;
}

function main() {
  const manifest = JSON.parse(fs.readFileSync(process.env.TESTPRISM_V8_TARGETS, "utf8"));
  if (!manifest || typeof manifest !== "object" || !Array.isArray(manifest.targets)) fail("malformed V8 target manifest");
  if (typeof manifest.workdir !== "string" || !path.isAbsolute(manifest.workdir)) fail("malformed V8 target manifest");
  const workdir = resolvedPath(manifest.workdir);
  const targets = new Map();
  for (const target of manifest.targets) {
    if (!target || typeof target.relative !== "string" || typeof target.absolute !== "string") fail("malformed V8 target manifest entry");
    const relative = normalizeTargetPath(target.relative);
    if (target.absolute.replace(/\\/g, "/").split("/").includes("..")) fail(`coverage target escapes workdir: ${target.relative}`);
    const absolute = resolvedPath(target.absolute);
    if (!path.isAbsolute(target.absolute)) fail(`coverage target escapes workdir: ${target.relative}`);
    if (!isWithin(workdir, absolute)) fail(`coverage target escapes workdir: ${target.relative}`);
    const actualRelative = path.relative(workdir, absolute).split(path.sep).join("/");
    if (relative !== actualRelative) fail(`malformed V8 target manifest entry: ${target.relative}`);
    if (targets.has(relative)) fail(`duplicate V8 target manifest entry: ${relative}`);
    targets.set(relative, absolute);
  }
  if (!targets.size) fail("JavaScript coverage requires at least one target path");
  const targetLines = new Map();
  for (const [relative, absolute] of targets) {
    let source = Buffer.alloc(0);
    try { source = fs.readFileSync(absolute); } catch (error) { if (error.code !== "ENOENT") throw error; }
    targetLines.set(relative, lineUniverse(source));
  }
  const executed = new Map([...targets.keys()].map((relative) => [relative, new Set()]));
  const coverageDir = process.env.TESTPRISM_V8_COVERAGE_DIR;
  let reportNames = [];
  if (fs.existsSync(coverageDir)) reportNames = fs.readdirSync(coverageDir).filter((name) => name.endsWith(".json")).sort();
  for (const reportName of reportNames) {
    const report = JSON.parse(fs.readFileSync(path.join(coverageDir, reportName), "utf8"));
    if (!report || !Array.isArray(report.result)) fail("V8 coverage report is missing a result list");
    for (const script of report.result) {
      if (!script || typeof script !== "object" || !("url" in script) || !Array.isArray(script.functions)) fail("malformed V8 script entry");
      const location = scriptPath(script.url, workdir);
      const isTarget = Boolean(location && executed.has(location.relative));
      let source = Buffer.alloc(0);
      if (isTarget && fs.existsSync(location.absolute)) source = fs.readFileSync(location.absolute);
      for (const func of script.functions) {
        if (!func || typeof func !== "object" || !Array.isArray(func.ranges)) fail("malformed V8 function entry");
        for (const range of func.ranges) {
          validateRange(range);
          // Node may report its eval bootstrap as a file URL below the
          // workdir. Validate the entry shape but do not map its offsets
          // against an empty buffer when it is not a requested target.
          const lines = isTarget ? rangeLines(source, range.startOffset, range.endOffset) : new Set();
          if (isTarget && range.count > 0) for (const line of lines) executed.get(location.relative).add(line);
        }
      }
    }
  }
  const files = {};
  for (const relative of [...targets.keys()].sort()) {
    const universe = targetLines.get(relative);
    const covered = [...executed.get(relative)].filter((line) => universe.has(line)).sort((left, right) => left - right);
    const coveredSet = new Set(covered);
    const missing = [...universe].filter((line) => !coveredSet.has(line)).sort((left, right) => left - right);
    files[relative] = { executed_lines: covered, missing_lines: missing };
  }
  fs.writeFileSync(process.env.TESTPRISM_V8_REPORT, JSON.stringify({ files }));
}

main();
'''


def write_javascript_runtime_files(
    root: Path,
    *,
    workdir: str,
    target_paths: list[str],
    namespace: str = "generated",
) -> JavaScriptCoverageRuntimeFiles:
    """Create a target manifest and Node-only V8 finalizer."""

    paths = javascript_coverage_namespace(namespace)
    workdir_path, targets = _target_map(workdir, target_paths)
    root.mkdir(parents=True, exist_ok=True)
    (root / "normalize_v8_coverage.js").write_text(JAVASCRIPT_NORMALIZER_SOURCE)
    (root / "targets.json").write_text(
        json.dumps(
            {
                "workdir": workdir_path.as_posix(),
                "targets": [
                    {"relative": relative, "absolute": absolute.as_posix()}
                    for relative, absolute in targets.items()
                ],
            }
        )
    )
    return JavaScriptCoverageRuntimeFiles(
        root=root,
        namespace=_validate_namespace(namespace),
        container_root=paths["root"],
        coverage_dir=paths["coverage_dir"],
        targets_path=paths["targets_path"],
        normalizer_path=paths["normalizer_path"],
        json_path=paths["json_path"],
    )


class JavaScriptCoverageBackend(CoverageBackend):
    """Node's built-in V8 coverage collector for JS/TS source files."""

    language = "javascript"
    source_suffixes = JAVASCRIPT_SOURCE_SUFFIXES

    def write_runtime_files(
        self,
        root: Path,
        *,
        workdir: str,
        target_paths: list[str],
        namespace: str = "generated",
    ) -> JavaScriptCoverageRuntimeFiles:
        return write_javascript_runtime_files(
            root,
            workdir=workdir,
            target_paths=target_paths,
            namespace=namespace,
        )

    def setup_command(self, namespace: str = "generated") -> str:
        paths = javascript_coverage_namespace(namespace)
        return (
            f"mkdir -p {paths['root']} {paths['coverage_dir']}; "
            "__ctb_node=$(command -v node || true); "
            'if [ -z "$__ctb_node" ]; then '
            "echo __TESTPRISM_COVERAGE_SETUP__=unsupported; "
            f"elif NODE_V8_COVERAGE={paths['coverage_dir']} \"$__ctb_node\" "
            "-e 'process.exit(0)' >/dev/null 2>&1; then "
            f"__ctb_probe=$(find {paths['coverage_dir']} -maxdepth 1 "
            "-type f -name '*.json' -print -quit 2>/dev/null); "
            'if [ -n "$__ctb_probe" ]; then '
            f"find {paths['coverage_dir']} -maxdepth 1 -type f -name '*.json' "
            "-exec rm -f {} + >/dev/null 2>&1; "
            "echo __TESTPRISM_COVERAGE_SETUP__=ready; "
            "else echo __TESTPRISM_COVERAGE_SETUP__=unsupported; fi; "
            "else echo __TESTPRISM_COVERAGE_SETUP__=unsupported; fi"
        )

    def environment(
        self, workdir: str, namespace: str = "generated"
    ) -> dict[str, str]:
        del workdir
        return {
            "NODE_V8_COVERAGE": javascript_coverage_namespace(namespace)[
                "coverage_dir"
            ]
        }

    def collect_command(self, namespace: str = "generated") -> str:
        paths = javascript_coverage_namespace(namespace)
        return (
            "__ctb_node=$(command -v node || true); "
            'if [ -z "$__ctb_node" ]; then '
            "echo __TESTPRISM_COVERAGE_COLLECT__=unsupported; "
            f"elif TESTPRISM_V8_COVERAGE_DIR={paths['coverage_dir']} "
            f"TESTPRISM_V8_TARGETS={paths['targets_path']} "
            f"TESTPRISM_V8_REPORT={paths['json_path']} "
            f"env -u NODE_V8_COVERAGE \"$__ctb_node\" {paths['normalizer_path']}; then "
            "echo __TESTPRISM_COVERAGE_COLLECT__=collected; "
            "else echo __TESTPRISM_COVERAGE_COLLECT__=failed; fi"
        )

    def normalized_report_path(self, namespace: str = "generated") -> str:
        return javascript_coverage_namespace(namespace)["json_path"]


JAVASCRIPT_COVERAGE_BACKEND = JavaScriptCoverageBackend()
JS_COVERAGE_BACKEND = JAVASCRIPT_COVERAGE_BACKEND


__all__ = [
    "JAVASCRIPT_COVERAGE_BACKEND",
    "JAVASCRIPT_NORMALIZER_SOURCE",
    "JAVASCRIPT_SOURCE_SUFFIXES",
    "JS_COVERAGE_BACKEND",
    "JavaScriptCoverageBackend",
    "JavaScriptCoverageRuntimeFiles",
    "javascript_coverage_namespace",
    "normalize_v8_coverage",
    "normalize_v8_coverage_directory",
    "normalize_v8_report",
    "normalise_v8_coverage",
    "write_javascript_runtime_files",
]
