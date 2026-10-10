import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from testprism.harness.coverage_backends import get_default_coverage_backend_registry
from testprism.harness.java_coverage_backend import (
    JAVA_SOURCE_SUFFIXES,
    JavaCoverageBackend,
    _class_root_discovery_command,
    normalize_jacoco_xml,
    write_java_runtime_files,
)
from testprism.harness.testbench_harness import TestBenchHarness as RuntimeHarness
from testprism.harness.testbench_harness import _render_coverage_environment_assignment


def test_registry_selects_java_targets_and_keeps_other_languages_additive():
    selection = get_default_coverage_backend_registry().select(
        ["src/main/java/example/App.java", "src/app.js", "src/app.py"]
    )

    assert selection.unsupported_paths == ()
    assert selection.languages == ["java", "javascript", "python"]
    assert selection.backend_for("src/main/java/example/App.java").language == "java"
    assert JavaCoverageBackend().suffixes == JAVA_SOURCE_SUFFIXES


def test_jacoco_xml_maps_package_sourcefile_and_fills_absent_target(tmp_path):
    source = tmp_path / "src" / "main" / "java" / "example" / "App.java"
    source.parent.mkdir(parents=True)
    source.write_text(
        "package example;\n"
        "public class App {\n"
        "  int value() { return 1; }\n"
        "}\n"
    )
    absent = tmp_path / "src" / "main" / "java" / "example" / "Absent.java"
    absent.write_text(
        "package example;\n"
        "class Absent {\n"
        "  void call() {}\n"
        "}\n"
    )
    xml = """
    <report name="demo">
      <package name="example">
        <sourcefile name="App.java">
          <line nr="1" mi="0" ci="1" mb="0" cb="0" />
          <line nr="3" mi="2" ci="0" mb="0" cb="0" />
        </sourcefile>
      </package>
    </report>
    """

    report = normalize_jacoco_xml(
        xml,
        workdir=tmp_path,
        target_paths=[
            "src/main/java/example/App.java",
            "src/main/java/example/Absent.java",
        ],
    )

    assert report == {
        "files": {
            "src/main/java/example/Absent.java": {
                "executed_lines": [],
                "missing_lines": [1, 2, 3],
            },
            "src/main/java/example/App.java": {
                "executed_lines": [1],
                "missing_lines": [3],
            },
        }
    }


def test_jacoco_xml_maps_unique_declared_package_alias_to_canonical_root_target(
    tmp_path,
):
    target = tmp_path / "BREADTH_FIRST_SEARCH.java"
    target.write_text(
        "/* package ignored.comment; */\n"
        "// package ignored.line;\n"
        "package\n"
        "    java_programs\n"
        "    ;\n"
        "public class BREADTH_FIRST_SEARCH {}\n"
    )
    xml = """
    <report>
      <package name="java_programs">
        <sourcefile name="BREADTH_FIRST_SEARCH.java">
          <line nr="6" mi="0" ci="1" />
        </sourcefile>
      </package>
    </report>
    """

    report = normalize_jacoco_xml(
        xml,
        workdir=tmp_path,
        target_paths=["BREADTH_FIRST_SEARCH.java"],
    )

    assert report == {
        "files": {
            "BREADTH_FIRST_SEARCH.java": {
                "executed_lines": [6],
                "missing_lines": [],
            }
        }
    }


def test_jacoco_exact_and_suffix_matching_precede_declared_package_alias(tmp_path):
    root_alias = tmp_path / "App.java"
    root_alias.write_text("package java_programs;\nclass App {}\n")
    exact = tmp_path / "java_programs" / "Exact.java"
    exact.parent.mkdir()
    exact.write_text("package inconsistent;\nclass Exact {}\n")
    suffix = tmp_path / "src" / "java_programs" / "App.java"
    suffix.parent.mkdir(parents=True)
    suffix.write_text("package inconsistent;\nclass App {}\n")
    xml = """
    <report>
      <package name="java_programs">
        <sourcefile name="Exact.java"><line nr="2" mi="0" ci="1" /></sourcefile>
        <sourcefile name="App.java"><line nr="2" mi="0" ci="1" /></sourcefile>
      </package>
    </report>
    """

    report = normalize_jacoco_xml(
        xml,
        workdir=tmp_path,
        target_paths=[
            "App.java",
            "java_programs/Exact.java",
            "src/java_programs/App.java",
        ],
    )

    assert report["files"]["java_programs/Exact.java"]["executed_lines"] == [2]
    assert report["files"]["src/java_programs/App.java"]["executed_lines"] == [2]
    assert report["files"]["App.java"]["executed_lines"] == []


@pytest.mark.parametrize(
    "source, candidate_package",
    [
        ("package expected;\nclass App {}\n", "mismatched"),
        ("package expected\nclass App {}\n", "expected"),
        ("package expected;\npackage second;\nclass App {}\n", "expected"),
        ("class App {}\n", "unrelated"),
    ],
)
def test_jacoco_package_alias_rejects_mismatch_malformed_multiple_and_basename(
    tmp_path,
    source,
    candidate_package,
):
    target = tmp_path / "nested" / "App.java"
    target.parent.mkdir()
    target.write_text(source)
    xml = (
        f"<report><package name='{candidate_package}'><sourcefile name='App.java'>"
        "<line nr='1' mi='0' ci='1' /></sourcefile></package></report>"
    )

    report = normalize_jacoco_xml(
        xml,
        workdir=tmp_path,
        target_paths=["nested/App.java"],
    )

    assert report["files"]["nested/App.java"]["executed_lines"] == []


def test_jacoco_package_alias_requires_one_unique_target_owner(tmp_path):
    targets = []
    for directory in ("first", "second"):
        target = tmp_path / directory / "App.java"
        target.parent.mkdir()
        target.write_text("package shared;\nclass App {}\n")
        targets.append(f"{directory}/App.java")
    xml = (
        "<report><package name='shared'><sourcefile name='App.java'>"
        "<line nr='2' mi='0' ci='1' /></sourcefile></package></report>"
    )

    report = normalize_jacoco_xml(xml, workdir=tmp_path, target_paths=targets)

    assert all(not item["executed_lines"] for item in report["files"].values())


def test_jacoco_package_alias_rejects_invalid_utf8_target(tmp_path):
    target = tmp_path / "App.java"
    target.write_bytes(b"package shared;\nclass App {}\n\xff")

    with pytest.raises(ValueError, match="valid UTF-8"):
        normalize_jacoco_xml(
            "<report />",
            workdir=tmp_path,
            target_paths=["App.java"],
        )


@pytest.mark.parametrize(
    "xml, message",
    [
        ("<not-report />", "root must be report"),
        (
            "<report><package name='../outside'>"
            "<sourcefile name='A.java' /></package></report>",
            "escapes workdir",
        ),
        (
            "<report><package name='pkg'><sourcefile name='A.java'>"
            "<line nr='x' mi='0' ci='1' /></sourcefile></package></report>",
            "counter",
        ),
        (
            "<report><package name='/absolute'>"
            "<sourcefile name='A.java' /></package></report>",
            "escapes workdir",
        ),
        (
            "<report><package name='pkg'>"
            "<sourcefile name='/A.java' /></package></report>",
            "escapes workdir",
        ),
        (
            "<report><package name='pkg'>"
            "<sourcefile name='C:\\A.java' /></package></report>",
            "escapes workdir",
        ),
    ],
)
def test_jacoco_normalizer_rejects_malformed_xml(xml, message, tmp_path):
    target = tmp_path / "src" / "A.java"
    target.parent.mkdir()
    target.write_text("class A {}\n")
    with pytest.raises(ValueError, match=message):
        normalize_jacoco_xml(xml, workdir=tmp_path, target_paths=["src/A.java"])


def test_runtime_namespaces_isolate_exec_and_agent_destinations(tmp_path):
    generated = write_java_runtime_files(
        tmp_path / "generated",
        workdir="/app",
        target_paths=["src/App.java"],
        namespace="generated",
    )
    reference = write_java_runtime_files(
        tmp_path / "reference",
        workdir="/app",
        target_paths=["src/App.java"],
        namespace="reference",
    )
    backend = JavaCoverageBackend()

    assert generated.exec_path != reference.exec_path
    assert generated.agent_path != reference.agent_path
    assert generated.class_roots_path != reference.class_roots_path
    assert generated.gradle_user_home != reference.gradle_user_home
    assert generated.gradle_init_script_path != reference.gradle_init_script_path
    assert backend.environment("/app", "generated") != backend.environment(
        "/app", "reference"
    )
    assert backend.environment("/app", "generated")["TESTPRISM_JACOCO_CLASS_ROOTS"] == (
        generated.class_roots_path
    )
    assert backend.environment("/app", "generated")["GRADLE_USER_HOME"] == (
        generated.gradle_user_home
    )
    assert "JAVA_TOOL_OPTIONS" in backend.environment("/app", "generated")
    assert "-Dorg.gradle.daemon=false -javaagent:" in backend.environment(
        "/app", "generated"
    )["JAVA_TOOL_OPTIONS"]
    assert "destfile=" + generated.exec_path in backend.environment(
        "/app", "generated"
    )["JAVA_TOOL_OPTIONS"]
    setup = backend.setup_command("reference")
    assert "command -v java" in setup
    assert "jacoco" in setup.lower()
    assert (
        "-javaagent:/tmp/testbench/coverage/reference/java/jacocoagent.jar=destfile="
        in setup
    )
    assert "agent-probe.exec" in setup
    assert "jacoco-payload.sha256" in setup
    assert "sha256sum" in setup
    assert "/root/.gradle/caches" not in setup
    assert "find /" not in setup
    assert "curl" not in setup
    assert "wget" not in setup
    collect = backend.collect_command("reference")
    assert "build/classes/java/main" in collect
    assert "target/classes" in collect
    assert "$PWD/agent-tests/.build/classes" in collect
    assert "find -P /tests" in collect
    assert "build/classes/*" not in collect
    assert "sort -u" in collect
    assert "unsupported" in setup
    assert backend.normalized_report_path("generated").endswith("coverage.json")
    assert (tmp_path / "generated" / "targets.json").is_file()
    assert (tmp_path / "generated" / "jacocoagent.jar").is_file()
    assert (tmp_path / "generated" / "jacococli.jar").is_file()
    assert (tmp_path / "generated" / "jacoco-manifest.json").is_file()
    assert (
        tmp_path
        / "generated"
        / "gradle-user-home"
        / "init.d"
        / "testprism-disable-project-jacoco.init.gradle"
    ).is_file()
    checksums = (tmp_path / "generated" / "jacoco-payload.sha256").read_text()
    assert "jacocoagent.jar" in checksums
    assert "jacococli.jar" in checksums
    assert hashlib.sha256(
        (tmp_path / "generated" / "jacocoagent.jar").read_bytes()
    ).hexdigest() in checksums
    manifest = json.loads((tmp_path / "generated" / "targets.json").read_text())
    assert manifest["targets"][0]["relative"] == "src/App.java"


def test_runtime_files_include_gradle_init_script_that_disables_project_jacoco(
    tmp_path,
):
    runtime = write_java_runtime_files(
        tmp_path / "generated",
        workdir="/app",
        target_paths=["src/App.java"],
        namespace="generated",
    )

    script = (
        tmp_path
        / "generated"
        / "gradle-user-home"
        / "init.d"
        / "testprism-disable-project-jacoco.init.gradle"
    )
    content = script.read_text()

    assert runtime.gradle_user_home.endswith("/generated/java/gradle-user-home")
    assert runtime.gradle_init_script_path.endswith(
        "/generated/java/gradle-user-home/init.d/"
        "testprism-disable-project-jacoco.init.gradle"
    )
    assert "pluginManager.withPlugin('jacoco')" in content
    assert "tasks.withType(org.gradle.api.tasks.testing.Test)" in content
    assert "jacocoExtension.enabled = false" in content


def test_class_root_discovery_is_bounded_sorted_and_deduplicated(tmp_path):
    workdir = tmp_path / "task workdir"
    trusted = tmp_path / "trusted tests"
    outside = tmp_path / "outside"
    expected = [
        workdir / "agent-tests" / ".build" / "classes",
        workdir / "modules" / "maven" / "target" / "classes",
        workdir / "modules" / "z-gradle" / "build" / "classes" / "java" / "main",
        trusted / "gradle" / "build" / "classes" / "java" / "main",
        trusted / "maven" / "target" / "classes",
    ]
    for path in expected:
        path.mkdir(parents=True)

    # Similar-looking cache/test layouts are outside the exact allowlist.
    (workdir / ".gradle" / "caches" / "classes").mkdir(parents=True)
    (workdir / "build" / "classes" / "java" / "test").mkdir(parents=True)
    (trusted / "agent-tests" / ".build" / "classes").mkdir(parents=True)

    # Neither a symlinked ancestor nor a class-root symlink may escape a root.
    (outside / "target" / "classes").mkdir(parents=True)
    (workdir / "escaped-module").symlink_to(outside, target_is_directory=True)
    (workdir / "linked" / "target").mkdir(parents=True)
    (workdir / "linked" / "target" / "classes").symlink_to(
        outside / "target" / "classes",
        target_is_directory=True,
    )

    command = _class_root_discovery_command(str(trusted))
    result = subprocess.run(
        ["sh", "-c", command + "printf '%s\\n' \"$__ctb_classes\""],
        cwd=workdir,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.splitlines() == sorted(str(path) for path in expected)
    assert str(outside) not in result.stdout

    duplicate_result = subprocess.run(
        [
            "sh",
            "-c",
            _class_root_discovery_command(str(workdir))
            + "printf '%s\\n' \"$__ctb_classes\"",
        ],
        cwd=workdir,
        check=True,
        capture_output=True,
        text=True,
    )
    duplicate_roots = duplicate_result.stdout.splitlines()
    assert duplicate_roots == sorted(set(duplicate_roots))


@pytest.mark.parametrize("test_root", ["../tests", "/"])
def test_class_root_discovery_rejects_unbounded_test_root(test_root):
    with pytest.raises(ValueError, match="absolute bounded path"):
        _class_root_discovery_command(test_root)


def test_collection_without_allowed_class_root_is_explicit_failure(tmp_path):
    payload_root = tmp_path / "payload"
    write_java_runtime_files(
        payload_root,
        workdir=str(tmp_path / "workdir"),
        target_paths=["src/App.java"],
        namespace="generated",
    )
    (payload_root / "jacoco.exec").write_bytes(b"execution-data")
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    java = bin_dir / "java"
    java.write_text("#!/bin/sh\nexit 0\n")
    java.chmod(0o755)

    command = (
        JavaCoverageBackend()
        .collect_command("generated")
        .replace("/tmp/testbench/coverage/generated/java", str(payload_root))
        .replace("find -P /tests", f"find -P '{trusted}'")
    )
    result = subprocess.run(
        ["sh", "-c", command],
        cwd=workdir,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )

    assert "__TESTPRISM_COVERAGE_COLLECT__=failed" in result.stdout
    assert not (payload_root / "coverage.json").exists()


def test_generated_collection_without_exec_emits_source_derived_zero_report(tmp_path):
    payload_root = tmp_path / "payload"
    workdir = tmp_path / "task workdir"
    source = workdir / "src" / "App.java"
    source.parent.mkdir(parents=True)
    source.write_text(
        "class App {\n"
        "    int answer() {\n"
        "        return 42;\n"
        "    }\n"
        "}\n"
    )
    write_java_runtime_files(
        payload_root,
        workdir=str(workdir),
        target_paths=["src/App.java"],
        namespace="generated",
    )
    command = JavaCoverageBackend().collect_command("generated").replace(
        "/tmp/testbench/coverage/generated/java", str(payload_root)
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    java = bin_dir / "java"
    java.write_text("#!/bin/sh\nexit 0\n")
    java.chmod(0o755)

    result = subprocess.run(
        ["sh", "-c", command],
        cwd=workdir,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )

    assert "__TESTPRISM_COVERAGE_COLLECT__=collected" in result.stdout
    assert json.loads((payload_root / "coverage.json").read_text()) == {
        "files": {
            "src/App.java": {
                "executed_lines": [],
                "missing_lines": [1, 2, 3],
            }
        }
    }

    (payload_root / "coverage.json").unlink()
    (payload_root / "jacoco.exec").write_bytes(b"")
    empty_exec = subprocess.run(
        ["sh", "-c", command],
        cwd=workdir,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )
    assert "__TESTPRISM_COVERAGE_COLLECT__=failed" in empty_exec.stdout
    assert not (payload_root / "coverage.json").exists()


def test_container_normalizer_maps_declared_package_alias(tmp_path):
    payload_root = tmp_path / "payload"
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / "BREADTH_FIRST_SEARCH.java").write_text(
        "package java_programs;\nclass BREADTH_FIRST_SEARCH {}\n"
    )
    runtime = write_java_runtime_files(
        payload_root,
        workdir=str(workdir),
        target_paths=["BREADTH_FIRST_SEARCH.java"],
        namespace="reference",
    )
    xml_path = payload_root / "jacoco.xml"
    xml_path.write_text(
        "<report><package name='java_programs'>"
        "<sourcefile name='BREADTH_FIRST_SEARCH.java'>"
        "<line nr='2' mi='0' ci='1' />"
        "</sourcefile></package></report>"
    )
    report_path = payload_root / "coverage.json"

    subprocess.run(
        [sys.executable, str(payload_root / "normalize_jacoco_coverage.py")],
        check=True,
        env={
            **os.environ,
            "TESTPRISM_JACOCO_TARGETS": str(payload_root / "targets.json"),
            "TESTPRISM_JACOCO_XML": str(xml_path),
            "TESTPRISM_JACOCO_REPORT": str(report_path),
        },
    )

    assert runtime.report_path.endswith("/reference/java/coverage.json")
    assert json.loads(report_path.read_text()) == {
        "files": {
            "BREADTH_FIRST_SEARCH.java": {
                "executed_lines": [2],
                "missing_lines": [],
            }
        }
    }


def test_reference_collection_without_exec_remains_explicit_failure(tmp_path):
    payload_root = tmp_path / "payload"
    workdir = tmp_path / "workdir"
    source = workdir / "src" / "App.java"
    source.parent.mkdir(parents=True)
    source.write_text("class App {}\n")
    write_java_runtime_files(
        payload_root,
        workdir=str(workdir),
        target_paths=["src/App.java"],
        namespace="reference",
    )
    command = JavaCoverageBackend().collect_command("reference").replace(
        "/tmp/testbench/coverage/reference/java", str(payload_root)
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    java = bin_dir / "java"
    java.write_text("#!/bin/sh\nexit 0\n")
    java.chmod(0o755)

    result = subprocess.run(
        ["sh", "-c", command],
        cwd=workdir,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )

    assert "__TESTPRISM_COVERAGE_COLLECT__=failed" in result.stdout
    assert not (payload_root / "coverage.json").exists()


@pytest.mark.parametrize(
    "java_body",
    [
        "exit 1",
        (
            "while [ \"$#\" -gt 0 ]; do "
            "if [ \"$1\" = --xml ]; then shift; printf '<report' > \"$1\"; fi; "
            "shift; done"
        ),
    ],
)
def test_nonempty_malformed_exec_or_xml_remains_failure(tmp_path, java_body):
    payload_root = tmp_path / "payload"
    workdir = tmp_path / "workdir"
    source = workdir / "src" / "App.java"
    source.parent.mkdir(parents=True)
    source.write_text("class App {}\n")
    (workdir / "build" / "classes" / "java" / "main").mkdir(parents=True)
    write_java_runtime_files(
        payload_root,
        workdir=str(workdir),
        target_paths=["src/App.java"],
        namespace="generated",
    )
    (payload_root / "jacoco.exec").write_bytes(b"malformed")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    java = bin_dir / "java"
    java.write_text(f"#!/bin/sh\n{java_body}\n")
    java.chmod(0o755)
    command = JavaCoverageBackend().collect_command("generated").replace(
        "/tmp/testbench/coverage/generated/java", str(payload_root)
    )

    result = subprocess.run(
        ["sh", "-c", command],
        cwd=workdir,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )

    assert "__TESTPRISM_COVERAGE_COLLECT__=failed" in result.stdout
    assert not (payload_root / "coverage.json").exists()


def test_collection_renders_every_class_root_in_sorted_order(tmp_path):
    payload_root = tmp_path / "payload"
    workdir = tmp_path / "workdir"
    trusted = tmp_path / "trusted"
    source = workdir / "src" / "App.java"
    source.parent.mkdir(parents=True)
    source.write_text("class App {}\n")
    expected = [
        workdir / "agent-tests" / ".build" / "classes",
        workdir / "module" / "target" / "classes",
        trusted / "module" / "build" / "classes" / "java" / "main",
    ]
    for path in expected:
        path.mkdir(parents=True)
    write_java_runtime_files(
        payload_root,
        workdir=str(workdir),
        target_paths=["src/App.java"],
        namespace="generated",
    )
    (payload_root / "jacoco.exec").write_bytes(b"execution-data")
    arguments = tmp_path / "java-arguments"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    java = bin_dir / "java"
    java.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$@\" > \"$JAVA_ARGUMENTS\"\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  if [ \"$1\" = --xml ]; then shift; printf '<report />' > \"$1\"; fi\n"
        "  shift\n"
        "done\n"
    )
    java.chmod(0o755)
    command = (
        JavaCoverageBackend()
        .collect_command("generated")
        .replace("/tmp/testbench/coverage/generated/java", str(payload_root))
        .replace("find -P /tests", f"find -P '{trusted}'")
    )

    result = subprocess.run(
        ["sh", "-c", command],
        cwd=workdir,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "JAVA_ARGUMENTS": str(arguments),
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
        },
    )

    java_arguments = arguments.read_text().splitlines()
    class_roots = [
        java_arguments[index + 1]
        for index, argument in enumerate(java_arguments)
        if argument == "--classfiles"
    ]
    assert "__TESTPRISM_COVERAGE_COLLECT__=collected" in result.stdout
    assert class_roots == sorted(str(path) for path in expected)
    assert (payload_root / "coverage.json").is_file()


def test_collection_merges_runner_declared_class_roots_with_safe_boundaries(tmp_path):
    payload_root = tmp_path / "payload"
    workdir = tmp_path / "task workdir"
    outside = tmp_path / "outside"
    source = workdir / "src" / "App.java"
    source.parent.mkdir(parents=True)
    source.write_text("class App {}\n")
    auto_root = workdir / "build" / "classes" / "java" / "main"
    relative_root = workdir / "custom classes"
    absolute_root = workdir / "module" / "classes"
    auto_root.mkdir(parents=True)
    relative_root.mkdir(parents=True)
    absolute_root.mkdir(parents=True)
    outside.mkdir()
    linked_root = workdir / "linked-classes"
    linked_root.symlink_to(outside, target_is_directory=True)
    inside_symlink = workdir / "inside-link"
    inside_symlink.symlink_to(relative_root, target_is_directory=True)
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    write_java_runtime_files(
        payload_root,
        workdir=str(workdir),
        target_paths=["src/App.java"],
        namespace="generated",
    )
    (payload_root / "jacoco.exec").write_bytes(b"execution-data")
    marker = tmp_path / "injected"
    (payload_root / "class-roots.txt").write_text(
        "\n".join(
            [
                "custom classes",
                str(absolute_root),
                "custom classes",
                "../outside",
                str(outside),
                "missing classes",
                "linked-classes",
                "inside-link",
                f"$(touch {marker})",
                "",
            ]
        )
        + "\n"
    )
    arguments = tmp_path / "java-arguments"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    java = bin_dir / "java"
    java.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$@\" > \"$JAVA_ARGUMENTS\"\n"
        "while [ \"$#\" -gt 0 ]; do\n"
        "  if [ \"$1\" = --xml ]; then shift; printf '<report />' > \"$1\"; fi\n"
        "  shift\n"
        "done\n"
    )
    java.chmod(0o755)
    command = (
        JavaCoverageBackend()
        .collect_command("generated")
        .replace("/tmp/testbench/coverage/generated/java", str(payload_root))
        .replace("find -P /tests", f"find -P '{trusted}'")
    )

    result = subprocess.run(
        ["sh", "-c", command],
        cwd=workdir,
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "JAVA_ARGUMENTS": str(arguments),
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
        },
    )

    java_arguments = arguments.read_text().splitlines()
    class_roots = [
        java_arguments[index + 1]
        for index, argument in enumerate(java_arguments)
        if argument == "--classfiles"
    ]
    expected = sorted(str(path) for path in (auto_root, relative_root, absolute_root))
    assert class_roots == expected
    assert "__TESTPRISM_COVERAGE_COLLECT__=collected" in result.stdout
    assert not marker.exists()


def test_setup_cleans_stale_runner_class_root_declarations(tmp_path):
    payload_root = tmp_path / "payload"
    write_java_runtime_files(
        payload_root,
        workdir=str(tmp_path / "workdir"),
        target_paths=["src/App.java"],
        namespace="generated",
    )
    declaration = payload_root / "class-roots.txt"
    declaration.write_text("stale/classes\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    java = bin_dir / "java"
    java.write_text("#!/bin/sh\nexit 0\n")
    java.chmod(0o755)
    command = JavaCoverageBackend().setup_command("generated").replace(
        "/tmp/testbench/coverage/generated/java", str(payload_root)
    )

    result = subprocess.run(
        ["sh", "-c", command],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )

    assert "__TESTPRISM_COVERAGE_SETUP__=ready" in result.stdout
    assert not declaration.exists()


def test_collection_preserves_jacoco_cli_stdout_and_stderr_on_failure(tmp_path):
    payload_root = tmp_path / "payload"
    workdir = tmp_path / "workdir"
    source = workdir / "src" / "App.java"
    source.parent.mkdir(parents=True)
    source.write_text("class App {}\n")
    (workdir / "target" / "classes").mkdir(parents=True)
    write_java_runtime_files(
        payload_root,
        workdir=str(workdir),
        target_paths=["src/App.java"],
        namespace="reference",
    )
    (payload_root / "jacoco.exec").write_bytes(b"execution-data")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    java = bin_dir / "java"
    java.write_text(
        "#!/bin/sh\n"
        "echo jacoco-cli-stdout\n"
        "echo jacoco-cli-stderr >&2\n"
        "exit 23\n"
    )
    java.chmod(0o755)
    command = JavaCoverageBackend().collect_command("reference").replace(
        "/tmp/testbench/coverage/reference/java", str(payload_root)
    )

    result = subprocess.run(
        ["sh", "-c", command],
        cwd=workdir,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
    )

    assert "jacoco-cli-stdout" in result.stdout
    assert "jacoco-cli-stderr" in result.stderr
    assert "__TESTPRISM_COVERAGE_COLLECT__=failed" in result.stdout
    assert not (payload_root / "coverage.json").exists()


def test_vendored_payload_tampering_is_unsupported_without_cache_or_network(tmp_path):
    write_java_runtime_files(
        tmp_path / "payload",
        workdir="/app",
        target_paths=["src/App.java"],
        namespace="generated",
    )
    payload_root = tmp_path / "payload"
    jar = payload_root / "jacocoagent.jar"
    jar.write_bytes(jar.read_bytes() + b"tampered")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    java = bin_dir / "java"
    java.write_text("#!/bin/sh\nexit 0\n")
    java.chmod(0o755)
    command = JavaCoverageBackend().setup_command("generated").replace(
        "/tmp/testbench/coverage/generated/java", str(payload_root)
    )
    result = subprocess.run(
        ["sh", "-c", command],
        check=True,
        capture_output=True,
        text=True,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin"},
    )
    assert "__TESTPRISM_COVERAGE_SETUP__=unsupported" in result.stdout
    assert "__TESTPRISM_COVERAGE_SETUP_REASON__=vendored-payload-integrity" in (
        result.stdout
    )


def test_missing_vendored_payload_is_unsupported_without_discovery(tmp_path):
    write_java_runtime_files(
        tmp_path / "payload",
        workdir="/app",
        target_paths=["src/App.java"],
        namespace="generated",
    )
    (tmp_path / "payload" / "jacococli.jar").unlink()
    command = JavaCoverageBackend().setup_command("generated").replace(
        "/tmp/testbench/coverage/generated/java", str(tmp_path / "payload")
    )
    result = subprocess.run(
        ["sh", "-c", command],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "__TESTPRISM_COVERAGE_SETUP__=unsupported" in result.stdout
    assert "__TESTPRISM_COVERAGE_SETUP_REASON__=vendored-payload-missing" in (
        result.stdout
    )


def test_harness_persists_java_normalized_artifact(tmp_path):
    backend = JavaCoverageBackend()
    harness = object.__new__(RuntimeHarness)
    harness._logger = __import__("logging").getLogger("java-coverage-test")

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
                '{"files":{"src/App.java":{"executed_lines":[1],"missing_lines":[]}}}'
            )

    handler = SimpleNamespace(
        task_id="java-task",
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
        / "java"
        / "coverage.json"
    )
    assert json.loads(report.read_text())["files"]["src/App.java"][
        "executed_lines"
    ] == [1]


def test_java_tool_options_renderer_preserves_task_flags_without_literal_expansion():
    backend = JavaCoverageBackend()
    value = backend.environment("/app", "generated")["JAVA_TOOL_OPTIONS"]
    rendered = _render_coverage_environment_assignment("JAVA_TOOL_OPTIONS", value)

    assert rendered.startswith("${JAVA_TOOL_OPTIONS:+${JAVA_TOOL_OPTIONS} }")
    assert "'${JAVA_TOOL_OPTIONS" not in rendered
    command = (
        "JAVA_TOOL_OPTIONS='-Xmx64m -Dtask.flag=true'; "
        f"export JAVA_TOOL_OPTIONS={rendered}; "
        "printf '%s' \"$JAVA_TOOL_OPTIONS\""
    )
    result = subprocess.run(
        ["sh", "-c", command],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.startswith("-Xmx64m -Dtask.flag=true ")
    assert "-Dorg.gradle.daemon=false" in result.stdout
    assert "-javaagent:" in result.stdout


def test_gradle_user_home_renderer_preserves_spaces_and_targets_init_script():
    backend = JavaCoverageBackend()
    value = backend.environment("/app with spaces", "generated")["GRADLE_USER_HOME"]
    rendered = _render_coverage_environment_assignment("GRADLE_USER_HOME", value)

    command = (
        f"export GRADLE_USER_HOME={rendered}; "
        "printf '%s' \"$GRADLE_USER_HOME\""
    )
    result = subprocess.run(
        ["sh", "-c", command],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout == "/tmp/testbench/coverage/generated/java/gradle-user-home"
