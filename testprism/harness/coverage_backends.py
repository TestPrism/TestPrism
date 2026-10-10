"""Coverage backend contracts and deterministic source-target selection.

The patch metric and harness only need a small, normalized backend surface.  A
backend owns the runtime-specific setup and collection commands while this
module owns registration, suffix matching, and target-path validation.  The
registry exposes the existing vendored Python runtime plus built-in Node V8,
JaCoCo, gcov-compatible native, and Go ``covdata`` runtimes; later language
backends can be added without changing metric arithmetic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Iterable, Mapping


def normalize_target_path(path: str) -> str:
    """Return a safe repository-relative source path.

    Diff paths are normalized to POSIX separators and optional git ``a/`` or
    ``b/`` prefixes are removed.  Absolute paths, ``..`` components, empty
    paths, and NUL bytes are rejected so a registry cannot select a target
    outside the task workdir.
    """

    if not isinstance(path, str):
        raise TypeError("coverage target path must be a string")
    rendered = path.replace("\\", "/")
    if "\x00" in rendered:
        raise ValueError("coverage target path contains NUL")
    rendered = rendered.strip()
    while rendered.startswith("./"):
        rendered = rendered[2:]
    if rendered.startswith("/") or (
        len(rendered) >= 2 and rendered[1] == ":"
    ):
        raise ValueError(f"coverage target escapes workdir: {path}")
    if rendered.startswith("a/") or rendered.startswith("b/"):
        rendered = rendered[2:]
    candidate = PurePosixPath(rendered)
    if not rendered or rendered == "." or ".." in candidate.parts:
        raise ValueError(f"coverage target escapes workdir: {path}")
    # ``PurePosixPath`` collapses repeated separators and ``.`` components,
    # giving every equivalent diff spelling one deterministic registry key.
    normalized = candidate.as_posix()
    if not normalized or normalized == ".":
        raise ValueError(f"coverage target escapes workdir: {path}")
    return normalized


def normalize_source_suffix(suffix: str) -> str:
    """Normalize one registered source suffix for deterministic matching."""

    if not isinstance(suffix, str):
        raise TypeError("coverage source suffix must be a string")
    rendered = suffix.strip().lower()
    if not rendered:
        raise ValueError("coverage source suffix cannot be empty")
    if not rendered.startswith("."):
        rendered = "." + rendered
    if "/" in rendered or "\\" in rendered or rendered in {".", ".."}:
        raise ValueError(f"invalid coverage source suffix: {suffix!r}")
    return rendered


def source_suffix(path: str) -> str:
    """Return the normalized final suffix for a repository-relative path."""

    normalized = normalize_target_path(path)
    return PurePosixPath(normalized).suffix.lower()


class CoverageBackend:
    """Runtime contract consumed by the harness.

    Runtime methods intentionally have no Docker/session arguments.  They
    produce harness-owned files, shell commands, and environment mappings; the
    harness remains responsible for executing those values and persisting
    normalized reports.  A backend may override only the runtime methods it
    needs, while the registry uses ``language`` and ``source_suffixes`` for
    deterministic selection.
    """

    language: str = ""
    source_suffixes: tuple[str, ...] = ()

    @property
    def suffixes(self) -> tuple[str, ...]:
        """Compatibility alias used by backend implementations and tests."""

        return self.source_suffixes

    def supports_path(self, path: str) -> bool:
        """Whether this backend owns the normalized suffix of ``path``."""

        suffixes = {
            normalize_source_suffix(suffix) for suffix in self.source_suffixes
        }
        return source_suffix(path) in suffixes

    def write_runtime_files(
        self,
        root,
        *,
        workdir: str,
        target_paths: list[str],
        namespace: str,
    ):
        raise NotImplementedError

    def setup_command(self, namespace: str) -> str:
        raise NotImplementedError

    def environment(self, workdir: str, namespace: str) -> dict[str, str]:
        raise NotImplementedError

    def collect_command(self, namespace: str) -> str:
        raise NotImplementedError

    def normalized_report_path(self, namespace: str) -> str:
        """Container path of the backend's normalized line report."""

        raise NotImplementedError

    # ``finalize_command`` is the terminology used by the architecture
    # contract; ``collect_command`` remains the compatibility spelling used by
    # the existing Python runtime.
    def finalize_command(self, namespace: str) -> str:
        return self.collect_command(namespace)

    def report_path(self, namespace: str) -> str:
        return self.normalized_report_path(namespace)


@dataclass(frozen=True)
class CoverageBackendTargetGroup:
    """One deterministic backend plus the target paths it owns."""

    backend: CoverageBackend
    target_paths: tuple[str, ...] = ()

    @property
    def language(self) -> str:
        return self.backend.language


@dataclass(frozen=True)
class CoverageBackendSelection:
    """Deterministic mapping of changed target paths to registered backends."""

    backends_by_path: Mapping[str, CoverageBackend] = field(default_factory=dict)
    unsupported_paths: tuple[str, ...] = ()

    @property
    def supported_paths(self) -> tuple[str, ...]:
        return tuple(sorted(self.backends_by_path))

    @property
    def selected_paths(self) -> tuple[str, ...]:
        return self.supported_paths

    @property
    def unsupported_changed_files(self) -> tuple[str, ...]:
        return self.unsupported_paths

    @property
    def backend_by_path(self) -> Mapping[str, CoverageBackend]:
        return self.backends_by_path

    @property
    def languages(self) -> list[str]:
        return sorted({backend.language for backend in self.backends_by_path.values()})

    @property
    def language(self) -> str | None:
        languages = self.languages
        if not languages:
            return None
        return languages[0] if len(languages) == 1 else "mixed"

    @property
    def backend_groups(self) -> tuple[CoverageBackendTargetGroup, ...]:
        grouped: dict[int, tuple[CoverageBackend, list[str]]] = {}
        for path, backend in self.backends_by_path.items():
            key = id(backend)
            if key not in grouped:
                grouped[key] = (backend, [])
            grouped[key][1].append(path)
        return tuple(
            CoverageBackendTargetGroup(
                backend=backend,
                target_paths=tuple(sorted(paths)),
            )
            for backend, paths in sorted(
                grouped.values(),
                key=lambda item: item[0].language,
            )
        )

    def backend_for(self, path: str) -> CoverageBackend | None:
        """Resolve a path after applying the same safety normalization."""

        return self.backends_by_path.get(normalize_target_path(path))


class CoverageBackendRegistry:
    """Registry keyed by normalized source suffix.

    Registration rejects duplicate suffixes and invalid metadata.  Selection
    sorts normalized paths before resolving them, so equivalent input order
    cannot change backend/language output.
    """

    def __init__(self, backends: Iterable[CoverageBackend] = ()):
        self._by_suffix: dict[str, CoverageBackend] = {}
        self._autoload_builtin = False
        for backend in backends:
            self.register(backend)

    def _ensure_builtin_backends(self) -> None:
        """Lazily load optional managed runtimes without an import cycle."""

        if not self._autoload_builtin:
            return
        if not any(
            backend.language == "javascript"
            for backend in self._by_suffix.values()
        ):
            from testprism.harness.javascript_coverage_backend import (
                JAVASCRIPT_COVERAGE_BACKEND,
            )

            self.register(JAVASCRIPT_COVERAGE_BACKEND)
        if not any(
            backend.language == "java" for backend in self._by_suffix.values()
        ):
            from testprism.harness.java_coverage_backend import (
                JACOCO_COVERAGE_BACKEND,
            )

            self.register(JACOCO_COVERAGE_BACKEND)
        if not any(
            backend.language == "native" for backend in self._by_suffix.values()
        ):
            from testprism.harness.native_coverage_backend import (
                NATIVE_COVERAGE_BACKEND,
            )

            self.register(NATIVE_COVERAGE_BACKEND)
        if not any(backend.language == "go" for backend in self._by_suffix.values()):
            from testprism.harness.go_coverage_backend import (
                GO_COVERAGE_BACKEND,
            )

            self.register(GO_COVERAGE_BACKEND)

    @property
    def backends(self) -> tuple[CoverageBackend, ...]:
        self._ensure_builtin_backends()
        unique: dict[int, CoverageBackend] = {}
        for suffix in sorted(self._by_suffix):
            backend = self._by_suffix[suffix]
            unique.setdefault(id(backend), backend)
        return tuple(unique.values())

    def register(self, backend: CoverageBackend) -> CoverageBackend:
        """Register ``backend`` and return it for convenient composition."""

        language = getattr(backend, "language", "")
        if not isinstance(language, str) or not language.strip():
            raise ValueError("coverage backend language must be non-empty")
        suffixes = getattr(backend, "source_suffixes", None)
        if suffixes is None:
            suffixes = getattr(backend, "suffixes", ())
        normalized = tuple(sorted({normalize_source_suffix(item) for item in suffixes}))
        if not normalized:
            raise ValueError(f"coverage backend {language!r} has no source suffixes")
        # Store normalized metadata on simple test doubles as well as concrete
        # backends; this keeps matching deterministic without requiring a
        # particular inheritance hierarchy.
        try:
            backend.source_suffixes = normalized
        except (AttributeError, TypeError):
            pass
        for suffix in normalized:
            existing = self._by_suffix.get(suffix)
            if existing is not None and existing is not backend:
                raise ValueError(
                    f"coverage suffix {suffix!r} already registered by "
                    f"{existing.language!r}"
                )
            self._by_suffix[suffix] = backend
        return backend

    def backend_for_path(self, path: str) -> CoverageBackend | None:
        """Resolve one safe path by its normalized final suffix."""

        self._ensure_builtin_backends()
        return self._by_suffix.get(source_suffix(path))

    register_backend = register

    # Short aliases make the contract pleasant for downstream backends and
    # preserve the naming used by early harness experiments.
    resolve = backend_for_path
    for_path = backend_for_path

    def select(self, paths: Iterable[str]) -> CoverageBackendSelection:
        """Select registered backends and audit unsupported paths."""

        self._ensure_builtin_backends()
        mapping: dict[str, CoverageBackend] = {}
        unsupported: set[str] = set()
        normalized_paths = sorted({normalize_target_path(path) for path in paths})
        for path in normalized_paths:
            backend = self._by_suffix.get(source_suffix(path))
            if backend is None:
                unsupported.add(path)
            else:
                mapping[path] = backend
        return CoverageBackendSelection(
            backends_by_path=dict(sorted(mapping.items())),
            unsupported_paths=tuple(sorted(unsupported)),
        )

    select_paths = select
    select_backends = select
    select_files = select

    def select_target_paths(self, paths: Iterable[str]) -> CoverageBackendSelection:
        """Alias clarifying that callers pass changed source targets."""

        return self.select(paths)

    select_targets = select_target_paths

    def backend_for_targets(self, paths: Iterable[str]) -> CoverageBackend | None:
        """Return the sole backend for ``paths`` when selection is unambiguous."""

        selection = self.select(paths)
        backends = {
            id(backend): backend for backend in selection.backends_by_path.values()
        }
        if selection.unsupported_paths or len(backends) != 1:
            return None
        return next(iter(backends.values()))


class PythonCoverageBackend(CoverageBackend):
    """Adapter exposing the existing vendored coverage.py runtime contract."""

    language = "python"
    source_suffixes = (".py", ".pyi", ".pyx", ".bin")

    def write_runtime_files(
        self,
        root,
        *,
        workdir: str,
        target_paths: list[str],
        namespace: str = "generated",
    ):
        from testprism.harness.coverage_runtime import write_runtime_files

        return write_runtime_files(
            root,
            workdir=workdir,
            target_paths=target_paths,
            namespace=namespace,
        )

    def setup_command(self, namespace: str = "generated") -> str:
        from testprism.harness.coverage_runtime import coverage_setup_command

        return coverage_setup_command(namespace)

    def environment(self, workdir: str, namespace: str = "generated") -> dict[str, str]:
        from testprism.harness.coverage_runtime import coverage_environment

        return coverage_environment(workdir, namespace)

    def collect_command(self, namespace: str = "generated") -> str:
        from testprism.harness.coverage_runtime import coverage_collect_command

        return coverage_collect_command(namespace)

    def normalized_report_path(self, namespace: str = "generated") -> str:
        from testprism.harness.coverage_runtime import coverage_namespace

        return coverage_namespace(namespace).json_path


PYTHON_COVERAGE_BACKEND = PythonCoverageBackend()
DEFAULT_COVERAGE_BACKEND_REGISTRY = CoverageBackendRegistry(
    (PYTHON_COVERAGE_BACKEND,)
)
DEFAULT_COVERAGE_BACKEND_REGISTRY._autoload_builtin = True

# Public aliases used by the harness and downstream backend registration.
COVERAGE_BACKEND_REGISTRY = DEFAULT_COVERAGE_BACKEND_REGISTRY
default_coverage_backend_registry = DEFAULT_COVERAGE_BACKEND_REGISTRY


def get_default_coverage_backend_registry() -> CoverageBackendRegistry:
    """Return the process-wide additive registry."""

    DEFAULT_COVERAGE_BACKEND_REGISTRY._ensure_builtin_backends()
    return DEFAULT_COVERAGE_BACKEND_REGISTRY


def __getattr__(name: str):
    """Expose managed-runtime aliases without forcing an import cycle."""

    if name == "JAVASCRIPT_COVERAGE_BACKEND":
        from testprism.harness.javascript_coverage_backend import (
            JAVASCRIPT_COVERAGE_BACKEND,
        )

        return JAVASCRIPT_COVERAGE_BACKEND
    if name in {"JAVA_COVERAGE_BACKEND", "JACOCO_COVERAGE_BACKEND"}:
        from testprism.harness.java_coverage_backend import (
            JACOCO_COVERAGE_BACKEND,
        )

        return JACOCO_COVERAGE_BACKEND
    if name in {
        "NATIVE_COVERAGE_BACKEND",
        "GCOV_COVERAGE_BACKEND",
        "C_CPP_COVERAGE_BACKEND",
    }:
        from testprism.harness.native_coverage_backend import (
            NATIVE_COVERAGE_BACKEND,
        )

        return NATIVE_COVERAGE_BACKEND
    if name == "GO_COVERAGE_BACKEND":
        from testprism.harness.go_coverage_backend import GO_COVERAGE_BACKEND

        return GO_COVERAGE_BACKEND
    raise AttributeError(name)


# Descriptive aliases keep the public contract easy to discover without
# coupling downstream backends to one historical class spelling.
BackendSelection = CoverageBackendSelection
CoverageBackendSpec = CoverageBackend
normalize_path = normalize_target_path
