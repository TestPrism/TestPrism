"""TestBench Harness -- evaluates an agent's ability to write tests.

Extends the base ``Harness`` with a pre-fix / post-fix evaluation loop:

    Phase 1  Sandbox build (reuses original Dockerfile / docker-compose)
    Phase 2  Agent writes tests from the task's test-oriented instruction
    Phase 4  Pre-fix run: execute agent tests, expect FAIL
    Phase 5  Apply solution.sh (fix the bug)
    Phase 6  Post-fix run: execute same tests, expect PASS
    Phase 7  Grade: compute Fail-to-Pass (F2P) verdicts

The static task body still comes from each task's ``task.yaml`` ``instruction``
field (unchanged by this harness). TestBench-only evaluation guidance is the
runtime system prompt defined in :mod:`testprism.harness.testbench_system_prompt`,
which :class:`TestBenchHarness` assigns in native replacement mode (see
:class:`testprism.agents.base_agent.BaseAgent`).
"""

import hashlib
import json
import logging
import shlex
import shutil
import tempfile
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from testprism.agents.base_agent import BaseAgent, SystemPromptMode
from testprism.agents.failure_mode import FailureMode
from testprism.agents.installed_agents.mini_swe_agent import MiniSweAgent
from testprism.handlers.trial_handler import TrialHandler
from testprism.harness.coverage_backends import (
    CoverageBackend,
    CoverageBackendRegistry,
    CoverageBackendTargetGroup,
    get_default_coverage_backend_registry,
)
from testprism.harness.entry_coverage import (
    ENTRY_METADATA_LANGUAGE_TO_RUNTIME_LANGUAGE,
    EntryCoverageMetric,
    calculate_entry_coverage,
)
from testprism.harness.entry_coverage_applicability import (
    EntryCoverageApplicabilityResolution,
    resolve_entry_coverage_applicability,
)
from testprism.harness.gold_coverage_applicability import (
    GoldCoverageApplicabilityResolution,
    resolve_gold_coverage_applicability,
)
from testprism.harness.harness import Harness
from testprism.harness.models import (
    CoverageStatus,
    EntryCoverage,
    GoldPatchCoverage,
    PatchExecutionResult,
    TestBenchResults,
    TestBenchTrialResults,
    TestCaseVerdict,
    TrialResults,
)
from testprism.harness.patch_coverage import (
    PatchChanges,
    calculate_patch_coverage,
    parse_unified_diff,
)
from testprism.harness.testbench_system_prompt import TESTBENCH_SYSTEM_PROMPT
from testprism.parsers.base_parser import UnitTestStatus
from testprism.patches.models import (
    PatchEntry,
    PatchLabel,
    PatchSetManifestProtocol,
    load_patch_set_manifest,
)
from testprism.patches.models_v4 import SolutionV4
from testprism.terminal.docker_compose_manager import DockerComposeManager
from testprism.terminal.terminal import Terminal, spin_up_terminal
from testprism.terminal.tmux_session import TmuxSession

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _EntryCoveragePlan:
    """Resolved per-trial Entry Coverage orchestration inputs."""

    target_paths: tuple[str, ...] = ()
    runtime_language: str | None = None
    metric: EntryCoverageMetric | None = None


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AGENT_TEST_SUBDIR = "agent-tests"
AGENT_TEST_QUARANTINE_ROOT = "/tmp/testbench/quarantine"
_AGENT_TEST_QUARANTINE_PREFIX = "__TESTPRISM_AGENT_TESTS_QUARANTINE__="


def _agent_test_dir(trial_handler: TrialHandler) -> str:
    """Absolute path of the agent-tests directory for a task.

    Anchored at ``task.workdir`` so the tests live inside the container's
    working directory (which varies per source: /app, /home/user, /workspace, ...).
    """
    workdir = (trial_handler.task.workdir or "/app").rstrip("/")
    return f"{workdir}/{AGENT_TEST_SUBDIR}"


def _agent_test_quarantine_command(
    trial_handler: TrialHandler,
    state_id: str,
) -> str:
    """Render a checked move of generated tests outside the task source root."""

    source = _agent_test_dir(trial_handler)
    state_key = hashlib.sha256(state_id.encode("utf-8")).hexdigest()[:16]
    state_root = f"{AGENT_TEST_QUARANTINE_ROOT}/{state_key}"
    destination = f"{state_root}/{AGENT_TEST_SUBDIR}"
    source_q = shlex.quote(source)
    state_root_q = shlex.quote(state_root)
    destination_q = shlex.quote(destination)
    quarantine_root_q = shlex.quote(AGENT_TEST_QUARANTINE_ROOT)
    return (
        "__ctb_quarantine_status=unknown; "
        f"if [ -L {source_q} ] || [ ! -d {source_q} ]; then "
        "__ctb_quarantine_status=invalid-source; "
        "elif [ -L /tmp/testbench ] || "
        f"[ -L {quarantine_root_q} ]; then "
        "__ctb_quarantine_status=unsafe-root; "
        f"elif [ -e {state_root_q} ] || [ -L {state_root_q} ]; then "
        "__ctb_quarantine_status=collision; "
        f"elif ! mkdir -p {state_root_q}; then "
        "__ctb_quarantine_status=prepare-failed; "
        f"elif [ -L {state_root_q} ]; then "
        "__ctb_quarantine_status=unsafe-root; "
        f"elif [ -e {destination_q} ] || [ -L {destination_q} ]; then "
        "__ctb_quarantine_status=collision; "
        f"elif ! __ctb_source_device=$(stat -c %d -- {source_q}) || "
        f"! __ctb_state_device=$(stat -c %d -- {state_root_q}); then "
        "__ctb_quarantine_status=stat-failed; "
        'elif [ "$__ctb_source_device" != "$__ctb_state_device" ]; then '
        "__ctb_quarantine_status=cross-device; "
        f"elif mv -- {source_q} {destination_q}; then "
        "__ctb_quarantine_status=quarantined; "
        "else __ctb_quarantine_status=move-failed; fi; "
        f"echo {_AGENT_TEST_QUARANTINE_PREFIX}$__ctb_quarantine_status"
    )


def _has_output_marker(pane: str, marker: str) -> bool:
    """Match a command result marker without accepting its echoed command text."""

    return any(line.strip() == marker for line in pane.splitlines())


def _output_marker_value(pane: str, prefix: str) -> str | None:
    """Return the final exact marker value, ignoring echoed shell commands."""

    for line in reversed(pane.splitlines()):
        rendered = line.strip()
        if rendered.startswith(prefix):
            return rendered[len(prefix) :]
    return None


_JAVA_TOOL_OPTIONS_EXPANSION = "${JAVA_TOOL_OPTIONS:+${JAVA_TOOL_OPTIONS} }"
_NATIVE_FLAG_KEYS = frozenset({"CFLAGS", "CXXFLAGS", "LDFLAGS"})


def _render_coverage_environment_assignment(key: str, value: str) -> str:
    """Quote backend environment values while preserving safe append contracts.

    JaCoCo uses one exact, backend-owned parameter expansion to append its
    agent to a task-provided ``JAVA_TOOL_OPTIONS`` value.  Quoting the whole
    value would pass that expansion literally, while allowing arbitrary shell
    syntax would make the task environment injectable.  Recognize only this
    prefix and quote its static suffix; all other values retain the historical
    shell quoting behavior (including the Python ``PYTHONPATH`` expansion).
    """

    if key == "PYTHONPATH" and "${PYTHONPATH" in value:
        prefix, expansion = value.split("${PYTHONPATH", 1)
        return f"{shlex.quote(prefix)}${{PYTHONPATH{expansion}"
    if key == "PATH" and value.endswith("${PATH:+:${PATH}}"):
        prefix = value[: -len("${PATH:+:${PATH}}")]
        if any(
            token in prefix
            for token in ("\x00", "\n", "\r", "${", "$(", "`", ";", "|", "&")
        ):
            raise ValueError("malformed Go coverage PATH value")
        return shlex.quote(prefix) + "${PATH:+:${PATH}}"
    native_expansion = f"${{{key}:+${{{key}}} }}"
    if key in _NATIVE_FLAG_KEYS and value.startswith(native_expansion):
        suffix = value[len(native_expansion) :]
        if (
            suffix != "--coverage"
            or any(
                token in suffix
                for token in ("\x00", "\n", "\r", "${", "$(", "`", ";", "|", "&")
            )
        ):
            raise ValueError("malformed native coverage compiler flag value")
        return native_expansion + shlex.quote(suffix)
    if key == "JAVA_TOOL_OPTIONS" and value.startswith(
        _JAVA_TOOL_OPTIONS_EXPANSION
    ):
        suffix = value[len(_JAVA_TOOL_OPTIONS_EXPANSION) :]
        if (
            not suffix.startswith(
                "-Dorg.gradle.daemon=false -javaagent:"
            )
            or any(
                token in suffix
                for token in ("\x00", "\n", "\r", "${", "$(", "`", ";", "|", "&")
            )
        ):
            raise ValueError("malformed Java coverage JAVA_TOOL_OPTIONS value")
        return _JAVA_TOOL_OPTIONS_EXPANSION + shlex.quote(suffix)
    return shlex.quote(value)


# ``git add -N --all`` is intentional: it lets a fresh applied worktree
# expose legitimate new production files in the canonical diff.  Explicit
# pathspec exclusions remove patch-tool backups and harness-owned artifacts
# without falling back to a tracked-files-only diff.
_CANONICAL_DIFF_EXCLUDES = (
    "solution_patch.diff",
    "*.orig",
    "**/*.orig",
    "*.rej",
    "**/*.rej",
    "agent-tests/**",
    "**/agent-tests/**",
    "coverage/**",
    "**/coverage/**",
    "**/.coverage*",
)


_CANONICAL_SNAPSHOT_EXIT_PREFIX = "__GOLD_SNAPSHOT_EXIT__="
_CANONICAL_DIFF_EXIT_PREFIX = "__GOLD_DIFF_EXIT__="
_PATCH_TOOL_MISSING_MARKER = "__TESTPRISM_PATCH_BINARY_MISSING__=1"
_PATCH_TOOL_WRAPPER_DIR = "/tmp/testbench/patch-tool"
CoverageArtifactMap = dict[str, Path]


def _canonical_stage_command(*, intent_to_add: bool) -> str:
    """Stage tracked updates plus non-ignored untracked files for a diff."""

    pathspecs = _canonical_pathspecs()
    add_command = "git add -N --" if intent_to_add else "git add --"
    return (
        f"git add -u -- . {pathspecs} && "
        f"git ls-files --others --exclude-standard -z -- . {pathspecs} | "
        f"xargs -0 -r {add_command}"
    )


def _canonical_pathspecs() -> str:
    """Render the shared snapshot/diff pathspec boundary."""

    return " ".join(
        f"{shlex.quote(f':(exclude){pathspec}')}"
        for pathspec in _CANONICAL_DIFF_EXCLUDES
    )


def _canonical_snapshot_command(workdir: str) -> str:
    """Reuse a valid HEAD or create a command-local pre-gold snapshot."""

    return (
        f"cd {shlex.quote(workdir)} && "
        "if git rev-parse --verify HEAD >/dev/null 2>&1; then true; else "
        "git init --quiet && "
        f"{_canonical_stage_command(intent_to_add=False)} && "
        "git -c user.name=TestBench -c user.email=testbench@localhost "
        "commit --quiet --allow-empty -m testbench-coverage-baseline; "
        "fi; "
        "snapshot_status=$?; "
        f"printf '\\n{_CANONICAL_SNAPSHOT_EXIT_PREFIX}%s\\n' \"$snapshot_status\""
    )


def _canonical_diff_command(workdir: str, output_path: str) -> str:
    """Build the sanitized zero-context diff command used by patch inspection."""

    pathspecs = _canonical_pathspecs()
    return (
        f"cd {shlex.quote(workdir)} && "
        f"{_canonical_stage_command(intent_to_add=True)} && "
        "git diff --find-renames --no-ext-diff --unified=0 HEAD -- . "
        f"{pathspecs} > {shlex.quote(output_path)}; "
        "diff_status=$?; "
        f"printf '\\n{_CANONICAL_DIFF_EXIT_PREFIX}%s\\n' \"$diff_status\""
    )


def _inspection_command_diagnostic(
    pane: str,
    *,
    prefix: str,
    stage: str,
) -> str | None:
    """Validate a machine-readable inspection command outcome."""

    value = _output_marker_value(pane, prefix)
    if value is None:
        return f"gold patch inspection {stage} missing exit marker"
    try:
        exit_code = int(value)
    except ValueError:
        return f"gold patch inspection {stage} has invalid exit marker: {value!r}"
    if exit_code != 0:
        return f"gold patch inspection {stage} failed with exit code {exit_code}"
    return None


def _patch_tool_wrapper_install_command() -> str:
    """Install a non-interactive ``patch`` wrapper inside the task container."""

    wrapper_path = f"{_PATCH_TOOL_WRAPPER_DIR}/patch"
    install_prefix = (
        f"mkdir -p {_PATCH_TOOL_WRAPPER_DIR} && "
        f"cat > {wrapper_path} <<'__TESTPRISM_PATCH_WRAPPER__'\n"
    )
    return install_prefix + f"""#!/bin/sh
set -eu
patch_bin=
for patch_dir in $(printf '%s' "$PATH" | tr ':' ' '); do
    candidate="$patch_dir/patch"
    if [ "$candidate" = "{_PATCH_TOOL_WRAPPER_DIR}/patch" ]; then
        continue
    fi
    if [ -x "$candidate" ]; then
        patch_bin="$candidate"
        break
    fi
done
if [ -z "$patch_bin" ]; then
    printf '%s\\n' '{_PATCH_TOOL_MISSING_MARKER}' >&2
    exit 127
fi
forward_flag=--forward
for patch_arg in "$@"; do
    case "$patch_arg" in
        -R|--reverse)
            forward_flag=
            break
            ;;
    esac
done
if [ -n "$forward_flag" ]; then
    exec "$patch_bin" "$forward_flag" --batch "$@"
fi
exec "$patch_bin" --batch "$@"
__TESTPRISM_PATCH_WRAPPER__
chmod 755 {_PATCH_TOOL_WRAPPER_DIR}/patch"""


def _inspection_apply_failure_reason(
    pane: str,
    failure: FailureMode,
) -> str | None:
    """Promote known patch-tool failures into deterministic diagnostics."""

    lowered = pane.lower()
    if _PATCH_TOOL_MISSING_MARKER in pane:
        return "patch fallback binary missing"
    if "patch: command not found" in lowered or "patch: not found" in lowered:
        return "patch fallback binary missing"
    if "reversed (or previously applied) patch detected" in lowered:
        return "patch rejected as reversed or previously applied"
    if failure == FailureMode.TEST_TIMEOUT and "assume -r? [n]" in lowered:
        return "patch rejected as reversed or previously applied"
    return None


# ---------------------------------------------------------------------------
# Harness implementation
# ---------------------------------------------------------------------------


class TestBenchHarness(Harness):
    """Harness that evaluates agent test-writing ability via F2P evaluation."""

    _results_cls = TestBenchResults
    _trial_results_cls = TestBenchTrialResults
    _MIN_APPLY_SOLUTION_TIMEOUT_SEC = 1800

    def __init__(
        self,
        *args,
        agent_tests_path: Path | None = None,
        coverage_registry: CoverageBackendRegistry | None = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._coverage_registry = (
            coverage_registry or get_default_coverage_backend_registry()
        )
        self._agent_tests_path = (
            agent_tests_path.expanduser().resolve(strict=True)
            if agent_tests_path is not None
            else None
        )
        if self._agent_tests_path is not None:
            source = self._agent_tests_path / AGENT_TEST_SUBDIR
            if not source.is_dir():
                source = self._agent_tests_path
            if not source.is_dir() or not (source / "run-tests.sh").is_file():
                raise ValueError(
                    "--agent-tests-path must be an agent-tests directory (or a "
                    "trial directory containing one) with run-tests.sh"
                )
            if len(self._dataset.tasks) != 1:
                raise ValueError(
                    "--agent-tests-path requires exactly one selected task"
                )
        self._patch_manifests: dict[str, tuple[PatchSetManifestProtocol, Path]] = {}
        if self._patch_set_path is None:
            return
        for task_path in self._dataset.tasks:
            task_id = self._task_id_for_path(task_path)
            manifest, root = load_patch_set_manifest(
                self._patch_set_path,
                task_path.name,
                task_key=task_id,
            )
            self._patch_manifests[task_id] = (manifest, root)

    def _create_agent_for_task(self, task_id: str) -> BaseAgent:
        agent = super()._create_agent_for_task(task_id)
        # Oracle and nop never send the task instruction to a model. Injecting
        # evaluation guidance into them is both meaningless and, under the
        # fail-closed prompt contract, would reject these validation agents.
        if getattr(agent, "consumes_task_instruction", True):
            agent.system_prompt_mode = SystemPromptMode.REPLACE
            agent.system_prompt = TESTBENCH_SYSTEM_PROMPT
            if isinstance(agent, MiniSweAgent):
                agent.enable_testbench_prompt_config()
        return agent

    # ── Container helpers ─────────────────────────────────────────────────

    def _coverage_backend_registry(self) -> CoverageBackendRegistry:
        """Return the additive backend registry used by this harness."""

        registry = getattr(self, "_coverage_registry", None)
        if registry is None:
            registry = get_default_coverage_backend_registry()
        return registry

    def _coverage_backend_for_targets(
        self, target_paths: list[str]
    ) -> CoverageBackend | None:
        """Resolve one runtime backend for a selected target set."""

        return self._coverage_backend_registry().backend_for_targets(target_paths)

    def _coverage_backend_groups_for_targets(
        self,
        target_paths: list[str],
    ) -> tuple[tuple[CoverageBackendTargetGroup, ...], str | None]:
        """Resolve deterministic backend groups for a shared target universe."""

        if not target_paths:
            return (), "coverage state has no registered target files"
        selection = self._coverage_backend_registry().select(target_paths)
        if selection.unsupported_paths:
            unsupported = ", ".join(selection.unsupported_paths)
            return (
                (),
                f"coverage state has unsupported target files: {unsupported}",
            )
        if not selection.backend_groups:
            return (), "coverage state has no registered target files"
        return selection.backend_groups, None

    def _coverage_targets_for_side(
        self, changes: PatchChanges, *, side: str
    ) -> list[str]:
        """Select registered changed-file targets for one patch side."""

        if side not in {"baseline", "gold"}:
            raise ValueError(f"invalid coverage patch side: {side!r}")
        candidates = [
            path
            for item in changes.files
            for path in (
                item.old_path if side == "baseline" else item.new_path,
            )
            if path is not None
            and (
                item.pre_target_lines if side == "baseline" else item.post_target_lines
            )
        ]
        selection = self._coverage_backend_registry().select(candidates)
        return list(selection.supported_paths)

    def _coverage_language_metadata(
        self, changes: PatchChanges
    ) -> tuple[str | None, list[str], list[str]]:
        """Return language labels and unsupported-path audit for a patch."""

        paths = [
            path
            for item in changes.files
            for path in (item.old_path, item.new_path)
            if path is not None
        ]
        selection = self._coverage_backend_registry().select(paths)
        return (
            selection.language,
            selection.languages,
            list(selection.unsupported_paths),
        )

    @staticmethod
    def _coverage_language_label(
        languages: list[str] | tuple[str, ...],
    ) -> str | None:
        """Render the public single-language label used by summaries."""

        if not languages:
            return None
        return languages[0] if len(languages) == 1 else "mixed"

    @staticmethod
    def _qualify_coverage_diagnostic(
        language: str,
        message: str,
        *,
        multi_backend: bool,
    ) -> str:
        if not multi_backend:
            return message
        return f"{language} {message}"

    def _merge_coverage_environments(
        self,
        merged: dict[str, str],
        environment: dict[str, str],
        *,
        language: str,
        multi_backend: bool,
    ) -> str | None:
        for key, value in environment.items():
            existing = merged.get(key)
            if existing is not None and existing != value:
                return self._qualify_coverage_diagnostic(
                    language,
                    f"coverage environment conflicts on {key}",
                    multi_backend=multi_backend,
                )
        merged.update(environment)
        return None

    def _setup_coverage_runtimes(
        self,
        terminal: Terminal,
        session: TmuxSession,
        trial_handler: TrialHandler,
        target_paths: list[str],
        namespace: str = "generated",
    ) -> tuple[
        dict[str, str] | None,
        tuple[CoverageBackendTargetGroup, ...],
        str | None,
    ]:
        """Prepare every backend needed by one generated/reference run."""

        groups, selection_error = self._coverage_backend_groups_for_targets(
            target_paths
        )
        if selection_error is not None:
            return None, (), selection_error

        merged_environment: dict[str, str] = {}
        ready_groups: list[CoverageBackendTargetGroup] = []
        errors: list[str] = []
        multi_backend = len(groups) > 1

        for group in groups:
            environment, error = self._setup_coverage_runtime(
                terminal,
                session,
                trial_handler,
                list(group.target_paths),
                namespace=namespace,
                backend=group.backend,
            )
            if error is not None:
                errors.append(
                    self._qualify_coverage_diagnostic(
                        group.language,
                        error,
                        multi_backend=multi_backend,
                    )
                )
            if environment is None:
                continue
            merge_error = self._merge_coverage_environments(
                merged_environment,
                environment,
                language=group.language,
                multi_backend=multi_backend,
            )
            if merge_error is not None:
                errors.append(merge_error)
                continue
            ready_groups.append(group)

        if not ready_groups:
            return None, (), "; ".join(errors) or "coverage setup failed"
        return merged_environment, tuple(ready_groups), "; ".join(errors) or None

    def _collect_coverage_artifacts(
        self,
        terminal: Terminal,
        session: TmuxSession,
        trial_handler: TrialHandler,
        state_id: str,
        *,
        namespace: str = "generated",
        groups: tuple[CoverageBackendTargetGroup, ...] = (),
    ) -> tuple[CoverageArtifactMap | None, str | None]:
        """Collect all backend reports produced by one generated/reference run."""

        if not groups:
            return None, "coverage collection has no registered backend"

        artifacts: CoverageArtifactMap = {}
        errors: list[str] = []
        multi_backend = len(groups) > 1

        for group in groups:
            report, error = self._collect_coverage_artifact(
                terminal,
                session,
                trial_handler,
                state_id,
                namespace=namespace,
                backend=group.backend,
            )
            if error is not None:
                errors.append(
                    self._qualify_coverage_diagnostic(
                        group.language,
                        error,
                        multi_backend=multi_backend,
                    )
                )
            if report is not None:
                artifacts[group.language] = report

        if not artifacts:
            return (
                None,
                "; ".join(errors)
                or "coverage collector did not produce coverage.json",
            )
        return artifacts, "; ".join(errors) or None

    def _resolve_gold_coverage_applicability(
        self,
        trial_handler: TrialHandler,
    ) -> GoldCoverageApplicabilityResolution | None:
        """Resolve governed applicability when the trial exposes a task path."""

        task_paths = getattr(trial_handler, "task_paths", None)
        task_input_path = getattr(task_paths, "input_path", None)
        if task_input_path is None:
            return None
        return resolve_gold_coverage_applicability(task_input_path)

    def _resolve_entry_coverage_applicability(
        self,
        trial_handler: TrialHandler,
    ) -> EntryCoverageApplicabilityResolution | None:
        """Resolve governed Entry Coverage applicability when a task path exists."""

        task_paths = getattr(trial_handler, "task_paths", None)
        task_input_path = getattr(task_paths, "input_path", None)
        if task_input_path is None:
            return None
        return resolve_entry_coverage_applicability(task_input_path)

    def _missing_gold_coverage_runtime_languages(
        self,
        resolution: GoldCoverageApplicabilityResolution,
    ) -> list[str]:
        """Return manifest/runtime families that lack a registered backend."""

        record = resolution.record
        if record is None:
            return []
        supported_languages = {
            backend.language for backend in self._coverage_backend_registry().backends
        }
        missing_languages: list[str] = []
        seen: set[str] = set()
        for language in (*record.unmapped_languages, *resolution.runtime_languages):
            if language in supported_languages or language in seen:
                continue
            seen.add(language)
            missing_languages.append(language)
        return missing_languages

    @staticmethod
    def _gold_patch_coverage_artifact_path(
        trial_handler,
    ) -> tuple[Path, str]:
        """Resolve the durable gold-patch summary path and wire name."""

        summary_path = trial_handler.trial_paths.coverage_path / "gold-patch.json"
        try:
            artifact_path = summary_path.relative_to(
                trial_handler.trial_paths.task_output_path
            ).as_posix()
        except ValueError:
            artifact_path = "coverage/gold-patch.json"
        return summary_path, artifact_path

    def _gate_gold_patch_coverage_from_applicability(
        self,
        trial_handler: TrialHandler,
    ) -> tuple[bool, GoldPatchCoverage | None]:
        """Return whether gold coverage should run for this task."""

        resolution = self._resolve_gold_coverage_applicability(trial_handler)
        if resolution is None or resolution.legacy:
            return True, None

        _, artifact_path = self._gold_patch_coverage_artifact_path(trial_handler)
        runtime_languages = list(resolution.runtime_languages)
        language = self._coverage_language_label(runtime_languages)

        if resolution.diagnostic is not None:
            return False, GoldPatchCoverage(
                status=CoverageStatus.FAILED,
                language=language,
                languages=runtime_languages,
                reason=(
                    "gold coverage applicability failed: "
                    f"{resolution.diagnostic}"
                ),
                artifact_path=artifact_path,
            )

        if resolution.decision == "not_applicable":
            return False, GoldPatchCoverage(
                status=CoverageStatus.UNSUPPORTED,
                language=language,
                languages=runtime_languages,
                reason=resolution.reason_code or "not_applicable",
                artifact_path=artifact_path,
            )

        missing_languages = self._missing_gold_coverage_runtime_languages(
            resolution
        )
        if missing_languages:
            summary_languages = runtime_languages + [
                language
                for language in missing_languages
                if language not in runtime_languages
            ]
            return False, GoldPatchCoverage(
                status=CoverageStatus.UNSUPPORTED,
                language=self._coverage_language_label(summary_languages),
                languages=summary_languages,
                reason=(
                    "gold coverage runtime has no backend for governed "
                    f"applicability language(s): {', '.join(missing_languages)}"
                ),
                artifact_path=artifact_path,
            )

        return True, None

    def _setup_agent_test_dir(
        self, session: TmuxSession, trial_handler: TrialHandler
    ) -> None:
        """Create the agent-tests directory inside the container."""
        session.send_keys(
            [f"mkdir -p {_agent_test_dir(trial_handler)}", "Enter"],
            block=True,
            max_timeout_sec=300,
        )

    def _quarantine_agent_tests(
        self,
        session: TmuxSession,
        trial_handler: TrialHandler,
        state_id: str,
    ) -> str | None:
        """Atomically isolate the generated suite before trusted setup."""

        try:
            session.send_keys(
                [_agent_test_quarantine_command(trial_handler, state_id), "Enter"],
                block=True,
                max_timeout_sec=300,
            )
            pane = session.capture_pane(capture_entire=True)
            status = _output_marker_value(pane, _AGENT_TEST_QUARANTINE_PREFIX)
        except Exception as exc:
            self._logger.warning(
                "Agent-test quarantine failed for %s/%s: %s",
                trial_handler.task_id,
                state_id,
                exc,
            )
            return f"agent-test quarantine failed: {exc}"
        if status == "quarantined":
            return None
        return (
            "agent-test quarantine failed in task container "
            f"(status={status or 'missing marker'})"
        )

    def _run_agent_tests(
        self,
        terminal: Terminal,
        session: TmuxSession,
        trial_handler: TrialHandler,
        timeout_sec: float | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> tuple[dict[str, UnitTestStatus] | None, FailureMode]:
        """Execute the agent-provided ``/agent-tests/run-tests.sh``.

        Exit code 0 → single virtual test ``agent_tests`` PASSED.
        Non-zero  → FAILED. Anything else (timeout, missing script) maps
        to a failure mode. A script-internal 127 is still a normal failure:
        only a missing ``run-tests.sh`` is classified as AGENT_TEST_MISSING.

        ``timeout_sec`` defaults to ``task.max_test_timeout_sec *
        global_timeout_multiplier`` (or ``global_test_timeout_sec`` when
        that CLI override is set), matching the logic used by
        ``Harness._run_tests`` for the standard test path.

        Returns ``(results_dict, failure_mode)``.
        """
        if timeout_sec is None:
            if self._global_test_timeout_sec:
                timeout_sec = self._global_test_timeout_sec
            else:
                timeout_sec = (
                    trial_handler.task.max_test_timeout_sec
                    * self._global_timeout_multiplier
                )

        agent_runner = f"{_agent_test_dir(trial_handler)}/run-tests.sh"
        workdir = (trial_handler.task.workdir or "/app").rstrip("/")
        marker = "__AGENT_TESTS_EXIT__"
        missing_marker = "__AGENT_TESTS_MISSING__"
        # Tests often live under agent-tests/ and ``import solution`` expects
        # ``solution.py`` on sys.path. Scripts run from that subdir only put
        # agent-tests on sys.path[0], not the task root — set PYTHONPATH.
        wd_q = shlex.quote(workdir)
        runner_q = shlex.quote(agent_runner)
        environment = dict(extra_env or {})
        environment.setdefault(
            "PYTHONPATH", f"{workdir}${{PYTHONPATH:+:${{PYTHONPATH}}}}"
        )
        exports: list[str] = []
        for key, value in environment.items():
            rendered = _render_coverage_environment_assignment(key, value)
            exports.append(f"export {key}={rendered}")
        export_command = " && ".join(exports)
        cmd = (
            f"if [ ! -f {runner_q} ]; then "
            f"echo {missing_marker}=1; "
            f"else cd {wd_q} && {export_command} && bash {runner_q}; "
            f"echo {marker}=$?; fi"
        )

        try:
            session.send_keys(
                [cmd, "Enter"],
                block=True,
                max_timeout_sec=timeout_sec,
            )
        except TimeoutError:
            logger.warning(
                f"Agent test execution timed out after {timeout_sec}s "
                f"for task {trial_handler.task_id}"
            )
            return None, FailureMode.TEST_TIMEOUT

        pane_output = session.capture_pane(capture_entire=True)

        exit_code: int | None = None
        missing_runner = False
        for line in reversed(pane_output.splitlines()):
            line = line.strip()
            if line.startswith(f"{missing_marker}="):
                missing_runner = True
                break
            if line.startswith(f"{marker}="):
                try:
                    exit_code = int(line.split("=", 1)[1])
                except ValueError:
                    exit_code = None
                break

        if missing_runner:
            logger.warning(f"{agent_runner} missing for {trial_handler.task_id}")
            return None, FailureMode.AGENT_TEST_MISSING

        if exit_code is None:
            logger.warning(
                f"Could not determine agent test exit code for {trial_handler.task_id}"
            )
            return None, FailureMode.PARSE_ERROR

        status = UnitTestStatus.PASSED if exit_code == 0 else UnitTestStatus.FAILED
        return {"agent_tests": status}, FailureMode.NONE

    def _run_reference_tests(
        self,
        terminal: Terminal,
        session: TmuxSession,
        trial_handler: TrialHandler,
        timeout_sec: float | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> FailureMode:
        """Run the trusted task ``run-tests.sh`` after agent status capture.

        The reference suite is observational: its exit code is intentionally
        ignored because a known failing baseline (and occasionally a failing
        golden test) is expected.  Timeouts, setup errors, missing runners, and
        an unparseable command marker are returned as coverage diagnostics and
        never overwrite the generated-test result.
        """

        if timeout_sec is None:
            if self._global_test_timeout_sec:
                timeout_sec = self._global_test_timeout_sec
            else:
                timeout_sec = (
                    trial_handler.task.max_test_timeout_sec
                    * self._global_timeout_multiplier
                )

        marker = "__REFERENCE_TESTS_EXIT__"
        workdir = (trial_handler.task.workdir or "/app").rstrip("/")
        runner = DockerComposeManager.CONTAINER_TEST_DIR / "run-tests.sh"
        environment = dict(extra_env or {})
        environment.setdefault(
            "PYTHONPATH", f"{workdir}${{PYTHONPATH:+:${{PYTHONPATH}}}}"
        )
        exports: list[str] = []
        for key, value in environment.items():
            rendered = _render_coverage_environment_assignment(key, value)
            exports.append(f"export {key}={rendered}")
        export_command = " && ".join(exports)
        runner_q = shlex.quote(str(runner))
        wd_q = shlex.quote(workdir)
        try:
            # Copy only the trusted runner/tests payload, never the agent test
            # directory.  This happens after _run_agent_tests has captured the
            # generated suite's status.
            self._setup_test_env(terminal, trial_handler)
            command = (
                f"cd {wd_q} && unset PYTHONPATH COVERAGE_PROCESS_START COVERAGE_FILE "
                f"&& {export_command} && bash {runner_q}; "
                f"echo {marker}=$?"
            )
            session.send_keys(
                [command, "Enter"],
                block=True,
                max_timeout_sec=timeout_sec,
            )
        except TimeoutError:
            self._logger.warning(
                "Reference test execution timed out after %ss for task %s",
                timeout_sec,
                trial_handler.task_id,
            )
            return FailureMode.TEST_TIMEOUT
        except Exception as exc:
            self._logger.warning(
                "Reference test setup failed for %s: %s",
                trial_handler.task_id,
                exc,
            )
            return FailureMode.UNKNOWN_AGENT_ERROR

        for line in reversed(session.capture_pane(capture_entire=True).splitlines()):
            line = line.strip()
            if not line.startswith(f"{marker}="):
                continue
            try:
                # A non-zero trusted-runner result is diagnostic only.  The
                # coverage collector decides whether an artifact is usable.
                int(line.split("=", 1)[1])
            except ValueError:
                return FailureMode.PARSE_ERROR
            return FailureMode.NONE
        return FailureMode.PARSE_ERROR

    def _setup_coverage_runtime(
        self,
        terminal: Terminal,
        session: TmuxSession,
        trial_handler: TrialHandler,
        target_paths: list[str],
        namespace: str = "generated",
        backend: CoverageBackend | None = None,
    ) -> tuple[dict[str, str] | None, str | None]:
        """Install one namespaced coverage runtime in a state container.

        Generated and trusted-reference suites deliberately receive separate
        bootstrap, config, data, and report paths.  A runtime setup failure is
        returned to the caller rather than changing the agent-test verdict.
        """

        if not target_paths:
            return None, "coverage state has no registered target files"
        if backend is None:
            backend = self._coverage_backend_for_targets(target_paths)
        if backend is None:
            return None, "coverage state has no single registered backend"
        try:
            with tempfile.TemporaryDirectory(
                prefix="testprism-coverage-runtime-"
            ) as raw:
                runtime_root = Path(raw)
                files = backend.write_runtime_files(
                    runtime_root,
                    workdir=(trial_handler.task.workdir or "/app").rstrip("/"),
                    target_paths=target_paths,
                    namespace=namespace,
                )
                terminal.copy_to_container(
                    files.root,
                    container_dir=files.container_root,
                )
            session.send_keys(
                [backend.setup_command(namespace), "Enter"],
                block=True,
                max_timeout_sec=300,
            )
            pane = session.capture_pane(capture_entire=True)
            setup_status = _output_marker_value(
                pane, "__TESTPRISM_COVERAGE_SETUP__="
            )
            if setup_status != "ready":
                rendered_status = setup_status or "missing marker"
                return (
                    None,
                    "coverage package setup failed in task container "
                    f"(status={rendered_status})",
                )
            return (
                backend.environment(
                    (trial_handler.task.workdir or "/app").rstrip("/"),
                    namespace,
                ),
                None,
            )
        except Exception as exc:
            self._logger.warning(
                "Coverage setup failed for %s: %s",
                trial_handler.task_id,
                exc,
            )
            return None, f"coverage setup failed: {exc}"

    def _collect_coverage_artifact(
        self,
        terminal: Terminal,
        session: TmuxSession,
        trial_handler: TrialHandler,
        state_id: str,
        namespace: str = "generated",
        backend: CoverageBackend | None = None,
    ) -> tuple[Path | None, str | None]:
        """Finalize one namespaced data directory into the trial artifact tree."""

        # Production call sites pass the backend selected for the matching
        # target paths.  A single-backend fallback keeps direct legacy callers
        # deterministic without assuming Python or another language.
        if backend is None:
            registered = self._coverage_backend_registry().backends
            backend = registered[0] if len(registered) == 1 else None
        if backend is None:
            return None, "coverage collection has no unambiguous backend"
        report_path = backend.normalized_report_path(namespace)
        try:
            session.send_keys(
                [backend.collect_command(namespace), "Enter"],
                block=True,
                max_timeout_sec=300,
            )
            pane = session.capture_pane(capture_entire=True)
            collection_status = _output_marker_value(
                pane, "__TESTPRISM_COVERAGE_COLLECT__="
            )
            if collection_status != "collected":
                rendered_status = collection_status or "missing marker"
                return (
                    None,
                    "coverage data collection failed in task container "
                    f"(status={rendered_status})",
                )
            state_dir = trial_handler.trial_paths.coverage_path / namespace / state_id
            # Python keeps the historical unsuffixed artifact location for
            # compatibility.  New language backends receive a language
            # segment so mixed-language reports cannot overwrite one another.
            if backend.language != "python":
                state_dir = state_dir / backend.language
            terminal.copy_from_container(report_path, state_dir)
            report = state_dir / Path(report_path).name
            if not report.is_file():
                return None, "coverage collector did not produce coverage.json"
            return report, None
        except Exception as exc:
            self._logger.warning(
                "Coverage collection failed for %s/%s: %s",
                trial_handler.task_id,
                state_id,
                exc,
            )
            return None, f"coverage collection failed: {exc}"

    def _apply_solution(
        self,
        terminal: Terminal,
        session: TmuxSession,
        trial_handler: TrialHandler,
    ) -> FailureMode:
        """Copy ``solution.sh`` into the container and execute it."""
        solution_path = trial_handler.task_paths.solution_path

        terminal.copy_to_container(
            paths=[solution_path],
            container_dir="/tmp/testbench",
        )

        workdir = (trial_handler.task.workdir or "/app").rstrip("/")
        if self._global_test_timeout_sec:
            timeout_sec = max(
                self._global_test_timeout_sec,
                self._MIN_APPLY_SOLUTION_TIMEOUT_SEC,
            )
        else:
            timeout_sec = max(
                trial_handler.task.max_test_timeout_sec
                * self._global_timeout_multiplier,
                self._MIN_APPLY_SOLUTION_TIMEOUT_SEC,
            )
        try:
            session.send_keys(
                [f"cd {workdir} && bash /tmp/testbench/{solution_path.name}", "Enter"],
                block=True,
                max_timeout_sec=timeout_sec,
            )
            return FailureMode.NONE
        except TimeoutError:
            logger.warning(
                f"solution.sh timed out after {timeout_sec}s "
                f"for task {trial_handler.task_id}"
            )
            return FailureMode.TEST_TIMEOUT

    def _apply_patch_entry(
        self,
        terminal: Terminal,
        session: TmuxSession,
        trial_handler: TrialHandler,
        patch: PatchEntry | SolutionV4,
        patch_root: Path,
    ) -> FailureMode:
        """Copy and execute one versioned ``solution.sh`` artifact."""

        patch_path = (patch_root / patch.path).resolve(strict=True)
        patch_path.relative_to(patch_root.resolve(strict=True))
        terminal.copy_to_container(
            trial_handler.task_paths.test_dir,
            container_dir="/tests",
        )
        terminal.copy_to_container(patch_path, container_dir="/tmp/testbench")

        workdir = (trial_handler.task.workdir or "/app").rstrip("/")
        if self._global_test_timeout_sec:
            timeout_sec = max(
                self._global_test_timeout_sec,
                self._MIN_APPLY_SOLUTION_TIMEOUT_SEC,
            )
        else:
            timeout_sec = max(
                trial_handler.task.effective_max_solution_timeout_sec
                * self._global_timeout_multiplier,
                self._MIN_APPLY_SOLUTION_TIMEOUT_SEC,
            )
        marker = "__PATCH_SOLUTION_EXIT__"
        command = (
            f"cd {shlex.quote(workdir)} && "
            f"{_patch_tool_wrapper_install_command()} && "
            f"PATH={shlex.quote(_PATCH_TOOL_WRAPPER_DIR)}:$PATH "
            f"bash /tmp/testbench/{shlex.quote(patch_path.name)}; "
            f"echo {marker}=$?"
        )
        try:
            session.send_keys(
                [command, "Enter"],
                block=True,
                max_timeout_sec=timeout_sec,
            )
        except TimeoutError:
            self._logger.warning(
                "solution %s timed out after %ss for task %s",
                patch.id,
                timeout_sec,
                trial_handler.task_id,
            )
            return FailureMode.TEST_TIMEOUT

        for line in reversed(session.capture_pane(capture_entire=True).splitlines()):
            line = line.strip()
            if not line.startswith(f"{marker}="):
                continue
            try:
                return (
                    FailureMode.NONE
                    if int(line.split("=", 1)[1]) == 0
                    else FailureMode.UNKNOWN_AGENT_ERROR
                )
            except ValueError:
                break
        return FailureMode.PARSE_ERROR

    # ── Verdict computation ───────────────────────────────────────────────

    @staticmethod
    def _compute_verdicts(
        pre_results: dict[str, UnitTestStatus],
        post_results: dict[str, UnitTestStatus],
    ) -> dict[str, TestCaseVerdict]:
        """Compare pre-fix and post-fix results to assign verdicts."""
        verdicts: dict[str, TestCaseVerdict] = {}
        all_tests = set(pre_results.keys()) | set(post_results.keys())

        for test_name in all_tests:
            pre = pre_results.get(test_name)
            post = post_results.get(test_name)

            if pre is None or post is None:
                verdicts[test_name] = TestCaseVerdict.ERROR
            elif pre == UnitTestStatus.FAILED and post == UnitTestStatus.PASSED:
                verdicts[test_name] = TestCaseVerdict.EFFECTIVE
            elif pre == UnitTestStatus.PASSED and post == UnitTestStatus.PASSED:
                verdicts[test_name] = TestCaseVerdict.FALSE_POSITIVE
            elif pre == UnitTestStatus.FAILED and post == UnitTestStatus.FAILED:
                verdicts[test_name] = TestCaseVerdict.FALSE_NEGATIVE
            elif pre == UnitTestStatus.PASSED and post == UnitTestStatus.FAILED:
                verdicts[test_name] = TestCaseVerdict.REGRESSION
            else:
                verdicts[test_name] = TestCaseVerdict.ERROR

        return verdicts

    # ── Coverage reading ──────────────────────────────────────────────────

    def _read_coverage_from_container(
        self,
        session: TmuxSession,
        trial_handler: TrialHandler,
    ) -> tuple[float | None, float | None]:
        """Attempt to read coverage JSON from the container."""
        session.send_keys(
            [
                (
                    f"cat {_agent_test_dir(trial_handler)}/coverage.json "
                    "2>/dev/null || echo '{}'"
                ),
                "Enter",
            ],
            block=True,
            max_timeout_sec=10,
        )
        pane = session.capture_pane(capture_entire=True)

        try:
            for line in pane.splitlines():
                line = line.strip()
                if line.startswith("{") and "totals" in line:
                    data = json.loads(line)
                    totals = data.get("totals", {})
                    return (
                        totals.get("percent_covered"),
                        totals.get("percent_covered_branches"),
                    )
        except (json.JSONDecodeError, KeyError):
            pass

        return None, None

    # ── Entry Coverage integration ────────────────────────────────────────

    @staticmethod
    def _entry_metadata(trial_handler):
        """Return the optional task entry metadata without inferring anything.

        Entry inference is a build-time concern.  Runtime orchestration only
        reads the versioned ``test_entry_inferences`` object already attached
        to the task and treats every other shape as unavailable for target
        expansion.  The calculator still receives the task so malformed
        metadata can be reported as ``failed`` rather than silently ignored.
        """

        task = getattr(trial_handler, "task", None)
        if isinstance(task, Mapping):
            return task.get("test_entry_inferences")
        return getattr(task, "test_entry_inferences", None)

    @classmethod
    def _entry_metadata_language(cls, trial_handler) -> str | None:
        """Return the declared entry-metadata language, if any."""

        metadata = cls._entry_metadata(trial_handler)
        if isinstance(metadata, Mapping):
            language = metadata.get("language")
        else:
            language = getattr(metadata, "language", None)
        return language if isinstance(language, str) and language else None

    @classmethod
    def _entry_coverage_target_paths(
        cls,
        trial_handler,
        *,
        allow_non_python: bool = False,
    ) -> list[str]:
        """Return declared entry files eligible for runtime instrumentation.

        Legacy, non-governed behavior stays Python-only. Governed hardest-500
        tasks may enable non-Python metadata families once applicability has
        been resolved separately from gold changed-line coverage.
        """

        metadata = cls._entry_metadata(trial_handler)
        if isinstance(metadata, Mapping):
            language = metadata.get("language")
            entries = metadata.get("entries")
        else:
            language = getattr(metadata, "language", None)
            entries = getattr(metadata, "entries", None)
        runtime_language = (
            ENTRY_METADATA_LANGUAGE_TO_RUNTIME_LANGUAGE.get(language)
            if isinstance(language, str)
            else None
        )
        if metadata is None or runtime_language is None:
            return []
        if not allow_non_python and language != "python":
            return []
        if not isinstance(entries, list):
            return []
        paths = {
            path
            for entry in entries
            for path in [
                (
                    entry.get("path")
                    if isinstance(entry, Mapping)
                    else getattr(entry, "path", None)
                )
            ]
            if isinstance(path, str) and path
        }
        return sorted(paths)

    @classmethod
    def _entry_coverage_runtime_language(cls, trial_handler) -> str | None:
        """Map declared entry metadata onto the runtime report family."""

        language = cls._entry_metadata_language(trial_handler)
        if language is None:
            return None
        return ENTRY_METADATA_LANGUAGE_TO_RUNTIME_LANGUAGE.get(language)

    @staticmethod
    def _coverage_report_paths(
        reports: Mapping[str, Path] | None,
    ) -> list[Path]:
        if not reports:
            return []
        return [reports[language] for language in sorted(reports)]

    @staticmethod
    def _coverage_report_for_language(
        reports: Mapping[str, Path] | None,
        language: str | None,
    ) -> Path | None:
        if not reports:
            return None
        if language is not None and language in reports:
            return reports[language]
        if len(reports) == 1:
            return next(iter(reports.values()))
        return None

    @staticmethod
    def _entry_coverage_artifact_path(trial_handler) -> tuple[Path, str]:
        """Resolve the durable Entry Coverage summary path and wire name."""

        summary_path = trial_handler.trial_paths.coverage_path / "entry-coverage.json"
        try:
            artifact_path = summary_path.relative_to(
                trial_handler.trial_paths.task_output_path
            ).as_posix()
        except ValueError:
            # Test doubles may not expose a common task output root.  The
            # stable artifact name remains useful for those callers.
            artifact_path = "coverage/entry-coverage.json"
        return summary_path, artifact_path

    def _persist_generated_gold_report(
        self,
        trial_handler,
        report: Path | str | None,
    ) -> Path | None:
        """Persist one canonical generated gold report for audit/replay.

        The coverage collector already stores a state-specific report under
        ``coverage/generated/<state>/coverage.json``.  A stable ``gold`` alias
        makes the report consumed by Entry Coverage discoverable for both
        patch-set and legacy solution evaluations, while retaining the
        original artifact used by changed-line coverage.
        """

        if report is None:
            return None
        source = Path(report)
        if not source.is_file():
            return None
        destination = (
            trial_handler.trial_paths.coverage_path
            / "generated"
            / "gold"
            / "coverage.json"
        )
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.resolve() != destination.resolve():
                shutil.copy2(source, destination)
            return destination
        except Exception as exc:
            self._logger.warning(
                "Could not persist generated gold coverage report for %s: %s",
                trial_handler.task_id,
                exc,
            )
            return source

    def _calculate_and_persist_entry_coverage(
        self,
        trial_handler,
        results: TestBenchTrialResults,
        generated_report: Path | str | None,
        collection_error: str | None = None,
    ) -> None:
        """Calculate Entry Coverage from the existing generated gold report.

        This method is deliberately fail-soft: a setup/collection/calculation
        problem becomes an observational ``failed`` Entry Coverage result and
        never alters agent status, F2P, patch verdicts, or gold-patch coverage.
        The raw report (when available) and summary are persisted under the
        trial's ``coverage/`` artifact tree.
        """
        _, artifact_path = self._entry_coverage_artifact_path(trial_handler)
        canonical_report = self._persist_generated_gold_report(
            trial_handler,
            generated_report,
        )
        try:
            metric = calculate_entry_coverage(
                getattr(trial_handler, "task", None),
                canonical_report or generated_report,
                artifact_path=artifact_path,
            )
        except Exception as exc:
            metric = EntryCoverageMetric(
                status="failed",
                reason=f"entry coverage calculation failed: {exc}",
                artifact_path=artifact_path,
            )

        # A collector/setup diagnostic is authoritative only when no usable
        # generated report exists.  If a report was collected, unrelated
        # trusted-reference diagnostics must not poison the observational
        # Entry Coverage result.
        if (
            collection_error
            and canonical_report is None
            and metric.status not in {"unavailable", "unsupported"}
        ):
            metric.status = "failed"
            metric.reason = collection_error
        self._persist_entry_coverage_summary(
            trial_handler,
            results,
            metric,
        )

    def _persist_entry_coverage_summary(
        self,
        trial_handler,
        results: TestBenchTrialResults,
        metric: EntryCoverageMetric,
    ) -> None:
        """Serialize one Entry Coverage summary without affecting verdict logic."""

        summary_path, artifact_path = self._entry_coverage_artifact_path(
            trial_handler
        )
        payload = metric.as_dict()
        payload["artifact_path"] = payload.get("artifact_path") or artifact_path
        try:
            results.entry_coverage = EntryCoverage.model_validate(payload)
            summary_path.parent.mkdir(parents=True, exist_ok=True)
            summary_path.write_text(results.entry_coverage.model_dump_json(indent=2))
        except Exception as exc:
            # Keep a valid result object even if an artifact filesystem or
            # model-conversion edge case occurs.  This path is not allowed to
            # fail the agent evaluation.
            self._logger.warning(
                "Could not persist Entry Coverage summary for %s: %s",
                trial_handler.task_id,
                exc,
            )
            results.entry_coverage = EntryCoverage(
                status="failed",
                reason=f"entry coverage summary persistence failed: {exc}",
                artifact_path=artifact_path,
            )

    def _plan_entry_coverage(
        self,
        trial_handler: TrialHandler,
    ) -> _EntryCoveragePlan:
        """Resolve one trial's Entry Coverage gate, runtime, and targets."""

        resolution = self._resolve_entry_coverage_applicability(trial_handler)
        if resolution is None or resolution.legacy:
            return _EntryCoveragePlan(
                target_paths=tuple(
                    self._entry_coverage_target_paths(trial_handler)
                ),
                runtime_language=self._entry_coverage_runtime_language(
                    trial_handler
                ),
            )

        if resolution.diagnostic is not None:
            return _EntryCoveragePlan(
                metric=EntryCoverageMetric(
                    status="failed",
                    reason=(
                        "entry coverage applicability failed: "
                        f"{resolution.diagnostic}"
                    ),
                )
            )

        if resolution.decision == "not_applicable":
            return _EntryCoveragePlan(
                metric=EntryCoverageMetric(
                    status="unsupported",
                    reason=resolution.reason_code or "not_applicable",
                )
            )

        metadata = self._entry_metadata(trial_handler)
        if metadata is None:
            return _EntryCoveragePlan(
                metric=EntryCoverageMetric(
                    status="unavailable",
                    reason="task has no test entry metadata",
                )
            )

        metadata_language = self._entry_metadata_language(trial_handler)
        runtime_language = self._entry_coverage_runtime_language(trial_handler)
        if (
            metadata_language is not None
            and metadata_language in ENTRY_METADATA_LANGUAGE_TO_RUNTIME_LANGUAGE
            and runtime_language is None
        ):
            return _EntryCoveragePlan(
                metric=EntryCoverageMetric(
                    status="unsupported",
                    language=metadata_language,
                    reason=(
                        "entry coverage runtime has no backend for metadata "
                        f"language {metadata_language!r}"
                    ),
                )
            )

        if runtime_language is not None:
            supported_languages = {
                backend.language
                for backend in self._coverage_backend_registry().backends
            }
            if runtime_language not in supported_languages:
                return _EntryCoveragePlan(
                    metric=EntryCoverageMetric(
                        status="unsupported",
                        language=metadata_language,
                        reason=(
                            "entry coverage runtime has no backend for metadata "
                            f"language {metadata_language!r}"
                        ),
                    )
                )

        return _EntryCoveragePlan(
            target_paths=tuple(
                self._entry_coverage_target_paths(
                    trial_handler,
                    allow_non_python=True,
                )
            ),
            runtime_language=runtime_language,
        )

    # ── Core trial loop (override) ────────────────────────────────────────

    def _run_trial(
        self,
        trial_handler: TrialHandler,
    ) -> TrialResults:
        if self._patch_set_path is not None:
            return self._run_patch_set_trial(trial_handler)
        return self._run_legacy_trial(trial_handler)

    def _generate_tests_for_patch_evaluation(
        self,
        trial_handler: TrialHandler,
        results: TestBenchTrialResults,
    ) -> Path | None:
        """Generate tests once, then return a host copy for isolated runs."""

        if self._agent_tests_path is not None:
            source = self._agent_tests_path / AGENT_TEST_SUBDIR
            if not source.is_dir():
                source = self._agent_tests_path
            host_dir = Path(tempfile.mkdtemp(prefix="testbench-agent-tests-replay-"))
            shutil.copytree(source, host_dir / AGENT_TEST_SUBDIR, symlinks=True)
            self._persist_agent_tests(trial_handler, host_dir)
            return host_dir

        try:
            with spin_up_terminal(
                client_container_name=trial_handler.client_container_name,
                client_image_name=trial_handler.client_image_name,
                server_image_name=getattr(trial_handler, "server_image_name", None),
                docker_image_name_prefix=trial_handler.docker_image_name_prefix,
                docker_compose_path=trial_handler.task_paths.docker_compose_path,
                sessions_logs_path=trial_handler.trial_paths.sessions_path / "agent",
                agent_logs_path=trial_handler.trial_paths.agent_logging_dir,
                commands_path=trial_handler.trial_paths.commands_path,
                no_rebuild=self._no_rebuild,
                cleanup=self._cleanup,
                livestream=self._livestream,
                disable_recording=trial_handler.task.disable_asciinema,
                agent_name=self._agent_class.name(),
                agent_kwargs=self._agent_kwargs,
            ) as terminal:
                session = terminal.create_session(
                    "agent",
                    is_active_stream=self._livestream,
                    as_configured_user=True,
                )
                self._setup_agent_test_dir(session, trial_handler)
                agent = self._create_agent_for_task(trial_handler.task_id)
                results.agent_started_at = datetime.now(timezone.utc).isoformat()
                agent_result, failure = self._run_agent(
                    session=session,
                    agent=agent,
                    trial_handler=trial_handler,
                )
                results.agent_ended_at = datetime.now(timezone.utc).isoformat()
                if agent_result is not None:
                    results.total_input_tokens = agent_result.total_input_tokens
                    results.total_output_tokens = agent_result.total_output_tokens
                if failure not in (FailureMode.NONE, FailureMode.AGENT_TIMEOUT):
                    results.failure_mode = failure
                    return None

                host_dir = Path(tempfile.mkdtemp(prefix="testbench-agent-tests-"))
                try:
                    terminal.copy_from_container(
                        _agent_test_dir(trial_handler), host_dir
                    )
                except Exception:
                    shutil.rmtree(host_dir, ignore_errors=True)
                    results.failure_mode = FailureMode.UNKNOWN_AGENT_ERROR
                    return None
                self._persist_agent_tests(trial_handler, host_dir)
                return host_dir
        except Exception as exc:
            results.failure_mode = self._map_runtime_exception_to_failure_mode(exc)
            self._logger.error(
                "Test generation failed for %s: %s",
                trial_handler.task_id,
                exc,
                exc_info=True,
            )
            return None

    def _persist_agent_tests(
        self,
        trial_handler: TrialHandler,
        extracted_root: Path,
    ) -> None:
        """Persist a replayable test snapshot and a content-hash manifest."""

        extracted = extracted_root / AGENT_TEST_SUBDIR
        if not extracted.is_dir():
            extracted = extracted_root
        destination = trial_handler.trial_paths.agent_tests_path
        if destination.exists():
            self._logger.warning(
                "Preserving existing agent-test snapshot for %s at %s",
                trial_handler.task_id,
                destination,
            )
            return

        shutil.copytree(extracted, destination, symlinks=True)
        files: dict[str, str] = {}
        for path in sorted(destination.rglob("*")):
            relative = path.relative_to(destination).as_posix()
            if path.is_symlink():
                files[relative] = f"symlink:{path.readlink()}"
            elif path.is_file():
                files[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest = {
            "schema_version": 1,
            "task_id": trial_handler.task_id,
            "files": files,
        }
        trial_handler.trial_paths.agent_tests_manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        )

    def _inspect_gold_patch(
        self,
        trial_handler: TrialHandler,
        patch: PatchEntry,
        patch_root: Path,
    ) -> tuple[PatchChanges | None, str | None]:
        """Apply gold once in a disposable container and persist its canonical diff."""

        container_name = f"{trial_handler.client_container_name}-coverage-inspect"
        session_root = trial_handler.trial_paths.sessions_path / "coverage-inspect"
        artifact_dir = trial_handler.trial_paths.coverage_path
        artifact_dir.mkdir(parents=True, exist_ok=True)
        container_diff = "/tmp/testbench/gold.patch.diff"
        diff_path = artifact_dir / Path(container_diff).name
        try:
            diff_path.unlink(missing_ok=True)
            with spin_up_terminal(
                client_container_name=container_name,
                client_image_name=trial_handler.client_image_name,
                server_image_name=getattr(trial_handler, "server_image_name", None),
                docker_image_name_prefix=trial_handler.docker_image_name_prefix,
                docker_compose_path=trial_handler.task_paths.docker_compose_path,
                sessions_logs_path=session_root,
                agent_logs_path=trial_handler.trial_paths.agent_logging_dir,
                commands_path=trial_handler.trial_paths.commands_path,
                no_rebuild=True,
                cleanup=False,
                livestream=False,
                disable_recording=trial_handler.task.disable_asciinema,
            ) as terminal:
                workdir = (trial_handler.task.workdir or "/app").rstrip("/")
                snapshot_session = terminal.create_session(
                    "snapshot-coverage-inspect",
                    as_configured_user=False,
                )
                snapshot_session.send_keys(
                    [_canonical_snapshot_command(workdir), "Enter"],
                    block=True,
                    max_timeout_sec=300,
                )
                snapshot_error = _inspection_command_diagnostic(
                    snapshot_session.capture_pane(capture_entire=True),
                    prefix=_CANONICAL_SNAPSHOT_EXIT_PREFIX,
                    stage="snapshot",
                )
                if snapshot_error is not None:
                    return None, snapshot_error

                apply_session = terminal.create_session(
                    "apply-coverage-inspect",
                    as_configured_user=False,
                )
                failure = self._apply_patch_entry(
                    terminal,
                    apply_session,
                    trial_handler,
                    patch,
                    patch_root,
                )
                if failure != FailureMode.NONE:
                    apply_pane = apply_session.capture_pane(capture_entire=True)
                    apply_reason = _inspection_apply_failure_reason(
                        apply_pane,
                        failure,
                    )
                    return (
                        None,
                        "gold patch inspection apply failed: "
                        f"{apply_reason or failure.value}",
                    )

                diff_session = terminal.create_session(
                    "diff-coverage-inspect",
                    as_configured_user=False,
                )
                diff_session.send_keys(
                    [
                        _canonical_diff_command(
                            workdir,
                            container_diff,
                        ),
                        "Enter",
                    ],
                    block=True,
                    max_timeout_sec=300,
                )
                diff_error = _inspection_command_diagnostic(
                    diff_session.capture_pane(capture_entire=True),
                    prefix=_CANONICAL_DIFF_EXIT_PREFIX,
                    stage="diff",
                )
                if diff_error is not None:
                    return None, diff_error
                try:
                    terminal.copy_from_container(container_diff, artifact_dir)
                except Exception as exc:
                    return (
                        None,
                        "gold patch inspection diff artifact copy failed: "
                        f"{exc}",
                    )

            if not diff_path.is_file():
                return None, "gold patch inspection did not produce a diff artifact"
            try:
                diff_text = diff_path.read_text()
            except Exception as exc:
                return None, f"gold patch inspection diff artifact read failed: {exc}"
            try:
                return parse_unified_diff(diff_text), None
            except Exception as exc:
                return None, f"gold patch inspection diff parse failed: {exc}"
        except Exception as exc:
            self._logger.error(
                "Gold patch inspection failed for %s: %s",
                trial_handler.task_id,
                exc,
                exc_info=True,
            )
            return None, f"gold patch inspection failed: {exc}"

    def _run_patch_container(
        self,
        trial_handler: TrialHandler,
        agent_tests_host_dir: Path,
        manifest: PatchSetManifestProtocol,
        patch_root: Path,
        patch: PatchEntry | SolutionV4 | None,
        coverage_targets: list[str] | None = None,
    ) -> tuple[
        UnitTestStatus | None,
        FailureMode,
        CoverageArtifactMap | None,
        CoverageArtifactMap | None,
        str | None,
    ]:
        """Run baseline or one solution in its own fresh container.

        When ``coverage_targets`` is supplied (baseline or the unique gold
        candidate), this executes generated tests and then the trusted task
        runner with separate ``generated``/``reference`` runtimes.  Other
        candidates never receive instrumentation.
        """

        state_id = patch.id if patch is not None else "baseline"
        coverage_selection_error: str | None = None
        if coverage_targets:
            _, coverage_selection_error = (
                self._coverage_backend_groups_for_targets(coverage_targets)
            )
        container_name = f"{trial_handler.client_container_name}-patch-{state_id}"
        session_root = trial_handler.trial_paths.sessions_path / state_id
        session_root.mkdir(parents=True, exist_ok=True)
        try:
            with spin_up_terminal(
                client_container_name=container_name,
                client_image_name=trial_handler.client_image_name,
                server_image_name=getattr(trial_handler, "server_image_name", None),
                docker_image_name_prefix=trial_handler.docker_image_name_prefix,
                docker_compose_path=trial_handler.task_paths.docker_compose_path,
                sessions_logs_path=session_root,
                agent_logs_path=trial_handler.trial_paths.agent_logging_dir,
                commands_path=trial_handler.trial_paths.commands_path,
                no_rebuild=True,
                cleanup=False,
                livestream=False,
                disable_recording=trial_handler.task.disable_asciinema,
            ) as terminal:
                setup = terminal.create_session(
                    f"setup-{state_id}",
                    as_configured_user=False,
                )
                self._setup_agent_test_dir(setup, trial_handler)
                extracted = agent_tests_host_dir / AGENT_TEST_SUBDIR
                if not extracted.exists():
                    extracted = agent_tests_host_dir
                terminal.copy_to_container(
                    paths=extracted,
                    container_dir=_agent_test_dir(trial_handler),
                )

                if patch is not None:
                    apply_session = terminal.create_session(
                        f"apply-{state_id}",
                        as_configured_user=False,
                    )
                    failure = self._apply_patch_entry(
                        terminal,
                        apply_session,
                        trial_handler,
                        patch,
                        patch_root,
                    )
                    if failure != FailureMode.NONE:
                        return None, failure, None, None, None

                coverage_env: dict[str, str] | None = None
                generated_backend_groups: tuple[CoverageBackendTargetGroup, ...] = ()
                generated_setup_error: str | None = None
                if coverage_selection_error:
                    generated_setup_error = (
                        "coverage backend selection failed: "
                        f"{coverage_selection_error}"
                    )
                    self._logger.warning(
                        "Coverage target selection unavailable for %s/%s: %s",
                        trial_handler.task_id,
                        state_id,
                        coverage_selection_error,
                    )
                if coverage_targets and coverage_selection_error is None:
                    (
                        coverage_env,
                        generated_backend_groups,
                        setup_error,
                    ) = self._setup_coverage_runtimes(
                        terminal,
                        setup,
                        trial_handler,
                        coverage_targets,
                        namespace="generated",
                    )
                    if setup_error is not None:
                        generated_setup_error = setup_error
                        self._logger.warning(
                            "Generated coverage unavailable for %s/%s: %s",
                            trial_handler.task_id,
                            state_id,
                            setup_error,
                        )

                test_session = terminal.create_session(
                    f"tests-{state_id}",
                    as_configured_user=False,
                )
                parsed, failure = self._run_agent_tests(
                    terminal,
                    test_session,
                    trial_handler,
                    extra_env=coverage_env,
                )
                # Capture the generated-suite status before starting trusted
                # tests.  Reference exit status is intentionally independent
                # of this value and never changes it.
                agent_status = parsed.get("agent_tests") if parsed else None
                generated_reports: CoverageArtifactMap | None = None
                generated_collection_error: str | None = None
                if coverage_env is not None:
                    if parsed is None or failure != FailureMode.NONE:
                        generated_collection_error = (
                            "generated runner did not complete with a parseable status"
                        )
                    else:
                        generated_reports, generated_collection_error = (
                            self._collect_coverage_artifacts(
                                terminal,
                                test_session,
                                trial_handler,
                                state_id,
                                namespace="generated",
                                groups=generated_backend_groups,
                            )
                        )
                    if generated_collection_error:
                        self._logger.warning(
                            "Generated coverage unavailable for %s/%s: %s",
                            trial_handler.task_id,
                            state_id,
                            generated_collection_error,
                        )

                reference_reports: CoverageArtifactMap | None = None
                reference_env: dict[str, str] | None = None
                reference_backend_groups: tuple[CoverageBackendTargetGroup, ...] = ()
                reference_setup_error: str | None = None
                reference_runner_error: str | None = None
                reference_collection_error: str | None = None
                reference_isolation_error: str | None = None
                if coverage_targets and coverage_selection_error is None:
                    reference_isolation_error = self._quarantine_agent_tests(
                        setup,
                        trial_handler,
                        state_id,
                    )
                    if reference_isolation_error:
                        self._logger.warning(
                            "Reference suite isolation unavailable for %s/%s: %s",
                            trial_handler.task_id,
                            state_id,
                            reference_isolation_error,
                        )
                    else:
                        (
                            reference_env,
                            reference_backend_groups,
                            reference_setup_error,
                        ) = self._setup_coverage_runtimes(
                            terminal,
                            setup,
                            trial_handler,
                            coverage_targets,
                            namespace="reference",
                        )
                        if reference_setup_error:
                            self._logger.warning(
                                "Reference coverage unavailable for %s/%s: %s",
                                trial_handler.task_id,
                                state_id,
                                reference_setup_error,
                            )
                    if not reference_isolation_error and reference_env is not None:
                        reference_session = terminal.create_session(
                            f"reference-tests-{state_id}",
                            as_configured_user=False,
                        )
                        reference_failure = self._run_reference_tests(
                            terminal,
                            reference_session,
                            trial_handler,
                            extra_env=reference_env,
                        )
                        if reference_failure != FailureMode.NONE:
                            reference_runner_error = (
                                "reference test runner "
                                f"{reference_failure.value}"
                            )
                        reference_reports, reference_collection_error = (
                            self._collect_coverage_artifacts(
                                terminal,
                                reference_session,
                                trial_handler,
                                state_id,
                                namespace="reference",
                                groups=reference_backend_groups,
                            )
                        )
                        if reference_collection_error:
                            self._logger.warning(
                                "Reference coverage unavailable for %s/%s: %s",
                                trial_handler.task_id,
                                state_id,
                                reference_collection_error,
                            )

                coverage_errors = [
                    message
                    for message in (
                        (
                            f"generated coverage setup failed: {generated_setup_error}"
                            if generated_setup_error
                            else None
                        ),
                        (
                            f"generated coverage collection failed: "
                            f"{generated_collection_error}"
                            if generated_collection_error
                            else None
                        ),
                        (
                            f"reference coverage setup failed: {reference_setup_error}"
                            if reference_setup_error
                            else None
                        ),
                        (
                            f"reference suite isolation failed: "
                            f"{reference_isolation_error}"
                            if reference_isolation_error
                            else None
                        ),
                        reference_runner_error,
                        (
                            f"reference coverage collection failed: "
                            f"{reference_collection_error}"
                            if reference_collection_error
                            else None
                        ),
                    )
                    if message
                ]
                return (
                    agent_status,
                    failure,
                    generated_reports,
                    reference_reports,
                    "; ".join(coverage_errors) or None,
                )
        except Exception as exc:
            self._logger.error(
                "Patch container %s failed for %s: %s",
                state_id,
                trial_handler.task_id,
                exc,
                exc_info=True,
            )
            return (
                None,
                self._map_runtime_exception_to_failure_mode(exc),
                None,
                None,
                None,
            )

    @staticmethod
    def _normalise_patch_run_reports(
        raw_reports: Path | str | Mapping[str, Path | str] | None,
    ) -> CoverageArtifactMap:
        """Normalize one generated/reference artifact payload into a map."""

        if raw_reports is None:
            return {}
        if isinstance(raw_reports, (str, Path)):
            return {"": Path(raw_reports)}
        if not isinstance(raw_reports, Mapping):
            raise ValueError("unexpected coverage artifact payload")

        normalized: CoverageArtifactMap = {}
        for language, report in raw_reports.items():
            if not isinstance(language, str):
                raise ValueError("coverage artifact language keys must be strings")
            if report is None:
                continue
            if not isinstance(report, (str, Path)):
                raise ValueError(
                    "coverage artifact values must be paths or path strings"
                )
            normalized[language] = Path(report)
        return dict(sorted(normalized.items()))

    @staticmethod
    def _normalise_patch_run_result(
        raw_result: tuple,
    ) -> tuple[
        UnitTestStatus | None,
        FailureMode,
        CoverageArtifactMap,
        CoverageArtifactMap,
        str | None,
    ]:
        """Accept legacy test doubles while enforcing the multi-report shape.

        Older harness-focused tests monkeypatch ``_run_patch_container`` with
        a three-tuple (status, failure, generated report).  Production code
        returns per-language generated/reference reports plus a coverage
        diagnostic.  The compatibility adapter keeps older tests useful
        without treating a generated report as trusted reference evidence.
        """

        if len(raw_result) == 5:
            status, failure, generated, reference, error = raw_result
            return (
                status,
                failure,
                TestBenchHarness._normalise_patch_run_reports(generated),
                TestBenchHarness._normalise_patch_run_reports(reference),
                error,
            )
        if len(raw_result) == 4:
            status, failure, generated, reference = raw_result
            return (
                status,
                failure,
                TestBenchHarness._normalise_patch_run_reports(generated),
                TestBenchHarness._normalise_patch_run_reports(reference),
                None,
            )
        if len(raw_result) == 3:
            status, failure, generated = raw_result
            return (
                status,
                failure,
                TestBenchHarness._normalise_patch_run_reports(generated),
                {},
                None,
            )
        raise ValueError(
            "_run_patch_container returned an unexpected result tuple length"
        )

    def _run_patch_set_trial(
        self, trial_handler: TrialHandler
    ) -> TestBenchTrialResults:
        manifest, patch_root = self._patch_manifests[trial_handler.task_id]
        schema_version = int(getattr(manifest, "schema_version", 3))
        patch_set_status = str(getattr(manifest, "status", "ready"))
        actual_valid = sum(
            solution.label is PatchLabel.ORACLE_VALID for solution in manifest.solutions
        )
        actual_invalid = sum(
            solution.label is PatchLabel.ORACLE_INVALID
            for solution in manifest.solutions
        )
        completion = getattr(manifest, "completion", None)
        target_valid = int(completion.target_valid) if completion is not None else 5
        target_invalid = int(completion.target_invalid) if completion is not None else 5
        results = TestBenchTrialResults(
            trial_name=trial_handler.trial_name,
            task_id=trial_handler.task_id,
            instruction=trial_handler.instruction,
            patch_set_id=manifest.patch_set_id,
            patch_set_schema_version=schema_version,
            patch_set_status=patch_set_status,
            patch_set_complete=patch_set_status == "ready",
            patch_set_target_valid=target_valid,
            patch_set_target_invalid=target_invalid,
            patch_set_actual_valid=actual_valid,
            patch_set_actual_invalid=actual_invalid,
        )
        results.trial_started_at = datetime.now(timezone.utc).isoformat()
        tests_dir = self._generate_tests_for_patch_evaluation(trial_handler, results)
        if tests_dir is None:
            results.is_resolved = False
            results.trial_ended_at = datetime.now(timezone.utc).isoformat()
            return results

        try:
            gold = next(patch for patch in manifest.solutions if patch.is_gold)
            entry_plan = self._plan_entry_coverage(trial_handler)
            coverage_enabled, gated_gold_patch_coverage = (
                self._gate_gold_patch_coverage_from_applicability(trial_handler)
            )
            changes: PatchChanges | None = None
            inspection_error: str | None = None
            baseline_targets: list[str] = []
            gold_targets: list[str] = []
            if coverage_enabled:
                changes, inspection_error = self._inspect_gold_patch(
                    trial_handler,
                    gold,
                    patch_root,
                )
                baseline_targets = (
                    self._coverage_targets_for_side(changes, side="baseline")
                    if changes is not None
                    else []
                )
                gold_targets = (
                    self._coverage_targets_for_side(changes, side="gold")
                    if changes is not None
                    else []
                )
                # Entry files are part of the generated gold/head report's
                # target universe.  Keep baseline targets unchanged: metadata
                # describes post-fix source spans and may refer to files
                # introduced only by the gold patch, which must not break
                # changed-line baseline collection.
            gold_targets = sorted(set(gold_targets) | set(entry_plan.target_paths))

            baseline_result = self._normalise_patch_run_result(
                self._run_patch_container(
                    trial_handler,
                    tests_dir,
                    manifest,
                    patch_root,
                    None,
                    coverage_targets=baseline_targets,
                )
            )
            (
                baseline_status,
                baseline_failure,
                baseline_generated,
                baseline_reference,
                baseline_coverage_error,
            ) = baseline_result
            results.pre_fix_results = (
                {"agent_tests": baseline_status}
                if baseline_status is not None
                else None
            )
            results.baseline_detected = baseline_status == UnitTestStatus.FAILED
            if baseline_failure != FailureMode.NONE:
                results.failure_mode = baseline_failure

            observed: dict[
                str,
                tuple[
                    UnitTestStatus | None,
                    FailureMode,
                    CoverageArtifactMap,
                    CoverageArtifactMap,
                    str | None,
                ],
            ] = {}
            with ThreadPoolExecutor(max_workers=self._patch_concurrency) as executor:
                futures = {
                    executor.submit(
                        self._run_patch_container,
                        trial_handler,
                        tests_dir,
                        manifest,
                        patch_root,
                        patch,
                        coverage_targets=(gold_targets if patch.is_gold else None),
                    ): patch
                    for patch in manifest.solutions
                }
                for future in as_completed(futures):
                    patch = futures[future]
                    try:
                        observed[patch.id] = self._normalise_patch_run_result(
                            future.result()
                        )
                    except Exception as exc:
                        self._logger.error(
                            "Patch %s failed for %s: %s",
                            patch.id,
                            trial_handler.task_id,
                            exc,
                            exc_info=True,
                        )
                        observed[patch.id] = (
                            None,
                            self._map_runtime_exception_to_failure_mode(exc),
                            {},
                            {},
                            None,
                        )

            (
                gold_status,
                _,
                gold_generated,
                gold_reference,
                gold_coverage_error,
            ) = observed[gold.id]
            results.post_fix_results = (
                {"agent_tests": gold_status} if gold_status is not None else None
            )
            if entry_plan.metric is not None:
                self._persist_entry_coverage_summary(
                    trial_handler,
                    results,
                    entry_plan.metric,
                )
            else:
                entry_generated_report = self._coverage_report_for_language(
                    gold_generated,
                    entry_plan.runtime_language,
                )
                self._calculate_and_persist_entry_coverage(
                    trial_handler,
                    results,
                    entry_generated_report,
                    collection_error=gold_coverage_error,
                )
            coverage_dir = trial_handler.trial_paths.coverage_path
            summary_path, artifact_path = self._gold_patch_coverage_artifact_path(
                trial_handler
            )
            if gated_gold_patch_coverage is not None:
                results.gold_patch_coverage = gated_gold_patch_coverage
            elif changes is None:
                results.gold_patch_coverage = GoldPatchCoverage(
                    status=CoverageStatus.FAILED,
                    reason=inspection_error or "gold patch inspection failed",
                    artifact_path=artifact_path,
                )
            elif baseline_coverage_error or gold_coverage_error:
                language, languages, unsupported_changed_files = (
                    self._coverage_language_metadata(changes)
                )
                results.gold_patch_coverage = GoldPatchCoverage(
                    status=CoverageStatus.FAILED,
                    language=language,
                    languages=languages,
                    unsupported_changed_files=unsupported_changed_files,
                    changed_files=changes.changed_files,
                    added_line_count=changes.added_line_count,
                    modified_line_count=changes.modified_line_count,
                    deleted_line_count=changes.deleted_line_count,
                    reason=(
                        "; ".join(
                            error
                            for error in (
                                baseline_coverage_error,
                                gold_coverage_error,
                            )
                            if error
                        )
                        or "one or more required coverage state artifacts are missing"
                    ),
                    artifact_path=artifact_path,
                )
            else:
                metric = calculate_patch_coverage(
                    changes,
                    generated_baseline_reports=self._coverage_report_paths(
                        baseline_generated
                    ),
                    generated_gold_reports=self._coverage_report_paths(gold_generated),
                    reference_baseline_reports=self._coverage_report_paths(
                        baseline_reference
                    ),
                    reference_gold_reports=self._coverage_report_paths(gold_reference),
                    artifact_path=artifact_path,
                    backend_registry=self._coverage_backend_registry(),
                )
                results.gold_patch_coverage = GoldPatchCoverage.model_validate(
                    metric.as_dict()
                )
            coverage_dir.mkdir(parents=True, exist_ok=True)
            summary_path.write_text(
                results.gold_patch_coverage.model_dump_json(indent=2)
            )

            if results.pre_fix_results and results.post_fix_results:
                results.verdicts = self._compute_verdicts(
                    results.pre_fix_results,
                    results.post_fix_results,
                )
                results.n_effective = sum(
                    verdict is TestCaseVerdict.EFFECTIVE
                    for verdict in results.verdicts.values()
                )
                results.n_false_positive = sum(
                    verdict is TestCaseVerdict.FALSE_POSITIVE
                    for verdict in results.verdicts.values()
                )
                results.n_false_negative = sum(
                    verdict is TestCaseVerdict.FALSE_NEGATIVE
                    for verdict in results.verdicts.values()
                )
                results.n_regression = sum(
                    verdict is TestCaseVerdict.REGRESSION
                    for verdict in results.verdicts.values()
                )
                results.n_error = sum(
                    verdict is TestCaseVerdict.ERROR
                    for verdict in results.verdicts.values()
                )
                results.f2p_rate = float(results.n_effective > 0)

            for patch in manifest.solutions:
                status, failure, _, _, _ = observed.get(
                    patch.id,
                    (None, FailureMode.UNKNOWN_AGENT_ERROR, {}, {}, None),
                )
                expected = (
                    UnitTestStatus.PASSED
                    if patch.label is PatchLabel.ORACLE_VALID
                    else UnitTestStatus.FAILED
                )
                results.patch_results.append(
                    PatchExecutionResult(
                        patch_id=patch.id,
                        label=patch.label,
                        expected_status=expected,
                        observed_status=status,
                        failure_mode=failure,
                        matched_expectation=(
                            failure == FailureMode.NONE and status == expected
                        ),
                    )
                )

            results.n_patch_errors = sum(
                item.failure_mode != FailureMode.NONE or item.observed_status is None
                for item in results.patch_results
            )
            valid_results = [
                item
                for item in results.patch_results
                if item.label is PatchLabel.ORACLE_VALID
            ]
            invalid_results = [
                item
                for item in results.patch_results
                if item.label is PatchLabel.ORACLE_INVALID
            ]
            results.valid_acceptance_rate = (
                sum(item.matched_expectation for item in valid_results)
                / len(valid_results)
                if valid_results
                else (None if schema_version == 4 else 0.0)
            )
            results.invalid_rejection_rate = (
                sum(item.matched_expectation for item in invalid_results)
                / len(invalid_results)
                if invalid_results
                else (None if schema_version == 4 else 0.0)
            )
            results.mutation_score = results.invalid_rejection_rate
            if schema_version == 4:
                results.balanced_patch_accuracy = (
                    (results.valid_acceptance_rate + results.invalid_rejection_rate) / 2
                    if results.valid_acceptance_rate is not None
                    and results.invalid_rejection_rate is not None
                    else None
                )
                results.is_resolved = bool(
                    results.baseline_detected
                    and results.valid_acceptance_rate == 1.0
                    and results.invalid_rejection_rate == 1.0
                    and results.n_patch_errors == 0
                )
            else:
                # Preserve the schema-v3 metric and verdict semantics.  In
                # particular, its historical empty-invalid behavior remains
                # 0.0 rather than inheriting v4's null missing-class metric.
                results.balanced_patch_accuracy = (
                    results.valid_acceptance_rate + results.invalid_rejection_rate
                ) / 2
                results.is_resolved = bool(
                    results.baseline_detected
                    and results.valid_acceptance_rate == 1.0
                    and results.invalid_rejection_rate == 1.0
                    and results.n_patch_errors == 0
                )
            if results.failure_mode == FailureMode.UNSET:
                results.failure_mode = FailureMode.NONE
        finally:
            shutil.rmtree(tests_dir, ignore_errors=True)
        results.trial_ended_at = datetime.now(timezone.utc).isoformat()
        return results

    def _run_legacy_trial(
        self,
        trial_handler: TrialHandler,
    ) -> TestBenchTrialResults:
        """Run the full TestBench evaluation loop for a single task.

        Uses two separate containers for isolation:
          Container A (agent): agent writes tests
          Container B (eval):  pre-fix → apply fix → post-fix → grade

        This prevents the agent from polluting the codebase before pre-fix
        evaluation (e.g. by generating its own solution alongside the tests).
        """
        self._logger.debug(f"[TestBench] Running task: {trial_handler.task_id}")

        results = TestBenchTrialResults(
            trial_name=trial_handler.trial_name,
            task_id=trial_handler.task_id,
            instruction=trial_handler.instruction,
        )
        results.trial_started_at = datetime.now(timezone.utc).isoformat()
        entry_plan = self._plan_entry_coverage(trial_handler)
        entry_target_paths = list(entry_plan.target_paths)

        # ── Phase 2: Agent generates tests (Container A) ────────────────
        with spin_up_terminal(
            client_container_name=trial_handler.client_container_name,
            client_image_name=trial_handler.client_image_name,
            server_image_name=getattr(trial_handler, "server_image_name", None),
            docker_image_name_prefix=trial_handler.docker_image_name_prefix,
            docker_compose_path=trial_handler.task_paths.docker_compose_path,
            sessions_logs_path=trial_handler.trial_paths.sessions_path,
            agent_logs_path=trial_handler.trial_paths.agent_logging_dir,
            commands_path=trial_handler.trial_paths.commands_path,
            no_rebuild=self._no_rebuild,
            cleanup=self._cleanup,
            livestream=self._livestream,
            disable_recording=trial_handler.task.disable_asciinema,
            agent_name=self._agent_class.name(),
            agent_kwargs=self._agent_kwargs,
        ) as agent_terminal:
            session = agent_terminal.create_session(
                "agent",
                is_active_stream=self._livestream,
                as_configured_user=True,
            )

            self._setup_agent_test_dir(session, trial_handler)

            task_agent = self._create_agent_for_task(trial_handler.task_id)
            results.agent_started_at = datetime.now(timezone.utc).isoformat()

            agent_result, agent_failure_mode = self._run_agent(
                session=session,
                agent=task_agent,
                trial_handler=trial_handler,
            )
            results.agent_ended_at = datetime.now(timezone.utc).isoformat()

            if agent_result is not None:
                results.total_input_tokens = agent_result.total_input_tokens
                results.total_output_tokens = agent_result.total_output_tokens

            if agent_failure_mode not in (
                FailureMode.NONE,
                FailureMode.AGENT_TIMEOUT,
            ):
                results.failure_mode = agent_failure_mode
                results.trial_ended_at = datetime.now(timezone.utc).isoformat()
                return results

            # Extract agent-written tests to host
            agent_tests_host_dir = Path(
                tempfile.mkdtemp(prefix="testbench-agent-tests-")
            )
            agent_test_dir_in_container = _agent_test_dir(trial_handler)
            try:
                agent_terminal.copy_from_container(
                    agent_test_dir_in_container, agent_tests_host_dir
                )
            except Exception:
                self._logger.warning(
                    f"Failed to extract {agent_test_dir_in_container} from agent "
                    f"container for {trial_handler.task_id}"
                )
                results.failure_mode = FailureMode.UNKNOWN_AGENT_ERROR
                results.trial_ended_at = datetime.now(timezone.utc).isoformat()
                return results

        # ── Phases 4–7: Evaluation (Container B — clean state) ──────────
        eval_container_name = f"{trial_handler.client_container_name}-eval"
        with spin_up_terminal(
            client_container_name=eval_container_name,
            client_image_name=trial_handler.client_image_name,
            server_image_name=getattr(trial_handler, "server_image_name", None),
            docker_image_name_prefix=trial_handler.docker_image_name_prefix,
            docker_compose_path=trial_handler.task_paths.docker_compose_path,
            sessions_logs_path=trial_handler.trial_paths.sessions_path,
            agent_logs_path=trial_handler.trial_paths.agent_logging_dir,
            commands_path=trial_handler.trial_paths.commands_path,
            no_rebuild=True,
            cleanup=self._cleanup,
            livestream=self._livestream,
            disable_recording=trial_handler.task.disable_asciinema,
        ) as eval_terminal:
            # Copy agent tests into the clean eval container
            setup_session = eval_terminal.create_session(
                "eval-setup",
                is_active_stream=False,
                as_configured_user=False,
            )
            self._setup_agent_test_dir(setup_session, trial_handler)

            extracted = agent_tests_host_dir / "agent-tests"
            if not extracted.exists():
                extracted = agent_tests_host_dir
            eval_terminal.copy_to_container(
                paths=extracted,
                container_dir=_agent_test_dir(trial_handler),
            )

            # ── Phase 4: Pre-fix test run ────────────────────────────────
            pre_session = eval_terminal.create_session(
                "pre-fix-tests",
                is_active_stream=self._livestream,
                as_configured_user=False,
            )

            pre_results, pre_failure = self._run_agent_tests(
                eval_terminal, pre_session, trial_handler
            )
            results.pre_fix_results = pre_results

            if pre_failure != FailureMode.NONE:
                self._logger.warning(
                    f"Pre-fix test issue for {trial_handler.task_id}: {pre_failure}"
                )
                if results.failure_mode == FailureMode.UNSET:
                    results.failure_mode = pre_failure

            # ── Phase 5: Apply the fix ───────────────────────────────────
            fix_session = eval_terminal.create_session(
                "apply-fix",
                is_active_stream=self._livestream,
                as_configured_user=False,
            )

            fix_failure = self._apply_solution(
                eval_terminal, fix_session, trial_handler
            )
            if fix_failure != FailureMode.NONE:
                if entry_plan.metric is not None:
                    self._persist_entry_coverage_summary(
                        trial_handler,
                        results,
                        entry_plan.metric,
                    )
                else:
                    self._calculate_and_persist_entry_coverage(
                        trial_handler,
                        results,
                        None,
                    )
                results.failure_mode = fix_failure
                results.trial_ended_at = datetime.now(timezone.utc).isoformat()
                return results

            # Instrument only the generated post-fix (gold/head) execution.
            # The pre-fix run remains semantically identical and does not need
            # entry targets, while a single post-fix run supplies both verdict
            # status and the Entry Coverage report.
            entry_coverage_env: dict[str, str] | None = None
            entry_setup_error: str | None = None
            if entry_target_paths:
                entry_coverage_env, entry_setup_error = (
                    self._setup_coverage_runtime(
                        eval_terminal,
                        setup_session,
                        trial_handler,
                        entry_target_paths,
                        namespace="generated",
                    )
                )

            # ── Phase 6: Post-fix test run ───────────────────────────────
            post_session = eval_terminal.create_session(
                "post-fix-tests",
                is_active_stream=self._livestream,
                as_configured_user=False,
            )

            post_results, post_failure = self._run_agent_tests(
                eval_terminal,
                post_session,
                trial_handler,
                extra_env=entry_coverage_env,
            )
            results.post_fix_results = post_results

            entry_generated_report: Path | None = None
            entry_collection_error: str | None = entry_setup_error
            if entry_coverage_env is not None:
                entry_generated_report, entry_collection_error = (
                    self._collect_coverage_artifact(
                        eval_terminal,
                        post_session,
                        trial_handler,
                        "gold",
                        namespace="generated",
                    )
                )
            if entry_plan.metric is not None:
                self._persist_entry_coverage_summary(
                    trial_handler,
                    results,
                    entry_plan.metric,
                )
            else:
                self._calculate_and_persist_entry_coverage(
                    trial_handler,
                    results,
                    entry_generated_report,
                    collection_error=entry_collection_error,
                )

            if (
                post_failure != FailureMode.NONE
                and results.failure_mode == FailureMode.UNSET
            ):
                results.failure_mode = post_failure

            # ── Phase 7: Grade ───────────────────────────────────────────
            if pre_results and post_results:
                verdicts = self._compute_verdicts(pre_results, post_results)
                results.verdicts = verdicts

                results.n_effective = sum(
                    1 for v in verdicts.values() if v == TestCaseVerdict.EFFECTIVE
                )
                results.n_false_positive = sum(
                    1 for v in verdicts.values() if v == TestCaseVerdict.FALSE_POSITIVE
                )
                results.n_false_negative = sum(
                    1 for v in verdicts.values() if v == TestCaseVerdict.FALSE_NEGATIVE
                )
                results.n_regression = sum(
                    1 for v in verdicts.values() if v == TestCaseVerdict.REGRESSION
                )
                results.n_error = sum(
                    1 for v in verdicts.values() if v == TestCaseVerdict.ERROR
                )

                n_total = len(verdicts)
                results.f2p_rate = results.n_effective / n_total if n_total > 0 else 0.0

                results.is_resolved = (
                    results.n_effective > 0 and results.n_false_positive == 0
                )

                line_cov, branch_cov = self._read_coverage_from_container(
                    post_session, trial_handler
                )
                results.line_coverage = line_cov
                results.branch_coverage = branch_cov
            else:
                results.is_resolved = False

        # Clean up temp dir
        import shutil

        shutil.rmtree(agent_tests_host_dir, ignore_errors=True)

        results.trial_ended_at = datetime.now(timezone.utc).isoformat()
        return results
