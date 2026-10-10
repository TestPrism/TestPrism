import json
import shutil
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from testprism.harness.go_coverage_backend import (
    GoCoverageBackend,
    go_coverage_namespace,
    normalize_go_coverage_profiles,
    write_go_runtime_files,
)
from testprism.harness.testbench_harness import TestBenchHarness as RuntimeHarness
from testprism.harness.testbench_harness import (
    _render_coverage_environment_assignment,
)


def test_go_normalizer_maps_import_paths_and_fills_absent_target(tmp_path):
    source = tmp_path / "pkg" / "add.go"
    source.parent.mkdir()
    source.write_text(
        "package pkg\n"
        "\n"
        "func Add(a, b int) int {\n"
        "    sum := a + b\n"
        "    return sum\n"
        "}\n"
    )
    absent = tmp_path / "pkg" / "missing.go"
    absent.write_text(
        "package pkg\n"
        "\n"
        "func Missing() int {\n"
        "    value := 2\n"
        "    return value\n"
        "}\n"
    )
    profile = (
        "mode: set\n"
        "example.com/demo/pkg/add.go:3.1,6.1 2 1\n"
    )

    report = normalize_go_coverage_profiles(
        profile,
        workdir=tmp_path,
        target_paths=["pkg/add.go", "pkg/missing.go"],
    )

    assert report == {
        "files": {
            "pkg/add.go": {
                "executed_lines": [3, 4, 5],
                "missing_lines": [],
            },
            "pkg/missing.go": {
                "executed_lines": [],
                "missing_lines": [3, 4, 5],
            },
        }
    }


@pytest.mark.parametrize(
    ("profile", "message"),
    [
        (
            "mode: set\n../pkg/add.go:3.1,4.1 1 1\n",
            "escapes workdir",
        ),
        (
            "mode: set\n/tmp/outside.go:3.1,4.1 1 1\n",
            "escapes workdir",
        ),
        (
            "mode: set\npkg/add.go:3.1,3.1 1 1\n",
            "block range is invalid",
        ),
        (
            "pkg/add.go:3.1,4.1 1 1\n",
            "missing mode header",
        ),
    ],
)
def test_go_normalizer_rejects_malformed_and_escaping_profiles(
    tmp_path,
    profile,
    message,
):
    target = tmp_path / "pkg" / "add.go"
    target.parent.mkdir()
    target.write_text(
        "package pkg\n"
        "\n"
        "func Add() int {\n"
        "    return 1\n"
        "}\n"
    )

    with pytest.raises(ValueError, match=message):
        normalize_go_coverage_profiles(
            profile,
            workdir=tmp_path,
            target_paths=["pkg/add.go"],
        )


def test_go_runtime_namespaces_and_wrapper_path_preservation(tmp_path):
    generated = write_go_runtime_files(
        tmp_path / "generated",
        workdir="/app",
        target_paths=["pkg/add.go", "pkg/more.go"],
        namespace="generated",
    )
    reference = write_go_runtime_files(
        tmp_path / "reference",
        workdir="/app",
        target_paths=["pkg/add.go", "pkg/more.go"],
        namespace="reference",
    )
    backend = GoCoverageBackend()

    assert generated.covdata_dir != reference.covdata_dir
    assert generated.profile_path != reference.profile_path
    assert generated.json_path != reference.json_path
    assert generated.wrapper_dir != reference.wrapper_dir
    assert backend.environment("/app", "generated")["GOCOVERDIR"] == (
        generated.covdata_dir
    )
    assert backend.environment("/app", "reference")["GOCOVERDIR"] == (
        reference.covdata_dir
    )
    setup = backend.setup_command("generated")
    assert "tool covdata textfmt -h" in setup
    assert "__TESTPRISM_COVERAGE_SETUP__=unsupported" in setup
    collect = backend.collect_command("reference")
    assert "tool covdata textfmt" in collect
    assert "normalize_go_coverage.go" in collect
    assert "__TESTPRISM_COVERAGE_COLLECT__=unsupported" in collect
    manifest = json.loads((tmp_path / "generated" / "targets.json").read_text())
    assert [item["relative"] for item in manifest["targets"]] == [
        "pkg/add.go",
        "pkg/more.go",
    ]
    assert (tmp_path / "generated" / "packages.txt").read_text().splitlines() == [
        "./pkg"
    ]
    rendered = _render_coverage_environment_assignment(
        "PATH",
        backend.environment("/app", "generated")["PATH"],
    )
    result = subprocess.run(
        [
            "sh",
            "-c",
            f"PATH='/usr/bin:/bin'; export PATH={rendered}; printf '%s' \"$PATH\"",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout == f"{generated.wrapper_dir}:/usr/bin:/bin"


def test_harness_persists_go_normalized_artifact(tmp_path):
    backend = GoCoverageBackend()
    harness = object.__new__(RuntimeHarness)
    harness._logger = __import__("logging").getLogger("go-coverage-test")

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
                '{"files":{"pkg/add.go":{"executed_lines":[3],"missing_lines":[4]}}}'
            )

    handler = SimpleNamespace(
        task_id="go-task",
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
        / "go"
        / "coverage.json"
    )
    assert json.loads(report.read_text())["files"]["pkg/add.go"]["executed_lines"] == [
        3
    ]


def test_harness_setup_dispatches_go_backend(tmp_path):
    backend = GoCoverageBackend()
    harness = object.__new__(RuntimeHarness)
    harness._logger = __import__("logging").getLogger("go-coverage-setup-test")

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
        task_id="go-setup-task",
        task=SimpleNamespace(workdir="/app"),
    )
    terminal = Terminal()
    environment, error = harness._setup_coverage_runtime(
        terminal,
        Session(),
        handler,
        ["pkg/add.go"],
        namespace="generated",
        backend=backend,
    )

    assert error is None
    assert environment is not None
    assert environment["GOCOVERDIR"].endswith("/generated/go/covdata")
    assert environment["PATH"].startswith("/tmp/testbench/coverage/generated/go/bin")
    assert terminal.copied is not None
    assert terminal.copied[1].endswith("/generated/go")


@pytest.mark.skipif(
    not shutil.which("go"),
    reason="local Go capability is unavailable",
)
def test_go_runtime_wrapper_keeps_go_list_unmodified_and_collects_covdata(
    tmp_path,
):
    workdir = tmp_path / "go-task"
    workdir.mkdir()
    subprocess.run(
        ["go", "mod", "init", "example.com/demo"],
        cwd=workdir,
        check=True,
        capture_output=True,
        text=True,
    )
    package_dir = workdir / "pkg"
    package_dir.mkdir()
    (package_dir / "add.go").write_text(
        "package pkg\n"
        "\n"
        "func Add(a, b int) int {\n"
        "    if a > b {\n"
        "        return a - b\n"
        "    }\n"
        "    return a + b\n"
        "}\n"
    )
    (package_dir / "add_test.go").write_text(
        "package pkg\n"
        "\n"
        "import \"testing\"\n"
        "\n"
        "func TestAdd(t *testing.T) {\n"
        "    if Add(1, 2) != 3 {\n"
        "        t.Fatal(\"bad add\")\n"
        "    }\n"
        "}\n"
    )
    backend = GoCoverageBackend()
    namespace = "pytest_go_" + uuid.uuid4().hex
    files = write_go_runtime_files(
        tmp_path / "runtime",
        workdir=str(workdir),
        target_paths=["pkg/add.go"],
        namespace=namespace,
    )
    container_root = Path(go_coverage_namespace(namespace)["root"])

    try:
        container_root.mkdir(parents=True, exist_ok=True)
        for path in files.root.iterdir():
            destination = container_root / path.name
            if path.is_dir():
                shutil.copytree(path, destination, dirs_exist_ok=True)
            else:
                shutil.copy2(path, destination)

        setup = subprocess.run(
            ["sh", "-c", backend.setup_command(namespace)],
            cwd=workdir,
            check=True,
            capture_output=True,
            text=True,
        )
        assert "__TESTPRISM_COVERAGE_SETUP__=ready" in setup.stdout

        unsupported = subprocess.run(
            ["sh", "-c", backend.collect_command(namespace)],
            cwd=workdir,
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
            for key, value in backend.environment(str(workdir), namespace).items()
        )
        run = subprocess.run(
            [
                "sh",
                "-c",
                (
                    f"{assignments} && go list ./pkg >/dev/null "
                    "&& go test ./pkg >/dev/null"
                ),
            ],
            cwd=workdir,
            check=True,
            capture_output=True,
            text=True,
        )
        assert run.returncode == 0

        collected = subprocess.run(
            ["sh", "-c", backend.collect_command(namespace)],
            cwd=workdir,
            check=True,
            capture_output=True,
            text=True,
        )
        assert "__TESTPRISM_COVERAGE_COLLECT__=collected" in collected.stdout
        report = json.loads(Path(files.json_path).read_text())
        entry = report["files"]["pkg/add.go"]
        assert entry["executed_lines"]
        assert entry["missing_lines"]
        assert set(entry["executed_lines"]).isdisjoint(entry["missing_lines"])
        assert 7 in entry["executed_lines"]
    finally:
        if container_root.exists():
            shutil.rmtree(container_root)
