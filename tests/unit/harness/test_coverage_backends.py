from pathlib import Path
from types import SimpleNamespace

import pytest

from testprism.harness.coverage_backends import (
    CoverageBackend,
    CoverageBackendRegistry,
    PythonCoverageBackend,
    get_default_coverage_backend_registry,
    normalize_source_suffix,
    normalize_target_path,
)
from testprism.harness.testbench_harness import TestBenchHarness as RuntimeHarness


class TextCoverageBackend(CoverageBackend):
    language = "text"
    source_suffixes = ("TXT",)


class RecordingTextBackend(CoverageBackend):
    language = "text"
    source_suffixes = (".txt",)

    def __init__(self):
        self.events: list[tuple[str, str]] = []

    def write_runtime_files(
        self,
        root: Path,
        *,
        workdir: str,
        target_paths: list[str],
        namespace: str,
    ):
        self.events.append(("write", namespace))
        root.mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(
            root=root,
            container_root="/tmp/testbench/coverage/text",
        )

    def setup_command(self, namespace: str) -> str:
        self.events.append(("setup_command", namespace))
        return "echo __TESTPRISM_COVERAGE_SETUP__=ready"

    def environment(self, workdir: str, namespace: str) -> dict[str, str]:
        self.events.append(("environment", namespace))
        return {"TEXT_COVERAGE_NAMESPACE": namespace}

    def collect_command(self, namespace: str) -> str:
        self.events.append(("collect_command", namespace))
        return "echo __TESTPRISM_COVERAGE_COLLECT__=collected"

    def normalized_report_path(self, namespace: str) -> str:
        self.events.append(("report_path", namespace))
        return f"/tmp/testbench/coverage/text/{namespace}/report.json"


class RecordingSuffixBackend(CoverageBackend):
    def __init__(
        self,
        language: str,
        suffix: str,
        *,
        setup_status: str = "ready",
        collect_status: str = "collected",
    ):
        self.language = language
        self.source_suffixes = (suffix,)
        self.setup_status = setup_status
        self.collect_status = collect_status
        self.events: list[tuple[str, str]] = []

    def write_runtime_files(
        self,
        root: Path,
        *,
        workdir: str,
        target_paths: list[str],
        namespace: str,
    ):
        del workdir, target_paths
        self.events.append(("write", namespace))
        root.mkdir(parents=True, exist_ok=True)
        return SimpleNamespace(
            root=root,
            container_root=f"/tmp/testbench/coverage/{self.language}",
        )

    def setup_command(self, namespace: str) -> str:
        self.events.append(("setup_command", namespace))
        return (
            f"echo {self.language}-setup && "
            f"echo __TESTPRISM_COVERAGE_SETUP__={self.setup_status}"
        )

    def environment(self, workdir: str, namespace: str) -> dict[str, str]:
        del workdir
        self.events.append(("environment", namespace))
        return {f"{self.language.upper()}_COVERAGE_NAMESPACE": namespace}

    def collect_command(self, namespace: str) -> str:
        self.events.append(("collect_command", namespace))
        return (
            f"echo {self.language}-collect && "
            f"echo __TESTPRISM_COVERAGE_COLLECT__={self.collect_status}"
        )

    def normalized_report_path(self, namespace: str) -> str:
        self.events.append(("report_path", namespace))
        return f"/tmp/testbench/coverage/{self.language}/{namespace}/coverage.json"


def test_registry_normalizes_suffixes_and_selects_deterministically():
    registry = CoverageBackendRegistry(
        [PythonCoverageBackend(), TextCoverageBackend()]
    )

    selection = registry.select(["./notes.TXT", "pkg/mod.py", "pkg/mod.py"])

    assert selection.supported_paths == ("notes.TXT", "pkg/mod.py")
    assert selection.unsupported_paths == ()
    assert selection.languages == ["python", "text"]
    assert selection.language == "mixed"
    assert selection.backend_for("./pkg/mod.py").language == "python"
    assert normalize_source_suffix(" TXT ") == ".txt"


def test_registry_audits_unsupported_paths_and_rejects_traversal():
    registry = CoverageBackendRegistry([PythonCoverageBackend()])

    selection = registry.select(["pkg/mod.py", "native/tool.rs"])

    assert selection.supported_paths == ("pkg/mod.py",)
    assert selection.unsupported_paths == ("native/tool.rs",)
    with pytest.raises(ValueError, match="escapes workdir"):
        registry.select(["../outside.py"])
    with pytest.raises(ValueError, match="escapes workdir"):
        normalize_target_path("/tmp/outside.py")


def test_default_registry_selects_go_targets_and_preserves_mixed_audit():
    registry = get_default_coverage_backend_registry()

    selection = registry.select(["pkg/add.go", "pkg/mod.py", "native/tool.rs"])

    assert selection.backend_for("pkg/add.go") is not None
    assert selection.backend_for("pkg/add.go").language == "go"
    assert selection.languages == ["go", "python"]
    assert selection.language == "mixed"
    assert selection.unsupported_paths == ("native/tool.rs",)


def test_registry_groups_supported_paths_by_backend_language():
    registry = CoverageBackendRegistry(
        [
            RecordingSuffixBackend("note", ".md"),
            RecordingSuffixBackend("text", ".txt"),
        ]
    )

    selection = registry.select(
        ["docs/guide.md", "notes/todo.txt", "docs/faq.md", "notes/todo.txt"]
    )

    assert [
        (group.language, group.target_paths)
        for group in selection.backend_groups
    ] == [
        ("note", ("docs/faq.md", "docs/guide.md")),
        ("text", ("notes/todo.txt",)),
    ]


def test_registry_rejects_duplicate_suffixes():
    class AnotherPython(CoverageBackend):
        language = "another"
        source_suffixes = (".py",)

    with pytest.raises(ValueError, match="already registered"):
        CoverageBackendRegistry([PythonCoverageBackend(), AnotherPython()])


def test_python_backend_preserves_runtime_command_contract():
    backend = PythonCoverageBackend()

    assert backend.language == "python"
    assert backend.suffixes == (".py", ".pyi", ".pyx", ".bin")
    assert "coverage combine --keep" in backend.collect_command("reference")
    assert backend.report_path("reference").endswith("reference/coverage.json")
    environment = backend.environment("/testbed", "generated")
    assert environment == {}


def test_default_registry_supports_noncanonical_python_and_vue_targets():
    registry = get_default_coverage_backend_registry()

    assert registry.backend_for_path("pkg/module.pyi").language == "python"
    assert registry.backend_for_path("pkg/module.pyx").language == "python"
    assert registry.backend_for_path("bin/tool.bin").language == "python"
    assert registry.backend_for_path("src/App.vue").language == "javascript"


def test_harness_setup_and_collection_use_the_selected_non_python_backend(tmp_path):
    backend = RecordingTextBackend()
    harness = object.__new__(RuntimeHarness)
    harness._coverage_registry = CoverageBackendRegistry([backend])
    harness._logger = __import__("logging").getLogger("coverage-backend-test")

    class Session:
        def __init__(self):
            self.last_command = ""

        def send_keys(self, keys, **kwargs):
            self.last_command = keys[0]

        def capture_pane(self, capture_entire=False):
            if "COVERAGE_SETUP" in self.last_command:
                return "__TESTPRISM_COVERAGE_SETUP__=ready"
            if "COVERAGE_COLLECT" in self.last_command:
                return "__TESTPRISM_COVERAGE_COLLECT__=collected"
            return self.last_command

    class Terminal:
        def __init__(self):
            self.copied_report_paths: list[str] = []

        def copy_to_container(self, *args, **kwargs):
            return None

        def copy_from_container(self, source, destination):
            self.copied_report_paths.append(source)
            destination.mkdir(parents=True, exist_ok=True)
            (destination / Path(source).name).write_text('{"files": {}}')

    handler = SimpleNamespace(
        task_id="text-task",
        task=SimpleNamespace(workdir="/app"),
        trial_paths=SimpleNamespace(coverage_path=tmp_path / "coverage"),
    )
    terminal = Terminal()
    session = Session()

    environment, setup_error = harness._setup_coverage_runtime(
        terminal,
        session,
        handler,
        ["pkg/mod.txt"],
        namespace="generated",
    )
    assert setup_error is None
    assert environment == {"TEXT_COVERAGE_NAMESPACE": "generated"}

    report, collection_error = harness._collect_coverage_artifact(
        terminal,
        session,
        handler,
        "baseline",
        namespace="generated",
        backend=backend,
    )
    assert collection_error is None
    assert report == (
        tmp_path / "coverage" / "generated" / "baseline" / "text" / "report.json"
    )
    assert terminal.copied_report_paths == [
        "/tmp/testbench/coverage/text/generated/report.json"
    ]
    assert [name for name, _namespace in backend.events] == [
        "write",
        "setup_command",
        "environment",
        "report_path",
        "collect_command",
    ]


def test_harness_multi_backend_runtime_setup_and_collection_merge_reports(tmp_path):
    note_backend = RecordingSuffixBackend("note", ".md")
    text_backend = RecordingSuffixBackend("text", ".txt")
    harness = object.__new__(RuntimeHarness)
    harness._coverage_registry = CoverageBackendRegistry(
        [note_backend, text_backend]
    )
    harness._logger = __import__("logging").getLogger("coverage-backend-test")

    class Session:
        def __init__(self):
            self.last_command = ""

        def send_keys(self, keys, **kwargs):
            self.last_command = keys[0]

        def capture_pane(self, capture_entire=False):
            if "__TESTPRISM_COVERAGE_SETUP__=" in self.last_command:
                status = self.last_command.rsplit(
                    "__TESTPRISM_COVERAGE_SETUP__=",
                    1,
                )[1]
                return f"__TESTPRISM_COVERAGE_SETUP__={status}"
            if "__TESTPRISM_COVERAGE_COLLECT__=" in self.last_command:
                status = self.last_command.rsplit(
                    "__TESTPRISM_COVERAGE_COLLECT__=",
                    1,
                )[1]
                return f"__TESTPRISM_COVERAGE_COLLECT__={status}"
            return self.last_command

    class Terminal:
        def __init__(self):
            self.copied_report_paths: list[str] = []

        def copy_to_container(self, *args, **kwargs):
            return None

        def copy_from_container(self, source, destination):
            self.copied_report_paths.append(source)
            destination.mkdir(parents=True, exist_ok=True)
            (destination / Path(source).name).write_text('{"files": {}}')

    handler = SimpleNamespace(
        task_id="mixed-task",
        task=SimpleNamespace(workdir="/app"),
        trial_paths=SimpleNamespace(coverage_path=tmp_path / "coverage"),
    )
    terminal = Terminal()
    session = Session()

    environment, groups, setup_error = harness._setup_coverage_runtimes(
        terminal,
        session,
        handler,
        ["docs/guide.md", "notes/todo.txt"],
        namespace="generated",
    )
    assert setup_error is None
    assert environment == {
        "NOTE_COVERAGE_NAMESPACE": "generated",
        "TEXT_COVERAGE_NAMESPACE": "generated",
    }
    assert [(group.language, group.target_paths) for group in groups] == [
        ("note", ("docs/guide.md",)),
        ("text", ("notes/todo.txt",)),
    ]

    reports, collection_error = harness._collect_coverage_artifacts(
        terminal,
        session,
        handler,
        "gold",
        namespace="generated",
        groups=groups,
    )
    assert collection_error is None
    assert reports == {
        "note": (
            tmp_path / "coverage" / "generated" / "gold" / "note" / "coverage.json"
        ),
        "text": (
            tmp_path / "coverage" / "generated" / "gold" / "text" / "coverage.json"
        ),
    }
    assert terminal.copied_report_paths == [
        "/tmp/testbench/coverage/note/generated/coverage.json",
        "/tmp/testbench/coverage/text/generated/coverage.json",
    ]


def test_harness_multi_backend_runtime_preserves_language_specific_diagnostics(
    tmp_path,
):
    note_backend = RecordingSuffixBackend("note", ".md")
    text_backend = RecordingSuffixBackend(
        "text",
        ".txt",
        setup_status="unsupported",
    )
    harness = object.__new__(RuntimeHarness)
    harness._coverage_registry = CoverageBackendRegistry(
        [note_backend, text_backend]
    )
    harness._logger = __import__("logging").getLogger("coverage-backend-test")

    class Session:
        def __init__(self):
            self.last_command = ""

        def send_keys(self, keys, **kwargs):
            self.last_command = keys[0]

        def capture_pane(self, capture_entire=False):
            if "__TESTPRISM_COVERAGE_SETUP__=" in self.last_command:
                status = self.last_command.rsplit(
                    "__TESTPRISM_COVERAGE_SETUP__=",
                    1,
                )[1]
                return f"__TESTPRISM_COVERAGE_SETUP__={status}"
            if "__TESTPRISM_COVERAGE_COLLECT__=" in self.last_command:
                status = self.last_command.rsplit(
                    "__TESTPRISM_COVERAGE_COLLECT__=",
                    1,
                )[1]
                return f"__TESTPRISM_COVERAGE_COLLECT__={status}"
            return self.last_command

    class Terminal:
        def copy_to_container(self, *args, **kwargs):
            return None

        def copy_from_container(self, source, destination):
            destination.mkdir(parents=True, exist_ok=True)
            (destination / Path(source).name).write_text('{"files": {}}')

    handler = SimpleNamespace(
        task_id="mixed-task",
        task=SimpleNamespace(workdir="/app"),
        trial_paths=SimpleNamespace(coverage_path=tmp_path / "coverage"),
    )

    environment, groups, setup_error = harness._setup_coverage_runtimes(
        Terminal(),
        Session(),
        handler,
        ["docs/guide.md", "notes/todo.txt"],
        namespace="generated",
    )

    assert environment == {"NOTE_COVERAGE_NAMESPACE": "generated"}
    assert [(group.language, group.target_paths) for group in groups] == [
        ("note", ("docs/guide.md",)),
    ]
    assert (
        setup_error
        == "text coverage package setup failed in task container "
        "(status=unsupported)"
    )
