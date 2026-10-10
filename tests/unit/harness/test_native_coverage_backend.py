import json
import shlex
import shutil
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from testprism.harness.coverage_backends import get_default_coverage_backend_registry
from testprism.harness.native_coverage_backend import (
    GCOV_SOURCE_SUFFIXES,
    NATIVE_SOURCE_SUFFIXES,
    NativeCoverageBackend,
    normalize_gcov_reports,
    write_native_runtime_files,
)
from testprism.harness.testbench_harness import TestBenchHarness as RuntimeHarness
from testprism.harness.testbench_harness import _render_coverage_environment_assignment


def test_registry_selects_all_native_suffixes_deterministically():
    paths = [
        "src/z.hxx",
        "src/a.c",
        "src/d.hh",
        "src/b.cc",
        "src/g.hpp",
        "src/c.cpp",
        "src/e.cxx",
        "src/f.h",
    ]
    first = get_default_coverage_backend_registry().select(paths)
    second = get_default_coverage_backend_registry().select(reversed(paths))

    assert first.unsupported_paths == ()
    assert first.supported_paths == tuple(sorted(paths))
    assert first.supported_paths == second.supported_paths
    assert first.languages == ["native"]
    assert NATIVE_SOURCE_SUFFIXES == GCOV_SOURCE_SUFFIXES == (
        ".c",
        ".cc",
        ".cpp",
        ".cxx",
        ".h",
        ".hh",
        ".hpp",
        ".hxx",
    )
    assert (
        get_default_coverage_backend_registry().backend_for_path("src/main.CPP").language
        == "native"
    )


def test_gcov_normalizer_maps_requested_targets_and_ignores_non_statements(tmp_path):
    source = tmp_path / "src" / "main.c"
    source.parent.mkdir()
    source.write_text(
        "#include <stdio.h>\n"
        "int main(void) {\n"
        "  // comment\n"
        "  if (1) {\n"
        "    return 0;\n"
        "  }\n"
        "}\n"
    )
    other = tmp_path / "src" / "other.cpp"
    other.write_text("int other() {\n  return 1;\n}\n")
    outside = tmp_path.parent / "outside.c"
    outside.write_text("int outside() { return 1; }\n")
    raw = f"""
        -:    0:Source:{source}
        -:    0:Graph:main.gcno
        -:    0:Data:main.gcda
        -:    1:#include <stdio.h>
        1:    2:int main(void) {{
        -:    3:  // comment
        #####:    4:  if (1) {{
        1:    5:    return 0;
        -:    6:  }}
        -:    7:}}
        -:    0:Source:{outside}
        9:    1:int outside() {{ return 1; }}
    """

    report = normalize_gcov_reports(
        raw,
        workdir=tmp_path,
        target_paths=["src/main.c", "src/other.cpp"],
    )

    assert report == {
        "files": {
            "src/main.c": {
                "executed_lines": [2, 5],
                "missing_lines": [4],
            },
            "src/other.cpp": {
                "executed_lines": [],
                "missing_lines": [1, 2],
            },
        }
    }


def test_gcov_intermediate_format_and_malformed_data(tmp_path):
    target = tmp_path / "src" / "main.cc"
    target.parent.mkdir()
    target.write_text("int main() {\n  return 0;\n}\n")

    report = normalize_gcov_reports(
        "file:src/main.cc\nlcount:1,1\nlcount:2,0\n",
        workdir=tmp_path,
        target_paths=["src/main.cc"],
    )
    assert report["files"]["src/main.cc"] == {
        "executed_lines": [1],
        "missing_lines": [2],
    }
    with pytest.raises(ValueError, match="execution count"):
        normalize_gcov_reports(
            "file:src/main.cc\nlcount:2,not-a-count\n",
            workdir=tmp_path,
            target_paths=["src/main.cc"],
        )


def test_gcov_json_line_records_are_supported(tmp_path):
    target = tmp_path / "main.c"
    target.write_text("int main(void) {\n  return 0;\n}\n")
    report = normalize_gcov_reports(
        {
            "files": [
                {
                    "file": "main.c",
                    "lines": [
                        {"line_number": 1, "count": 2},
                        {"line_number": 2, "count": 0},
                    ],
                }
            ]
        },
        workdir=tmp_path,
        target_paths=["main.c"],
    )
    assert report["files"]["main.c"] == {
        "executed_lines": [1],
        "missing_lines": [2],
    }


def test_native_runtime_namespaces_and_flag_preservation(tmp_path):
    generated = write_native_runtime_files(
        tmp_path / "generated",
        workdir="/app",
        target_paths=["src/main.c", "src/header.hpp"],
        namespace="generated",
    )
    reference = write_native_runtime_files(
        tmp_path / "reference",
        workdir="/app",
        target_paths=["src/main.c", "src/header.hpp"],
        namespace="reference",
    )
    backend = NativeCoverageBackend()

    assert generated.counters_dir != reference.counters_dir
    assert generated.raw_dir != reference.raw_dir
    assert generated.json_path != reference.json_path
    assert backend.environment("/app", "generated")["GCOV_PREFIX"] == (
        generated.counters_dir
    )
    assert backend.environment("/app", "reference")["GCOV_PREFIX"] == (
        reference.counters_dir
    )
    assert "--coverage" in backend.setup_command("generated")
    assert "command -v gcc" in backend.setup_command("generated")
    assert "command -v gcov" in backend.setup_command("generated")
    assert "__TESTPRISM_COVERAGE_SETUP__=unsupported" in backend.setup_command(
        "generated"
    )
    collect = backend.collect_command("reference")
    assert "GCOV_PREFIX" not in collect
    assert "normalize_gcov_coverage.py" in collect
    assert "__TESTPRISM_COVERAGE_COLLECT__=unsupported" in collect
    assert "rm -rf /tmp/testbench/coverage/reference/native" not in collect
    manifest = json.loads((tmp_path / "generated" / "targets.json").read_text())
    assert [item["relative"] for item in manifest["targets"]] == [
        "src/header.hpp",
        "src/main.c",
    ]

    for key in ("CFLAGS", "CXXFLAGS", "LDFLAGS"):
        value = backend.environment("/app", "generated")[key]
        rendered = _render_coverage_environment_assignment(key, value)
        assert rendered.startswith(f"${{{key}:+${{{key}}} }}")
        assert rendered.endswith("--coverage")
        result = subprocess.run(
            [
                "sh",
                "-c",
                f"{key}='-O2 -DKEEP=1'; export {key}={rendered}; "
                f"printf '%s' \"${key}\"",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        assert result.stdout == "-O2 -DKEEP=1 --coverage"


def test_harness_persists_native_normalized_artifact(tmp_path):
    backend = NativeCoverageBackend()
    harness = object.__new__(RuntimeHarness)
    harness._logger = __import__("logging").getLogger("native-coverage-test")

    class Session:
        def __init__(self):
            self.command = ""

        def send_keys(self, keys, **_kwargs):
            self.command = keys[0]

        def capture_pane(self, capture_entire=False):
            del capture_entire
            if "COVERAGE_COLLECT" in self.command:
                return "__TESTPRISM_COVERAGE_COLLECT__=collected"
            return self.command

    class Terminal:
        def copy_from_container(self, source, destination):
            destination.mkdir(parents=True, exist_ok=True)
            (destination / Path(source).name).write_text(
                '{"files":{"src/main.cpp":{"executed_lines":[2],"missing_lines":[3]}}}'
            )

    handler = SimpleNamespace(
        task_id="native-task",
        trial_paths=SimpleNamespace(coverage_path=tmp_path / "coverage"),
    )
    report, error = harness._collect_coverage_artifact(
        Terminal(),
        Session(),
        handler,
        "gold",
        namespace="reference",
        backend=backend,
    )

    assert error is None
    assert report == (
        tmp_path
        / "coverage"
        / "reference"
        / "gold"
        / "native"
        / "coverage.json"
    )
    assert json.loads(report.read_text())["files"]["src/main.cpp"][
        "executed_lines"
    ] == [2]


def test_harness_setup_dispatches_native_backend(tmp_path):
    backend = NativeCoverageBackend()
    harness = object.__new__(RuntimeHarness)
    harness._logger = __import__("logging").getLogger("native-coverage-setup-test")

    class Session:
        def __init__(self):
            self.command = ""

        def send_keys(self, keys, **_kwargs):
            self.command = keys[0]

        def capture_pane(self, capture_entire=False):
            del capture_entire
            assert "COVERAGE_SETUP" in self.command
            return "__TESTPRISM_COVERAGE_SETUP__=ready"

    class Terminal:
        def __init__(self):
            self.copied = None

        def copy_to_container(self, root, container_dir):
            self.copied = (root, container_dir)

    handler = SimpleNamespace(
        task_id="native-setup-task",
        task=SimpleNamespace(workdir="/app"),
    )
    terminal = Terminal()
    environment, error = harness._setup_coverage_runtime(
        terminal,
        Session(),
        handler,
        ["src/main.c"],
        namespace="generated",
        backend=backend,
    )

    assert error is None
    assert environment is not None
    assert environment["GCOV_PREFIX"].endswith("/generated/native/counters")
    assert environment["CFLAGS"].endswith("--coverage")
    assert terminal.copied is not None
    assert terminal.copied[1].endswith("/generated/native")


@pytest.mark.skipif(
    not (
        (shutil.which("gcc") or shutil.which("clang") or shutil.which("cc"))
        and (
            shutil.which("gcov")
            or shutil.which("llvm-cov")
        )
        and (shutil.which("python3") or shutil.which("python"))
    ),
    reason="local compiler/gcov/Python capability is unavailable",
)
def test_native_runtime_setup_collect_round_trip_and_no_counter_status(tmp_path):
    """Exercise the real setup, redirected counters, gcov, and finalizer."""

    source = tmp_path / "main.c"
    source.write_text(
        "int choose(int value) {\n"
        "  if (value > 0) {\n"
        "    return 1;\n"
        "  }\n"
        "  return -1;\n"
        "}\n"
        "int main(void) { return choose(1) == 1 ? 0 : 1; }\n"
    )
    namespace = "pytest_native_" + uuid.uuid4().hex
    backend = NativeCoverageBackend()
    runtime_root = tmp_path / "runtime"
    files = write_native_runtime_files(
        runtime_root,
        workdir=str(tmp_path),
        target_paths=["main.c"],
        namespace=namespace,
    )
    container_root = Path(files.container_root)
    compiler = shutil.which("gcc") or shutil.which("clang") or shutil.which("cc")
    assert compiler is not None

    try:
        # The harness normally performs this copy through Terminal; this test
        # stages the exact payload at the backend's expected container path.
        container_root.mkdir(parents=True, exist_ok=True)
        for path in files.root.iterdir():
            destination = container_root / path.name
            if path.is_dir():
                shutil.copytree(path, destination, dirs_exist_ok=True)
            else:
                shutil.copy2(path, destination)

        setup = subprocess.run(
            ["sh", "-c", backend.setup_command(namespace)],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )
        assert "__TESTPRISM_COVERAGE_SETUP__=ready" in setup.stdout

        # No task build has run yet, so the backend must distinguish absent
        # instrumentation from an empty collected report.
        unsupported = subprocess.run(
            ["sh", "-c", backend.collect_command(namespace)],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )
        assert "__TESTPRISM_COVERAGE_COLLECT__=unsupported" in unsupported.stdout

        assignments = " && ".join(
            "export "
            + key
            + "="
            + _render_coverage_environment_assignment(key, value)
            for key, value in backend.environment(str(tmp_path), namespace).items()
        )
        command = (
            "CFLAGS='-O0 -DKEEP_CALLER_FLAG'; CXXFLAGS=''; LDFLAGS=''; "
            f"{assignments} && {shlex.quote(compiler)} $CFLAGS main.c "
            "$LDFLAGS -o app && ./app"
        )
        run = subprocess.run(
            ["sh", "-c", command],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )
        assert run.returncode == 0

        collected = subprocess.run(
            ["sh", "-c", backend.collect_command(namespace)],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )
        assert "__TESTPRISM_COVERAGE_COLLECT__=collected" in collected.stdout
        report = json.loads(Path(files.json_path).read_text())
        entry = report["files"]["main.c"]
        assert entry["executed_lines"]
        assert entry["missing_lines"]
        assert set(entry["executed_lines"]).isdisjoint(entry["missing_lines"])
        assert 5 in entry["missing_lines"]
    finally:
        # The integration test owns one unique namespace and removes exactly
        # that namespace, never the shared coverage root or task workdir.
        if container_root.exists():
            shutil.rmtree(container_root)
