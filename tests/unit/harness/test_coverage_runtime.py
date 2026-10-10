import configparser
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from testprism.harness.coverage_backends import PythonCoverageBackend
from testprism.harness.coverage_runtime import (
    BOOTSTRAP_HOOK_PTH_SOURCE,
    BOOTSTRAP_HOOK_SOURCE,
    TESTPRISM_COVERAGE_BOOTSTRAP_MODULE,
    TESTPRISM_COVERAGE_BOOTSTRAP_PTH,
    TESTPRISM_COVERAGE_DISABLE_AUTO_START,
    TESTPRISM_COVERAGE_SOURCE_ROOTS,
    TESTPRISM_COVERAGE_SITE_DIR,
    COVERAGE_ACTIVE_BOOTSTRAP_PATH,
    COVERAGE_BOOTSTRAP_DIR,
    COVERAGE_CONFIG_PATH,
    COVERAGE_GENERATED_CONFIG_PATH,
    COVERAGE_REFERENCE_CONFIG_PATH,
    SITECUSTOMIZE_SOURCE,
    coverage_collect_command,
    coverage_environment,
    coverage_namespace,
    coverage_setup_command,
    write_runtime_files,
)


def _report_entry(report: dict, relative_path: str) -> dict:
    for key, entry in report.get("files", {}).items():
        if key == relative_path or key.endswith("/" + relative_path):
            return entry
    raise AssertionError(f"missing coverage entry for {relative_path!r}")


def test_write_runtime_files_scopes_coverage_to_gold_source_files(tmp_path):
    files = write_runtime_files(
        tmp_path,
        workdir="/testbed",
        target_paths=["django/core/validators.py", "./django/core/validators.py"],
    )

    assert files.sitecustomize_path.read_text() == SITECUSTOMIZE_SOURCE
    assert not (files.sitecustomize_path.parent / "coverage").exists()
    assert (
        files.sitecustomize_path.parent
        / "_ctb_coverage55"
        / "coverage"
        / "__init__.py"
    ).is_file()
    assert (
        "version_info = (5, 5, 0"
        in (
            files.sitecustomize_path.parent
            / "_ctb_coverage55"
            / "coverage"
            / "version.py"
        ).read_text()
    )
    assert (
        files.sitecustomize_path.parent
        / (TESTPRISM_COVERAGE_BOOTSTRAP_MODULE + ".py")
    ).read_text() == BOOTSTRAP_HOOK_SOURCE
    assert (
        files.sitecustomize_path.parent / TESTPRISM_COVERAGE_BOOTSTRAP_PTH
    ).read_text() == BOOTSTRAP_HOOK_PTH_SOURCE
    parser = configparser.ConfigParser()
    parser.read(files.config_path)
    assert parser.getboolean("run", "parallel") is True
    assert parser.get("run", "data_file").startswith("/tmp/testbench/coverage/")
    assert parser.get("run", "include").splitlines() == ["*/django/core/validators.py"]


def test_write_runtime_files_rejects_targets_outside_workdir(tmp_path):
    with pytest.raises(ValueError, match="escapes workdir"):
        write_runtime_files(
            tmp_path,
            workdir="/testbed",
            target_paths=["../secret.py"],
        )
    with pytest.raises(ValueError, match="requires at least one"):
        write_runtime_files(tmp_path, workdir="/testbed", target_paths=[])


def test_runtime_commands_are_fail_soft_and_use_offline_bootstrap():
    setup = coverage_setup_command()
    collect = coverage_collect_command()

    assert TESTPRISM_COVERAGE_BOOTSTRAP_MODULE in setup
    assert TESTPRISM_COVERAGE_BOOTSTRAP_PTH in setup
    assert COVERAGE_ACTIVE_BOOTSTRAP_PATH in setup
    assert "pip install" not in setup
    assert "unsupported" in setup
    assert "coverage combine --keep" in collect
    assert "coverage json" in collect
    assert (
        f"export {TESTPRISM_COVERAGE_DISABLE_AUTO_START}=1" in collect
    )
    assert TESTPRISM_COVERAGE_SOURCE_ROOTS in collect
    assert f"export PYTHONPATH={COVERAGE_BOOTSTRAP_DIR}" in collect
    assert f"export COVERAGE_RCFILE={COVERAGE_CONFIG_PATH}" in collect
    assert "unsupported" in collect


def test_bootstrap_keeps_native_coverage_ahead_of_vendored_fallback(tmp_path):
    files = write_runtime_files(
        tmp_path / "runtime",
        workdir="/testbed",
        target_paths=["pkg/mod.py"],
    )
    native = tmp_path / "native"
    package = native / "coverage"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "__version__ = '7.13.5'\n"
        "version_info = (7, 13, 5, 'final', 0)\n"
        "def process_startup():\n    pass\n"
    )
    (package / "results.py").write_text(
        "def display_covered():\n    return 'native'\n"
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(files.sitecustomize_path.parent), str(native)]
    )
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            (
                "import sitecustomize\n"
                "import coverage\n"
                "from coverage.results import display_covered\n"
                "print(coverage.__version__)\n"
                "print(coverage.__file__)\n"
                "print(display_covered())\n"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    output = result.stdout.splitlines()
    assert output[0] == "7.13.5"
    assert str(native / "coverage" / "__init__.py") == output[1]
    assert output[2] == "native"


def test_bootstrap_loads_vendored_coverage_when_native_package_is_missing(tmp_path):
    files = write_runtime_files(
        tmp_path / "runtime",
        workdir="/testbed",
        target_paths=["pkg/mod.py"],
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(files.sitecustomize_path.parent)
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            (
                "import sitecustomize, coverage; "
                "print(coverage.__version__); print(coverage.__file__)"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    output = result.stdout.splitlines()
    assert output[0] == "5.5"
    assert output[1].endswith("_ctb_coverage55/coverage/__init__.py")


def test_collection_uses_installed_bootstrap_hook_without_runner_coverage_env(
    tmp_path,
):
    project = tmp_path / "project"
    package = project / "pkg"
    package.mkdir(parents=True)
    source = package / "module.py"
    source.write_text("value = 1\nif value:\n    value += 1\n")
    (project / "pyproject.toml").write_text(
        "[tool.coverage.run]\n"
        "branch = true\n"
    )

    namespace = "t02_" + uuid.uuid4().hex
    files = write_runtime_files(
        tmp_path / "runtime",
        workdir=str(project),
        target_paths=["pkg/module.py"],
        namespace=namespace,
    )
    container_root = Path(files.container_root)
    container_root.mkdir(parents=True, exist_ok=True)
    for payload in files.root.iterdir():
        destination = container_root / payload.name
        if payload.is_dir():
            shutil.copytree(payload, destination)
        else:
            shutil.copy2(payload, destination)

    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    shell_environment = dict(os.environ)
    shell_environment[TESTPRISM_COVERAGE_SITE_DIR] = str(site_dir)
    shell_environment.pop("COVERAGE_PROCESS_START", None)
    shell_environment.pop("COVERAGE_FILE", None)

    try:
        setup = subprocess.run(
            ["sh", "-c", coverage_setup_command(namespace)],
            cwd=project,
            check=True,
            capture_output=True,
            text=True,
            env=shell_environment,
        )
        assert "__TESTPRISM_COVERAGE_SETUP__=ready" in setup.stdout

        run_environment = dict(shell_environment)
        run_environment["PYTHONPATH"] = str(project / "runner-owned")
        run = subprocess.run(
            [
                sys.executable,
                "-S",
                "-c",
                (
                    "import runpy, site\n"
                    f"site.addsitedir({str(site_dir)!r})\n"
                    f"runpy.run_path({str(source)!r}, run_name='__main__')\n"
                ),
            ],
            cwd=project,
            check=True,
            capture_output=True,
            text=True,
            env=run_environment,
        )
        assert run.returncode == 0
        assert "COVERAGE_PROCESS_START" not in run_environment
        assert "COVERAGE_FILE" not in run_environment

        collect = subprocess.run(
            ["sh", "-c", coverage_collect_command(namespace)],
            cwd=project,
            check=True,
            capture_output=True,
            text=True,
            env=shell_environment,
        )
        assert "__TESTPRISM_COVERAGE_COLLECT__=collected" in collect.stdout
        report = json.loads(Path(files.json_path).read_text())
        assert report["files"]["pkg/module.py"]["executed_lines"]
    finally:
        if container_root.exists():
            shutil.rmtree(container_root)
        active_state = Path(COVERAGE_ACTIVE_BOOTSTRAP_PATH)
        if active_state.exists():
            active_state.unlink()


def test_collection_supports_instance_ansible_40ade_runner_env_rewrite(
    tmp_path,
):
    declared_workdir = tmp_path / "testbed"
    actual_root = tmp_path / "app"
    declared_workdir.mkdir(parents=True)
    package = actual_root / "lib" / "ansible" / "executor"
    package.mkdir(parents=True)
    source = package / "play_iterator.py"
    source.write_text("value = 1\nif value:\n    value += 1\n")

    namespace = "t03_" + uuid.uuid4().hex
    files = write_runtime_files(
        tmp_path / "runtime",
        workdir=str(declared_workdir),
        target_paths=["lib/ansible/executor/play_iterator.py"],
        namespace=namespace,
    )
    container_root = Path(files.container_root)
    container_root.mkdir(parents=True, exist_ok=True)
    for payload in files.root.iterdir():
        destination = container_root / payload.name
        if payload.is_dir():
            shutil.copytree(payload, destination)
        else:
            shutil.copy2(payload, destination)

    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    shell_environment = dict(os.environ)
    shell_environment[TESTPRISM_COVERAGE_SITE_DIR] = str(site_dir)
    shell_environment[TESTPRISM_COVERAGE_SOURCE_ROOTS] = str(actual_root)
    shell_environment.pop("COVERAGE_PROCESS_START", None)
    shell_environment.pop("COVERAGE_FILE", None)

    try:
        setup = subprocess.run(
            ["sh", "-c", coverage_setup_command(namespace)],
            cwd=actual_root,
            check=True,
            capture_output=True,
            text=True,
            env=shell_environment,
        )
        assert "__TESTPRISM_COVERAGE_SETUP__=ready" in setup.stdout

        run_environment = dict(shell_environment)
        run_environment["PYTHONPATH"] = str(actual_root / "runner-owned")
        run = subprocess.run(
            [
                sys.executable,
                "-S",
                "-c",
                (
                    "import runpy, site\n"
                    f"site.addsitedir({str(site_dir)!r})\n"
                    f"runpy.run_path({str(source)!r}, run_name='__main__')\n"
                ),
            ],
            cwd=actual_root,
            check=True,
            capture_output=True,
            text=True,
            env=run_environment,
        )
        assert run.returncode == 0

        collect = subprocess.run(
            ["sh", "-c", coverage_collect_command(namespace)],
            cwd=declared_workdir,
            check=True,
            capture_output=True,
            text=True,
            env=shell_environment,
        )
        assert "__TESTPRISM_COVERAGE_COLLECT__=collected" in collect.stdout
        report = json.loads(Path(files.json_path).read_text())
        entry = _report_entry(report, "lib/ansible/executor/play_iterator.py")
        assert entry["executed_lines"] == [1, 2, 3]
    finally:
        if container_root.exists():
            shutil.rmtree(container_root)
        active_state = Path(COVERAGE_ACTIVE_BOOTSTRAP_PATH)
        if active_state.exists():
            active_state.unlink()


def test_collection_yields_to_project_owned_pytest_cov_sessions(tmp_path):
    project = tmp_path / "project"
    package = project / "pkg"
    package.mkdir(parents=True)
    source = package / "module.py"
    source.write_text("value = 1\n")
    project_config = project / "pyproject.toml"
    project_config.write_text("[tool.coverage.run]\nbranch = true\n")

    namespace = "t03_" + uuid.uuid4().hex
    files = write_runtime_files(
        tmp_path / "runtime",
        workdir=str(project),
        target_paths=["pkg/module.py"],
        namespace=namespace,
    )
    container_root = Path(files.container_root)
    container_root.mkdir(parents=True, exist_ok=True)
    for payload in files.root.iterdir():
        destination = container_root / payload.name
        if payload.is_dir():
            shutil.copytree(payload, destination)
        else:
            shutil.copy2(payload, destination)

    site_dir = tmp_path / "site-packages"
    site_dir.mkdir()
    shell_environment = dict(os.environ)
    shell_environment[TESTPRISM_COVERAGE_SITE_DIR] = str(site_dir)

    try:
        subprocess.run(
            ["sh", "-c", coverage_setup_command(namespace)],
            cwd=project,
            check=True,
            capture_output=True,
            text=True,
            env=shell_environment,
        )

        run_environment = dict(shell_environment)
        run_environment.update(
            {
                "COV_CORE_SOURCE": "pkg",
                "COV_CORE_CONFIG": str(project_config),
                "COV_CORE_DATAFILE": str(project / ".coverage.project"),
            }
        )
        run = subprocess.run(
            [
                sys.executable,
                "-S",
                "-c",
                (
                    "import runpy, site\n"
                    f"site.addsitedir({str(site_dir)!r})\n"
                    f"runpy.run_path({str(source)!r}, run_name='__main__')\n"
                ),
            ],
            cwd=project,
            check=True,
            capture_output=True,
            text=True,
            env=run_environment,
        )
        assert run.returncode == 0
        assert list((container_root / "data").glob(".coverage*")) == []

        collect = subprocess.run(
            ["sh", "-c", coverage_collect_command(namespace)],
            cwd=project,
            check=True,
            capture_output=True,
            text=True,
            env=shell_environment,
        )
        assert "__TESTPRISM_COVERAGE_COLLECT__=collected" in collect.stdout
        report = json.loads(Path(files.json_path).read_text())
        entry = _report_entry(report, "pkg/module.py")
        assert entry["executed_lines"] == []
        assert entry["missing_lines"] == [1]
    finally:
        if container_root.exists():
            shutil.rmtree(container_root)
        active_state = Path(COVERAGE_ACTIVE_BOOTSTRAP_PATH)
        if active_state.exists():
            active_state.unlink()


def test_coverage_environment_does_not_export_runner_owned_coverage_vars():
    environment = coverage_environment("/testbed")

    assert environment == {}


def test_python_runtime_is_exposed_through_backend_contract():
    backend = PythonCoverageBackend()

    assert backend.setup_command("generated") == coverage_setup_command("generated")
    assert backend.collect_command("reference") == coverage_collect_command(
        "reference"
    )
    assert backend.environment("/testbed", "generated") == coverage_environment(
        "/testbed", "generated"
    )


def test_namespaced_runtime_files_cannot_cross_suite_boundaries(tmp_path):
    generated = write_runtime_files(
        tmp_path / "generated",
        workdir="/testbed",
        target_paths=["pkg/mod.py"],
        namespace="generated",
    )
    reference = write_runtime_files(
        tmp_path / "reference",
        workdir="/testbed",
        target_paths=["pkg/mod.py"],
        namespace="reference",
    )

    assert generated.container_root != reference.container_root
    assert generated.data_dir != reference.data_dir
    assert generated.json_path != reference.json_path
    assert generated.config_path.read_text() != reference.config_path.read_text()
    assert generated.config_path.read_text().find(COVERAGE_GENERATED_CONFIG_PATH) == -1
    assert generated.container_root == "/tmp/testbench/coverage/generated"
    assert reference.container_root == "/tmp/testbench/coverage/reference"
    assert coverage_namespace("generated").config_path == COVERAGE_GENERATED_CONFIG_PATH
    assert coverage_namespace("reference").config_path == COVERAGE_REFERENCE_CONFIG_PATH

    generated_env = coverage_environment("/testbed", "generated")
    reference_env = coverage_environment("/testbed", "reference")
    assert generated_env == {}
    assert reference_env == {}
    generated_collect = coverage_collect_command("generated")
    reference_collect = coverage_collect_command("reference")
    assert f"export COVERAGE_RCFILE={COVERAGE_GENERATED_CONFIG_PATH}" in generated_collect
    assert f"export COVERAGE_RCFILE={COVERAGE_REFERENCE_CONFIG_PATH}" in reference_collect
    assert (
        f"export {TESTPRISM_COVERAGE_DISABLE_AUTO_START}=1"
        in generated_collect
    )
    assert "coverage/generated" in generated_collect
    assert "coverage/reference" in reference_collect


def test_namespace_rejects_path_traversal():
    with pytest.raises(ValueError, match="invalid coverage namespace"):
        coverage_namespace("../reference")


def test_finalize_script_adds_unimported_targets_and_preserves_suffix_matches(tmp_path):
    workdir = tmp_path / "source"
    package = workdir / "pkg"
    package.mkdir(parents=True)
    (package / "never_imported.py").write_text("value = 1\nif value:\n    value += 1\n")
    (package / "editable.py").write_text("value = 2\nif value:\n    value += 1\n")
    files = write_runtime_files(
        tmp_path / "runtime",
        workdir=str(workdir),
        target_paths=["pkg/never_imported.py", "pkg/editable.py"],
        namespace="reference",
    )
    report_path = files.root / "coverage.json"
    report_path.write_text(
        json.dumps(
            {
                "files": {
                    "/opt/site/pkg/editable.py": {
                        "executed_lines": [1],
                        "missing_lines": [],
                    }
                },
                "totals": {},
            }
        )
    )
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    environment = dict(os.environ)
    environment.update(
        {
            "PYTHONPATH": str(
                Path(__file__).resolve().parents[3]
                / "testprism/harness/_vendor/coverage55"
            ),
            "TESTPRISM_COVERAGE_TARGETS": str(files.root / "targets.json"),
            "TESTPRISM_COVERAGE_JSON": str(report_path),
            "TESTPRISM_COVERAGE_CONFIG": str(files.config_path),
            "COVERAGE_FILE": str(data_dir / ".coverage"),
        }
    )
    subprocess.run(
        [sys.executable, "-S", str(files.root / "finalize_coverage.py")],
        check=True,
        env=environment,
    )

    report = json.loads(report_path.read_text())
    never = report["files"]["pkg/never_imported.py"]
    assert never["executed_lines"] == []
    assert never["missing_lines"] == [1, 2, 3]
    editable = report["files"]["/opt/site/pkg/editable.py"]
    assert editable["executed_lines"] == [1]
    assert editable["missing_lines"] == [2, 3]
    assert report["totals"]["covered_lines"] == 1
    assert report["totals"]["missing_lines"] == 5
    assert report["totals"]["num_statements"] == 6
    assert report["totals"]["excluded_lines"] == 0
    assert report["totals"]["percent_covered"] == pytest.approx(1 / 6 * 100)


def test_finalize_script_recovers_instance_ansible_811093_alternate_source_root(
    tmp_path,
):
    declared_workdir = tmp_path / "testbed"
    actual_root = tmp_path / "app"
    package = actual_root / "lib" / "ansible" / "executor"
    package.mkdir(parents=True)
    (package / "play_iterator.py").write_text(
        "value = 1\nif value:\n    value += 1\n"
    )
    files = write_runtime_files(
        tmp_path / "runtime",
        workdir=str(declared_workdir),
        target_paths=["lib/ansible/executor/play_iterator.py"],
        namespace="reference",
    )
    report_path = files.root / "coverage.json"
    report_path.write_text('{"files":{},"totals":{}}')
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    environment = dict(os.environ)
    environment.update(
        {
            "PYTHONPATH": str(
                Path(__file__).resolve().parents[3]
                / "testprism/harness/_vendor/coverage55"
            ),
            TESTPRISM_COVERAGE_SOURCE_ROOTS: str(actual_root),
            "TESTPRISM_COVERAGE_TARGETS": str(files.root / "targets.json"),
            "TESTPRISM_COVERAGE_JSON": str(report_path),
            "TESTPRISM_COVERAGE_CONFIG": str(files.config_path),
            "COVERAGE_FILE": str(data_dir / ".coverage"),
        }
    )

    subprocess.run(
        [sys.executable, "-S", str(files.root / "finalize_coverage.py")],
        check=True,
        env=environment,
    )

    report = json.loads(report_path.read_text())
    entry = _report_entry(report, "lib/ansible/executor/play_iterator.py")
    assert entry["executed_lines"] == []
    assert entry["missing_lines"] == [1, 2, 3]
