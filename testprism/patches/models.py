"""Versioned, secret-free manifest and evidence models for ready patch sets."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from testprism.patches.diversity import (
    behavior_matrix,
    required_invalid_signature_count,
)
from testprism.patches.oracle import (
    CandidateLabel,
    OracleDefinition,
    RunStatus,
    TestRunResult,
    build_oracle,
    classify_candidate,
    invalid_behavior_signature,
    is_suite_wide_oracle,
)

if TYPE_CHECKING:
    from testprism.patches.models_v4 import PatchSetManifestV4

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"
_SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{12,}"),
    re.compile(r"\b(?:sk|ghp|github_pat)_[A-Za-z0-9_-]{12,}"),
    re.compile(r"(?i)\b(?:api[_-]?key|access[_-]?token|password|secret)\s*[:=]"),
)


class _ManifestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PatchLabel(StrEnum):
    """Oracle-authoritative label for a published patch."""

    ORACLE_VALID = "oracle_valid"
    ORACLE_INVALID = "oracle_invalid"


class GeneratorProvenance(_ManifestModel):
    """Secret-free identity and accounting data for one Agent invocation."""

    profile_id: str = Field(min_length=1)
    agent: str = Field(min_length=1)
    model: str = Field(min_length=1)
    version: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    duration_seconds: float | None = Field(default=None, ge=0)


class FeedbackPeerReference(_ManifestModel):
    """One accepted peer identity retained without serializing its script body."""

    id: str = Field(pattern=_ID_PATTERN)
    sha256: str = Field(pattern=_SHA256_PATTERN)


class StrategyAssignmentProvenance(_ManifestModel):
    """Secret-free audit record for one non-gold strategy-aware dispatch."""

    schema_version: Literal[1] = 1
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    strategy_id: str = Field(pattern=_ID_PATTERN)
    strategy_category: str = Field(min_length=1, max_length=120)
    strategy_reuse_round: int = Field(ge=0)
    dispatch_index: int = Field(ge=0)
    feedback_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    feedback_peers: tuple[FeedbackPeerReference, ...]

    @model_validator(mode="after")
    def _validate_peer_references(self) -> StrategyAssignmentProvenance:
        peer_ids = tuple(peer.id for peer in self.feedback_peers)
        if len(peer_ids) != len(set(peer_ids)):
            raise ValueError("strategy feedback peer IDs must be unique")
        return self


class StrategyPlannerEvidence(_ManifestModel):
    """Planner identity from the verified strategy JSON, never a resolved profile."""

    profile_id: str = Field(min_length=1)
    agent: str = Field(min_length=1)
    model: str = Field(min_length=1)


class StrategyPlanEvidence(_ManifestModel):
    """Ready-output reference to canonical strategy plan bytes and provenance."""

    schema_version: Literal[1] = 1
    path: Literal["evidence/strategies.json"] = "evidence/strategies.json"
    sha256: str = Field(pattern=_SHA256_PATTERN)
    planner: StrategyPlannerEvidence


class PatchEntry(_ManifestModel):
    """One immutable executable artifact in a ready solution set."""

    id: str = Field(pattern=_ID_PATTERN)
    label: PatchLabel
    is_gold: bool = False
    path: str
    sha256: str = Field(pattern=_SHA256_PATTERN)
    artifact_type: Literal["solution_sh"] = "solution_sh"
    generator: GeneratorProvenance
    strategy_assignment: StrategyAssignmentProvenance | None = None

    @model_validator(mode="after")
    def _validate_relative_paths(self) -> PatchEntry:
        _validate_relative_path(self.path, "solution path")
        if PurePosixPath(self.path).name != "solution.sh":
            raise ValueError("every solution artifact must be named solution.sh")
        return self


class PatchOracleEvidence(_ManifestModel):
    """Per-test oracle output and, for invalid patches, its F2P signature."""

    result: TestRunResult
    invalid_signature: str | None = Field(default=None, pattern=_SHA256_PATTERN)


class OracleEvidence(_ManifestModel):
    """Baseline/gold oracle plus each published patch's complete test result."""

    baseline: TestRunResult
    gold: TestRunResult
    f2p_tests: tuple[str, ...]
    p2p_tests: tuple[str, ...]
    patch_results: dict[str, PatchOracleEvidence]
    canonical_gold_provenance: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_definition(self) -> OracleEvidence:
        definition = build_oracle(self.baseline, self.gold)
        if self.f2p_tests != definition.f2p:
            raise ValueError("F2P tests do not match baseline/gold evidence")
        if self.p2p_tests != definition.p2p:
            raise ValueError("P2P tests do not match baseline/gold evidence")
        return self

    def definition(self) -> OracleDefinition:
        """Rebuild the trusted oracle rather than trusting duplicated sets."""

        return build_oracle(self.baseline, self.gold)


class DiversityEvidence(_ManifestModel):
    """Deterministic behavioral diversity evidence saved for revalidation."""

    configured_invalid_signatures: int = Field(ge=0)
    behavior_matrix: dict[str, dict[str, float]]


class ReviewerPairEvidence(_ManifestModel):
    """Independent semantic judgment for one unordered same-label pair."""

    candidate_id: str = Field(pattern=_ID_PATTERN)
    peer_id: str = Field(pattern=_ID_PATTERN)
    candidate_sha256: str = Field(pattern=_SHA256_PATTERN)
    peer_sha256: str = Field(pattern=_SHA256_PATTERN)
    strategy_family: str = Field(min_length=1)
    heterogeneous: bool
    similarities: tuple[str, ...]
    material_differences: tuple[str, ...] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _validate_distinct_pair(self) -> ReviewerPairEvidence:
        if self.candidate_id == self.peer_id:
            raise ValueError("reviewer pair must contain two distinct patches")
        return self


class ReviewerEvidence(_ManifestModel):
    """Reviewer provenance and final all-pairs certification."""

    reviewer: GeneratorProvenance
    min_confidence: float = Field(ge=0, le=1)
    pairs: tuple[ReviewerPairEvidence, ...]
    final_conclusion: bool
    reason: str = Field(min_length=1)


class PatchSetManifest(_ManifestModel):
    """Version 3 contract for a published ready or partial solution set."""

    schema_version: Literal[3] = 3
    patch_set_id: str = Field(pattern=_ID_PATTERN)
    task_key: str = Field(min_length=3)
    source: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    status: Literal["ready", "partial"] = "ready"
    input_task_path: str
    baseline_tree_hash: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    original_instruction_hash: str = Field(pattern=_SHA256_PATTERN)
    effective_instruction_hash: str = Field(pattern=_SHA256_PATTERN)
    config_hash: str = Field(pattern=_SHA256_PATTERN)
    generated_at: datetime
    tool_version: str = Field(min_length=1)
    oracle: OracleEvidence
    solutions: tuple[PatchEntry, ...]
    diversity: DiversityEvidence
    reviewer: ReviewerEvidence
    strategy: StrategyPlanEvidence | None = None

    @model_validator(mode="after")
    def _validate_ready_contract(self) -> PatchSetManifest:
        _validate_relative_path(self.input_task_path, "input task path")
        try:
            _validate_relative_path(self.task_key, "task_key")
        except ValueError:
            raise ValueError("task_key must be a safe relative POSIX path") from None
        task_key_parts = PurePosixPath(self.task_key).parts
        if len(task_key_parts) < 2:
            raise ValueError("task_key must contain at least source and task ID")
        if task_key_parts[0] != self.source:
            raise ValueError("first task_key segment must equal source")
        if task_key_parts[-1] != self.task_id:
            raise ValueError("last task_key segment must equal task_id")

        valid = [p for p in self.solutions if p.label is PatchLabel.ORACLE_VALID]
        invalid = [p for p in self.solutions if p.label is PatchLabel.ORACLE_INVALID]
        if not valid:
            raise ValueError("manifest requires at least one oracle-valid solution")
        if self.status == "ready":
            if len(valid) != 5 or len(invalid) != 5:
                raise ValueError(
                    "ready manifest requires exactly 5 valid and 5 invalid solutions"
                )
        else:
            if len(valid) > 5 or len(invalid) > 5:
                raise ValueError(
                    "partial manifest cannot exceed ready-pool capacity of 5+5"
                )
        gold = [patch for patch in valid if patch.is_gold]
        if len(gold) != 1 or any(patch.is_gold for patch in invalid):
            raise ValueError("manifest requires exactly one gold oracle-valid solution")

        if self.strategy is not None:
            for patch in self.solutions:
                if patch.is_gold and patch.strategy_assignment is not None:
                    raise ValueError("gold patch must not have a strategy assignment")
                if not patch.is_gold:
                    assignment = patch.strategy_assignment
                    if assignment is None:
                        raise ValueError(
                            "strategy manifest requires every generated patch "
                            "assignment"
                        )
                    if assignment.plan_sha256 != self.strategy.sha256:
                        raise ValueError(
                            "strategy assignment plan hash does not match manifest"
                        )

        ids = [patch.id for patch in self.solutions]
        if len(ids) != len(set(ids)):
            raise ValueError("solution IDs must be unique")
        paths = [patch.path for patch in self.solutions]
        if len(paths) != len(set(paths)):
            raise ValueError("solution paths must be unique")
        hashes = [patch.sha256 for patch in self.solutions]
        if len(hashes) != len(set(hashes)):
            raise ValueError("solution SHA-256 values must be unique")
        if set(self.oracle.patch_results) != set(ids):
            raise ValueError("oracle evidence must cover every solution ID exactly")

        invalid_signatures: list[str] = []
        for patch in self.solutions:
            signature = self.oracle.patch_results[patch.id].invalid_signature
            if patch.label is PatchLabel.ORACLE_VALID and signature is not None:
                raise ValueError("valid patches cannot have an invalid signature")
            if patch.label is PatchLabel.ORACLE_INVALID:
                if signature is None:
                    raise ValueError("invalid patches require a behavior signature")
                invalid_signatures.append(signature)
        required_signatures = required_invalid_signature_count(
            self.diversity.configured_invalid_signatures,
            len(self.oracle.f2p_tests),
        )
        if (
            self.status == "ready"
            and len(set(invalid_signatures)) < required_signatures
        ):
            raise ValueError("invalid behavior signature quota is not met")

        self._validate_reviewer_pairs()
        _reject_serialized_secrets(self.model_dump(mode="json"))
        return self

    def _validate_reviewer_pairs(self) -> None:
        by_id = {patch.id: patch for patch in self.solutions}
        expected: set[frozenset[str]] = set()
        for label in PatchLabel:
            label_ids = sorted(
                patch.id for patch in self.solutions if patch.label is label
            )
            expected.update(
                frozenset((left, right))
                for index, left in enumerate(label_ids)
                for right in label_ids[index + 1 :]
            )

        actual: set[frozenset[str]] = set()
        for pair in self.reviewer.pairs:
            try:
                candidate = by_id[pair.candidate_id]
                peer = by_id[pair.peer_id]
            except KeyError as exc:
                raise ValueError(
                    "reviewer evidence references an unknown patch"
                ) from exc
            if candidate.label is not peer.label:
                raise ValueError(
                    "reviewer evidence may only compare same-label patches"
                )
            key = frozenset((candidate.id, peer.id))
            if key in actual:
                raise ValueError("reviewer evidence contains a duplicate peer pair")
            actual.add(key)
            if pair.candidate_sha256 != candidate.sha256:
                raise ValueError("reviewer candidate hash mismatch")
            if pair.peer_sha256 != peer.sha256:
                raise ValueError("reviewer peer hash mismatch")
            if not pair.heterogeneous:
                raise ValueError("ready reviewer pair must be heterogeneous")
            if pair.confidence < self.reviewer.min_confidence:
                raise ValueError("reviewer pair confidence is below threshold")

        if actual != expected:
            raise ValueError("reviewer evidence must cover every same-label peer pair")
        if not self.reviewer.final_conclusion:
            raise ValueError(
                "manifest reviewer evidence must have a positive conclusion"
            )

    def validate_files(self, root: str | Path) -> None:
        """Recompute artifact, oracle, behavior, and reviewer-linked evidence."""

        root_path = Path(root).resolve(strict=True)
        if not root_path.is_dir():
            raise ValueError(f"patch-set root is not a directory: {root_path}")

        for patch in self.solutions:
            artifact = _resolve_artifact(root_path, patch.path)
            actual_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
            if actual_hash != patch.sha256:
                raise ValueError(f"SHA-256 mismatch for solution {patch.id}")
            try:
                script = artifact.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(f"solution {patch.id} is not UTF-8 text") from exc
            if not script.strip() or "\x00" in script:
                raise ValueError(f"solution {patch.id} is not an executable script")

        if self.strategy is not None:
            strategy_path = _resolve_artifact(root_path, self.strategy.path)
            strategy_bytes = strategy_path.read_bytes()
            if hashlib.sha256(strategy_bytes).hexdigest() != self.strategy.sha256:
                raise ValueError("strategy evidence SHA-256 mismatch")
            try:
                strategy_document = json.loads(strategy_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("strategy evidence is not valid UTF-8 JSON") from exc
            if not isinstance(strategy_document, dict):
                raise ValueError("strategy evidence JSON must be an object")
            planner = strategy_document.get("strategy_generator")
            if (
                strategy_document.get("schema_version") != 2
                or strategy_document.get("task_key") != self.task_key
                or not isinstance(planner, dict)
                or planner != self.strategy.planner.model_dump(mode="json")
            ):
                raise ValueError("strategy evidence does not match manifest provenance")

        definition = self.oracle.definition()
        invalid_behaviors: dict[str, dict[str, object]] = {}
        signatures: set[str] = set()
        for patch in self.solutions:
            evidence = self.oracle.patch_results[patch.id]
            result = evidence.result
            if result.run_status is not RunStatus.COMPLETED:
                raise ValueError(f"oracle run is not completed for solution {patch.id}")
            if self.status == "ready" and patch.is_gold and result != self.oracle.gold:
                raise ValueError("gold patch result does not match canonical gold run")
            assessment = classify_candidate(
                definition, result, require_f2p_progress=False
            )
            expected_label = (
                CandidateLabel.ORACLE_VALID
                if patch.label is PatchLabel.ORACLE_VALID
                else CandidateLabel.ORACLE_INVALID
            )
            if assessment.label is not expected_label:
                raise ValueError(f"oracle label mismatch for solution {patch.id}")
            if patch.label is PatchLabel.ORACLE_VALID:
                if evidence.invalid_signature is not None:
                    raise ValueError("valid patches cannot have an invalid signature")
                continue

            signature = invalid_behavior_signature(definition, result)
            if evidence.invalid_signature != signature:
                raise ValueError(
                    f"invalid behavior signature mismatch for patch {patch.id}"
                )
            signatures.add(signature)
            invalid_behaviors[patch.id] = dict(result.tests)

        required = required_invalid_signature_count(
            self.diversity.configured_invalid_signatures,
            len(definition.f2p),
        )
        if self.status == "ready" and len(signatures) < required:
            raise ValueError(
                f"invalid signature quota not met: {len(signatures)} < {required}"
            )
        if self.status == "ready" or not is_suite_wide_oracle(definition):
            expected_behavior = behavior_matrix(invalid_behaviors, definition.f2p)
            if self.diversity.behavior_matrix != expected_behavior:
                raise ValueError("behavior matrix mismatch")

        # Re-run linkage checks after hashes and artifacts have been revalidated.
        self._validate_reviewer_pairs()


def _validate_relative_path(value: str, label: str) -> None:
    if not value or "\\" in value:
        raise ValueError(f"{label} must stay within patch-set root")
    path = PurePosixPath(value)
    if path.is_absolute() or path == PurePosixPath(".") or ".." in path.parts:
        raise ValueError(f"{label} must stay within patch-set root")


def _resolve_artifact(root: Path, relative: str) -> Path:
    try:
        resolved = (root / relative).resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, ValueError) as exc:
        raise ValueError(
            f"solution path escapes solution-set root: {relative}"
        ) from exc
    if not resolved.is_file():
        raise ValueError(f"solution artifact is not a file: {relative}")
    return resolved


def _reject_serialized_secrets(value: Any, path: str = "manifest") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized in {
                "apikey",
                "accesstoken",
                "authtoken",
                "password",
                "secret",
                "secretkey",
            }:
                raise ValueError(f"secret-like field is forbidden at {path}.{key}")
            _reject_serialized_secrets(nested, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _reject_serialized_secrets(nested, f"{path}[{index}]")
    elif isinstance(value, str) and any(
        pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS
    ):
        raise ValueError(f"secret-like value is forbidden at {path}")


def load_patch_set_manifest(
    patch_set_root: str | Path,
    task_id: str,
    task_key: str | None = None,
) -> tuple[PatchSetManifest | PatchSetManifestV4, Path]:
    """Load and validate a versioned manifest for ``task_id``.

    Published sidecars may be rooted either at ``<task-id>/`` or at a source
    tree such as ``from-swe-bench/<task-id>/``. Resolve the manifest by its
    validated ``task_id`` and optional dataset-relative ``task_key`` rather
    than assuming a particular source layout. When callers already scope
    ``patch_set_root`` to a source subtree, accept the task key relative to
    that subtree as long as the manifest's canonical key still resolves to the
    same leaf. Validate every referenced ``solution.sh`` hash before returning.
    """

    root = Path(patch_set_root).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"patch-set root is not a directory: {root}")

    def _task_key_matches_request(manifest_path: Path, raw_task_key: object) -> bool:
        if task_key is None:
            return True
        if raw_task_key == task_key:
            return True
        if not isinstance(raw_task_key, str):
            return False
        relative_key = manifest_path.parent.relative_to(root).as_posix()
        if relative_key != task_key:
            return False
        return raw_task_key.endswith(f"/{relative_key}")

    def _load_one(
        manifest_path: Path,
    ) -> tuple[PatchSetManifest | PatchSetManifestV4, Path]:
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"invalid patch-set manifest {manifest_path}: {exc}"
            ) from exc
        if not isinstance(raw, dict) or raw.get("task_id") != task_id:
            raise ValueError(
                f"invalid patch-set manifest {manifest_path}: task_id "
                f"{raw.get('task_id')!r} does not match expected {task_id!r}"
            )
        if not _task_key_matches_request(manifest_path, raw.get("task_key")):
            raise ValueError(
                f"invalid patch-set manifest {manifest_path}: task_key "
                f"{raw.get('task_key')!r} does not match expected {task_key!r}"
            )
        schema_version = raw.get("schema_version")
        if schema_version == 3:
            manifest_type = PatchSetManifest
        elif schema_version == 4:
            # Keep the v4 import lazy.  This preserves v3 import behavior and
            # avoids making the historical model depend on its new module at
            # import time.
            from testprism.patches.models_v4 import PatchSetManifestV4

            manifest_type = PatchSetManifestV4
        else:
            raise ValueError(
                f"unsupported patch-set schema_version: {schema_version!r}"
            )
        try:
            manifest = manifest_type.model_validate(raw)
            manifest.validate_files(manifest_path.parent)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"invalid patch-set manifest {manifest_path}: {exc}"
            ) from exc
        return manifest, manifest_path.parent

    if task_key is not None:
        _validate_relative_path(task_key, "task_key")
        direct = root.joinpath(*PurePosixPath(task_key).parts) / "manifest.json"
        if direct.is_file():
            return _load_one(direct)

    candidate_paths: list[Path] = []
    direct = root / task_id / "manifest.json"
    if direct.is_file():
        candidate_paths.append(direct)
    candidate_paths.extend(
        path for path in root.rglob("manifest.json") if path not in candidate_paths
    )

    matches: list[tuple[PatchSetManifest | PatchSetManifestV4, Path]] = []
    for manifest_path in candidate_paths:
        try:
            loaded = _load_one(manifest_path)
        except ValueError:
            if manifest_path == direct:
                raise
            continue
        matches.append(loaded)

    if not matches:
        raise ValueError(
            f"patch-set manifest for task {task_id!r} not found under {root}"
        )
    if len(matches) > 1:
        locations = ", ".join(str(path) for _, path in matches)
        raise ValueError(
            f"multiple patch-set manifests found for {task_id!r}: {locations}"
        )
    return matches[0]


@runtime_checkable
class PatchSetManifestProtocol(Protocol):
    """Read-only shape shared by v3 and v4 consumers.

    The protocol intentionally contains only fields used to locate and execute
    published solution artifacts.  Version-specific validation remains owned by
    each concrete manifest class; this is not a coercion or migration layer.
    """

    schema_version: int
    patch_set_id: str
    task_key: str
    source: str
    task_id: str
    status: str
    input_task_path: str
    baseline_tree_hash: str
    oracle: Any
    solutions: tuple[Any, ...]

    def validate_files(self, root: str | Path) -> None: ...


def __getattr__(name: str) -> Any:
    """Lazily expose v4 names without creating a v3↔v4 import cycle."""

    if name in {
        "PatchSetManifestV4",
        "CompletionV4",
        "V4StopReason",
        "EffectStatus",
        "StaticStatus",
        "PairTier",
        "PairDecision",
        "MutationOracleOutcome",
        "MutationSelectionOutcome",
        "SolutionV4",
        "PatchEntryV4",
        "PatchOracleEvidenceV4",
        "OracleEvidenceV4",
        "DiversityEvidenceV4",
        "PairEvidenceV4",
        "MutationRecordV4",
        "MutationSiteV4",
        "EffectDescriptorV4",
        "ArtifactDiversityV4",
        "StopReason",
        "GoldOrigin",
        "AgentOrigin",
        "MutationOrigin",
        "Origin",
        "OracleDescriptorV4",
        "StaticPolicyV4",
        "EffectFileV4",
        "EffectDescriptorV4",
        "SourceUnitV4",
        "Completion",
        "PatchEntryV4",
        "PatchOracleEvidence",
        "OracleEvidence",
        "DiversityEvidence",
        "PairEvidence",
        "MutationRecord",
        "MutationSite",
        "EffectDescriptor",
        "ArtifactDiversity",
    }:
        from testprism.patches import models_v4

        return getattr(models_v4, name)
    raise AttributeError(name)
