import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from testprism.harness.coverage_backends import get_default_coverage_backend_registry
from testprism.harness.javascript_coverage_backend import (
    JAVASCRIPT_SOURCE_SUFFIXES,
    JavaScriptCoverageBackend,
    normalize_v8_coverage,
    write_javascript_runtime_files,
)
from testprism.harness.testbench_harness import TestBenchHarness as RuntimeHarness


def _run_node_normalizer(runtime, coverage_dir, report_path):
    environment = os.environ.copy()
    environment.update(
        {
            "TESTPRISM_V8_COVERAGE_DIR": str(coverage_dir),
            "TESTPRISM_V8_TARGETS": str(runtime.root / "targets.json"),
            "TESTPRISM_V8_REPORT": str(report_path),
        }
    )
    return subprocess.run(
        ["node", str(runtime.root / "normalize_v8_coverage.js")],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_registry_selects_all_javascript_and_typescript_suffixes():
    selection = get_default_coverage_backend_registry().select(
        [
            "src/main.js",
            "src/view.jsx",
            "src/loader.mjs",
            "src/config.cjs",
            "src/app.ts",
            "src/view.tsx",
            "src/module.mts",
            "src/module.cts",
            "src/App.vue",
        ]
    )

    assert selection.unsupported_paths == ()
    assert selection.languages == ["javascript"]
    assert set(selection.supported_paths) == {
        "src/main.js",
        "src/view.jsx",
        "src/loader.mjs",
        "src/config.cjs",
        "src/app.ts",
        "src/view.tsx",
        "src/module.mts",
        "src/module.cts",
        "src/App.vue",
    }
    assert JavaScriptCoverageBackend().suffixes == JAVASCRIPT_SOURCE_SUFFIXES


def test_v8_ranges_map_urls_to_lines_and_fill_unexecuted_targets(tmp_path):
    source = tmp_path / "src" / "main.js"
    source.parent.mkdir()
    source.write_text(
        "function compute(value) {\n"
        "  // ignored\n"
        "  if (value) {\n"
        "    return value + 1;\n"
        "  }\n"
        "}\n"
    )
    unexecuted = tmp_path / "src" / "unused.ts"
    unexecuted.write_text("export const missing = true;\n")
    raw = {
        "result": [
            {
                "url": source.as_uri(),
                "functions": [
                    {
                        "ranges": [
                            {
                                "startOffset": 0,
                                "endOffset": len(source.read_bytes()),
                                "count": 1,
                            }
                        ]
                    }
                ],
            }
        ]
    }

    report = normalize_v8_coverage(
        raw,
        workdir=tmp_path,
        target_paths=["src/main.js", "src/unused.ts"],
    )

    assert report == {
        "files": {
            "src/main.js": {
                "executed_lines": [1, 3, 4],
                "missing_lines": [],
            },
            "src/unused.ts": {
                "executed_lines": [],
                "missing_lines": [1],
            },
        }
    }


def test_v8_normalizer_rejects_malformed_or_out_of_workdir_input(tmp_path):
    target = tmp_path / "src.js"
    target.write_text("x();\n")
    with pytest.raises(ValueError, match="result list"):
        normalize_v8_coverage({}, workdir=tmp_path, target_paths=["src.js"])
    with pytest.raises(ValueError, match="escapes workdir"):
        normalize_v8_coverage(
            {"result": []},
            workdir=tmp_path,
            target_paths=[tmp_path.parent / "outside.js"],
        )
    with pytest.raises(ValueError, match="outside the source bytes"):
        normalize_v8_coverage(
            {
                "result": [
                    {
                        "url": target.as_uri(),
                        "functions": [
                            {
                                "ranges": [
                                    {"startOffset": 0, "endOffset": 99, "count": 1}
                                ]
                            }
                        ],
                    }
                ]
            },
            workdir=tmp_path,
            target_paths=["src.js"],
        )


def test_v8_normalizer_ignores_non_target_file_urls_inside_workdir(tmp_path):
    target = tmp_path / "src.js"
    target.write_text("const value = 1;\n")
    report = {
        "result": [
            {
                "url": target.as_uri(),
                "functions": [
                    {
                        "ranges": [
                            {"startOffset": 0, "endOffset": 17, "count": 1}
                        ]
                    }
                ],
            },
            {
                # Node's ``--input-type=module`` bootstrap is represented as
                # a file URL under the workdir, but is not a requested target.
                "url": (tmp_path / "[eval1]").as_uri(),
                "functions": [
                    {
                        "ranges": [
                            {"startOffset": 0, "endOffset": 92, "count": 1}
                        ]
                    }
                ],
            },
            {
                # Node 18 also emits this synthetic startup script outside
                # the workdir whenever V8 coverage is enabled.
                "url": "file:///proc/.reset",
                "functions": [
                    {
                        "ranges": [
                            {"startOffset": 0, "endOffset": 92, "count": 1}
                        ]
                    }
                ],
            },
            {
                # npm's CLI is a global runtime file outside the task
                # workdir and must not abort target normalization.
                "url": "file:///usr/local/lib/node_modules/npm/bin/npm-cli.js",
                "functions": [
                    {
                        "ranges": [
                            {"startOffset": 0, "endOffset": 92, "count": 1}
                        ]
                    }
                ],
            },
            {
                "url": "evalmachine.<anonymous>",
                "functions": [
                    {
                        "ranges": [
                            {"startOffset": 0, "endOffset": 92, "count": 1}
                        ]
                    }
                ],
            },
            {
                "url": "internal/per_context/primordials.js",
                "functions": [
                    {
                        "ranges": [
                            {"startOffset": 0, "endOffset": 92, "count": 1}
                        ]
                    }
                ],
            },
        ]
    }

    normalized = normalize_v8_coverage(
        report,
        workdir=tmp_path,
        target_paths=["src.js"],
    )

    assert normalized == {
        "files": {
            "src.js": {
                "executed_lines": [1],
                "missing_lines": [],
            }
        }
    }


def test_v8_normalizer_rejects_malformed_ranges_in_ignored_scripts(tmp_path):
    target = tmp_path / "src.js"
    target.write_text("const value = 1;\n")
    with pytest.raises(ValueError, match="missing offsets"):
        normalize_v8_coverage(
            {
                "result": [
                    {
                        "url": "evalmachine.<anonymous>",
                        "functions": [{"ranges": [{"startOffset": 0, "count": 1}]}],
                    }
                ]
            },
            workdir=tmp_path,
            target_paths=["src.js"],
        )


def test_generated_node_normalizer_ignores_non_target_runtime_scripts(tmp_path):
    target = tmp_path / "src.js"
    target.write_text("const value = 1;\n")
    runtime = write_javascript_runtime_files(
        tmp_path / "runtime",
        workdir=str(tmp_path),
        target_paths=["src.js"],
    )
    coverage_dir = tmp_path / "v8"
    coverage_dir.mkdir()
    (coverage_dir / "coverage.json").write_text(
        json.dumps(
            {
                "result": [
                    {
                        "url": target.as_uri(),
                        "functions": [
                            {
                                "ranges": [
                                    {
                                        "startOffset": 0,
                                        "endOffset": len(target.read_bytes()),
                                        "count": 1,
                                    }
                                ]
                            }
                        ],
                    },
                    {
                        "url": "evalmachine.<anonymous>",
                        "functions": [
                            {
                                "ranges": [
                                    {"startOffset": 0, "endOffset": 999, "count": 1}
                                ]
                            }
                        ],
                    },
                    {
                        "url": "internal/per_context/primordials.js",
                        "functions": [
                            {
                                "ranges": [
                                    {"startOffset": 0, "endOffset": 999, "count": 1}
                                ]
                            }
                        ],
                    },
                    {
                        "url": "file:///usr/local/lib/node_modules/npm/bin/npm-cli.js",
                        "functions": [
                            {
                                "ranges": [
                                    {"startOffset": 0, "endOffset": 999, "count": 1}
                                ]
                            }
                        ],
                    },
                ]
            }
        )
    )
    report_path = tmp_path / "normalized.json"
    result = _run_node_normalizer(runtime, coverage_dir, report_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(report_path.read_text()) == {
        "files": {
            "src.js": {"executed_lines": [1], "missing_lines": []}
        }
    }


def test_generated_node_normalizer_rejects_target_offset_outside_source(tmp_path):
    target = tmp_path / "src.js"
    target.write_text("const value = 1;\n")
    runtime = write_javascript_runtime_files(
        tmp_path / "runtime",
        workdir=str(tmp_path),
        target_paths=["src.js"],
    )
    coverage_dir = tmp_path / "v8"
    coverage_dir.mkdir()
    (coverage_dir / "coverage.json").write_text(
        json.dumps(
            {
                "result": [
                    {
                        "url": target.as_uri(),
                        "functions": [
                            {
                                "ranges": [
                                    {
                                        "startOffset": 0,
                                        "endOffset": len(target.read_bytes()) + 1,
                                        "count": 1,
                                    }
                                ]
                            }
                        ],
                    }
                ]
            }
        )
    )
    result = _run_node_normalizer(runtime, coverage_dir, tmp_path / "normalized.json")
    assert result.returncode != 0
    assert "outside the source bytes" in result.stderr


def test_generated_node_normalizer_rejects_declared_target_outside_workdir(tmp_path):
    target = tmp_path / "src.js"
    target.write_text("const value = 1;\n")
    runtime = write_javascript_runtime_files(
        tmp_path / "runtime",
        workdir=str(tmp_path),
        target_paths=["src.js"],
    )
    outside = tmp_path.parent / "outside.js"
    outside.write_text("const outside = true;\n")
    escaped_link = tmp_path / "escaped.js"
    escaped_link.symlink_to(outside)
    manifest_path = runtime.root / "targets.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["targets"][0]["absolute"] = str(escaped_link)
    manifest_path.write_text(json.dumps(manifest))
    coverage_dir = tmp_path / "v8"
    coverage_dir.mkdir()
    result = _run_node_normalizer(runtime, coverage_dir, tmp_path / "normalized.json")
    assert result.returncode != 0
    assert "coverage target escapes workdir" in result.stderr


def test_runtime_namespaces_have_distinct_node_v8_directories(tmp_path):
    generated_root = tmp_path / "generated"
    reference_root = tmp_path / "reference"
    generated = write_javascript_runtime_files(
        generated_root,
        workdir="/app",
        target_paths=["src/main.js", "src/component.tsx"],
        namespace="generated",
    )
    reference = write_javascript_runtime_files(
        reference_root,
        workdir="/app",
        target_paths=["src/main.js", "src/component.tsx"],
        namespace="reference",
    )
    backend = JavaScriptCoverageBackend()

    assert generated.coverage_dir != reference.coverage_dir
    assert backend.environment("/app", "generated") != backend.environment(
        "/app", "reference"
    )
    assert "NODE_V8_COVERAGE" in backend.environment("/app", "generated")
    setup_command = backend.setup_command("reference")
    assert "command -v node" in setup_command
    assert "NODE_V8_COVERAGE" in setup_command
    assert "-e 'process.exit(0)'" in setup_command
    assert "find" in setup_command
    assert "unsupported" in setup_command
    assert backend.normalized_report_path("generated").endswith("coverage.json")
    assert (generated_root / "targets.json").is_file()
    assert (generated_root / "normalize_v8_coverage.js").is_file()
    manifest = json.loads((generated_root / "targets.json").read_text())
    assert [target["relative"] for target in manifest["targets"]] == [
        "src/component.tsx",
        "src/main.js",
    ]


def test_harness_persists_javascript_normalized_artifact(tmp_path):
    backend = JavaScriptCoverageBackend()
    harness = object.__new__(RuntimeHarness)
    harness._logger = __import__("logging").getLogger("javascript-coverage-test")

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
                '{"files":{"src/main.js":{"executed_lines":[1],"missing_lines":[]}}}'
            )

    handler = SimpleNamespace(
        task_id="javascript-task",
        trial_paths=SimpleNamespace(coverage_path=tmp_path / "coverage"),
    )
    report, error = harness._collect_coverage_artifact(
        Terminal(),
        Session(),
        handler,
        "baseline",
        namespace="generated",
        backend=backend,
    )

    assert error is None
    assert report == (
        tmp_path
        / "coverage"
        / "generated"
        / "baseline"
        / "javascript"
        / "coverage.json"
    )
    assert (
        json.loads(report.read_text())["files"]["src/main.js"]["executed_lines"]
        == [1]
    )
