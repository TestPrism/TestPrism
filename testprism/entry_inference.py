"""Static inference of production entry points from trusted Python tests.

The inference performed in this module is deliberately conservative.  It builds
an index from Python source files and follows imports and a small amount of local
assignment information; it never imports the repository or evaluates any of its
objects.  Calls that cannot be resolved to exactly one production definition are
reported as diagnostics and are not emitted as entries.

The public metadata model lives in :mod:`testprism.handlers.trial_handler` so
that task loading and preprocessing use the same validation contract.  This
module owns the AST analysis and exposes a small result object containing the
validated metadata plus diagnostics for auditability.
"""

from __future__ import annotations

import ast
import builtins
import keyword as keyword_module
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from testprism.handlers.trial_handler import (
    TestEntry,
    TestEntryInferences,
)
from testprism.polyglot_entry_inference import is_excluded_repository_path

SCHEMA_VERSION = 1
LANGUAGE = "python"
SOURCE = "static_test_invocation"


@dataclass(frozen=True, slots=True)
class InferenceDiagnostic:
    """A call which was intentionally omitted from inferred metadata."""

    test_id: str
    line: int | None
    expression: str
    reason: str

    @property
    def message(self) -> str:
        location = f"{self.test_id}:{self.line}" if self.line else self.test_id
        return f"{location}: omitted {self.expression!r} ({self.reason})"

    def as_dict(self) -> dict[str, object]:
        return {
            "test_id": self.test_id,
            "line": self.line,
            "expression": self.expression,
            "reason": self.reason,
            "message": self.message,
        }

    def __getitem__(self, key: str) -> object:
        """Allow dictionary-style diagnostics in lightweight integrations."""

        return self.as_dict()[key]


@dataclass(frozen=True, slots=True)
class InferenceResult:
    """Validated metadata and diagnostics produced by static inference."""

    metadata: TestEntryInferences
    diagnostics: tuple[InferenceDiagnostic, ...] = field(default_factory=tuple)

    @property
    def entries(self) -> list[TestEntry]:
        """Return the inferred entries as a list for convenient callers."""

        return list(self.metadata.entries)

    @property
    def test_entry_inferences(self) -> TestEntryInferences:
        """Compatibility alias used by preprocessing callers."""

        return self.metadata

    @property
    def schema_version(self) -> int:
        return self.metadata.schema_version

    @property
    def language(self) -> str:
        return self.metadata.language

    @property
    def source(self) -> str:
        return self.metadata.source

    def as_dict(self) -> dict[str, object]:
        return self.metadata.model_dump(mode="json")

    to_dict = as_dict

    def model_dump(self, *args: object, **kwargs: object) -> dict[str, object]:
        """Delegate metadata serialization for callers expecting a model API."""

        return self.metadata.model_dump(*args, **kwargs)

    def diagnostics_as_dict(self) -> list[dict[str, object]]:
        return [diagnostic.as_dict() for diagnostic in self.diagnostics]


@dataclass(frozen=True, slots=True)
class _Definition:
    path: str
    qualname: str
    kind: str
    start_line: int
    end_line: int


@dataclass(frozen=True, slots=True)
class _Module:
    path: str
    module_names: tuple[str, ...]
    tree: ast.Module
    is_test: bool


@dataclass(frozen=True, slots=True)
class _SymbolRef:
    definition: _Definition


@dataclass(frozen=True, slots=True)
class _ModuleRef:
    module_name: str
    paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _InstanceRef:
    class_ref: _SymbolRef


@dataclass(frozen=True, slots=True)
class _FrameworkInstanceRef:
    """A framework-created instance with statically bounded inheritance."""

    class_ref: _SymbolRef


@dataclass(frozen=True, slots=True)
class _UnknownRef:
    reason: str = "unresolved"


@dataclass(frozen=True, slots=True)
class _AmbiguousRef:
    reason: str = "ambiguous"


@dataclass(frozen=True, slots=True)
class _TestHelperRef:
    """A reference to a helper defined in the selected test module.

    ``definition`` is ``None`` for an imported/test-module symbol which the
    source index can identify as test code but which is intentionally outside
    the bounded same-module helper closure.  Keeping that distinction here
    lets callers inline only helpers for which we have the defining AST node.
    """

    definition: "_TestHelperDefinition | None" = None
    reason: str = "test/helper call"


@dataclass(frozen=True, slots=True)
class _TestHelperDefinition:
    """One statically indexed helper function/method in a test module."""

    path: str
    qualname: str
    node: ast.FunctionDef | ast.AsyncFunctionDef
    owner: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _FixtureDefinition:
    """One lexically proven pytest fixture applicable to a selected test."""

    path: str
    name: str
    node: ast.FunctionDef | ast.AsyncFunctionDef
    owner: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _ModuleSetupCall:
    """One lexically proven module-global production factory call."""

    target: _SymbolRef
    result: _Ref


_Ref = (
    _SymbolRef
    | _ModuleRef
    | _InstanceRef
    | _FrameworkInstanceRef
    | _UnknownRef
    | _AmbiguousRef
    | _TestHelperRef
)


@dataclass(frozen=True, slots=True)
class _RequestedTestSelector:
    path: str
    qualname: str
    raw: str


def _is_test_path(path: str) -> bool:
    """Return whether a repository-relative path looks like test/helper code."""

    pure_parts = Path(path).parts
    lower_parts = {part.lower() for part in pure_parts}
    filename = pure_parts[-1].lower() if pure_parts else ""
    stem = filename.rsplit(".", 1)[0]
    return bool(
        {"tests", "test"} & lower_parts
        or stem.startswith("test")
        or stem.endswith("_test")
        or stem == "conftest"
    )


def _module_name_for_path(path: str) -> str:
    parts = list(Path(path).parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = Path(parts[-1]).stem
    return ".".join(parts)


def _importable_suffix_module_name(path: str) -> str | None:
    """Return the module suffix after a syntactically invalid leading prefix.

    A filesystem component which is not a legal Python import identifier cannot
    be part of a dotted import.  Dropping one or more such *leading* components
    is therefore syntactically justified.  Once a valid component has started
    the candidate suffix, every remaining component must also be valid; this
    deliberately avoids guessing that a valid monorepo directory is a source
    root or skipping an invalid component from the middle of an import name.
    """

    module_name = _module_name_for_path(path)
    parts = module_name.split(".") if module_name else []
    invalid_prefix_length = 0
    for part in parts:
        if part.isidentifier() and not keyword_module.iskeyword(part):
            break
        invalid_prefix_length += 1
    if invalid_prefix_length == 0 or invalid_prefix_length == len(parts):
        return None
    suffix = parts[invalid_prefix_length:]
    if not all(
        part.isidentifier() and not keyword_module.iskeyword(part) for part in suffix
    ):
        return None
    return ".".join(suffix)


def _safe_relative_path(path: Path, root: Path) -> str | None:
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    if relative == Path("."):
        return None
    # Metadata paths are POSIX paths.  A source checkout on Windows is still
    # represented with forward slashes, and backslashes in a source name are
    # rejected by the schema itself.
    return relative.as_posix()


def _strip_parametrization(value: str) -> str:
    head, _separator, _tail = value.partition("[")
    return head.strip()


def _resolve_selector_module_path(
    root: Path,
    module_name: str,
) -> str | None:
    candidates: list[str] = []
    for candidate in root.rglob("*.py"):
        relative = _safe_relative_path(candidate, root)
        if relative is None or not _is_test_path(relative):
            continue
        if _module_name_for_path(relative) == module_name:
            candidates.append(relative)
    unique = sorted(set(candidates))
    if len(unique) != 1:
        return None
    return unique[0]


def _resolve_requested_test_selectors(
    root: Path,
    raw_test_ids: Iterable[str],
) -> tuple[dict[str, set[str]], list[InferenceDiagnostic]]:
    selectors: dict[str, set[str]] = defaultdict(set)
    diagnostics: list[InferenceDiagnostic] = []
    unittest_pattern = re.compile(r"^(?P<name>.+?)\s+\((?P<target>[^)]+)\)$")

    for raw_selector in raw_test_ids:
        selector = str(raw_selector).strip()
        if not selector:
            continue
        resolved_path: str | None = None
        qualname: str | None = None

        if "::" in selector:
            parts = [part.strip() for part in selector.split("::") if part.strip()]
            if parts and parts[0].endswith(".py"):
                resolved_path = _normalise_selector_path(parts[0])
                if not _selector_path_is_safe(resolved_path):
                    resolved_path = None
                if len(parts) >= 2:
                    qual_parts = parts[1:]
                    qual_parts[-1] = _strip_parametrization(qual_parts[-1])
                    qualname = ".".join(part for part in qual_parts if part)
        else:
            match = unittest_pattern.match(selector)
            if match is not None:
                test_name = _strip_parametrization(match.group("name"))
                target = match.group("target").strip()
                target_parts = [part for part in target.split(".") if part]
                if len(target_parts) >= 2:
                    class_name = target_parts[-1]
                    module_name = ".".join(target_parts[:-1])
                    resolved_path = _resolve_selector_module_path(root, module_name)
                    qualname = f"{class_name}.{test_name}" if class_name else test_name

        if resolved_path is None or qualname is None:
            diagnostics.append(
                InferenceDiagnostic(
                    test_id=selector,
                    line=None,
                    expression=selector,
                    reason="requested test selector is not statically resolvable",
                )
            )
            continue
        selectors[resolved_path].add(qualname)
    return selectors, diagnostics


def _normalise_selector_path(path: str) -> str:
    normalised = path.replace("\\", "/")
    while normalised.startswith("./"):
        normalised = normalised[2:]
    return normalised


def _selector_path_is_safe(path: str) -> bool:
    """Return whether a selector names a repository-relative Python path."""

    candidate = Path(path)
    return (
        not candidate.is_absolute()
        and candidate.suffix.lower() in {".py", ".pyi"}
        and ".." not in candidate.parts
    )


class _ScopeBindingCollector(ast.NodeVisitor):
    """Collect names bound in one lexical scope without entering child scopes."""

    def __init__(self) -> None:
        self.bindings: dict[str, list[str]] = defaultdict(list)
        self.control_depth = 0

    def _bind(self, name: str, kind: str = "other") -> None:
        self.bindings[name].append(kind)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self._bind(node.id)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._bind(alias.asname or alias.name.split(".", 1)[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name == "*":
                self._bind("*")
                continue
            local_name = alias.asname or alias.name
            kind = (
                "overload"
                if self.control_depth == 0
                and node.level == 0
                and node.module in {"typing", "typing_extensions"}
                and alias.name == "overload"
                else "other"
            )
            self._bind(local_name, kind)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._bind(node.name)
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                self.visit(default)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._bind(node.name)
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def _visit_control_flow(self, node: ast.AST) -> None:
        self.control_depth += 1
        self.generic_visit(node)
        self.control_depth -= 1

    visit_If = _visit_control_flow
    visit_For = _visit_control_flow
    visit_AsyncFor = _visit_control_flow
    visit_While = _visit_control_flow
    visit_Try = _visit_control_flow
    visit_TryStar = _visit_control_flow
    visit_With = _visit_control_flow
    visit_AsyncWith = _visit_control_flow
    visit_Match = _visit_control_flow

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self._bind(node.name)
        for statement in node.body:
            self.visit(statement)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name:
            self._bind(node.name)
        if node.pattern is not None:
            self.visit(node.pattern)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if node.name:
            self._bind(node.name)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest:
            self._bind(node.rest)
        self.generic_visit(node)


def _scope_bindings(statements: Sequence[ast.stmt]) -> dict[str, list[str]]:
    collector = _ScopeBindingCollector()
    for statement in statements:
        collector.visit(statement)
    return collector.bindings


def _function_scope_bindings(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> dict[str, list[str]]:
    bindings = _scope_bindings(node.body)
    arguments = (
        *node.args.posonlyargs,
        *node.args.args,
        *node.args.kwonlyargs,
    )
    for argument in arguments:
        bindings.setdefault(argument.arg, []).append("other")
    if node.args.vararg is not None:
        bindings.setdefault(node.args.vararg.arg, []).append("other")
    if node.args.kwarg is not None:
        bindings.setdefault(node.args.kwarg.arg, []).append("other")
    return bindings


class _DefinitionCollector(ast.NodeVisitor):
    """Collect classes, functions, and methods with inclusive AST spans."""

    def __init__(self, path: str, tree: ast.Module) -> None:
        self.path = path
        self.scope: list[str] = []
        module_bindings = _scope_bindings(tree.body)
        self.overload_aliases = (
            {}
            if "*" in module_bindings
            else {
                alias.asname or alias.name: statement.lineno
                for statement in tree.body
                if isinstance(statement, ast.ImportFrom)
                and statement.level == 0
                and statement.module in {"typing", "typing_extensions"}
                for alias in statement.names
                if alias.name == "overload"
                for local_name in [alias.asname or alias.name]
                if module_bindings[local_name] == ["overload"]
            }
        )
        self.literal_false_flags = (
            {}
            if "*" in module_bindings
            else {
                target.id: statement.lineno
                for statement in tree.body
                if isinstance(statement, ast.Assign)
                and len(statement.targets) == 1
                and isinstance(target := statement.targets[0], ast.Name)
                and isinstance(statement.value, ast.Constant)
                and statement.value.value is False
                and module_bindings[target.id] == ["other"]
            }
        )
        self.enclosing_bindings: list[dict[str, list[str]]] = []
        self.class_depth = 0
        self.function_depth = 0
        self.definitions: list[_Definition] = []

    @staticmethod
    def _end_line(node: ast.AST) -> int:
        return int(getattr(node, "end_lineno", None) or getattr(node, "lineno", 1))

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qualname = ".".join((*self.scope, node.name))
        self.definitions.append(
            _Definition(
                path=self.path,
                qualname=qualname,
                kind="class",
                start_line=node.lineno,
                end_line=self._end_line(node),
            )
        )
        self.scope.append(node.name)
        self.class_depth += 1
        self.enclosing_bindings.append(_scope_bindings(node.body))
        self.generic_visit(node)
        self.enclosing_bindings.pop()
        self.class_depth -= 1
        self.scope.pop()

    def _is_proven_overload_stub(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> bool:
        declaration_only = len(node.body) == 1 and (
            isinstance(node.body[0], ast.Pass)
            or (
                isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and node.body[0].value.value is Ellipsis
            )
        )
        if not declaration_only:
            return False
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Name):
                continue
            import_line = self.overload_aliases.get(decorator.id)
            if import_line is None or import_line >= node.lineno:
                continue
            if any(decorator.id in bindings for bindings in self.enclosing_bindings):
                continue
            return True
        return False

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        qualname = ".".join((*self.scope, node.name))
        if not self._is_proven_overload_stub(node):
            self.definitions.append(
                _Definition(
                    path=self.path,
                    qualname=qualname,
                    kind="method" if self.class_depth else "function",
                    start_line=node.lineno,
                    end_line=self._end_line(node),
                )
            )
        self.scope.append(node.name)
        self.function_depth += 1
        self.enclosing_bindings.append(_function_scope_bindings(node))
        self.generic_visit(node)
        self.enclosing_bindings.pop()
        self.function_depth -= 1
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_If(self, node: ast.If) -> None:
        if (
            self.function_depth == 0
            and isinstance(node.test, ast.Name)
            and (assignment_line := self.literal_false_flags.get(node.test.id))
            is not None
            and assignment_line < node.lineno
            and not any(
                node.test.id in bindings for bindings in self.enclosing_bindings
            )
        ):
            for statement in node.orelse:
                self.visit(statement)
            return
        self.generic_visit(node)


class _SourceIndex:
    """Repository source index used by the resolver."""

    def __init__(self, root: Path, test_paths: set[str]) -> None:
        self.root = root.resolve()
        self.modules: dict[str, _Module] = {}
        self.module_paths: dict[str, set[str]] = defaultdict(set)
        self.definitions: dict[tuple[str, str], list[_Definition]] = defaultdict(list)
        self.top_level: dict[tuple[str, str], list[_Definition]] = defaultdict(list)
        self.import_bindings: dict[tuple[str, str], list[tuple[str, str, int]]] = (
            defaultdict(list)
        )
        self.wildcard_import_bindings: dict[tuple[str, str], list[int]] = defaultdict(
            list
        )
        self.diagnostics: list[InferenceDiagnostic] = []
        self._test_paths = test_paths
        self._build()

    def _build(self) -> None:
        for source_path in sorted(self.root.rglob("*.py")):
            if not source_path.is_file():
                continue
            relative = _safe_relative_path(source_path, self.root)
            if relative is None or is_excluded_repository_path(relative):
                continue
            try:
                source = source_path.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=relative)
            except (OSError, UnicodeDecodeError, SyntaxError) as exc:
                # A malformed production file cannot safely contribute a symbol;
                # callers still receive a diagnostic rather than an import error.
                self.diagnostics.append(
                    InferenceDiagnostic(
                        test_id="<repository>",
                        line=None,
                        expression=relative,
                        reason=f"cannot parse source: {exc.__class__.__name__}",
                    )
                )
                continue

            is_test = relative in self._test_paths or _is_test_path(relative)
            module_name = _module_name_for_path(relative)
            # Multiple aliases are useful for src/ layouts.  ``src`` is a
            # source-root convention, not part of Python's import name.
            aliases = {module_name}
            path_parts = Path(relative).parts
            if path_parts and path_parts[0] in {"src", "lib", "workspace"}:
                aliases.add(_module_name_for_path("/".join(path_parts[1:])))
            importable_suffix = _importable_suffix_module_name(relative)
            if importable_suffix is not None:
                aliases.add(importable_suffix)
            module = _Module(
                path=relative,
                module_names=tuple(sorted(alias for alias in aliases if alias)),
                tree=tree,
                is_test=is_test,
            )
            for alias in aliases:
                if alias:
                    self.module_paths[alias].add(relative)
            self.modules[relative] = module

            collector = _DefinitionCollector(relative, tree)
            try:
                collector.visit(tree)
            except RecursionError:
                # One generated or expression-heavy module must not prevent
                # useful inference from the rest of a large repository.
                self.diagnostics.append(
                    InferenceDiagnostic(
                        test_id="<repository>",
                        line=None,
                        expression=relative,
                        reason="cannot index source: RecursionError",
                    )
                )
                continue
            for definition in collector.definitions:
                self.definitions[(relative, definition.qualname)].append(definition)
                if "." not in definition.qualname:
                    self.top_level[(relative, definition.qualname)].append(definition)
            for statement in tree.body:
                if not isinstance(statement, ast.ImportFrom):
                    continue
                if any(alias.name == "*" for alias in statement.names):
                    if statement.module:
                        wildcard_key = (relative, statement.module)
                        self.wildcard_import_bindings[wildcard_key].append(
                            statement.level
                        )
                    continue
                for alias in statement.names:
                    local_name = alias.asname or alias.name
                    self.import_bindings[(relative, local_name)].append(
                        (statement.module or "", alias.name, statement.level)
                    )

    def resolve_module(
        self,
        module_name: str,
        importer_path: str,
        level: int = 0,
    ) -> tuple[str, ...]:
        if level:
            importer_module = _module_name_for_path(importer_path).split(".")
            if importer_path.endswith("/__init__.py"):
                base = importer_module
            else:
                base = importer_module[:-1]
            if level > len(base) + 1:
                return ()
            base = base[: len(base) - (level - 1)]
            module_name = (
                ".".join((*base, module_name)) if module_name else ".".join(base)
            )
        return tuple(sorted(self.module_paths.get(module_name, ())))

    def resolve_import(
        self,
        module_name: str,
        imported_name: str,
        importer_path: str,
        level: int = 0,
        _visited: set[tuple[str, str]] | None = None,
    ) -> _Ref:
        visited = set() if _visited is None else set(_visited)
        visit_key = (importer_path, f"{module_name}:{imported_name}:{level}")
        if visit_key in visited:
            return _AmbiguousRef("cyclic import binding")
        visited.add(visit_key)
        paths = self.resolve_module(module_name, importer_path, level)
        if not paths:
            # ``from package import submodule`` is a module lookup even when
            # package itself is not represented as a normal file alias.
            nested_name = ".".join(
                part for part in (module_name, imported_name) if part
            )
            nested_paths = self.resolve_module(nested_name, importer_path, 0)
            if nested_paths:
                paths = nested_paths
                return self._module_ref_or_helper(nested_name, paths)
            return _UnknownRef("unresolved import")

        candidates: list[_Ref] = []
        for path in paths:
            module = self.modules.get(path)
            if module is None:
                continue
            if module.is_test:
                candidates.append(_TestHelperRef())
                continue
            definitions = self.top_level.get((path, imported_name), [])
            if definitions:
                candidates.extend(_SymbolRef(definition) for definition in definitions)
            binding = self._resolve_import_binding(path, imported_name, visited)
            if not isinstance(binding, _UnknownRef):
                candidates.append(binding)

            nested_name = ".".join(
                part for part in (module_name, imported_name) if part
            )
            nested_paths = self.resolve_module(nested_name, importer_path, 0)
            if nested_paths:
                candidates.append(_ModuleRef(nested_name, nested_paths))

        return self._collapse_refs(candidates, "ambiguous import")

    @staticmethod
    def _collapse_refs(candidates: Sequence[_Ref], ambiguous_reason: str) -> _Ref:
        if not candidates:
            return _UnknownRef("unresolved import")
        unique: list[_Ref] = []
        for candidate in candidates:
            if candidate not in unique:
                unique.append(candidate)
        if len(unique) == 1:
            return unique[0]
        return _AmbiguousRef(ambiguous_reason)

    def _module_ref_or_helper(self, module_name: str, paths: Sequence[str]) -> _Ref:
        if any(self.modules[path].is_test for path in paths if path in self.modules):
            return _TestHelperRef()
        if len(paths) != 1:
            return _AmbiguousRef("ambiguous module")
        return _ModuleRef(module_name, tuple(sorted(paths)))

    def _resolve_import_binding(
        self,
        path: str,
        name: str,
        visited: set[tuple[str, str]],
    ) -> _Ref:
        self_submodule = self._proven_package_self_submodule(path, name)
        if self_submodule is not None:
            return self_submodule
        if self._has_unproven_package_self_import(path, name):
            return _AmbiguousRef("unproven package submodule binding")

        candidates: list[_Ref] = []
        bindings = self.import_bindings.get((path, name), ())
        for module_name, imported_name, level in bindings:
            resolved = self.resolve_import(
                module_name,
                imported_name,
                path,
                level,
                _visited=visited,
            )
            if not isinstance(resolved, _UnknownRef):
                candidates.append(resolved)
        wildcard_bindings = self.wildcard_import_bindings.items()
        for (binding_path, module_name), levels in wildcard_bindings:
            if binding_path != path:
                continue
            for level in levels:
                resolved = self.resolve_import(
                    module_name,
                    name,
                    path,
                    level,
                    _visited=visited,
                )
                if not isinstance(resolved, _UnknownRef):
                    candidates.append(resolved)
        return self._collapse_refs(candidates, "ambiguous import binding")

    def _has_unproven_package_self_import(
        self, path: str, name: str
    ) -> bool:
        """Identify a same-package import shape that failed the exact proof."""

        if not path.endswith("/__init__.py"):
            return False
        module = self.modules.get(path)
        if module is None or module.is_test:
            return False
        package_name = _module_name_for_path(path)
        nested_paths = self.resolve_module(f"{package_name}.{name}", path)
        if not nested_paths:
            return False
        for node in ast.walk(module.tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if not any(alias.name == name for alias in node.names):
                continue
            if (node.level == 0 and node.module == package_name) or (
                node.level > 0 and not node.module
            ):
                return True
        return False

    def _proven_package_self_submodule(
        self, path: str, name: str
    ) -> _ModuleRef | None:
        """Collapse one exact package self-import to its physical submodule.

        Importing ``name`` from a package normally considers both the binding
        exposed by its initializer and the physical ``package.name`` module.
        Following an initializer's ``from package import name`` binding through
        the general resolver creates an apparent cycle, even when both routes
        identify that same module.  This proof is intentionally lexical: it
        accepts only one unaliased, unconditional absolute import, no other
        binding in the initializer, and one indexed nested module.
        """

        if not path.endswith("/__init__.py"):
            return None
        module = self.modules.get(path)
        if module is None or module.is_test:
            return None
        package_name = _module_name_for_path(path)
        if not package_name:
            return None

        bindings = _scope_bindings(module.tree.body)
        if "*" in bindings or bindings.get(name) != ["other"]:
            return None
        matches = [
            alias
            for statement in module.tree.body
            if isinstance(statement, ast.ImportFrom)
            and statement.level == 0
            and statement.module == package_name
            for alias in statement.names
            if alias.name == name and alias.asname is None
        ]
        if len(matches) != 1:
            return None

        nested_name = f"{package_name}.{name}"
        nested_paths = self.resolve_module(nested_name, path)
        if len(nested_paths) != 1:
            return None
        nested = self._module_ref_or_helper(nested_name, nested_paths)
        return nested if isinstance(nested, _ModuleRef) else None

    def resolve_attribute(self, base: _Ref, attribute: str) -> _Ref:
        if isinstance(base, _ModuleRef):
            module_name = f"{base.module_name}.{attribute}"
            nested_paths = tuple(sorted(self.module_paths.get(module_name, ())))
            candidates: list[_Ref] = []
            if nested_paths:
                candidates.append(self._module_ref_or_helper(module_name, nested_paths))
            for path in base.paths:
                module = self.modules.get(path)
                if module is None or module.is_test:
                    continue
                candidates.extend(
                    _SymbolRef(definition)
                    for definition in self.top_level.get((path, attribute), ())
                )
                binding = self._resolve_import_binding(path, attribute, set())
                if not isinstance(binding, _UnknownRef):
                    candidates.append(binding)
            return self._collapse_refs(candidates, "ambiguous attribute")

        if isinstance(base, _SymbolRef) and base.definition.kind == "class":
            return self._resolve_method(base.definition, attribute)
        if isinstance(base, _InstanceRef):
            return self._resolve_method(base.class_ref.definition, attribute)
        if isinstance(base, _FrameworkInstanceRef):
            return self.resolve_simple_inherited_method(
                base.class_ref.definition, attribute
            )
        if isinstance(base, (_AmbiguousRef, _TestHelperRef)):
            return base
        return _UnknownRef("unresolved attribute")

    def _resolve_method(self, class_definition: _Definition, name: str) -> _Ref:
        candidates = [
            _SymbolRef(definition)
            for (path, qualname), definitions in self.definitions.items()
            if path == class_definition.path
            and qualname == f"{class_definition.qualname}.{name}"
            for definition in definitions
        ]
        return self._collapse_refs(candidates, "ambiguous method")

    def resolve_simple_inherited_method(
        self, class_definition: _Definition, name: str
    ) -> _Ref:
        """Resolve a method through one exact local bare-name base chain."""

        visited: set[tuple[str, str]] = set()

        def resolve(current: _Definition) -> _Ref:
            key = (current.path, current.qualname)
            if key in visited:
                return _AmbiguousRef("cyclic class base")
            visited.add(key)
            direct = self._resolve_method(current, name)
            if not isinstance(direct, _UnknownRef):
                return direct
            module = self.modules.get(current.path)
            if module is None or "." in current.qualname:
                return _UnknownRef("unresolved class base")
            class_nodes = [
                statement
                for statement in module.tree.body
                if isinstance(statement, ast.ClassDef)
                and statement.name == current.qualname
                and statement.lineno == current.start_line
            ]
            if len(class_nodes) != 1 or len(class_nodes[0].bases) != 1:
                return _UnknownRef("unresolved class base")
            base = class_nodes[0].bases[0]
            if not isinstance(base, ast.Name):
                return _UnknownRef("unresolved class base")
            candidates = self.top_level.get((current.path, base.id), ())
            if len(candidates) != 1 or candidates[0].kind != "class":
                return _UnknownRef("unresolved class base")
            return resolve(candidates[0])

        return resolve(class_definition)

    def resolve_constructor(self, class_definition: _Definition) -> _Ref:
        """Resolve one explicit constructor method without widening to a class.

        Inherited constructors cannot be identified safely from a static source
        index.  Likewise, a class defining both ``__new__`` and ``__init__``
        does not have one uniquely attributable constructor span.  Only a class
        with exactly one explicit method is therefore emitted.
        """

        candidates = [
            _SymbolRef(definition)
            for method_name in ("__new__", "__init__")
            for definition in self.definitions.get(
                (class_definition.path, f"{class_definition.qualname}.{method_name}"),
                (),
            )
            if definition.kind == "method"
        ]
        return self._collapse_refs(candidates, "ambiguous explicit constructor")


class _TestFunctionCollector(ast.NodeVisitor):
    """Find pytest/unittest-style test functions in a selected source file."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.scope: list[str] = []
        self.functions: list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]] = []

    @staticmethod
    def _is_test_name(name: str) -> bool:
        return name == "test" or name.startswith("test_") or name.startswith("test")

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        for child in node.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.visit(child)
            elif isinstance(child, ast.ClassDef):
                self.visit(child)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if self._is_test_name(node.name):
            qualname = ".".join((*self.scope, node.name))
            self.functions.append((qualname, node))

    visit_AsyncFunctionDef = visit_FunctionDef


def _unique_direct_import(
    tree: ast.Module,
    *,
    module: str,
    imported_name: str,
    local_name: str,
    unaliased: bool,
    before_line: int | None = None,
) -> bool:
    """Prove one unconditional direct import and no competing binding."""

    bindings = _scope_bindings(tree.body)
    if "*" in bindings or bindings.get(local_name) != ["other"]:
        return False
    matches = [
        alias
        for statement in tree.body
        if isinstance(statement, ast.ImportFrom)
        and statement.level == 0
        and statement.module == module
        and (before_line is None or statement.lineno < before_line)
        for alias in statement.names
        if alias.name == imported_name
        and (alias.asname or alias.name) == local_name
        and (not unaliased or alias.asname is None)
    ]
    return len(matches) == 1


def _nested_function_returned_by(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    returns = [
        statement.value.id
        for statement in node.body
        if isinstance(statement, ast.Return)
        and isinstance(statement.value, ast.Name)
    ]
    if len(returns) != 1:
        return None
    candidates = [
        statement
        for statement in node.body
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
        and statement.name == returns[0]
    ]
    return candidates[0] if len(candidates) == 1 else None


def _scoped_descendants(statements: Sequence[ast.stmt]) -> Iterator[ast.AST]:
    """Yield nodes without entering nested function, class, or lambda bodies."""

    pending: list[ast.AST] = list(reversed(statements))
    while pending:
        node = pending.pop()
        yield node
        children = list(ast.iter_child_nodes(node))
        pending.extend(
            reversed(
                [
                    child
                    for child in children
                    if not isinstance(
                        child,
                        (
                            ast.FunctionDef,
                            ast.AsyncFunctionDef,
                            ast.ClassDef,
                            ast.Lambda,
                        ),
                    )
                ]
            )
        )


def _module_import_environment(
    index: _SourceIndex,
    path: str,
    tree: ast.Module,
) -> dict[str, _Ref]:
    """Resolve module imports without applying Python's runtime source order."""

    environment: dict[str, _Ref] = {}

    def bind(name: str, reference: _Ref) -> None:
        previous = environment.get(name)
        if previous is None or previous == reference:
            environment[name] = reference
        else:
            environment[name] = _AmbiguousRef("ambiguous import binding")

    for statement in tree.body:
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                local_name = alias.asname or alias.name.split(".", 1)[0]
                imported_module_name = alias.name
                if alias.asname is None and "." in alias.name:
                    imported_module_name = alias.name.split(".", 1)[0]
                paths = index.resolve_module(imported_module_name, path)
                if not paths and imported_module_name != alias.name:
                    paths = index.resolve_module(alias.name, path)
                reference: _Ref = (
                    index._module_ref_or_helper(imported_module_name, paths)
                    if paths
                    else _UnknownRef("unresolved import")
                )
                bind(local_name, reference)
        elif isinstance(statement, ast.ImportFrom):
            for alias in statement.names:
                local_name = alias.asname or alias.name
                reference = (
                    _AmbiguousRef("wildcard import")
                    if alias.name == "*"
                    else index.resolve_import(
                        statement.module or "",
                        alias.name,
                        path,
                        statement.level,
                    )
                )
                bind(local_name, reference)
    module_bindings = _scope_bindings(tree.body)
    if "*" in module_bindings:
        return {
            name: _AmbiguousRef("wildcard module binding") for name in environment
        }
    for name in environment:
        if module_bindings.get(name) != ["other"]:
            environment[name] = _AmbiguousRef("module import is rebound")
    return environment


class _PytestFixtureResolver:
    """Prove production instance types supplied by lexical pytest fixtures."""

    def __init__(self, index: _SourceIndex) -> None:
        self.index = index

    @staticmethod
    def _has_exact_pytest_binding(tree: ast.Module, before_line: int) -> bool:
        bindings = _scope_bindings(tree.body)
        if "*" in bindings or bindings.get("pytest") != ["other"]:
            return False
        matches = [
            alias
            for statement in tree.body
            if isinstance(statement, ast.Import)
            and statement.lineno < before_line
            for alias in statement.names
            if alias.name == "pytest" and alias.asname is None
        ]
        return len(matches) == 1

    @classmethod
    def _is_exact_fixture_decorator(
        cls,
        tree: ast.Module,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> bool:
        if isinstance(node, ast.AsyncFunctionDef) or len(node.decorator_list) != 1:
            return False
        matches = 0
        for decorator in node.decorator_list:
            target = decorator.func if isinstance(decorator, ast.Call) else decorator
            if not (
                isinstance(target, ast.Attribute)
                and target.attr == "fixture"
                and isinstance(target.value, ast.Name)
                and target.value.id == "pytest"
            ):
                continue
            if isinstance(decorator, ast.Call) and (
                decorator.args
                or any(keyword.arg in {None, "name"} for keyword in decorator.keywords)
            ):
                return False
            matches += 1
        return matches == 1 and cls._has_exact_pytest_binding(tree, node.lineno)

    @classmethod
    def _collect_fixtures(
        cls,
        path: str,
        tree: ast.Module,
    ) -> list[_FixtureDefinition]:
        fixtures: list[_FixtureDefinition] = []

        def collect(statements: Sequence[ast.stmt], owner: tuple[str, ...]) -> None:
            for statement in statements:
                if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if cls._is_exact_fixture_decorator(tree, statement):
                        fixtures.append(
                            _FixtureDefinition(path, statement.name, statement, owner)
                        )
                elif isinstance(statement, ast.ClassDef):
                    collect(statement.body, (*owner, statement.name))

        collect(tree.body, ())
        return fixtures

    @staticmethod
    def _conftest_is_applicable(conftest_path: str, test_path: str) -> bool:
        conftest_parent = Path(conftest_path).parent
        test_parent = Path(test_path).parent
        return conftest_parent == Path(".") or conftest_parent in (
            test_parent,
            *test_parent.parents,
        )

    def _applicable_fixtures(
        self,
        test_path: str,
        owner: tuple[str, ...],
        name: str,
    ) -> list[_FixtureDefinition]:
        candidates: list[_FixtureDefinition] = []
        for path, module in self.index.modules.items():
            same_module = path == test_path
            applicable_conftest = Path(path).name == "conftest.py" and (
                self._conftest_is_applicable(path, test_path)
            )
            if not (same_module or applicable_conftest):
                continue
            for fixture in self._collect_fixtures(path, module.tree):
                if fixture.name != name:
                    continue
                if fixture.owner and (not same_module or fixture.owner != owner):
                    continue
                candidates.append(fixture)
        return list(dict.fromkeys(candidates))

    @staticmethod
    def _local_bindings(
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        name: str,
    ) -> list[ast.AST]:
        bindings: list[ast.AST] = []
        parameters = (
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        )
        bindings.extend(argument for argument in parameters if argument.arg == name)
        if node.args.vararg is not None and node.args.vararg.arg == name:
            bindings.append(node.args.vararg)
        if node.args.kwarg is not None and node.args.kwarg.arg == name:
            bindings.append(node.args.kwarg)
        for descendant in _scoped_descendants(node.body):
            if (
                isinstance(descendant, ast.Name)
                and descendant.id == name
                and isinstance(descendant.ctx, (ast.Store, ast.Del))
            ):
                bindings.append(descendant)
        return bindings

    @staticmethod
    def _assignment_for_binding(
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        binding: ast.AST,
    ) -> ast.AST | None:
        for descendant in _scoped_descendants(node.body):
            if (
                isinstance(descendant, ast.Assign)
                and len(descendant.targets) == 1
                and descendant.targets[0] is binding
                and isinstance(binding, ast.Name)
            ):
                return descendant.value
            if (
                isinstance(descendant, ast.AnnAssign)
                and descendant.target is binding
                and isinstance(binding, ast.Name)
                and descendant.value is not None
            ):
                return descendant.value
        return None

    def _assigned_class(
        self,
        fixture: _FixtureDefinition,
        tree: ast.Module,
        local_name: str,
    ) -> _SymbolRef | None:
        imports = _module_import_environment(self.index, fixture.path, tree)
        classes: list[_SymbolRef] = []
        bindings = self._local_bindings(fixture.node, local_name)
        if not bindings:
            return None
        for binding in bindings:
            value = self._assignment_for_binding(fixture.node, binding)
            if not (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and isinstance((reference := imports.get(value.func.id)), _SymbolRef)
                and reference.definition.kind == "class"
                and reference.definition.path not in self.index._test_paths
                and not _is_test_path(reference.definition.path)
            ):
                return None
            classes.append(reference)
        unique = list(dict.fromkeys(classes))
        return unique[0] if len(unique) == 1 else None

    def _module_proves_self_export(
        self,
        module_name: str,
        importer_path: str,
        imported_name: str,
    ) -> bool:
        paths = self.index.resolve_module(module_name, importer_path)
        if len(paths) != 1:
            return False
        module = self.index.modules.get(paths[0])
        if module is None or module.is_test:
            return False

        def is_self_import(node: ast.AST, source: str) -> bool:
            return (
                isinstance(node, ast.ImportFrom)
                and node.level == 0
                and node.module == source
                and sum(
                    alias.name == "Self"
                    and (alias.asname or alias.name) == imported_name
                    for alias in node.names
                )
                == 1
            )

        direct = [
            statement
            for statement in module.tree.body
            if is_self_import(statement, "typing")
            or is_self_import(statement, "typing_extensions")
        ]
        bindings = _scope_bindings(module.tree.body)
        if len(direct) == 1 and bindings.get(imported_name) == ["other"]:
            return True

        relevant_try = [
            statement
            for statement in module.tree.body
            if isinstance(statement, ast.Try)
            and any(
                isinstance(descendant, (ast.ImportFrom, ast.AnnAssign))
                and (
                    isinstance(descendant, ast.ImportFrom)
                    and any(
                        (alias.asname or alias.name) == imported_name
                        for alias in descendant.names
                    )
                    or isinstance(descendant, ast.AnnAssign)
                    and isinstance(descendant.target, ast.Name)
                    and descendant.target.id == imported_name
                )
                for descendant in ast.walk(statement)
            )
        ]
        if len(relevant_try) != 1:
            return False
        shim = relevant_try[0]
        if (
            shim.orelse
            or shim.finalbody
            or len(shim.body) != 1
            or len(shim.handlers) != 1
        ):
            return False
        version_gate = shim.body[0]
        if not (
            isinstance(version_gate, ast.If)
            and isinstance(version_gate.test, ast.Compare)
            and isinstance(version_gate.test.left, ast.Attribute)
            and version_gate.test.left.attr == "version_info"
            and isinstance(version_gate.test.left.value, ast.Name)
            and version_gate.test.left.value.id == "sys"
            and len(version_gate.test.ops) == 1
            and isinstance(version_gate.test.ops[0], ast.GtE)
            and len(version_gate.test.comparators) == 1
            and isinstance(version_gate.test.comparators[0], ast.Tuple)
            and len(version_gate.test.comparators[0].elts) == 2
            and [
                element.value
                for element in version_gate.test.comparators[0].elts
                if isinstance(element, ast.Constant)
            ]
            == [3, 11]
            and len(version_gate.body) == 1
            and len(version_gate.orelse) == 1
            and is_self_import(version_gate.body[0], "typing")
            and is_self_import(version_gate.orelse[0], "typing_extensions")
        ):
            return False
        handler = shim.handlers[0]
        fallback_gate = handler.body[0] if len(handler.body) == 1 else None
        if not (
            isinstance(handler.type, ast.Name)
            and handler.type.id == "ImportError"
            and handler.name is None
            and isinstance(fallback_gate, ast.If)
            and isinstance(fallback_gate.test, ast.Name)
            and fallback_gate.test.id == "TYPE_CHECKING"
            and len(fallback_gate.body) == 1
            and isinstance(fallback_gate.body[0], ast.Raise)
            and len(fallback_gate.orelse) == 1
            and isinstance((fallback := fallback_gate.orelse[0]), ast.AnnAssign)
            and isinstance(fallback.target, ast.Name)
            and fallback.target.id == imported_name
            and isinstance(fallback.value, ast.Constant)
            and fallback.value.value is None
        ):
            return False
        module_bindings = _scope_bindings(module.tree.body)
        sys_imports = [
            alias
            for statement in module.tree.body
            if isinstance(statement, ast.Import) and statement.lineno < shim.lineno
            for alias in statement.names
            if alias.name == "sys" and alias.asname is None
        ]
        return (
            module_bindings.get("sys") == ["other"]
            and len(sys_imports) == 1
            and _unique_direct_import(
                module.tree,
                module="typing",
                imported_name="TYPE_CHECKING",
                local_name="TYPE_CHECKING",
                unaliased=True,
                before_line=shim.lineno,
            )
            and module_bindings.get(imported_name)
            == ["other", "other", "other"]
        )

    def _method_has_proven_self(self, method: _SymbolRef) -> bool:
        module = self.index.modules.get(method.definition.path)
        if module is None:
            return False
        matches = [
            node
            for node in ast.walk(module.tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.lineno == method.definition.start_line
            and node.name == method.definition.qualname.rsplit(".", 1)[-1]
        ]
        if (
            len(matches) != 1
            or isinstance(matches[0], ast.AsyncFunctionDef)
            or not isinstance(matches[0].returns, ast.Name)
        ):
            return False
        annotation_name = matches[0].returns.id
        bindings = _scope_bindings(module.tree.body)
        if "*" in bindings or bindings.get(annotation_name) != ["other"]:
            return False
        imports = [
            (statement, alias)
            for descendant in _scoped_descendants(module.tree.body)
            if isinstance(descendant, ast.ImportFrom)
            for statement in [descendant]
            for alias in statement.names
            if alias.name == "Self"
            and (alias.asname or alias.name) == annotation_name
            and statement.lineno < matches[0].lineno
        ]
        if len(imports) != 1:
            return False
        statement, _alias = imports[0]
        directly_scoped = statement in module.tree.body
        type_checking_scopes = [
            scope
            for scope in module.tree.body
            if isinstance(scope, ast.If)
            and isinstance(scope.test, ast.Name)
            and scope.test.id == "TYPE_CHECKING"
            and statement in scope.body
        ]
        if not directly_scoped:
            if len(type_checking_scopes) != 1 or not _unique_direct_import(
                module.tree,
                module="typing",
                imported_name="TYPE_CHECKING",
                local_name="TYPE_CHECKING",
                unaliased=True,
                before_line=type_checking_scopes[0].lineno,
            ):
                return False
        if statement.level == 0 and statement.module in {
            "typing",
            "typing_extensions",
        }:
            return True
        return statement.level == 0 and bool(statement.module) and (
            self._module_proves_self_export(
                statement.module, method.definition.path, annotation_name
            )
        )

    def _returned_instance(
        self,
        fixture: _FixtureDefinition,
    ) -> _InstanceRef | None:
        module = self.index.modules.get(fixture.path)
        if module is None or any(
            isinstance(node, (ast.Yield, ast.YieldFrom))
            for node in _scoped_descendants(fixture.node.body)
        ):
            return None
        returns = [
            node
            for node in _scoped_descendants(fixture.node.body)
            if isinstance(node, ast.Return)
        ]
        if not returns or not _TestBodyAnalyzer._statements_guarantee_return(
            fixture.node.body
        ):
            return None

        class_refs: list[_SymbolRef] = []
        for returned in returns:
            value = returned.value
            method_name: str | None = None
            if isinstance(value, ast.Name):
                local_name = value.id
            elif (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Attribute)
                and isinstance(value.func.value, ast.Name)
            ):
                local_name = value.func.value.id
                method_name = value.func.attr
            else:
                return None
            class_ref = self._assigned_class(
                fixture, module.tree, local_name
            )
            if class_ref is None:
                return None
            if method_name is not None:
                method = self.index.resolve_attribute(
                    _InstanceRef(class_ref), method_name
                )
                if not isinstance(
                    method, _SymbolRef
                ) or not self._method_has_proven_self(
                    method,
                ):
                    return None
            class_refs.append(class_ref)
        unique = list(dict.fromkeys(class_refs))
        return _InstanceRef(unique[0]) if len(unique) == 1 else None

    def resolve(
        self,
        test_path: str,
        owner: tuple[str, ...],
        parameter_name: str,
    ) -> _InstanceRef | None:
        fixtures = self._applicable_fixtures(test_path, owner, parameter_name)
        if len(fixtures) != 1:
            return None
        return self._returned_instance(fixtures[0])


def _exact_figure_factory_assignment(node: ast.AST, name: str) -> bool:
    return (
        isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == name
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "figure"
        and isinstance(node.value.func.value, ast.Name)
        and node.value.func.value.id == "plt"
    )


def _forwards_figure_keywords(node: ast.AST, callable_name: str) -> bool:
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == callable_name
    ):
        return False
    keywords = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
    return (
        any(
            isinstance(argument, ast.Starred)
            and isinstance(argument.value, ast.Name)
            and argument.value.id == "args"
            for argument in node.args
        )
        and any(
            keyword.arg is None
            and isinstance(keyword.value, ast.Name)
            and keyword.value.id == "kwargs"
            for keyword in node.keywords
        )
        and all(
            isinstance(keywords.get(name), ast.Name)
            and keywords[name].id == name  # type: ignore[union-attr]
            for name in ("fig_test", "fig_ref")
        )
    )


def _proven_check_figures_equal_fixture(
    index: _SourceIndex,
    test_path: str,
    module_tree: ast.Module,
    root: ast.FunctionDef | ast.AsyncFunctionDef,
) -> _FrameworkInstanceRef | None:
    """Prove Matplotlib's checked-in figure-comparison wrapper statically."""

    if root not in module_tree.body:
        return None
    parameters = (*root.args.posonlyargs, *root.args.args, *root.args.kwonlyargs)
    if (
        {argument.arg for argument in parameters} != {"fig_test", "fig_ref"}
        or len(parameters) != 2
        or root.args.vararg is not None
        or root.args.kwarg is not None
        or root.args.defaults
        or any(default is not None for default in root.args.kw_defaults)
    ):
        return None
    if not _unique_direct_import(
        module_tree,
        module="matplotlib.testing.decorators",
        imported_name="check_figures_equal",
        local_name="check_figures_equal",
        unaliased=True,
        before_line=root.lineno,
    ):
        return None
    decorator_calls = [
        decorator
        for decorator in root.decorator_list
        if isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Name)
        and decorator.func.id == "check_figures_equal"
        and not decorator.args
        and all(keyword.arg in {"extensions", "tol"} for keyword in decorator.keywords)
        and len({keyword.arg for keyword in decorator.keywords})
        == len(decorator.keywords)
    ]
    if len(decorator_calls) != 1:
        return None

    imported = index.resolve_import(
        "matplotlib.testing.decorators",
        "check_figures_equal",
        test_path,
    )
    if not isinstance(imported, _SymbolRef) or imported.definition.kind != "function":
        return None
    decorator_module = index.modules.get(imported.definition.path)
    if decorator_module is None:
        return None
    definitions = [
        statement
        for statement in decorator_module.tree.body
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
        and statement.name == "check_figures_equal"
        and statement.lineno == imported.definition.start_line
    ]
    if len(definitions) != 1:
        return None
    factory = definitions[0]
    decorator = _nested_function_returned_by(factory)
    wrapper = _nested_function_returned_by(decorator) if decorator else None
    if decorator is None or wrapper is None:
        return None
    decorator_parameters = (
        *decorator.args.posonlyargs,
        *decorator.args.args,
        *decorator.args.kwonlyargs,
    )
    if (
        len(decorator_parameters) != 1
        or decorator_parameters[0].arg != "func"
        or decorator.args.vararg is not None
        or decorator.args.kwarg is not None
    ):
        return None
    if (
        wrapper.args.vararg is None
        or wrapper.args.vararg.arg != "args"
        or wrapper.args.kwarg is None
        or wrapper.args.kwarg.arg != "kwargs"
    ):
        return None
    if any(
        name in _scope_bindings(node.body)
        for node in (factory, decorator, wrapper)
        for name in ("plt",)
    ) or "func" in _scope_bindings(decorator.body) or "func" in _scope_bindings(
        wrapper.body
    ):
        return None

    wrapper_bindings = _scope_bindings(wrapper.body)
    nodes = list(_scoped_descendants(wrapper.body))
    if any(wrapper_bindings.get(name) != ["other"] for name in ("fig_test", "fig_ref")):
        return None
    assignments = {
        name: [
            node for node in nodes if _exact_figure_factory_assignment(node, name)
        ]
        for name in ("fig_test", "fig_ref")
    }
    forwards = [node for node in nodes if _forwards_figure_keywords(node, "func")]
    if any(len(matches) != 1 for matches in assignments.values()) or len(
        forwards
    ) != 1:
        return None
    if any(
        getattr(matches[0], "lineno", 0) >= getattr(forwards[0], "lineno", 0)
        for matches in assignments.values()
    ):
        return None

    if not _unique_direct_import(
        decorator_module.tree,
        module="matplotlib",
        imported_name="pyplot",
        local_name="plt",
        unaliased=False,
        before_line=factory.lineno,
    ):
        return None
    pyplot = index.resolve_import("matplotlib", "pyplot", imported.definition.path)
    if not isinstance(pyplot, _ModuleRef):
        return None
    figure_factory = index.resolve_attribute(pyplot, "figure")
    if not isinstance(figure_factory, _SymbolRef) or (
        figure_factory.definition.kind != "function"
    ):
        return None
    pyplot_module = index.modules.get(figure_factory.definition.path)
    if pyplot_module is None or not _unique_direct_import(
        pyplot_module.tree,
        module="matplotlib.figure",
        imported_name="Figure",
        local_name="Figure",
        unaliased=True,
        before_line=figure_factory.definition.start_line,
    ):
        return None
    factory_nodes = [
        statement
        for statement in pyplot_module.tree.body
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
        and statement.name == "figure"
        and statement.lineno == figure_factory.definition.start_line
    ]
    if len(factory_nodes) != 1:
        return None
    factory_node = factory_nodes[0]
    positional = (*factory_node.args.posonlyargs, *factory_node.args.args)
    defaults_start = len(positional) - len(factory_node.args.defaults)
    defaults = {
        argument.arg: default
        for argument, default in zip(
            positional[defaults_start:],
            factory_node.args.defaults,
            strict=True,
        )
    }
    defaults.update(
        {
            argument.arg: default
            for argument, default in zip(
                factory_node.args.kwonlyargs,
                factory_node.args.kw_defaults,
                strict=True,
            )
            if default is not None
        }
    )
    figure_class_default = defaults.get("FigureClass")
    if not (
        isinstance(figure_class_default, ast.Name)
        and figure_class_default.id == "Figure"
    ):
        return None
    figure = index.resolve_import(
        "matplotlib.figure", "Figure", figure_factory.definition.path
    )
    if not isinstance(figure, _SymbolRef) or figure.definition.kind != "class":
        return None
    subplots = index.resolve_simple_inherited_method(figure.definition, "subplots")
    if not isinstance(subplots, _SymbolRef):
        return None
    return _FrameworkInstanceRef(figure)


class _TestHelperRegistry:
    """Index same-module helper definitions without following imports.

    The registry is deliberately local to one parsed test module.  Top-level
    helpers are resolved by their bare name and methods by their lexical class
    owner (``self.helper()``).  Test functions themselves are excluded so a
    selected test can never inline a peer test and leak its production calls.
    """

    def __init__(self, path: str, tree: ast.Module) -> None:
        self.path = path
        self.by_name: dict[str, list[_TestHelperDefinition]] = defaultdict(list)
        self.by_method: dict[
            tuple[tuple[str, ...], str], list[_TestHelperDefinition]
        ] = defaultdict(list)
        self._collect(tree.body, ())

    @staticmethod
    def _is_test_name(name: str) -> bool:
        return name == "test" or name.startswith("test_") or name.startswith("test")

    def _collect(self, body: Iterable[ast.stmt], owner: tuple[str, ...]) -> None:
        for statement in body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if self._is_test_name(statement.name):
                    # A test function is an analysis root, never a helper.
                    continue
                definition = _TestHelperDefinition(
                    path=self.path,
                    qualname=".".join((*owner, statement.name)),
                    node=statement,
                    owner=owner,
                )
                if owner:
                    self.by_method[(owner, statement.name)].append(definition)
                else:
                    self.by_name[statement.name].append(definition)
                # Nested helper definitions are indexed under their lexical
                # owner for bounded helper-to-helper calls.  They are not
                # exposed by bare name outside that owner.
                self._collect(statement.body, (*owner, statement.name))
            elif isinstance(statement, ast.ClassDef):
                self._collect(statement.body, (*owner, statement.name))

    @staticmethod
    def _collapse(
        candidates: Sequence[_TestHelperDefinition], reason: str
    ) -> _TestHelperRef:
        unique = list(dict.fromkeys(candidates))
        if len(unique) == 1:
            return _TestHelperRef(unique[0])
        if len(unique) > 1:
            return _TestHelperRef(None, reason)
        return _TestHelperRef(None, "test/helper call")

    def resolve_name(
        self, name: str, owner: tuple[str, ...] = ()
    ) -> _TestHelperRef:
        if owner:
            nested = self.by_method.get((owner, name), ())
            if nested:
                return self._collapse(nested, "ambiguous nested test helper")
        return self._collapse(self.by_name.get(name, ()), "ambiguous test helper")

    def resolve_method(
        self, owner: tuple[str, ...], name: str
    ) -> _TestHelperRef:
        return self._collapse(
            self.by_method.get((owner, name), ()), "ambiguous test helper method"
        )


class _UnittestSetupResolver:
    """Prove receiver types assigned by one direct unittest ``setUp``.

    This deliberately models only lexical state in the selected method's own
    concrete class.  It does not walk bases or execute setup/helper code.
    """

    def __init__(self, index: _SourceIndex, path: str, tree: ast.Module) -> None:
        self.index = index
        self.path = path
        self.tree = tree
        self.imports = _module_import_environment(index, path, tree)

    @staticmethod
    def _direct_class(
        tree: ast.Module, owner: tuple[str, ...]
    ) -> ast.ClassDef | None:
        if len(owner) != 1:
            return None
        matches = [
            statement
            for statement in tree.body
            if isinstance(statement, ast.ClassDef) and statement.name == owner[0]
        ]
        return matches[0] if len(matches) == 1 else None

    @staticmethod
    def _direct_methods(
        class_node: ast.ClassDef, name: str
    ) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
        return [
            statement
            for statement in class_node.body
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
            and statement.name == name
        ]

    @staticmethod
    def _exact_self_method(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        return (
            isinstance(node, ast.FunctionDef)
            and not node.decorator_list
            and not node.args.posonlyargs
            and [argument.arg for argument in node.args.args] == ["self"]
            and not node.args.kwonlyargs
            and not node.args.defaults
            and node.args.vararg is None
            and node.args.kwarg is None
        )

    @staticmethod
    def _helper_callable_without_values(
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> bool:
        if (
            isinstance(node, ast.AsyncFunctionDef)
            or node.decorator_list
            or node.args.posonlyargs
            or not node.args.args
            or node.args.args[0].arg != "self"
            or node.args.vararg is not None
            or node.args.kwarg is not None
        ):
            return False
        non_self = node.args.args[1:]
        return len(node.args.defaults) == len(non_self) and all(
            default is not None for default in node.args.kw_defaults
        )

    def _resolve_constructor(self, node: ast.AST) -> _SymbolRef | None:
        if isinstance(node, ast.Name):
            reference = self.imports.get(node.id)
        elif isinstance(node, ast.Attribute):
            base = self._resolve_constructor(node.value)
            if base is not None:
                return None
            reference = self._resolve_import_expr(node.value)
            reference = self.index.resolve_attribute(reference, node.attr)
        else:
            return None
        if not isinstance(reference, _SymbolRef):
            return None
        definition = reference.definition
        if (
            definition.kind != "class"
            or definition.path in self.index._test_paths
            or _is_test_path(definition.path)
        ):
            return None
        return reference

    def _resolve_import_expr(self, node: ast.AST) -> _Ref:
        if isinstance(node, ast.Name):
            return self.imports.get(node.id, _UnknownRef("unresolved setup import"))
        if isinstance(node, ast.Attribute):
            return self.index.resolve_attribute(
                self._resolve_import_expr(node.value), node.attr
            )
        return _UnknownRef("unresolved setup constructor")

    def _helper_result(
        self,
        class_node: ast.ClassDef,
        helper_name: str,
    ) -> _InstanceRef | None:
        class_bindings = _scope_bindings(class_node.body)
        helpers = self._direct_methods(class_node, helper_name)
        if class_bindings.get(helper_name) != ["other"] or len(helpers) != 1:
            return None
        helper = helpers[0]
        if not self._helper_callable_without_values(helper):
            return None
        descendants = list(_scoped_descendants(helper.body))
        if any(isinstance(node, (ast.Yield, ast.YieldFrom)) for node in descendants):
            return None
        returns = [node for node in descendants if isinstance(node, ast.Return)]
        if not returns or not _TestBodyAnalyzer._statements_guarantee_return(
            helper.body
        ):
            return None

        class_refs: list[_SymbolRef] = []
        allowed_calls: set[int] = set()
        for returned in returns:
            if not isinstance(returned.value, ast.Call):
                return None
            call = returned.value
            class_ref = self._resolve_constructor(call.func)
            if class_ref is None:
                return None
            allowed_calls.add(id(call))
            class_refs.append(class_ref)
        if any(
            isinstance(node, ast.Call) and id(node) not in allowed_calls
            for node in descendants
        ):
            return None
        unique = list(dict.fromkeys(class_refs))
        return _InstanceRef(unique[0]) if len(unique) == 1 else None

    @staticmethod
    def _selected_method_rebinds(
        root: ast.FunctionDef | ast.AsyncFunctionDef, attribute: str
    ) -> bool:
        return any(
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and node.attr == attribute
            and isinstance(node.ctx, (ast.Store, ast.Del))
            for node in _scoped_descendants(root.body)
        )

    def resolve(
        self,
        owner: tuple[str, ...],
        root: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> dict[str, _InstanceRef]:
        class_node = self._direct_class(self.tree, owner)
        if class_node is None or root not in class_node.body:
            return {}
        class_bindings = _scope_bindings(class_node.body)
        setups = self._direct_methods(class_node, "setUp")
        if class_bindings.get("setUp") != ["other"] or len(setups) != 1:
            return {}
        setup = setups[0]
        if not self._exact_self_method(setup):
            return {}
        statements = [
            statement
            for statement in setup.body
            if not (
                isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)
            )
        ]
        if len(statements) != 1:
            return {}
        assignment = statements[0]
        if not (
            isinstance(assignment, ast.Assign)
            and len(assignment.targets) == 1
            and isinstance((target := assignment.targets[0]), ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
            and isinstance((call := assignment.value), ast.Call)
            and not call.args
            and not call.keywords
            and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "self"
        ):
            return {}
        writes = [
            node
            for node in _scoped_descendants(setup.body)
            if isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and node.attr == target.attr
            and isinstance(node.ctx, (ast.Store, ast.Del))
        ]
        if len(writes) != 1 or writes[0] is not target:
            return {}
        if self._selected_method_rebinds(root, target.attr):
            return {}
        instance = self._helper_result(class_node, call.func.attr)
        return {target.attr: instance} if instance is not None else {}


class _TestBodyAnalyzer(ast.NodeVisitor):
    """Resolve direct calls in one trusted test function."""

    def __init__(
        self,
        index: _SourceIndex,
        test_id: str,
        test_path: str,
        module_tree: ast.Module,
        root: ast.FunctionDef | ast.AsyncFunctionDef,
        *,
        helper_registry: _TestHelperRegistry | None = None,
        initial_env: dict[str, _Ref] | None = None,
        helper_stack: tuple[_TestHelperDefinition, ...] = (),
        helper_depth: int = 0,
        active_owner: tuple[str, ...] = (),
    ) -> None:
        self.index = index
        self.test_id = test_id
        self.test_path = test_path
        self.env: dict[str, _Ref] = {}
        self.entries: list[TestEntry] = []
        self.diagnostics: list[InferenceDiagnostic] = []
        self._root = root
        self._helper_registry = helper_registry or _TestHelperRegistry(
            test_path, module_tree
        )
        self._helper_stack = helper_stack
        self._helper_depth = helper_depth
        if active_owner:
            self._active_owner = active_owner
        else:
            selected_qualname = test_id.split("::", 1)[-1]
            self._active_owner = tuple(selected_qualname.split(".")[:-1])
        self._call_results: dict[int, _Ref] = {}
        self._return_refs: list[_Ref] = []
        self._return_seen = False
        self._module_setup_calls: dict[str, _ModuleSetupCall] = {}
        self._module_setup_calls = self._collect_module_setup_calls(module_tree)
        self._local_bindings = set(_function_scope_bindings(root))
        self.env = {}
        self._collect_module_imports(module_tree)
        self._module_env = dict(self.env)
        self._mark_local_helpers()
        self._module_env = dict(self.env)
        self._self_attributes = _UnittestSetupResolver(
            index, test_path, module_tree
        ).resolve(self._active_owner, root)
        local_class_names = {
            node.name
            for node in ast.walk(module_tree)
            if isinstance(node, ast.ClassDef)
        }
        if initial_env is not None:
            self.env = dict(initial_env)
        figure_fixture = _proven_check_figures_equal_fixture(
            index, test_path, module_tree, root
        )
        fixture_resolver = _PytestFixtureResolver(index)
        for argument in (
            *root.args.posonlyargs,
            *root.args.args,
            *root.args.kwonlyargs,
        ):
            if argument.arg not in self.env or isinstance(
                self.env[argument.arg], _TestHelperRef
            ):
                reference: _Ref = _UnknownRef("test argument")
                if argument.arg in {"fig_test", "fig_ref"} and figure_fixture:
                    reference = figure_fixture
                elif (
                    argument.arg not in {"self", "cls"}
                    and isinstance(argument.annotation, ast.Name)
                    and argument.annotation.id not in local_class_names
                ):
                    imported = self._module_env.get(argument.annotation.id)
                    if (
                        isinstance(imported, _SymbolRef)
                        and imported.definition.kind == "class"
                        and imported.definition.path not in self.index._test_paths
                        and not _is_test_path(imported.definition.path)
                    ):
                        reference = _InstanceRef(imported)
                if (
                    isinstance(reference, _UnknownRef)
                    and argument.arg not in {"self", "cls"}
                ):
                    fixture_instance = fixture_resolver.resolve(
                        test_path,
                        self._active_owner,
                        argument.arg,
                    )
                    if fixture_instance is not None:
                        reference = fixture_instance
                self.env[argument.arg] = reference
        if root.args.vararg:
            self.env[root.args.vararg.arg] = _UnknownRef("test argument")
        if root.args.kwarg:
            self.env[root.args.kwarg.arg] = _UnknownRef("test argument")

    def _collect_module_imports(self, tree: ast.Module) -> None:
        for statement in tree.body:
            if isinstance(statement, (ast.Import, ast.ImportFrom)):
                self._record_import(statement)

    def _collect_module_setup_calls(
        self, tree: ast.Module
    ) -> dict[str, _ModuleSetupCall]:
        """Prove direct module assignments without evaluating module setup.

        A candidate must be the sole binding of its simple-name target.  Its
        call target is resolved using only imports that textually precede the
        assignment; every intervening non-import binding invalidates the local
        name it touches.  This keeps conditional imports, rebinding, and later
        imports outside the proof.
        """

        module_bindings = _scope_bindings(tree.body)
        candidates: dict[str, _ModuleSetupCall] = {}
        self.env = {}
        for statement in tree.body:
            candidate_name: str | None = None
            candidate_call: ast.Call | None = None
            if (
                isinstance(statement, ast.Assign)
                and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Name)
                and isinstance(statement.value, ast.Call)
            ):
                candidate_name = statement.targets[0].id
                candidate_call = statement.value

            if (
                candidate_name is not None
                and candidate_call is not None
                and module_bindings.get(candidate_name) == ["other"]
            ):
                target = self._resolve_expr(candidate_call.func)
                result: _Ref | None = None
                if isinstance(target, _SymbolRef):
                    if target.definition.kind == "function":
                        result = _UnknownRef("production factory return value")
                    elif target.definition.kind == "class" and isinstance(
                        self.index.resolve_constructor(target.definition), _SymbolRef
                    ):
                        result = _InstanceRef(target)
                if (
                    result is not None
                    and target.definition.path not in self.index._test_paths
                    and not _is_test_path(target.definition.path)
                ):
                    candidates[candidate_name] = _ModuleSetupCall(target, result)

            if isinstance(statement, (ast.Import, ast.ImportFrom)):
                self._record_import(statement)
                continue
            for name in _scope_bindings([statement]):
                self.env[name] = _AmbiguousRef("module name is rebound")
        return candidates

    def _mark_local_helpers(self) -> None:
        """Bind bare names to same-module helpers, never imported helpers."""

        for name in sorted(self._helper_registry.by_name):
            # A local definition shadows an import with the same spelling, as
            # Python's module binding rules do.  Ambiguous duplicate helpers
            # remain an explicit ``_TestHelperRef`` diagnostic.
            self.env[name] = self._helper_registry.resolve_name(name)

    def _bind_import(self, name: str, reference: _Ref) -> None:
        previous = self.env.get(name)
        if previous is None or previous == reference:
            self.env[name] = reference
        else:
            # Multiple static imports that bind one name are intentionally not
            # resolved by source order: a maintainer can select one branch at
            # runtime, so silently choosing one would make metadata unstable.
            self.env[name] = _AmbiguousRef("ambiguous import binding")

    def _record_import(self, node: ast.Import | ast.ImportFrom) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                local_name = alias.asname or alias.name.split(".", 1)[0]
                imported_module_name = alias.name
                if alias.asname is None and "." in alias.name:
                    imported_module_name = alias.name.split(".", 1)[0]
                paths = self.index.resolve_module(imported_module_name, self.test_path)
                if not paths and imported_module_name != alias.name:
                    # Namespace packages may not have a physical __init__.py;
                    # retain the root alias while using the concrete submodule
                    # path for later attribute resolution.
                    paths = self.index.resolve_module(alias.name, self.test_path)
                if paths:
                    self._bind_import(
                        local_name,
                        self.index._module_ref_or_helper(imported_module_name, paths),
                    )
                else:
                    self._bind_import(local_name, _UnknownRef("unresolved import"))
            return

        module_name = node.module or ""
        for alias in node.names:
            local_name = alias.asname or alias.name
            if alias.name == "*":
                self._bind_import(local_name, _AmbiguousRef("wildcard import"))
            else:
                self._bind_import(
                    local_name,
                    self.index.resolve_import(
                        module_name,
                        alias.name,
                        self.test_path,
                        node.level,
                    ),
                )

    def _diagnostic(self, node: ast.Call, expression: str, reason: str) -> None:
        diagnostic = InferenceDiagnostic(
            test_id=self.test_id,
            line=getattr(node, "lineno", None),
            expression=expression,
            reason=reason,
        )
        if diagnostic not in self.diagnostics:
            self.diagnostics.append(diagnostic)

    @staticmethod
    def _expression(node: ast.AST) -> str:
        try:
            return ast.unparse(node)
        except (AttributeError, ValueError):
            return node.__class__.__name__

    def _resolve_helper_attribute(self, node: ast.Attribute) -> _Ref | None:
        """Resolve ``self.helper`` against the active test/helper owner."""

        if not isinstance(node.value, ast.Name) or node.value.id != "self":
            return None
        helper = self._helper_registry.resolve_method(self._active_owner, node.attr)
        if helper.definition is not None or helper.reason != "test/helper call":
            return helper
        return None

    def _resolve_expr(self, node: ast.AST) -> _Ref:
        if isinstance(node, ast.Name):
            setup = self._module_setup_calls.get(node.id)
            if setup is not None and node.id not in self._local_bindings:
                self._record_module_setup_call(setup)
                return setup.result
            reference = self.env.get(node.id)
            if reference is not None:
                return reference
            helper = self._helper_registry.resolve_name(node.id, self._active_owner)
            if helper.definition is not None or helper.reason != "test/helper call":
                return helper
            return _UnknownRef("unresolved name")
        if isinstance(node, ast.Attribute):
            helper = self._resolve_helper_attribute(node)
            if helper is not None:
                return helper
            if (
                isinstance(node.value, ast.Name)
                and node.value.id == "self"
                and node.attr in self._self_attributes
            ):
                return self._self_attributes[node.attr]
            return self.index.resolve_attribute(
                self._resolve_expr(node.value), node.attr
            )
        if isinstance(node, ast.Call):
            cached = self._call_results.get(id(node))
            if cached is not None:
                return cached
            called = self._resolve_expr(node.func)
            if isinstance(called, _SymbolRef) and called.definition.kind == "class":
                return _InstanceRef(called)
            return _UnknownRef("unresolved return value")
        return _UnknownRef("unresolved expression")

    def _record_module_setup_call(self, setup: _ModuleSetupCall) -> None:
        """Attribute one proven module setup call to the active selected test."""

        target = setup.target
        if target.definition.kind == "class":
            constructor = self.index.resolve_constructor(target.definition)
            if not isinstance(constructor, _SymbolRef):
                return
            target = constructor
        definition = target.definition
        entry = TestEntry(
            test_id=self.test_id,
            path=definition.path,
            symbol=f"{definition.path}:{definition.qualname}",
            start_line=definition.start_line,
            end_line=definition.end_line,
        )
        if entry not in self.entries:
            self.entries.append(entry)

    @staticmethod
    def _is_builtin_name(node: ast.Call) -> bool:
        return isinstance(node.func, ast.Name) and node.func.id in dir(builtins)

    @staticmethod
    def _is_self_assert_raises(node: ast.Call) -> bool:
        """Return whether *node* is the exact unittest assertion protocol."""

        return (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "assertRaises"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
        )

    @classmethod
    def _is_side_effect_free_callable(cls, node: ast.AST) -> bool:
        """Accept only a name or a name-rooted attribute chain."""

        if isinstance(node, ast.Name):
            return True
        return isinstance(node, ast.Attribute) and cls._is_side_effect_free_callable(
            node.value
        )

    def _visit_assert_raises(self, node: ast.Call) -> None:
        """Model unittest's call-form ``assertRaises`` without execution.

        ``self.assertRaises(Error, target, *args, **kwargs)`` invokes ``target``
        with the arguments following it.  Only a side-effect-free, uniquely
        resolvable production function or class is eligible; the class still
        has to pass the existing explicit-constructor rule in ``_record_call``.
        The assertion method itself is test infrastructure and deliberately
        never becomes a call diagnostic.
        """

        if len(node.args) < 2:
            # This includes the context-manager form.  Its body, not a callable
            # argument, is the production invocation authority.
            return
        callable_node = node.args[1]
        if not self._is_side_effect_free_callable(callable_node):
            return

        target = self._resolve_expr(callable_node)
        if not isinstance(target, (_SymbolRef, _AmbiguousRef, _UnknownRef)) or (
            isinstance(target, _SymbolRef)
            and target.definition.kind not in {"class", "function"}
        ):
            self._diagnostic(
                node,
                self._expression(callable_node),
                "unsupported assertRaises callable",
            )
            return

        invoked = ast.Call(
            func=callable_node,
            args=list(node.args[2:]),
            keywords=list(node.keywords),
        )
        ast.copy_location(invoked, node)
        self.visit(invoked)

    def _argument_reference(self, value: ast.AST) -> _Ref:
        if isinstance(value, ast.Name):
            return self.env.get(value.id, _UnknownRef("unresolved helper argument"))
        if isinstance(value, ast.Call):
            # Evaluate a directly constructed production argument before the
            # helper body is entered.  ``visit_Call`` will encounter it again
            # while traversing the caller expression, but entry deduplication
            # makes that repeat deterministic.
            called = self._resolve_expr(value.func)
            if isinstance(called, _SymbolRef) and called.definition.kind == "class":
                self._record_call(value)
                return self._call_results.get(id(value), _InstanceRef(called))
            cached = self._call_results.get(id(value))
            if cached is not None:
                return cached
        # Literals and containers are intentionally opaque.  They may still be
        # iterated by a helper, but cannot safely identify a production target.
        return _UnknownRef("static helper argument")

    def _bind_helper_arguments(
        self,
        definition: _TestHelperDefinition,
        node: ast.Call,
    ) -> dict[str, _Ref] | None:
        """Bind only statically named helper arguments.

        Starred positional/keyword expansion is intentionally rejected: its
        runtime shape cannot be proven from the test source without executing
        code.  Missing/duplicate parameters likewise make the helper result
        unsafe to propagate.
        """

        parameters = [
            *definition.node.args.posonlyargs,
            *definition.node.args.args,
        ]
        keyword_parameters = {
            argument.arg: argument
            for argument in definition.node.args.kwonlyargs
        }
        bound: dict[str, _Ref] = {}
        implicit_receiver = bool(
            definition.owner
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
        )
        positional_index = 1 if implicit_receiver else 0
        if implicit_receiver and parameters:
            bound[parameters[0].arg] = _UnknownRef("test helper receiver")
        for argument in node.args:
            if isinstance(argument, ast.Starred) or positional_index >= len(parameters):
                return None
            bound[parameters[positional_index].arg] = self._argument_reference(argument)
            positional_index += 1
        for keyword in node.keywords:
            if keyword.arg is None:
                return None
            if keyword.arg in bound:
                return None
            if keyword.arg not in {argument.arg for argument in parameters} and (
                keyword.arg not in keyword_parameters
            ):
                return None
            bound[keyword.arg] = self._argument_reference(keyword.value)

        all_parameters = [*parameters, *definition.node.args.kwonlyargs]
        defaults_start = len(parameters) - len(definition.node.args.defaults)
        required = {
            argument.arg
            for index, argument in enumerate(parameters)
            if index < defaults_start
        }
        required.update(
            argument.arg
            for argument, default in zip(
                definition.node.args.kwonlyargs,
                definition.node.args.kw_defaults,
            )
            if default is None
        )
        if any(name not in bound for name in required):
            return None
        # ``self``/``cls`` is a test fixture receiver, not a production object.
        # Leave it unknown unless a caller provided an explicit argument.
        for argument in all_parameters:
            bound.setdefault(argument.arg, _UnknownRef("unbound helper argument"))
        if definition.node.args.vararg is not None:
            bound[definition.node.args.vararg.arg] = _UnknownRef(
                "unbound helper varargs"
            )
        if definition.node.args.kwarg is not None:
            bound[definition.node.args.kwarg.arg] = _UnknownRef(
                "unbound helper kwargs"
            )
        return bound

    @staticmethod
    def _combine_return_refs(refs: Sequence[_Ref]) -> _Ref:
        if not refs:
            return _UnknownRef("helper has no return value")
        unique = list(dict.fromkeys(refs))
        if len(unique) == 1:
            return unique[0]
        # Distinct branches (including an unresolved branch) are never guessed.
        return _AmbiguousRef("ambiguous helper return value")

    @classmethod
    def _statements_guarantee_return(cls, statements: Sequence[ast.stmt]) -> bool:
        """Conservatively prove that every reachable helper path returns.

        This is intentionally a tiny structural check, not a general control
        flow engine.  A plain ``if`` without an ``else`` (or loops/try blocks)
        is treated as potentially falling through.  A later unconditional
        return can still close that fall-through path.
        """

        for statement in statements:
            if isinstance(statement, (ast.Return, ast.Raise)):
                return True
            if isinstance(statement, ast.If):
                if (
                    statement.orelse
                    and cls._statements_guarantee_return(statement.body)
                    and cls._statements_guarantee_return(statement.orelse)
                ):
                    return True
                # The condition may fall through; continue checking statements
                # following the if for an unconditional return.
        return False

    def _inline_helper(self, node: ast.Call, helper: _TestHelperRef) -> _Ref:
        definition = helper.definition
        if definition is None:
            self._diagnostic(node, self._expression(node.func), helper.reason)
            return _UnknownRef(helper.reason)
        if definition in self._helper_stack:
            self._diagnostic(node, self._expression(node.func), "recursive test helper")
            return _AmbiguousRef("recursive test helper")
        max_depth = 8
        if self._helper_depth >= max_depth:
            self._diagnostic(
                node,
                self._expression(node.func),
                f"test helper depth exceeds {max_depth}",
            )
            return _AmbiguousRef("test helper depth exceeded")
        bound = self._bind_helper_arguments(definition, node)
        if bound is None:
            self._diagnostic(
                node,
                self._expression(node.func),
                "helper arguments are not statically bindable",
            )
            return _AmbiguousRef("unresolved helper arguments")

        # Reuse this analyzer's entry/diagnostic sinks so all production calls
        # retain the selected caller's test ID.  The child environment starts
        # from module imports/helpers and receives only the bound parameters.
        child_env = dict(self._module_env)
        child_env.update(bound)
        saved_env = self.env
        saved_owner = self._active_owner
        saved_returns = self._return_refs
        saved_stack = self._helper_stack
        saved_depth = self._helper_depth
        saved_return_seen = self._return_seen
        saved_local_bindings = self._local_bindings
        self.env = child_env
        self._active_owner = definition.owner
        self._return_refs = []
        self._return_seen = False
        self._helper_stack = (*saved_stack, definition)
        self._helper_depth = saved_depth + 1
        self._local_bindings = set(_function_scope_bindings(definition.node))
        try:
            self._visit_statements(definition.node.body)
            return_refs = list(self._return_refs)
            if not self._statements_guarantee_return(definition.node.body):
                return_refs.append(
                    _UnknownRef("helper may fall through without return")
                )
            result = self._combine_return_refs(return_refs)
        finally:
            self.env = saved_env
            self._active_owner = saved_owner
            self._return_refs = saved_returns
            self._helper_stack = saved_stack
            self._helper_depth = saved_depth
            self._return_seen = saved_return_seen
            self._local_bindings = saved_local_bindings
        if isinstance(result, (_AmbiguousRef, _UnknownRef)):
            self._diagnostic(node, self._expression(node.func), result.reason)
        return result

    def _record_call(self, node: ast.Call) -> None:
        target = self._resolve_expr(node.func)
        if isinstance(target, _TestHelperRef):
            self._call_results[id(node)] = self._inline_helper(node, target)
            return
        constructor_instance: _InstanceRef | None = None
        if isinstance(target, _SymbolRef) and target.definition.kind == "class":
            constructor_instance = _InstanceRef(target)
            constructor = self.index.resolve_constructor(target.definition)
            if isinstance(constructor, _SymbolRef):
                target = constructor
            elif isinstance(constructor, (_AmbiguousRef, _UnknownRef)):
                self._diagnostic(node, self._expression(node.func), constructor.reason)
                # The constructor span itself is intentionally omitted when it
                # is inherited/ambiguous, but the statically named class still
                # provides enough type information for a caller-side method
                # access (``Widget().render()``).
                self._call_results[id(node)] = constructor_instance
                return
        if isinstance(target, _SymbolRef):
            definition = target.definition
            if definition.path in self.index._test_paths or _is_test_path(
                definition.path
            ):
                self._diagnostic(
                    node, self._expression(node.func), "test/helper symbol"
                )
                return
            self.entries.append(
                TestEntry(
                    test_id=self.test_id,
                    path=definition.path,
                    symbol=f"{definition.path}:{definition.qualname}",
                    start_line=definition.start_line,
                    end_line=definition.end_line,
                )
            )
            self._call_results[id(node)] = (
                constructor_instance
                if constructor_instance is not None
                else _UnknownRef("production call return value")
            )
            return

        if isinstance(target, _AmbiguousRef):
            self._diagnostic(node, self._expression(node.func), target.reason)
            self._call_results[id(node)] = target
        elif isinstance(target, _UnknownRef) and not self._is_builtin_name(node):
            self._diagnostic(node, self._expression(node.func), target.reason)
            self._call_results[id(node)] = target
        elif not self._is_builtin_name(node):
            self._diagnostic(node, self._expression(node.func), "unresolved call")
            self._call_results[id(node)] = _UnknownRef("unresolved call")

    def _assign_target(self, target: ast.AST, reference: _Ref) -> None:
        if isinstance(target, ast.Name):
            self.env[target.id] = reference
        elif isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self._assign_target(element, _UnknownRef("unresolved assignment"))

    def _assignment_reference(self, value: ast.AST) -> _Ref:
        return self._resolve_expr(value)

    def _visit_statements(self, statements: Iterable[ast.stmt]) -> None:
        for statement in statements:
            self.visit(statement)
            if self._return_seen:
                break

    def visit_Import(self, node: ast.Import) -> None:
        self._record_import(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self._record_import(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        # Visit the value first so constructor calls are recorded before binding
        # the resulting instance.
        self.visit(node.value)
        reference = self._assignment_reference(node.value)
        for target in node.targets:
            self._assign_target(target, reference)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)
            self._assign_target(node.target, self._assignment_reference(node.value))

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        self._assign_target(node.target, self._assignment_reference(node.value))

    def visit_Return(self, node: ast.Return) -> None:
        if node.value is None:
            self._return_refs.append(_UnknownRef("helper returns None"))
            self._return_seen = True
            return
        # Visiting first records direct production calls in a return expression
        # and caches helper/constructor return references for resolution below.
        self.visit(node.value)
        self._return_refs.append(self._resolve_expr(node.value))
        self._return_seen = True

    def _merge_branch_envs(
        self,
        before: dict[str, _Ref],
        *branches: dict[str, _Ref],
    ) -> None:
        names = set(before)
        for branch in branches:
            names.update(branch)
        merged: dict[str, _Ref] = {}
        for name in names:
            unbound = _UnknownRef("unbound control-flow assignment")
            candidates = [
                branch.get(name, before.get(name, unbound)) for branch in branches
            ]
            if not candidates:
                continue
            first = candidates[0]
            if all(candidate == first for candidate in candidates):
                merged[name] = first
            else:
                merged[name] = _AmbiguousRef("ambiguous control-flow assignment")
        self.env = merged

    def visit_If(self, node: ast.If) -> None:
        # Calls in the condition are direct invocations too.  Branch-local
        # imports/assignments are merged conservatively so a name with multiple
        # possible production targets is never guessed.
        self.visit(node.test)
        before = dict(self.env)
        self.env = dict(before)
        self._return_seen = False
        self._visit_statements(node.body)
        body_env = dict(self.env)
        body_returned = self._return_seen
        self.env = dict(before)
        self._return_seen = False
        self._visit_statements(node.orelse)
        else_env = dict(self.env)
        else_returned = self._return_seen
        self._merge_branch_envs(before, body_env, else_env)
        # A branch return only terminates the enclosing path when both sides
        # return.  Otherwise callers must continue after this ``if`` and the
        # structural helper-return check below will include a fall-through.
        self._return_seen = bool(node.orelse) and body_returned and else_returned

    def visit_Call(self, node: ast.Call) -> None:
        if self._is_self_assert_raises(node):
            self._visit_assert_raises(node)
            return
        self._record_call(node)
        # A direct constructor can be the receiver of a method call
        # (``Widget(...).render()``).  Visit that nested call explicitly because
        # the generic visitor would otherwise only traverse call arguments.
        if isinstance(node.func, ast.Call):
            self.visit(node.func)
        elif isinstance(node.func, ast.Attribute) and isinstance(
            node.func.value, ast.Call
        ):
            self.visit(node.func.value)
        for argument in node.args:
            self.visit(argument)
        for keyword in node.keywords:
            self.visit(keyword.value)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self._resolve_expr(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # Nested helpers are not trusted test functions and must not leak their
        # production calls into the parent test's direct invocation set.
        if node is not self._root:
            return
        for statement in node.body:
            self.visit(statement)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return

    def analyze(self) -> tuple[list[TestEntry], list[InferenceDiagnostic]]:
        self.visit(self._root)
        return self.entries, self.diagnostics


def _discover_test_paths(
    repository: Path, test_paths: Iterable[Path | str] | None
) -> tuple[list[Path], list[Path]]:
    if test_paths is None:
        candidate_paths = [repository / "tests"]
    else:
        candidate_paths = [Path(path) for path in test_paths]
    discovered: set[Path] = set()
    resources: set[Path] = set()
    for candidate in candidate_paths:
        if not candidate.is_absolute():
            candidate = repository / candidate
        candidate = candidate.resolve()
        if candidate.is_dir():
            discovered.update(
                path for path in candidate.rglob("*.py") if path.is_file()
            )
        elif candidate.is_file() and candidate.suffix == ".py":
            discovered.add(candidate)
        elif candidate.is_file():
            # Data-driven suites often change a corpus rather than a Python
            # function.  Explicitly selected resources are traced to the test
            # setup block which opens them; arbitrary files found below a
            # selected directory are intentionally not inferred as resources.
            resources.add(candidate)
    return sorted(discovered), sorted(resources)


def _matching_resource_blocks(
    tree: ast.Module,
    test_path: str,
    resources: Sequence[str],
) -> list[tuple[str, ast.With | ast.AsyncWith]]:
    """Find setup blocks whose context expression names a trusted resource.

    Matching is deliberately literal and block-scoped: a resource basename (or
    repository-relative path) must occur in the ``with`` context expression.
    This covers conventional ``open(path_helper('cases.txt'))`` suites without
    guessing from arbitrary module-level strings or following runtime values.
    """

    matches: list[tuple[str, ast.With | ast.AsyncWith]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.With, ast.AsyncWith)):
            continue
        literals = {
            value.value
            for item in node.items
            for value in ast.walk(item.context_expr)
            if isinstance(value, ast.Constant) and isinstance(value.value, str)
        }
        for resource_id in resources:
            same_directory_basename = (
                Path(resource_id).parent == Path(test_path).parent
                and Path(resource_id).name in literals
            )
            if resource_id in literals or same_directory_basename:
                matches.append((resource_id, node))
    return matches


def infer_test_entries(
    repository: Path | str,
    test_paths: Iterable[Path | str] | None = None,
    *,
    tests: Iterable[Path | str] | None = None,
    test_files: Iterable[Path | str] | None = None,
    test_ids: Iterable[str] | None = None,
    selected_test_ids: Iterable[str] | None = None,
) -> InferenceResult:
    """Infer production entries directly invoked by selected trusted tests.

    ``repository`` is parsed as untrusted text.  ``test_paths`` can contain Python
    files, explicitly selected data resources, or directories and defaults to
    ``repository/tests``.  A data resource is traced only to a Python test setup
    block which names it literally.  All output is sorted by stable metadata
    fields, making repeated runs byte-stable after serialization.
    """

    provided_aliases = [
        candidate for candidate in (tests, test_files) if candidate is not None
    ]
    if test_paths is not None and provided_aliases:
        raise TypeError("pass test_paths, tests, or test_files, not more than one")
    if len(provided_aliases) > 1:
        raise TypeError("pass only one of tests and test_files")
    provided_test_id_aliases = [
        candidate
        for candidate in (test_ids, selected_test_ids)
        if candidate is not None
    ]
    if len(provided_test_id_aliases) > 1:
        raise TypeError("pass only one of test_ids and selected_test_ids")
    selected_test_paths = test_paths
    if selected_test_paths is None and provided_aliases:
        selected_test_paths = provided_aliases[0]
    selected_test_ids_value = (
        provided_test_id_aliases[0] if provided_test_id_aliases else None
    )
    selectors_supplied = selected_test_ids_value is not None

    root = Path(repository).resolve()
    if not root.is_dir():
        raise ValueError(f"repository is not a directory: {repository}")

    # An explicit selector is a closed analysis boundary.  Falling back to the
    # repository's whole ``tests`` tree when no path argument is supplied would
    # silently analyze peer tests and contaminate the denominator.  Selectors
    # are resolved first below and only their files are parsed in that case.
    if selected_test_paths is None and selectors_supplied:
        selected_paths, selected_resources = [], []
    else:
        selected_paths, selected_resources = _discover_test_paths(
            root, selected_test_paths
        )
    explicitly_selected_python = {path.resolve() for path in selected_paths}
    selector_diagnostics: list[InferenceDiagnostic] = []
    selectors_by_path: dict[str, set[str]] = {}
    if selectors_supplied:
        selectors_by_path, selector_diagnostics = _resolve_requested_test_selectors(
            root, selected_test_ids_value
        )
        for relative_path in selectors_by_path:
            candidate = (root / relative_path).resolve()
            if candidate.is_file():
                explicitly_selected_python.add(candidate)
                selected_paths.append(candidate)
        selected_paths = sorted(set(selected_paths))

    resource_specs: list[str] = []
    for resource in selected_resources:
        relative = _safe_relative_path(resource, root)
        if relative is None:
            continue
        resource_specs.append(relative)

    if resource_specs:
        # A non-Python test input is meaningful only through Python test setup.
        # Restrict the search to test-looking paths, then later analyze only the
        # exact ``with`` blocks containing a literal reference to the resource.
        for candidate in root.rglob("*.py"):
            relative = _safe_relative_path(candidate, root)
            if candidate.is_file() and relative is not None and _is_test_path(relative):
                selected_paths.append(candidate.resolve())
        selected_paths = sorted(set(selected_paths))
    relative_test_paths: set[str] = set()
    parsed_tests: list[
        tuple[
            str,
            str,
            ast.Module,
            list[tuple[str, ast.FunctionDef | ast.AsyncFunctionDef]],
        ]
    ] = []
    parsed_resource_blocks: list[
        tuple[str, str, ast.Module, ast.With | ast.AsyncWith]
    ] = []
    parse_diagnostics: list[InferenceDiagnostic] = []
    for path in selected_paths:
        relative = _safe_relative_path(path, root)
        if relative is None:
            parse_diagnostics.append(
                InferenceDiagnostic(
                    test_id=str(path),
                    line=None,
                    expression=str(path),
                    reason="test path is outside repository",
                )
            )
            continue
        relative_test_paths.add(relative)
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=relative)
        except (OSError, UnicodeDecodeError, SyntaxError) as exc:
            parse_diagnostics.append(
                InferenceDiagnostic(
                    test_id=relative,
                    line=None,
                    expression=relative,
                    reason=f"cannot parse test source: {exc.__class__.__name__}",
                )
            )
            continue
        collector = _TestFunctionCollector(relative)
        if path.resolve() in explicitly_selected_python:
            try:
                collector.visit(tree)
            except RecursionError:
                parse_diagnostics.append(
                    InferenceDiagnostic(
                        test_id=relative,
                        line=None,
                        expression=relative,
                        reason="cannot index test source: RecursionError",
                    )
                )
                continue
        parsed_tests.append((relative, relative, tree, collector.functions))
        for resource_id, block in _matching_resource_blocks(
            tree, relative, resource_specs
        ):
            parsed_resource_blocks.append((resource_id, relative, tree, block))

    index = _SourceIndex(root, relative_test_paths)
    entries: list[TestEntry] = []
    diagnostics = parse_diagnostics + selector_diagnostics + index.diagnostics
    for relative, _unused, tree, functions in parsed_tests:
        requested_qualnames = selectors_by_path.get(relative)
        if selectors_supplied and requested_qualnames is None:
            # Selectors are a closed analysis boundary even when none resolve.
            # A file explicitly supplied via ``test_paths`` is parsed only for
            # the source index and must never contribute trusted-test calls.
            functions = []
        elif requested_qualnames is not None:
            functions = [
                (qualname, function)
                for qualname, function in functions
                if qualname in requested_qualnames
            ]
        for qualname, function in functions:
            test_id = f"{relative}::{qualname}"
            analyzer = _TestBodyAnalyzer(
                index,
                test_id,
                relative,
                tree,
                function,
                helper_registry=_TestHelperRegistry(relative, tree),
            )
            function_entries, function_diagnostics = analyzer.analyze()
            entries.extend(function_entries)
            diagnostics.extend(function_diagnostics)

    for relative, requested_qualnames in selectors_by_path.items():
        parsed_qualnames = {
            qualname
            for path, _unused, _tree, functions in parsed_tests
            if path == relative
            for qualname, _function in functions
        }
        missing = sorted(requested_qualnames - parsed_qualnames)
        diagnostics.extend(
            InferenceDiagnostic(
                test_id=f"{relative}::{qualname}",
                line=None,
                expression=qualname,
                reason="requested test selector was not found in parsed test source",
            )
            for qualname in missing
        )

    for resource_id, relative, tree, block in parsed_resource_blocks:
        # Reuse the normal direct-call analyzer with a synthetic function shell;
        # no source is executed or modified, and the original AST nodes retain
        # their line coordinates for diagnostics.
        synthetic_root = ast.FunctionDef(
            name="test_resource_setup",
            args=ast.arguments(
                posonlyargs=[],
                args=[],
                kwonlyargs=[],
                kw_defaults=[],
                defaults=[],
            ),
            body=block.body,
            decorator_list=[],
        )
        analyzer = _TestBodyAnalyzer(
            index,
            f"{resource_id}::<module_setup>",
            relative,
            tree,
            synthetic_root,
            helper_registry=_TestHelperRegistry(relative, tree),
        )
        resource_entries, resource_diagnostics = analyzer.analyze()
        entries.extend(resource_entries)
        diagnostics.extend(resource_diagnostics)

    # Deduplicate repeated calls from one test and produce deterministic output.
    unique_entries = {
        (
            entry.test_id,
            entry.path,
            entry.symbol,
            entry.start_line,
            entry.end_line,
        ): entry
        for entry in entries
    }
    ordered_entries = [unique_entries[key] for key in sorted(unique_entries)]
    ordered_diagnostics = tuple(
        sorted(
            diagnostics,
            key=lambda diagnostic: (
                diagnostic.test_id,
                diagnostic.line or 0,
                diagnostic.expression,
                diagnostic.reason,
            ),
        )
    )
    metadata = TestEntryInferences(
        schema_version=SCHEMA_VERSION,
        language=LANGUAGE,
        source=SOURCE,
        entries=ordered_entries,
    )
    return InferenceResult(metadata=metadata, diagnostics=ordered_diagnostics)


def infer_entries(
    repository: Path | str,
    test_paths: Iterable[Path | str] | None = None,
    **kwargs: Iterable[Path | str] | None,
) -> InferenceResult:
    """Alias accepting the same test selection keywords as infer_test_entries."""

    return infer_test_entries(repository, test_paths, **kwargs)


infer_python_test_entries = infer_test_entries


def metadata_from_result(result: InferenceResult) -> dict[str, object]:
    """Return canonical JSON/YAML-ready metadata for a result."""

    return result.metadata.model_dump(mode="json")


__all__ = [
    "InferenceDiagnostic",
    "InferenceResult",
    "LANGUAGE",
    "SCHEMA_VERSION",
    "SOURCE",
    "infer_entries",
    "infer_python_test_entries",
    "infer_test_entries",
    "metadata_from_result",
]
