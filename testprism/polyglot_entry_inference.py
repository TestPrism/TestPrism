"""Conservative static test-invocation inference for Java, C++, JavaScript,
and TypeScript.

This module deliberately has no compiler or tree-sitter dependency.  It masks
comments and literals, matches balanced delimiters, and emits an entry only
when a receiver type and a production definition can be resolved uniquely.
It is intended for benchmark preparation, where false positives are more
damaging than omitted dynamic or heavily templated calls.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from testprism.handlers.trial_handler import TestEntry, TestEntryInferences

LANGUAGE_EXTENSIONS = {
    "java": {".java"},
    "cpp": {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"},
    "javascript": {".js", ".jsx", ".mjs", ".cjs"},
    "typescript": {".ts", ".tsx", ".mts", ".cts"},
}

EXCLUDED_PATH_SEGMENTS = frozenset(
    {
        "third_party",
        "vendor",
        "vendors",
        "node_modules",
        ".venv",
        "venv",
        "build",
        "dist",
        "generated",
        "target",
    }
)


def is_excluded_repository_path(path: Path | str) -> bool:
    """Return whether a repository-relative path crosses an untrusted tree.

    Matching complete, case-insensitive path segments keeps conventional paths
    such as ``src/test`` and names such as ``build_tools`` eligible.
    """

    normalized = str(path).replace("\\", "/")
    return any(
        part.lower() in EXCLUDED_PATH_SEGMENTS for part in normalized.split("/") if part
    )


@dataclass(frozen=True, slots=True)
class _Definition:
    path: str
    owner: str
    name: str
    arity: int | None
    parameter_types: tuple[str, ...]
    start_line: int
    end_line: int

    @property
    def symbol(self) -> str:
        separator = "." if "::" not in self.owner else "::"
        return f"{self.owner}{separator}{self.name}" if self.owner else self.name


@dataclass(frozen=True, slots=True)
class TestRegion:
    test_id: str
    start_line: int
    end_line: int
    body_start: int
    body_end: int


def normalize_language(value: str) -> str:
    language = value.strip().lower()
    if language in {"java"}:
        return "java"
    if language in {"c++", "cpp", "cxx", "cc"}:
        return "cpp"
    if language in {"javascript", "js", "node", "nodejs"}:
        return "javascript"
    if language in {"typescript", "ts", "tsx"}:
        return "typescript"
    if language in {"python", "python3", "py", "cpython"}:
        return "python"
    return language


def _mask_source(source: str) -> str:
    """Replace comments and string/character contents while retaining layout."""

    chars = list(source)
    index = 0
    state = "code"
    quote = ""
    while index < len(chars):
        char = chars[index]
        following = chars[index + 1] if index + 1 < len(chars) else ""
        if state == "code":
            if char == "/" and following == "/":
                chars[index] = chars[index + 1] = " "
                state = "line"
                index += 2
                continue
            if char == "/" and following == "*":
                chars[index] = chars[index + 1] = " "
                state = "block"
                index += 2
                continue
            if char in {'"', "'", "`"}:
                quote = char
                state = "string"
        elif state == "line":
            if char == "\n":
                state = "code"
            else:
                chars[index] = " "
        elif state == "block":
            if char == "*" and following == "/":
                chars[index] = chars[index + 1] = " "
                state = "code"
                index += 2
                continue
            if char != "\n":
                chars[index] = " "
        else:
            if char == "\\":
                chars[index] = " "
                if index + 1 < len(chars) and chars[index + 1] != "\n":
                    chars[index + 1] = " "
                    index += 2
                    continue
            elif char == quote:
                state = "code"
            elif char != "\n":
                chars[index] = " "
        index += 1
    return "".join(chars)


def _matching_delimiter(
    text: str, start: int, opening: str, closing: str
) -> int | None:
    depth = 0
    for index in range(start, len(text)):
        if text[index] == opening:
            depth += 1
        elif text[index] == closing:
            depth -= 1
            if depth == 0:
                return index
    return None


def _line(source: str, offset: int) -> int:
    return source.count("\n", 0, offset) + 1


def _arity(parameters: str) -> int:
    parameters = parameters.strip()
    if not parameters or parameters == "void":
        return 0
    depth = 0
    count = 1
    for char in parameters:
        if char in "(<[{":
            depth += 1
        elif char in ")>]}" and depth:
            depth -= 1
        elif char == "," and depth == 0:
            count += 1
    return count


def _split_arguments(value: str) -> list[str]:
    if not value.strip():
        return []
    result: list[str] = []
    start = 0
    depth = 0
    for index, char in enumerate(value):
        if char in "(<[{":
            depth += 1
        elif char in ")>]}" and depth:
            depth -= 1
        elif char == "," and depth == 0:
            result.append(value[start:index].strip())
            start = index + 1
    result.append(value[start:].strip())
    return result


def _parameter_types(parameters: str) -> tuple[str, ...]:
    types: list[str] = []
    for parameter in _split_arguments(parameters):
        cleaned = re.sub(r"@\w+(?:\([^)]*\))?", "", parameter)
        cleaned = re.sub(r"\b(?:final|const|volatile|typename)\b", "", cleaned)
        type_match = re.match(r"\s*([A-Za-z_]\w*(?:(?:\.|::)\w+)*)", cleaned)
        if not type_match:
            types.append("")
            continue
        types.append(_simple_type(type_match.group(1)))
    return tuple(types)


def _class_ranges(masked: str, language: str) -> list[tuple[int, int, str]]:
    pattern = (
        r"(?m)^[ \t]*(?:(?:public|protected|private|abstract|final|static)\s+)*"
        r"(?:class|interface|enum|record)\s+(\w+)\b[^;{\n]*\{"
        if language == "java"
        else r"(?m)^[ \t]*(?:class|struct)\s+(\w+)\b[^;{\n]*\{"
    )
    ranges: list[tuple[int, int, str]] = []
    for match in re.finditer(pattern, masked):
        opening = masked.find("{", match.start(), match.end())
        closing = _matching_delimiter(masked, opening, "{", "}")
        if closing is not None:
            ranges.append((opening, closing, match.group(1)))
    return ranges


def _namespace_ranges(masked: str) -> list[tuple[int, int, str]]:
    """Return named C++ namespace body ranges for lexical ownership."""

    ranges: list[tuple[int, int, str]] = []
    pattern = re.compile(r"\bnamespace\s+([A-Za-z_]\w*(?:::\w+)*)\s*\{")
    for match in pattern.finditer(masked):
        opening = masked.find("{", match.start(), match.end())
        closing = _matching_delimiter(masked, opening, "{", "}")
        if closing is not None:
            ranges.append((opening, closing, match.group(1)))
    return ranges


def _owner_at(offset: int, ranges: list[tuple[int, int, str]]) -> str:
    owners = [name for start, end, name in ranges if start < offset < end]
    return ".".join(owners)


_CONTROL_WORDS = {"if", "for", "while", "switch", "catch", "return", "new", "throw"}


def _definitions(path: Path, root: Path, language: str) -> list[_Definition]:
    if language in {"javascript", "typescript"}:
        return []
    source = path.read_text(encoding="utf-8")
    masked = _mask_source(source)
    classes = _class_ranges(masked, language)
    namespaces = _namespace_ranges(masked) if language == "cpp" else []
    definitions: list[_Definition] = []
    # A definition candidate must end in an opening brace.  Requiring either a
    # class owner, a qualified C++ name, or a return-type-like prefix excludes
    # ordinary calls and control-flow constructs.
    pattern = re.compile(
        r"(?m)^[ \t]*(?:[\w@][\w<>\[\],.?&:*~ \t]*?[ \t]+)?"
        r"(?P<name>[A-Za-z_]\w*(?:::\w+)*)[ \t]*"
        r"\((?P<params>[^(){};]*)\)[ \t]*"
        r"(?:const\b[ \t]*)?(?:noexcept\b[ \t]*)?(?:override\b[ \t]*)?"
        r"(?:throws\s+[^\n{]+)?"
        r"(?:\s*:\s*[^\n{]+)?\s*\{"
    )
    for match in pattern.finditer(masked):
        raw_name = match.group("name")
        leaf = raw_name.rsplit("::", 1)[-1]
        if leaf in _CONTROL_WORDS:
            continue
        opening = masked.rfind("{", match.start(), match.end())
        closing = _matching_delimiter(masked, opening, "{", "}")
        if closing is None:
            continue
        lexical_owner = _owner_at(match.start(), classes)
        namespace_owner = _owner_at(match.start(), namespaces)
        qualified_owner = raw_name.rsplit("::", 1)[0] if "::" in raw_name else ""
        if qualified_owner:
            owner = qualified_owner
        elif lexical_owner and namespace_owner:
            owner = f"{namespace_owner}::{lexical_owner.replace('.', '::')}"
        else:
            owner = lexical_owner or namespace_owner
        if not owner and language == "java":
            continue
        definitions.append(
            _Definition(
                path=path.resolve().relative_to(root.resolve()).as_posix(),
                owner=owner.replace(".", "::") if language == "cpp" else owner,
                name=leaf,
                arity=_arity(match.group("params")),
                parameter_types=_parameter_types(match.group("params")),
                start_line=_line(source, match.start()),
                end_line=_line(source, closing),
            )
        )
    return definitions


_JS_TEST_CALL_START_RE = re.compile(
    r"\b(?P<kind>describe|suite|context|it|test)(?:\.(?:only|skip))?\s*"
    r"\(\s*(?P<quote>['\"`])"
)


def _scan_js_string_end(source: str, start: int, quote: str) -> int | None:
    index = start
    while index < len(source):
        char = source[index]
        if char == "\\":
            index += 2
            continue
        if char == quote:
            return index
        index += 1
    return None


def _unescape_js_title(value: str) -> str:
    return re.sub(r"\\([\\'\"`])", r"\1", value).strip()


def _collect_js_test_regions(source: str, relative_path: str) -> list[TestRegion]:
    masked = _mask_source(source)
    candidates: list[tuple[str, str, int, int, int, int]] = []
    for match in _JS_TEST_CALL_START_RE.finditer(source):
        quote = match.group("quote")
        title_end = _scan_js_string_end(source, match.end(), quote)
        if title_end is None:
            continue
        comma = title_end + 1
        while comma < len(source) and source[comma].isspace():
            comma += 1
        if comma >= len(source) or source[comma] != ",":
            continue
        call_opening = source.find("(", match.start(), match.end())
        if call_opening == -1:
            continue
        call_closing = _matching_delimiter(masked, call_opening, "(", ")")
        if call_closing is None:
            continue
        opening = masked.find("{", comma, call_closing)
        if opening != -1:
            closing = _matching_delimiter(masked, opening, "{", "}")
            if closing is None:
                continue
            body_start = opening + 1
            body_end = closing
            end_offset = closing
        else:
            arrow = masked.find("=>", comma, call_closing)
            if arrow == -1:
                continue
            body_start = arrow + 2
            while body_start < call_closing and source[body_start].isspace():
                body_start += 1
            body_end = call_closing
            end_offset = call_closing
        candidates.append(
            (
                match.group("kind"),
                _unescape_js_title(source[match.end() : title_end]),
                match.start(),
                body_start,
                body_end,
                end_offset,
            )
        )

    regions: list[TestRegion] = []
    suite_stack: list[tuple[str, int]] = []
    for kind, title, start, body_start, body_end, end_offset in sorted(
        candidates, key=lambda item: item[2]
    ):
        while suite_stack and start > suite_stack[-1][1]:
            suite_stack.pop()
        if kind in {"describe", "suite", "context"}:
            suite_stack.append((title, end_offset))
            continue
        titles = [part for part, _ in suite_stack] + [title]
        test_name = ":".join(titles) if titles else title
        regions.append(
            TestRegion(
                f"{relative_path}::{test_name}",
                _line(source, start),
                _line(source, end_offset),
                body_start,
                body_end,
            )
        )
    return regions


def collect_test_regions(
    source: str, relative_path: str, language: str
) -> list[TestRegion]:
    if language in {"javascript", "typescript"}:
        return _collect_js_test_regions(source, relative_path)
    masked = _mask_source(source)
    regions: list[TestRegion] = []
    if language == "cpp":
        pattern = re.compile(r"\bTEST(?:_F|_P)?\s*\(\s*(\w+)\s*,\s*(\w+)\s*\)\s*\{")
        for match in pattern.finditer(masked):
            opening = masked.rfind("{", match.start(), match.end())
            closing = _matching_delimiter(masked, opening, "{", "}")
            if closing is not None:
                regions.append(
                    TestRegion(
                        f"{relative_path}::{match.group(1)}.{match.group(2)}",
                        _line(source, match.start()),
                        _line(source, closing),
                        opening + 1,
                        closing,
                    )
                )
        return regions

    classes = _class_ranges(masked, language)
    pattern = re.compile(
        r"@(?:org\.junit(?:\.jupiter\.api)?\.)?(?:Test|ParameterizedTest|RepeatedTest)\b"
        r"[\s\S]*?(?:public|protected|private)?[ \t]*(?:static[ \t]+)?"
        r"(?:[\w<>\[\],.?]+[ \t]+)(\w+)[ \t]*\([^;{}]*\)"
        r"[ \t]*(?:throws[^{]+)?\{"
    )
    for match in pattern.finditer(masked):
        opening = masked.rfind("{", match.start(), match.end())
        closing = _matching_delimiter(masked, opening, "{", "}")
        if closing is None:
            continue
        owner = _owner_at(match.start(), classes)
        qualname = f"{owner}.{match.group(1)}" if owner else match.group(1)
        regions.append(
            TestRegion(
                f"{relative_path}::{qualname}",
                _line(source, match.start()),
                _line(source, closing),
                opening + 1,
                closing,
            )
        )
    return regions


def _looks_like_test(path: str, language: str) -> bool:
    parts = Path(path).parts
    lowered = {part.lower() for part in parts}
    raw_stem = Path(path).stem
    stem = raw_stem.lower()
    in_test_root = bool({"test", "tests", "unit_tests", "acceptance_tests"} & lowered)
    # Keep the established ``test`` prefix and admit its exact suffix
    # counterpart for every supported language.  Checking the beginning (or
    # the complete ``_test`` suffix) avoids incidental words such as
    # ``contest_utils`` becoming trusted tests merely because they contain
    # ``test``.
    conventional_stem = stem.startswith("test") or stem.endswith("_test")
    if language == "java":
        # Java repositories commonly use ``WidgetTest.java`` (and occasionally
        # ``WidgetTests.java``), in addition to the shared root/prefix/suffix
        # conventions above.
        in_test_root = in_test_root or "acceptancetest" in lowered
        conventional_stem = conventional_stem or raw_stem.endswith(("Test", "Tests"))
    if language in {"javascript", "typescript"}:
        in_test_root = in_test_root or "__tests__" in lowered or "spec" in lowered
        conventional_stem = (
            conventional_stem
            or stem.endswith(".test")
            or stem.endswith(".spec")
            or raw_stem.endswith(("Test", "Tests"))
        )
    return in_test_root or conventional_stem


def discover_test_paths(repository: Path, language: str) -> list[Path]:
    extensions = LANGUAGE_EXTENSIONS[language]
    return sorted(
        path
        for path in repository.rglob("*")
        if path.is_file()
        and path.suffix.lower() in extensions
        and not is_excluded_repository_path(path.relative_to(repository))
        and _looks_like_test(path.relative_to(repository).as_posix(), language)
    )


def _normalize_selector_text(value: str) -> str:
    value = value.strip()
    value = re.sub(r"\s+-\s+", ":", value)
    value = re.sub(r"\s*::\s*", "::", value)
    value = re.sub(r"\s*:\s*", ":", value)
    return re.sub(r"\s+", " ", value)


def _selector_matches(test_id: str, selectors: set[str], language: str) -> bool:
    """Match canonical IDs plus common Surefire and GoogleTest selectors."""

    if not selectors:
        return True
    path, _, qualname = test_id.partition("::")
    owner, _, method = qualname.rpartition(".")
    variants = {
        test_id,
        qualname,
        f"{owner}#{method}" if owner else method,
        f"{owner.rsplit('.', 1)[-1]}#{method}" if owner else method,
    }
    if language in {"javascript", "typescript"}:
        segments = [
            segment.strip() for segment in qualname.split(":") if segment.strip()
        ]
        if segments:
            variants.update(
                {
                    ":".join(segments),
                    " - ".join(segments),
                    " ".join(segments),
                    segments[-1],
                }
            )
        variants.add(path)
    if variants & selectors:
        return True
    normalized_variants = {_normalize_selector_text(variant) for variant in variants}
    normalized_selectors = {
        _normalize_selector_text(selector) for selector in selectors
    }
    if normalized_variants & normalized_selectors:
        return True
    normalized_qualname = qualname.replace("#", ".")
    return any(
        selector.replace("#", ".").endswith(normalized_qualname)
        for selector in selectors
    )


def _simple_type(value: str) -> str:
    stripped = value.strip()
    generic = re.match(r"(.+?)\s*<", stripped)
    if generic:
        container = generic.group(1).rsplit("::", 1)[-1]
        if container in {"unique_ptr", "shared_ptr", "weak_ptr"}:
            inner = stripped[stripped.find("<") + 1 : stripped.rfind(">")]
            value = _split_arguments(inner)[0]
        else:
            value = generic.group(1)
    value = re.sub(r"<[^<>]*>", "", value)
    return value.rsplit(".", 1)[-1].rsplit("::", 1)[-1].strip(" *&")


def _variable_types(masked: str, language: str) -> dict[str, str]:
    result: dict[str, str] = {}
    # Covers Java/C++ declarations, including fields and ``T x = new T(...)``.
    pattern = re.compile(
        r"\b(?P<type>[A-Za-z_]\w*(?:(?:\.|::)\w+)*(?:\s*<[^;={}()]+>)?)"
        r"(?:\s+|\s*[*&]\s*)(?P<name>[a-zA-Z_]\w*)\s*(?=[=;,){])"
    )
    for match in pattern.finditer(masked):
        result[match.group("name")] = _simple_type(match.group("type"))
    return result


def _argument_count(masked: str, opening: int) -> int | None:
    closing = _matching_delimiter(masked, opening, "(", ")")
    if closing is None:
        return None
    return _arity(masked[opening + 1 : closing])


def _argument_types(
    masked: str, opening: int, variables: dict[str, str]
) -> tuple[str, ...] | None:
    closing = _matching_delimiter(masked, opening, "(", ")")
    if closing is None:
        return None
    result: list[str] = []
    for argument in _split_arguments(masked[opening + 1 : closing]):
        new_match = re.match(r"\s*new\s+([A-Z]\w*)", argument)
        if new_match:
            result.append(new_match.group(1))
            continue
        # String contents are masked, but their delimiters are retained.
        if re.fullmatch(r'\s*"[ ]*"\s*', argument):
            result.append("String")
            continue
        if re.fullmatch(r"\s*\d+[lLfFdD]?\s*", argument):
            result.append("int")
            continue
        if re.fullmatch(r"\s*(?:true|false)\s*", argument):
            result.append("boolean")
            continue
        identifier = re.fullmatch(r"\s*(\w+)\s*", argument)
        result.append(variables.get(identifier.group(1), "") if identifier else "")
    return tuple(result)


def _relative_posix(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _add_definition(
    target: dict[str, list[_Definition]], key: str, definition: _Definition
) -> None:
    target.setdefault(key, []).append(definition)


def _add_member_definition(
    target: dict[str, dict[str, list[_Definition]]],
    owner: str,
    name: str,
    definition: _Definition,
) -> None:
    target.setdefault(owner, {}).setdefault(name, []).append(definition)


def _make_definition(
    source: str,
    relative_path: str,
    owner: str,
    name: str,
    start: int,
    closing: int,
) -> _Definition:
    return _Definition(
        path=relative_path,
        owner=owner,
        name=name,
        arity=None,
        parameter_types=(),
        start_line=_line(source, start),
        end_line=_line(source, closing),
    )


def _resolve_js_module(root: Path, source_path: Path, specifier: str) -> str | None:
    if not specifier.startswith("."):
        return None
    base = (source_path.parent / specifier).resolve()
    candidates: list[Path] = []
    extensions = LANGUAGE_EXTENSIONS["javascript"] | LANGUAGE_EXTENSIONS["typescript"]
    if base.suffix:
        candidates.append(base)
    else:
        candidates.extend(base / f"index{suffix}" for suffix in sorted(extensions))
        candidates.extend(Path(f"{base}{suffix}") for suffix in sorted(extensions))
    matches = []
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            relative = candidate.relative_to(root.resolve())
        except ValueError:
            continue
        if is_excluded_repository_path(relative):
            continue
        matches.append(relative.as_posix())
    unique = sorted(set(matches))
    return unique[0] if len(unique) == 1 else None


def _collect_js_class_info(
    source: str,
    masked: str,
    relative_path: str,
    local_callables: dict[str, list[_Definition]],
    class_constructors: dict[str, list[_Definition]],
    class_methods: dict[str, dict[str, list[_Definition]]],
) -> dict[str, list[str]]:
    class_mixins: dict[str, list[str]] = {}
    pattern = re.compile(
        r"(?m)^[ \t]*(?:export[ \t]+(?:default[ \t]+)?)?class[ \t]+"
        r"(?P<name>[A-Za-z_$]\w*)[^{\n]*\{"
    )
    for match in pattern.finditer(masked):
        name = match.group("name")
        opening = masked.find("{", match.start(), match.end())
        closing = _matching_delimiter(masked, opening, "{", "}")
        if closing is None:
            continue
        body = masked[opening + 1 : closing]
        offset = opening + 1
        method_pattern = re.compile(
            r"(?m)^[ \t]*(?:(?:public|protected|private|static|async|readonly|get|set)"
            r"[ \t]+)*(?P<name>[A-Za-z_$]\w*)[ \t]*\([^;{}]*\)[ \t]*\{"
        )
        for method_match in method_pattern.finditer(body):
            method_name = method_match.group("name")
            absolute_start = offset + method_match.start()
            method_opening = masked.find(
                "{", absolute_start, offset + method_match.end()
            )
            method_closing = _matching_delimiter(masked, method_opening, "{", "}")
            if method_closing is None:
                continue
            definition = _make_definition(
                source,
                relative_path,
                name,
                method_name,
                absolute_start,
                method_closing,
            )
            if method_name == "constructor":
                _add_definition(class_constructors, name, definition)
            else:
                _add_member_definition(class_methods, name, method_name, definition)
        constructor_match = re.search(
            r"(?m)^[ \t]*constructor[ \t]*\([^;{}]*\)[ \t]*\{", body
        )
        if constructor_match is not None:
            constructor_opening = masked.find(
                "{",
                offset + constructor_match.start(),
                offset + constructor_match.end(),
            )
            constructor_closing = _matching_delimiter(
                masked, constructor_opening, "{", "}"
            )
            if constructor_closing is None:
                continue
            constructor_body = source[constructor_opening + 1 : constructor_closing]
            assign_pattern = re.compile(
                r"Object\.assign\s*\(\s*this\s*,(?P<args>[^)]*)\)"
            )
            for assign_match in assign_pattern.finditer(constructor_body):
                aliases = [
                    token.strip()
                    for token in assign_match.group("args").split(",")
                    if re.fullmatch(r"[A-Za-z_$]\w*", token.strip())
                ]
                if aliases:
                    class_mixins.setdefault(name, []).extend(aliases)
    return class_mixins


def _collect_js_assignment_info(
    source: str,
    masked: str,
    relative_path: str,
    local_callables: dict[str, list[_Definition]],
    namespaces: dict[str, dict[str, list[_Definition]]],
    prototype_methods: dict[str, dict[str, list[_Definition]]],
) -> None:
    patterns = [
        re.compile(
            r"(?m)^[ \t]*(?:export[ \t]+)?(?:(?:const|let|var)[ \t]+)?"
            r"(?P<owner>[A-Za-z_$][\w$]*(?:\.(?:prototype|[A-Za-z_$][\w$]*))*)"
            r"\.(?P<name>[A-Za-z_$]\w*)[ \t]*=[ \t]*(?:async[ \t]+)?function\b"
            r"[^{]*\([^;{}]*\)[ \t]*\{"
        ),
        re.compile(
            r"(?m)^[ \t]*(?:export[ \t]+)?(?:(?:const|let|var)[ \t]+)?"
            r"(?P<owner>[A-Za-z_$][\w$]*(?:\.(?:prototype|[A-Za-z_$][\w$]*))*)"
            r"\.(?P<name>[A-Za-z_$]\w*)[ \t]*=[ \t]*(?:async[ \t]+)?"
            r"(?:\([^;{}]*\)|[A-Za-z_$]\w*)[ \t]*=>[ \t]*\{"
        ),
        re.compile(
            r"(?m)^[ \t]*(?:export[ \t]+)?(?:(?:const|let|var)[ \t]+)?"
            r"(?P<owner>[A-Za-z_$][\w$]*(?:\.(?:prototype|[A-Za-z_$][\w$]*))*)"
            r"\.(?P<name>[A-Za-z_$]\w*)[ \t]*=[ \t]*(?P<target>[A-Za-z_$]\w*)[ \t]*;"
        ),
    ]
    for pattern in patterns:
        for match in pattern.finditer(masked):
            owner = match.group("owner")
            name = match.group("name")
            opening = masked.find("{", match.start(), match.end())
            if opening == -1 and "target" not in match.groupdict():
                continue
            closing = (
                _matching_delimiter(masked, opening, "{", "}")
                if opening != -1
                else match.end()
            )
            if closing is None:
                continue
            if "target" in match.groupdict() and match.group("target"):
                referenced = local_callables.get(match.group("target"), [])
                for definition in referenced:
                    if owner.endswith(".prototype"):
                        _add_member_definition(
                            prototype_methods, owner.rsplit(".", 1)[0], name, definition
                        )
                    else:
                        _add_member_definition(namespaces, owner, name, definition)
                continue
            definition = _make_definition(
                source,
                relative_path,
                owner,
                name,
                match.start(),
                closing,
            )
            if owner.endswith(".prototype"):
                _add_member_definition(
                    prototype_methods, owner.rsplit(".", 1)[0], name, definition
                )
            else:
                _add_member_definition(namespaces, owner, name, definition)


def _collect_js_callable_info(
    source: str,
    masked: str,
    relative_path: str,
    local_callables: dict[str, list[_Definition]],
) -> None:
    patterns = [
        re.compile(
            r"(?m)^[ \t]*(?:export[ \t]+(?:default[ \t]+)?)?"
            r"(?:async[ \t]+)?function[ \t]+"
            r"(?P<name>[A-Za-z_$]\w*)[ \t]*\([^;{}]*\)[ \t]*\{"
        ),
        re.compile(
            r"(?m)^[ \t]*(?:export[ \t]+)?(?:const|let|var)[ \t]+"
            r"(?P<name>[A-Za-z_$]\w*)"
            r"[ \t]*=[ \t]*(?:async[ \t]+)?function\b[^{]*\([^;{}]*\)[ \t]*\{"
        ),
        re.compile(
            r"(?m)^[ \t]*(?:export[ \t]+)?(?:const|let|var)[ \t]+"
            r"(?P<name>[A-Za-z_$]\w*)"
            r"[ \t]*=[ \t]*(?:async[ \t]+)?(?:\([^;{}]*\)|[A-Za-z_$]\w*)"
            r"[ \t]*=>[ \t]*\{"
        ),
    ]
    for pattern in patterns:
        for match in pattern.finditer(masked):
            opening = masked.find("{", match.start(), match.end())
            if opening == -1:
                continue
            closing = _matching_delimiter(masked, opening, "{", "}")
            if closing is None:
                continue
            _add_definition(
                local_callables,
                match.group("name"),
                _make_definition(
                    source,
                    relative_path,
                    "",
                    match.group("name"),
                    match.start(),
                    closing,
                ),
            )


def _collect_js_module_object_info(
    source: str,
    masked: str,
    relative_path: str,
    namespaces: dict[str, dict[str, list[_Definition]]],
) -> None:
    pattern = re.compile(r"module\.exports[ \t]*=[ \t]*\{")
    for match in pattern.finditer(masked):
        opening = masked.find("{", match.start(), match.end())
        closing = _matching_delimiter(masked, opening, "{", "}")
        if closing is None:
            continue
        body = masked[opening + 1 : closing]
        offset = opening + 1
        property_patterns = [
            re.compile(r"(?m)^[ \t]*(?P<name>[A-Za-z_$]\w*)[ \t]*\([^;{}]*\)[ \t]*\{"),
            re.compile(
                r"(?m)^[ \t]*(?P<name>[A-Za-z_$]\w*)[ \t]*:[ \t]*(?:async[ \t]+)?"
                r"function\b[^{]*\([^;{}]*\)[ \t]*\{"
            ),
            re.compile(
                r"(?m)^[ \t]*(?P<name>[A-Za-z_$]\w*)[ \t]*:[ \t]*(?:async[ \t]+)?"
                r"(?:\([^;{}]*\)|[A-Za-z_$]\w*)[ \t]*=>[ \t]*\{"
            ),
        ]
        for property_pattern in property_patterns:
            for property_match in property_pattern.finditer(body):
                absolute_start = offset + property_match.start()
                property_opening = masked.find(
                    "{", absolute_start, offset + property_match.end()
                )
                property_closing = _matching_delimiter(
                    masked, property_opening, "{", "}"
                )
                if property_closing is None:
                    continue
                _add_member_definition(
                    namespaces,
                    "module.exports",
                    property_match.group("name"),
                    _make_definition(
                        source,
                        relative_path,
                        "module.exports",
                        property_match.group("name"),
                        absolute_start,
                        property_closing,
                    ),
                )


def _collect_js_raw_module(root: Path, path: Path) -> dict[str, object]:
    source = path.read_text(encoding="utf-8")
    masked = _mask_source(source)
    relative_path = _relative_posix(path, root)
    local_callables: dict[str, list[_Definition]] = {}
    class_constructors: dict[str, list[_Definition]] = {}
    class_methods: dict[str, dict[str, list[_Definition]]] = {}
    namespaces: dict[str, dict[str, list[_Definition]]] = {}
    prototype_methods: dict[str, dict[str, list[_Definition]]] = {}
    _collect_js_callable_info(source, masked, relative_path, local_callables)
    class_mixins = _collect_js_class_info(
        source,
        masked,
        relative_path,
        local_callables,
        class_constructors,
        class_methods,
    )
    _collect_js_assignment_info(
        source,
        masked,
        relative_path,
        local_callables,
        namespaces,
        prototype_methods,
    )
    _collect_js_module_object_info(source, masked, relative_path, namespaces)

    imports: dict[str, str] = {}
    import_pattern = re.compile(
        r"(?m)^[ \t]*import\s+(?P<alias>[A-Za-z_$]\w*)"
        r"(?:\s*,\s*\{[^}]*\})?\s+from\s+['\"](?P<spec>[^'\"]+)['\"]"
    )
    namespace_import_pattern = re.compile(
        r"(?m)^[ \t]*import\s+\*\s+as\s+(?P<alias>[A-Za-z_$]\w*)\s+from\s+['\"]"
        r"(?P<spec>[^'\"]+)['\"]"
    )
    require_pattern = re.compile(
        r"(?m)^[ \t]*(?:const|let|var)\s+(?P<alias>[A-Za-z_$]\w*)\s*=\s*"
        r"require\(\s*['\"](?P<spec>[^'\"]+)['\"]\s*\)"
    )
    for pattern in (import_pattern, namespace_import_pattern, require_pattern):
        for match in pattern.finditer(source):
            resolved = _resolve_js_module(root, path, match.group("spec"))
            if resolved is not None:
                imports[match.group("alias")] = resolved

    default_export: tuple[str, str] | None = None
    named_exports: dict[str, tuple[str, str]] = {}
    function_export_pattern = re.compile(
        r"(?m)^[ \t]*export\s+(?P<default>default\s+)?(?:async\s+)?function\s+"
        r"(?P<name>[A-Za-z_$]\w*)\b"
    )
    class_export_pattern = re.compile(
        r"(?m)^[ \t]*export\s+(?P<default>default\s+)?class\s+"
        r"(?P<name>[A-Za-z_$]\w*)\b"
    )
    const_export_pattern = re.compile(
        r"(?m)^[ \t]*export\s+(?:const|let|var)\s+(?P<name>[A-Za-z_$]\w*)\b"
    )
    for pattern, kind in (
        (function_export_pattern, "callable"),
        (class_export_pattern, "class"),
        (const_export_pattern, "callable"),
    ):
        for match in pattern.finditer(masked):
            name = match.group("name")
            if match.groupdict().get("default"):
                default_export = (kind, name)
            else:
                named_exports[name] = (kind, name)
    export_default_ref = re.search(
        r"(?m)^[ \t]*export\s+default\s+(?P<name>[A-Za-z_$]\w*)\s*;", masked
    )
    if export_default_ref is not None:
        name = export_default_ref.group("name")
        if name in namespaces:
            default_export = ("namespace", name)
        elif name in class_constructors:
            default_export = ("class", name)
        elif name in local_callables:
            default_export = ("callable", name)
    module_exports_ref = re.search(
        r"(?m)^[ \t]*module\.exports\s*=\s*(?P<name>[A-Za-z_$]\w*)\s*;", masked
    )
    if module_exports_ref is not None:
        name = module_exports_ref.group("name")
        if name in namespaces:
            default_export = ("namespace", name)
        elif name in class_constructors:
            default_export = ("class", name)
        elif name in local_callables:
            default_export = ("callable", name)
    if re.search(r"(?m)^[ \t]*module\.exports\s*=\s*\{", masked):
        default_export = ("namespace", "module.exports")

    return {
        "imports": imports,
        "default_export": default_export,
        "named_exports": named_exports,
        "local_callables": local_callables,
        "class_constructors": class_constructors,
        "class_methods": class_methods,
        "namespaces": namespaces,
        "prototype_methods": prototype_methods,
        "class_mixins": class_mixins,
    }


def _merge_definition_maps(
    target: dict[str, list[_Definition]], source: dict[str, list[_Definition]]
) -> None:
    for key, values in source.items():
        target.setdefault(key, []).extend(values)


def _finalize_js_module(
    path: str,
    raw_infos: dict[str, dict[str, object]],
    cache: dict[str, dict[str, object]],
    visiting: set[str],
) -> dict[str, object]:
    if path in cache:
        return cache[path]
    if path in visiting:
        return {
            "default_callable": [],
            "default_class": None,
            "default_namespace": {},
            "named_callables": {},
            "named_classes": {},
            "named_namespaces": {},
            "class_constructors": {},
            "class_methods": {},
            "prototype_methods": {},
        }
    visiting.add(path)
    raw = raw_infos[path]
    class_methods = {
        class_name: {name: list(definitions) for name, definitions in methods.items()}
        for class_name, methods in raw["class_methods"].items()
    }
    for class_name, aliases in raw["class_mixins"].items():
        for alias in aliases:
            imported_path = raw["imports"].get(alias)
            if imported_path is None or imported_path not in raw_infos:
                continue
            imported = _finalize_js_module(imported_path, raw_infos, cache, visiting)
            _merge_definition_maps(
                class_methods.setdefault(class_name, {}),
                imported["default_namespace"],
            )

    default_callable: list[_Definition] = []
    default_class: str | None = None
    default_namespace: dict[str, list[_Definition]] = {}
    named_callables: dict[str, list[_Definition]] = {}
    named_classes: dict[str, str] = {}
    named_namespaces: dict[str, dict[str, list[_Definition]]] = {}

    def resolve_ref(ref: tuple[str, str] | None):
        if ref is None:
            return None
        kind, name = ref
        if kind == "callable":
            return raw["local_callables"].get(name, [])
        if kind == "class":
            return name
        if kind == "namespace":
            return raw["namespaces"].get(name, {})
        return None

    resolved_default = resolve_ref(raw["default_export"])
    if isinstance(resolved_default, list):
        default_callable = resolved_default
    elif isinstance(resolved_default, str):
        default_class = resolved_default
    elif isinstance(resolved_default, dict):
        default_namespace = {
            name: list(definitions) for name, definitions in resolved_default.items()
        }

    for export_name, ref in raw["named_exports"].items():
        resolved = resolve_ref(ref)
        if isinstance(resolved, list):
            named_callables[export_name] = resolved
        elif isinstance(resolved, str):
            named_classes[export_name] = resolved
        elif isinstance(resolved, dict):
            named_namespaces[export_name] = {
                name: list(definitions) for name, definitions in resolved.items()
            }

    result = {
        "default_callable": default_callable,
        "default_class": default_class,
        "default_namespace": default_namespace,
        "named_callables": named_callables,
        "named_classes": named_classes,
        "named_namespaces": named_namespaces,
        "class_constructors": raw["class_constructors"],
        "class_methods": class_methods,
        "prototype_methods": raw["prototype_methods"],
    }
    visiting.remove(path)
    cache[path] = result
    return result


def _js_import_bindings(
    root: Path,
    path: Path,
    module_infos: dict[str, dict[str, object]],
) -> tuple[
    dict[str, list[_Definition]],
    dict[str, tuple[str, str]],
    dict[str, dict[str, list[_Definition]]],
]:
    source = path.read_text(encoding="utf-8")
    imported_callables: dict[str, list[_Definition]] = {}
    imported_classes: dict[str, tuple[str, str]] = {}
    imported_namespaces: dict[str, dict[str, list[_Definition]]] = {}

    for match in re.finditer(
        r"(?m)^[ \t]*import\s+(?P<alias>[A-Za-z_$]\w*)"
        r"(?:\s*,\s*\{(?P<named>[^}]*)\})?\s+from\s+['\"](?P<spec>[^'\"]+)['\"]",
        source,
    ):
        resolved = _resolve_js_module(root, path, match.group("spec"))
        if resolved is None or resolved not in module_infos:
            continue
        module_info = module_infos[resolved]
        alias = match.group("alias")
        if module_info["default_callable"]:
            imported_callables[alias] = module_info["default_callable"]
        elif module_info["default_class"] is not None:
            imported_classes[alias] = (resolved, module_info["default_class"])
        elif module_info["default_namespace"]:
            imported_namespaces[alias] = module_info["default_namespace"]
        named = match.group("named")
        if not named:
            continue
        for part in named.split(","):
            piece = part.strip()
            if not piece:
                continue
            left, _, right = piece.partition(" as ")
            export_name = left.strip()
            alias_name = (right or left).strip()
            if export_name in module_info["named_callables"]:
                imported_callables[alias_name] = module_info["named_callables"][
                    export_name
                ]
            elif export_name in module_info["named_classes"]:
                imported_classes[alias_name] = (
                    resolved,
                    module_info["named_classes"][export_name],
                )
            elif export_name in module_info["named_namespaces"]:
                imported_namespaces[alias_name] = module_info["named_namespaces"][
                    export_name
                ]

    for match in re.finditer(
        r"(?m)^[ \t]*import\s+\*\s+as\s+(?P<alias>[A-Za-z_$]\w*)\s+from\s+['\"]"
        r"(?P<spec>[^'\"]+)['\"]",
        source,
    ):
        resolved = _resolve_js_module(root, path, match.group("spec"))
        if resolved is None or resolved not in module_infos:
            continue
        module_info = module_infos[resolved]
        namespace = {
            **module_info["default_namespace"],
            **module_info["named_callables"],
        }
        if namespace:
            imported_namespaces[match.group("alias")] = namespace

    for match in re.finditer(
        r"(?m)^[ \t]*(?:const|let|var)\s+(?P<lhs>[^=]+?)\s*=\s*require\(\s*['\"]"
        r"(?P<spec>[^'\"]+)['\"]\s*\)",
        source,
    ):
        resolved = _resolve_js_module(root, path, match.group("spec"))
        if resolved is None or resolved not in module_infos:
            continue
        module_info = module_infos[resolved]
        lhs = match.group("lhs").strip()
        destructured = re.fullmatch(r"\{(?P<body>[^}]*)\}", lhs)
        if destructured:
            for part in destructured.group("body").split(","):
                piece = part.strip()
                if not piece:
                    continue
                left, _, right = piece.partition(":")
                export_name = left.strip()
                alias_name = (right or left).strip()
                if export_name in module_info["named_callables"]:
                    imported_callables[alias_name] = module_info["named_callables"][
                        export_name
                    ]
                elif export_name in module_info["named_classes"]:
                    imported_classes[alias_name] = (
                        resolved,
                        module_info["named_classes"][export_name],
                    )
                elif export_name in module_info["default_namespace"]:
                    imported_callables[alias_name] = module_info["default_namespace"][
                        export_name
                    ]
            continue
        alias = lhs
        if module_info["default_callable"]:
            imported_callables[alias] = module_info["default_callable"]
        elif module_info["default_class"] is not None:
            imported_classes[alias] = (resolved, module_info["default_class"])
        elif module_info["default_namespace"]:
            imported_namespaces[alias] = module_info["default_namespace"]

    return imported_callables, imported_classes, imported_namespaces


def _seed_js_receiver_types(
    source: str,
    masked: str,
    imported_classes: dict[str, tuple[str, str]],
) -> dict[str, tuple[str | None, str]]:
    result: dict[str, tuple[str | None, str]] = {}
    assignment_pattern = re.compile(
        r"(?m)(?:^|[;{(])\s*(?:(?:const|let|var)\s+)?(?P<target>[A-Za-z_$]\w*)\s*=\s*"
        r"new\s+(?P<class_name>[A-Za-z_$]\w*)\s*\("
    )
    for match in assignment_pattern.finditer(masked):
        class_binding = imported_classes.get(match.group("class_name"))
        if class_binding is not None:
            result[match.group("target")] = class_binding
    for match in re.finditer(
        r"new\s+p5\s*\(\s*function\s*\(\s*(?P<param>[A-Za-z_$]\w*)\s*\)\s*\{",
        masked,
    ):
        opening = masked.find("{", match.start(), match.end())
        closing = _matching_delimiter(masked, opening, "{", "}")
        if closing is None:
            continue
        callback_body = masked[opening + 1 : closing]
        assign_pattern = re.compile(
            rf"(?m)(?:^|[;{{(])\s*(?P<target>[A-Za-z_$]\w*)\s*=\s*{match.group('param')}\s*;"
        )
        for assign_match in assign_pattern.finditer(callback_body):
            result[assign_match.group("target")] = (None, "p5")
    return result


def _unique_definitions(definitions: list[_Definition]) -> list[_Definition]:
    return definitions if len(definitions) == 1 else []


def _infer_js_entries(
    root: Path,
    selected: list[Path],
    requested: set[str],
    module_infos: dict[str, dict[str, object]],
    prototype_methods: dict[str, dict[str, list[_Definition]]],
    language: str,
) -> set[tuple[str, str, str, int, int]]:
    entries: set[tuple[str, str, str, int, int]] = set()
    for path in selected:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        relative = _relative_posix(path, root)
        masked = _mask_source(source)
        imported_callables, imported_classes, imported_namespaces = _js_import_bindings(
            root, path, module_infos
        )
        file_receiver_types = _seed_js_receiver_types(source, masked, imported_classes)
        for region in collect_test_regions(source, relative, language):
            if not _selector_matches(region.test_id, requested, language):
                continue
            body_source = source[region.body_start : region.body_end]
            body_masked = masked[region.body_start : region.body_end]
            receiver_types = dict(file_receiver_types)
            receiver_types.update(
                _seed_js_receiver_types(body_source, body_masked, imported_classes)
            )
            for match in re.finditer(
                r"(?<![.$\w])(?P<name>[A-Za-z_$]\w*)\s*\(", body_masked
            ):
                name = match.group("name")
                if name in {
                    "if",
                    "for",
                    "while",
                    "switch",
                    "catch",
                    "function",
                    "describe",
                    "suite",
                    "context",
                    "test",
                    "it",
                    "expect",
                    "assert",
                    "require",
                }:
                    continue
                for definition in _unique_definitions(imported_callables.get(name, [])):
                    entries.add(
                        (
                            region.test_id,
                            definition.path,
                            definition.symbol,
                            definition.start_line,
                            definition.end_line,
                        )
                    )
            for match in re.finditer(
                r"new\s+(?P<name>[A-Za-z_$]\w*)\s*\(", body_masked
            ):
                class_binding = imported_classes.get(match.group("name"))
                if class_binding is None:
                    continue
                module_path, class_name = class_binding
                for definition in _unique_definitions(
                    module_infos[module_path]["class_constructors"].get(class_name, [])
                ):
                    entries.add(
                        (
                            region.test_id,
                            definition.path,
                            definition.symbol,
                            definition.start_line,
                            definition.end_line,
                        )
                    )
            for match in re.finditer(
                r"(?<!\.)\b(?P<receiver>[A-Za-z_$]\w*)\.(?P<name>[A-Za-z_$]\w*)\s*\(",
                body_masked,
            ):
                receiver = match.group("receiver")
                name = match.group("name")
                definitions = imported_namespaces.get(receiver, {}).get(name, [])
                if not definitions and receiver in receiver_types:
                    module_path, class_name = receiver_types[receiver]
                    if module_path is None:
                        definitions = prototype_methods.get(class_name, {}).get(
                            name, []
                        )
                    else:
                        definitions = (
                            module_infos[module_path]["class_methods"]
                            .get(class_name, {})
                            .get(name, [])
                        )
                        if not definitions:
                            definitions = (
                                module_infos[module_path]["prototype_methods"]
                                .get(class_name, {})
                                .get(name, [])
                            )
                for definition in _unique_definitions(definitions):
                    entries.add(
                        (
                            region.test_id,
                            definition.path,
                            definition.symbol,
                            definition.start_line,
                            definition.end_line,
                        )
                    )
    return entries


def infer_polyglot_test_entries(
    repository: Path | str,
    test_paths: Iterable[Path | str] | None = None,
    *,
    test_ids: Iterable[str] | None = None,
    language: str,
):
    """Infer directly invoked polyglot production definitions."""

    from testprism.entry_inference import InferenceResult

    root = Path(repository).resolve()
    language = normalize_language(language)
    if language not in {"java", "cpp", "javascript", "typescript"}:
        raise ValueError(f"unsupported polyglot inference language: {language}")
    if not root.is_dir():
        raise ValueError(f"repository is not a directory: {repository}")
    extensions = LANGUAGE_EXTENSIONS[language]
    if test_paths is None:
        selected = discover_test_paths(root, language)
    else:
        selected = []
        for raw in test_paths:
            path = Path(raw)
            path = path if path.is_absolute() else root / path
            if path.is_dir():
                selected.extend(
                    p
                    for p in path.rglob("*")
                    if p.suffix.lower() in extensions
                    and not is_excluded_repository_path(p.relative_to(root))
                )
            elif (
                path.is_file()
                and path.suffix.lower() in extensions
                and not is_excluded_repository_path(path.relative_to(root))
            ):
                selected.append(path)
    selected = sorted({path.resolve() for path in selected})
    selected_rel = {path.relative_to(root).as_posix() for path in selected}
    requested = {str(value).strip() for value in test_ids or ()}
    if language in {"javascript", "typescript"}:
        raw_infos: dict[str, dict[str, object]] = {}
        for path in root.rglob("*"):
            if (
                path.is_file()
                and path.suffix.lower() in extensions
                and not is_excluded_repository_path(path.relative_to(root))
                and path.relative_to(root).as_posix() not in selected_rel
                and not _looks_like_test(path.relative_to(root).as_posix(), language)
            ):
                try:
                    raw_infos[_relative_posix(path, root)] = _collect_js_raw_module(
                        root, path
                    )
                except (OSError, UnicodeDecodeError):
                    continue
        module_infos: dict[str, dict[str, object]] = {}
        for module_path in list(raw_infos):
            _finalize_js_module(module_path, raw_infos, module_infos, set())
        prototype_methods: dict[str, dict[str, list[_Definition]]] = {}
        for module_info in module_infos.values():
            for owner, methods in module_info["prototype_methods"].items():
                _merge_definition_maps(prototype_methods.setdefault(owner, {}), methods)
        entries = _infer_js_entries(
            root,
            selected,
            requested,
            module_infos,
            prototype_methods,
            language,
        )
        metadata = TestEntryInferences(
            language=language,
            entries=[
                TestEntry(
                    test_id=a,
                    path=b,
                    symbol=f"{b}:{c}",
                    start_line=d,
                    end_line=e,
                )
                for a, b, c, d, e in sorted(entries)
            ],
        )
        return InferenceResult(metadata=metadata)

    definitions: list[_Definition] = []
    for path in root.rglob("*"):
        if (
            path.is_file()
            and path.suffix.lower() in extensions
            and not is_excluded_repository_path(path.relative_to(root))
            and path.relative_to(root).as_posix() not in selected_rel
            and not _looks_like_test(path.relative_to(root).as_posix(), language)
        ):
            try:
                definitions.extend(_definitions(path, root, language))
            except (OSError, UnicodeDecodeError):
                continue

    by_owner_name: dict[tuple[str, str], list[_Definition]] = {}
    for definition in definitions:
        owner = _simple_type(definition.owner)
        by_owner_name.setdefault((owner, definition.name), []).append(definition)
    entries: set[tuple[str, str, str, int, int]] = set()
    for path in selected:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        relative = path.relative_to(root).as_posix()
        masked = _mask_source(source)
        file_types = _variable_types(masked, language)
        for region in collect_test_regions(source, relative, language):
            if not _selector_matches(region.test_id, requested, language):
                continue
            body = masked[region.body_start : region.body_end]
            types = dict(file_types)
            types.update(_variable_types(body, language))
            calls: list[tuple[str, str, int | None, tuple[str, ...] | None]] = []
            for match in re.finditer(r"\b(\w+)\s*(?:\.|->)\s*(\w+)\s*\(", body):
                receiver, method = match.group(1), match.group(2)
                owner = types.get(receiver, receiver if receiver[:1].isupper() else "")
                if owner:
                    opening = region.body_start + match.end() - 1
                    calls.append(
                        (
                            _simple_type(owner),
                            method,
                            _argument_count(masked, opening),
                            _argument_types(masked, opening, types),
                        )
                    )
            if language == "cpp":
                # Namespace-qualified free functions (for example
                # ``q2::read_file``) are direct production calls too.  Keep
                # template arguments out of the method name while preserving
                # the opening parenthesis used for arity/type checks.
                for match in re.finditer(
                    r"\b([A-Za-z_]\w*(?:::\w+)*)\s*::\s*(\w+)"
                    r"\s*(?:<[^;(){}]*>)?\s*\(",
                    body,
                ):
                    opening = region.body_start + match.end() - 1
                    calls.append(
                        (
                            _simple_type(match.group(1)),
                            match.group(2),
                            _argument_count(masked, opening),
                            _argument_types(masked, opening, types),
                        )
                    )
            for match in re.finditer(r"\bnew\s+([A-Z]\w*)\s*\(", body):
                opening = region.body_start + match.end() - 1
                calls.append(
                    (
                        match.group(1),
                        match.group(1),
                        _argument_count(masked, opening),
                        _argument_types(masked, opening, types),
                    )
                )
            if language == "cpp":
                for match in re.finditer(
                    r"\b([A-Z]\w*)(?:\s*<[^;(){}]+>)?\s+\w+\s*\(", body
                ):
                    opening = region.body_start + match.end() - 1
                    calls.append(
                        (
                            match.group(1),
                            match.group(1),
                            _argument_count(masked, opening),
                            _argument_types(masked, opening, types),
                        )
                    )
                for match in re.finditer(r"(?<![.>:])\b([a-z_]\w*)\s*\(", body):
                    opening = region.body_start + match.end() - 1
                    calls.append(
                        (
                            "",
                            match.group(1),
                            _argument_count(masked, opening),
                            _argument_types(masked, opening, types),
                        )
                    )
            for owner, name, arity, argument_types in calls:
                if language == "cpp" and not owner:
                    # An unqualified free call can bind either a global
                    # definition or an equally named namespace definition.
                    # Without include/using/ADL context, considering all
                    # owners is safer than silently choosing the global one.
                    candidates = [
                        definition
                        for (
                            candidate_owner,
                            candidate_name,
                        ), definitions_for_name in by_owner_name.items()
                        if candidate_name == name
                        for definition in definitions_for_name
                    ]
                else:
                    candidates = by_owner_name.get((owner, name), [])
                exact = [
                    item for item in candidates if arity is None or item.arity == arity
                ]
                if len(exact) > 1 and argument_types and any(argument_types):
                    exact = [
                        item
                        for item in exact
                        if len(item.parameter_types) == len(argument_types)
                        and all(
                            not actual or actual == expected
                            for actual, expected in zip(
                                argument_types, item.parameter_types, strict=True
                            )
                        )
                    ]
                resolved = exact if len(exact) == 1 else []
                for definition in resolved:
                    entries.add(
                        (
                            region.test_id,
                            definition.path,
                            definition.symbol,
                            definition.start_line,
                            definition.end_line,
                        )
                    )
    metadata = TestEntryInferences(
        language=language,
        entries=[
            TestEntry(test_id=a, path=b, symbol=f"{b}:{c}", start_line=d, end_line=e)
            for a, b, c, d, e in sorted(entries)
        ],
    )
    return InferenceResult(metadata=metadata)
