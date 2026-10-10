"""Strict, self-contained validators for the schema-v4 patch-set contract.

The v4 consumer is deliberately kept separate from the historical v3 models.
The producer is not imported here: every check is based on the manifest,
embedded oracle observations, and embedded diversity features.  This keeps a
published manifest portable and makes the loader a useful integrity boundary.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

from testprism.patches.diversity import behavior_matrix
from testprism.patches.models import (
    GeneratorProvenance,
    PatchLabel,
    _reject_serialized_secrets,
)
from testprism.patches.oracle import (
    CandidateLabel,
    RunStatus,
    TestRunResult,
    build_oracle,
    classify_candidate,
    invalid_behavior_signature,
    is_suite_wide_oracle,
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_TREE_HASH_PATTERN = r"^[0-9a-f]{40,64}$"
_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"


class _V4Model(BaseModel):
    """Base for all v4 JSON objects.

    ``extra=forbid`` is important here: a manifest is a signed-style evidence
    document, so silently ignoring a producer typo would make the evidence
    ambiguous.  Models are frozen after parsing to prevent accidental mutation
    of an object which has already been validated.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class V4StopReason(StrEnum):
    QUOTA_REACHED = "quota_reached"
    MAX_AGENT_ATTEMPTS = "max_agent_attempts"
    MAX_AGENT_CALLS = "max_agent_calls"
    ORACLE_BUDGET = "oracle_budget"
    PROFILE_EXHAUSTED = "profile_exhausted"
    DEADLINE = "deadline"
    CANCELLED = "cancelled"
    FATAL_ERROR = "fatal_error"


class EffectStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class StaticStatus(StrEnum):
    NOT_COMPUTED = "not_computed"
    COMPLETE = "complete"
    UNSUPPORTED = "unsupported"
    PARSE_ERROR = "parse_error"
    NON_SOURCE_EFFECT = "non_source_effect"


class PairTier(StrEnum):
    ORACLE_SIGNATURE = "oracle_signature"
    STATIC = "static"


class PairDecision(StrEnum):
    DIFFERENT = "different"


class MutationOracleOutcome(StrEnum):
    NOT_RUN = "not_run"
    ORACLE_VALID = "oracle_valid"
    ORACLE_INVALID = "oracle_invalid"
    UNCLASSIFIABLE = "unclassifiable"


class MutationSelectionOutcome(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    DIVERSITY_REJECTED = "diversity_rejected"
    QUOTA_FULL = "quota_full"
    SURVIVED = "survived"
    NO_SITE = "no_site"
    UNSUPPORTED = "unsupported"
    UNCLASSIFIABLE = "unclassifiable"
    CANCELLED = "cancelled"


class CompletionV4(_V4Model):
    """Requested and actually published counts for a run."""

    target_valid: int = Field(ge=1)
    target_invalid: int = Field(ge=0)
    actual_valid: int = Field(ge=1)
    actual_invalid: int = Field(ge=0)
    stop_reason: V4StopReason
    attempts_started: int = Field(ge=0)
    max_agent_attempts: int = Field(ge=0)


class GoldOrigin(_V4Model):
    kind: Literal["gold"] = "gold"
    canonical_source: str = Field(min_length=1)
    source_sha256: str = Field(pattern=_SHA256_PATTERN)


class AgentOrigin(_V4Model):
    kind: Literal["agent"] = "agent"
    lane: Literal["valid", "invalid"]
    attempt_id: str = Field(min_length=1)
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    oracle_access: Literal["source_visible", "hidden"]


class MutationOrigin(_V4Model):
    kind: Literal["mutation"] = "mutation"
    job_id: str = Field(pattern=_ID_PATTERN)
    parent_solution_id: str = Field(pattern=_ID_PATTERN)
    parent_sha256: str = Field(pattern=_SHA256_PATTERN)
    operator: str = Field(min_length=1)
    policy_version: str = Field(min_length=1)


SolutionOrigin = Annotated[
    Union[GoldOrigin, AgentOrigin, MutationOrigin],
    Field(discriminator="kind"),
]
Origin = SolutionOrigin


class SolutionV4(_V4Model):
    """One immutable executable solution and its source provenance."""

    id: str = Field(pattern=_ID_PATTERN)
    label: PatchLabel
    is_gold: bool = False
    path: str
    sha256: str = Field(pattern=_SHA256_PATTERN)
    artifact_type: Literal["solution_sh"] = "solution_sh"
    generator: GeneratorProvenance
    origin: SolutionOrigin

    @model_validator(mode="after")
    def _validate_path(self) -> SolutionV4:
        _validate_relative_path(self.path, "solution path")
        if PurePosixPath(self.path).name != "solution.sh":
            raise ValueError("every solution artifact must be named solution.sh")
        return self


class PatchOracleEvidenceV4(_V4Model):
    result: TestRunResult
    invalid_signature: str | None = Field(default=None, pattern=_SHA256_PATTERN)


class OracleEvidenceV4(_V4Model):
    """Baseline/gold evidence plus complete observations for every solution."""

    baseline: TestRunResult
    gold: TestRunResult
    f2p_tests: tuple[str, ...]
    p2p_tests: tuple[str, ...]
    patch_results: dict[str, PatchOracleEvidenceV4]
    canonical_gold_provenance: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_definition(self) -> OracleEvidenceV4:
        definition = build_oracle(self.baseline, self.gold)
        if self.f2p_tests != definition.f2p:
            raise ValueError("F2P tests do not match baseline/gold evidence")
        if self.p2p_tests != definition.p2p:
            raise ValueError("P2P tests do not match baseline/gold evidence")
        return self

    def definition(self):
        """Rebuild the oracle from baseline and Gold, never from duplicates."""

        return build_oracle(self.baseline, self.gold)


class OracleDescriptorV4(_V4Model):
    baseline_tree_hash: str = Field(pattern=_TREE_HASH_PATTERN)
    gold_sha256: str = Field(pattern=_SHA256_PATTERN)
    tests_tree_sha256: str = Field(pattern=_SHA256_PATTERN)
    runner_sha256: str = Field(pattern=_SHA256_PATTERN)
    parser_id: str = Field(min_length=1)
    parser_version: str = Field(min_length=1)
    image_digest: str = Field(min_length=1)


class StaticPolicyV4(_V4Model):
    algorithm: Literal["static-v1"] = "static-v1"
    min_distance: float = Field(ge=0, le=1)
    feature_version: Literal["subtree-multiset-v1"] = "subtree-multiset-v1"
    grammar_versions: dict[str, str]
    normalization_version: Literal["syntax-normalize-v2"] = "syntax-normalize-v2"

    @model_validator(mode="after")
    def _validate_grammar_versions(self) -> StaticPolicyV4:
        if not math.isclose(self.min_distance, 0.15, abs_tol=1e-12):
            raise ValueError("static-v1 min_distance must be 0.15")
        if any(not key or not value for key, value in self.grammar_versions.items()):
            raise ValueError("grammar_versions keys and values must be non-empty")
        return self


class EffectFileV4(_V4Model):
    path: str = Field(min_length=1)
    before_type: str | None = None
    after_type: str | None = None
    before_mode: int | str | None = None
    after_mode: int | str | None = None
    before_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    after_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _validate_file(self) -> EffectFileV4:
        _validate_relative_path(self.path, "effect file path")
        if self.before_sha256 is None and (
            self.before_type is not None or self.before_mode is not None
        ):
            raise ValueError("before file metadata requires before_sha256")
        if self.after_sha256 is None and (
            self.after_type is not None or self.after_mode is not None
        ):
            raise ValueError("after file metadata requires after_sha256")
        if (
            self.before_type is None
            and self.after_type is None
            and self.before_sha256 is None
            and self.after_sha256 is None
        ):
            raise ValueError("effect file must describe a before or after state")
        return self


class EffectDescriptorV4(_V4Model):
    version: Literal[1] = 1
    baseline_tree_hash: str = Field(pattern=_TREE_HASH_PATTERN)
    scope_id: str = Field(min_length=1)
    files: tuple[EffectFileV4, ...]
    non_file_effects: tuple[Any, ...]

    @model_validator(mode="after")
    def _validate_files(self) -> EffectDescriptorV4:
        if self.non_file_effects:
            raise ValueError("v4 effect descriptors cannot contain non-file effects")
        paths = tuple(item.path for item in self.files)
        if paths != tuple(sorted(paths)):
            raise ValueError("effect descriptor files must be sorted by path")
        if len(paths) != len(set(paths)):
            raise ValueError("effect descriptor file paths must be unique")
        return self


class SourceUnitV4(_V4Model):
    path: str = Field(min_length=1)
    language: str = Field(min_length=1)
    before_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    after_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _validate_unit(self) -> SourceUnitV4:
        _validate_relative_path(self.path, "source unit path")
        if self.before_sha256 is None and self.after_sha256 is None:
            raise ValueError("source unit must have a before or after hash")
        return self


class ArtifactDiversityV4(_V4Model):
    artifact_sha256: str = Field(pattern=_SHA256_PATTERN)
    effect_status: EffectStatus
    effect_scope_id: str = Field(min_length=1)
    effect_descriptor: EffectDescriptorV4 | None = None
    effect_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    static_status: StaticStatus
    features: dict[str, int]
    source_units: tuple[SourceUnitV4, ...]

    @model_validator(mode="after")
    def _validate_artifact(self) -> ArtifactDiversityV4:
        if self.effect_status is EffectStatus.COMPLETE:
            if self.effect_descriptor is None or self.effect_sha256 is None:
                raise ValueError(
                    "complete effects require an effect descriptor and hash"
                )
        elif self.effect_descriptor is not None or self.effect_sha256 is not None:
            raise ValueError("partial/unknown effects must not carry effect identity")

        if self.static_status is not StaticStatus.COMPLETE and self.features:
            raise ValueError("non-complete static evidence must have empty features")
        for feature_hash, count in self.features.items():
            if not re.fullmatch(_SHA256_PATTERN, feature_hash):
                raise ValueError("static feature keys must be SHA-256 hashes")
            if count < 1:
                raise ValueError("static feature counts must be positive")
        return self


class PairEvidenceV4(_V4Model):
    left_id: str = Field(pattern=_ID_PATTERN)
    right_id: str = Field(pattern=_ID_PATTERN)
    left_sha256: str = Field(pattern=_SHA256_PATTERN)
    right_sha256: str = Field(pattern=_SHA256_PATTERN)
    tier: PairTier
    decision: PairDecision
    oracle_left_signature: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    oracle_right_signature: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    static_distance: float | None = Field(default=None, ge=0, le=1)
    feature_left_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    feature_right_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    material_differences: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_pair_shape(self) -> PairEvidenceV4:
        if self.left_id >= self.right_id:
            raise ValueError("pair IDs must be strictly sorted")
        if self.tier is PairTier.ORACLE_SIGNATURE:
            if (
                self.oracle_left_signature is None
                or self.oracle_right_signature is None
                or self.oracle_left_signature == self.oracle_right_signature
            ):
                raise ValueError(
                    "oracle-signature pair must contain different signatures"
                )
            if (
                self.static_distance is not None
                or self.feature_left_sha256 is not None
                or self.feature_right_sha256 is not None
            ):
                raise ValueError(
                    "oracle-signature pair must not contain static evidence"
                )
        else:
            if self.static_distance is None:
                raise ValueError("static pair requires a distance")
            if self.feature_left_sha256 is None or self.feature_right_sha256 is None:
                raise ValueError("static pair requires feature hashes")
            if self.oracle_left_signature != self.oracle_right_signature:
                raise ValueError("static pair requires equal oracle signatures")
        return self


class DiversityEvidenceV4(_V4Model):
    policy_version: Literal["static-v1"] = "static-v1"
    oracle_descriptor: OracleDescriptorV4
    static_policy: StaticPolicyV4
    artifacts: dict[str, ArtifactDiversityV4]
    pairs: tuple[PairEvidenceV4, ...]
    invalid_behavior_matrix_f2p: dict[str, dict[str, float]]


class MutationSiteV4(_V4Model):
    path: str = Field(min_length=1)
    start_byte: int = Field(ge=0)
    end_byte: int = Field(ge=0)
    before_sha256: str = Field(pattern=_SHA256_PATTERN)
    after_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def _validate_site(self) -> MutationSiteV4:
        _validate_relative_path(self.path, "mutation site path")
        if self.end_byte <= self.start_byte:
            raise ValueError("mutation site end_byte must be greater than start_byte")
        if self.before_sha256 == self.after_sha256:
            raise ValueError("mutation site must change source bytes")
        return self


class MutationRecordV4(_V4Model):
    job_id: str = Field(pattern=_ID_PATTERN)
    parent_solution_id: str = Field(pattern=_ID_PATTERN)
    parent_sha256: str = Field(pattern=_SHA256_PATTERN)
    policy_version: Literal["first-order-v1"] = "first-order-v1"
    seed: int = Field(ge=0)
    operator: str | None = Field(default=None, min_length=1)
    site: MutationSiteV4 | None = None
    mutant_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    oracle_outcome: MutationOracleOutcome
    selection_outcome: MutationSelectionOutcome
    accepted_solution_id: str | None = Field(default=None, pattern=_ID_PATTERN)
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_outcome(self) -> MutationRecordV4:
        if (self.site is None) != (self.operator is None):
            raise ValueError("mutation operator and site must be present together")

        if self.selection_outcome is MutationSelectionOutcome.ACCEPTED:
            if self.oracle_outcome is not MutationOracleOutcome.ORACLE_INVALID:
                raise ValueError("accepted mutation must be oracle-invalid")
            if self.mutant_sha256 is None or self.accepted_solution_id is None:
                raise ValueError("accepted mutation must reference a mutant solution")
        elif self.accepted_solution_id is not None:
            raise ValueError("non-accepted mutation must not reference a solution")

        invalid_selection = {
            MutationSelectionOutcome.DUPLICATE,
            MutationSelectionOutcome.DIVERSITY_REJECTED,
            MutationSelectionOutcome.QUOTA_FULL,
        }
        if self.selection_outcome in invalid_selection:
            if self.oracle_outcome is not MutationOracleOutcome.ORACLE_INVALID:
                raise ValueError("invalid selection outcome requires oracle-invalid")
            if self.mutant_sha256 is None:
                raise ValueError("observed invalid mutant must have a hash")
        if self.selection_outcome is MutationSelectionOutcome.SURVIVED:
            if self.oracle_outcome is not MutationOracleOutcome.ORACLE_VALID:
                raise ValueError("survived mutation must be oracle-valid")
            if self.mutant_sha256 is None:
                raise ValueError("survived mutant must have a hash")
        if self.selection_outcome is MutationSelectionOutcome.UNCLASSIFIABLE:
            if self.oracle_outcome is not MutationOracleOutcome.UNCLASSIFIABLE:
                raise ValueError("unclassifiable selection needs an unclassifiable run")
        if self.selection_outcome in {
            MutationSelectionOutcome.NO_SITE,
            MutationSelectionOutcome.UNSUPPORTED,
        }:
            if self.oracle_outcome is not MutationOracleOutcome.NOT_RUN:
                raise ValueError("no-site/unsupported mutation must not run oracle")
            if self.mutant_sha256 is not None:
                raise ValueError("no-site/unsupported mutation has no mutant hash")
        if self.selection_outcome is MutationSelectionOutcome.CANCELLED:
            if self.oracle_outcome not in {
                MutationOracleOutcome.NOT_RUN,
                MutationOracleOutcome.UNCLASSIFIABLE,
            }:
                raise ValueError("cancelled mutation cannot have a completed label")
        return self


class PatchSetManifestV4(_V4Model):
    """Schema-v4 manifest, with dynamic quotas and first-class partial output."""

    schema_version: Literal[4] = 4
    patch_set_id: str = Field(pattern=_ID_PATTERN)
    task_key: str = Field(min_length=3)
    source: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    input_task_path: str
    baseline_tree_hash: str = Field(pattern=_TREE_HASH_PATTERN)
    original_instruction_hash: str = Field(pattern=_SHA256_PATTERN)
    effective_instruction_hash: str = Field(pattern=_SHA256_PATTERN)
    config_hash: str = Field(pattern=_SHA256_PATTERN)
    generated_at: datetime
    tool_version: str = Field(min_length=1)
    oracle_definition_hash: str = Field(pattern=_SHA256_PATTERN)
    status: Literal["ready", "partial"]
    completion: CompletionV4
    oracle: OracleEvidenceV4
    solutions: tuple[SolutionV4, ...]
    diversity: DiversityEvidenceV4
    mutations: tuple[MutationRecordV4, ...]

    @model_validator(mode="after")
    def _validate_contract(self) -> PatchSetManifestV4:
        if self.generated_at.tzinfo is None or self.generated_at.utcoffset() is None:
            raise ValueError("generated_at must be a timezone-aware UTC datetime")
        if self.generated_at.utcoffset() != timezone.utc.utcoffset(self.generated_at):
            raise ValueError("generated_at must be in UTC")
        _validate_relative_path(self.input_task_path, "input task path")
        try:
            _validate_relative_path(self.task_key, "task_key")
        except ValueError:
            raise ValueError("task_key must be a safe relative POSIX path") from None
        task_parts = PurePosixPath(self.task_key).parts
        if len(task_parts) < 2:
            raise ValueError("task_key must contain at least source and task ID")
        if task_parts[0] != self.source:
            raise ValueError("first task_key segment must equal source")
        if task_parts[-1] != self.task_id:
            raise ValueError("last task_key segment must equal task_id")

        valid = [
            solution
            for solution in self.solutions
            if solution.label is PatchLabel.ORACLE_VALID
        ]
        invalid = [
            solution
            for solution in self.solutions
            if solution.label is PatchLabel.ORACLE_INVALID
        ]
        if not valid:
            raise ValueError("manifest requires at least one oracle-valid solution")
        if self.completion.actual_valid != len(valid):
            raise ValueError("completion actual_valid does not match solutions")
        if self.completion.actual_invalid != len(invalid):
            raise ValueError("completion actual_invalid does not match solutions")
        if len(valid) > self.completion.target_valid:
            raise ValueError("actual valid solutions exceed target_valid")
        if len(invalid) > self.completion.target_invalid:
            raise ValueError("actual invalid solutions exceed target_invalid")
        expected_status = (
            "ready"
            if (
                len(valid) == self.completion.target_valid
                and len(invalid) == self.completion.target_invalid
            )
            else "partial"
        )
        if self.status != expected_status:
            raise ValueError("manifest status does not match quota completion")
        if self.completion.attempts_started > self.completion.max_agent_attempts:
            raise ValueError("attempts_started exceeds max_agent_attempts")

        gold = [solution for solution in self.solutions if solution.is_gold]
        if len(gold) != 1:
            raise ValueError("manifest requires exactly one gold solution")
        gold_solution = gold[0]
        if gold_solution.label is not PatchLabel.ORACLE_VALID:
            raise ValueError("gold solution must be oracle-valid")

        ids = [solution.id for solution in self.solutions]
        paths = [solution.path for solution in self.solutions]
        hashes = [solution.sha256 for solution in self.solutions]
        if len(ids) != len(set(ids)):
            raise ValueError("solution IDs must be unique")
        if len(paths) != len(set(paths)):
            raise ValueError("solution paths must be unique")
        if len(hashes) != len(set(hashes)):
            raise ValueError("solution SHA-256 values must be unique")
        if set(self.oracle.patch_results) != set(ids):
            raise ValueError("oracle evidence must cover every solution ID exactly")

        self._validate_solution_order_and_origins(valid, invalid, gold_solution)
        self._validate_oracle(gold_solution)
        self._validate_diversity(valid, invalid)
        self._validate_mutations(valid, invalid)
        _reject_serialized_secrets(self.model_dump(mode="json"))
        return self

    def _validate_solution_order_and_origins(
        self,
        valid: list[SolutionV4],
        invalid: list[SolutionV4],
        gold_solution: SolutionV4,
    ) -> None:
        if self.solutions[0].id != gold_solution.id:
            raise ValueError("gold solution must be first in publication order")
        seen_invalid = False
        for solution in self.solutions:
            if solution.label is PatchLabel.ORACLE_INVALID:
                seen_invalid = True
            elif seen_invalid:
                raise ValueError("valid solutions must precede invalid solutions")
            origin_kind = getattr(solution.origin, "kind", None)
            if solution.is_gold != (origin_kind == "gold"):
                raise ValueError("origin kind gold must match is_gold")
            if solution.is_gold and origin_kind == "gold":
                if solution.generator.agent != "canonical-gold":
                    raise ValueError("gold generator must be canonical-gold")
                if solution.generator.model != "not-applicable":
                    raise ValueError("gold generator model must be not-applicable")
                if solution.generator.attempt != 1:
                    raise ValueError("gold generator attempt must be one")
            elif origin_kind == "gold":
                raise ValueError("only the gold solution may use gold origin")
            elif origin_kind == "agent":
                if solution.origin.lane == "valid":
                    expected_access = "source_visible"
                else:
                    expected_access = "hidden"
                if solution.origin.oracle_access != expected_access:
                    raise ValueError("agent lane and oracle access do not match")
            elif origin_kind == "mutation":
                if solution.label is not PatchLabel.ORACLE_INVALID:
                    raise ValueError("mutation solutions must be oracle-invalid")
                if solution.generator.agent != "source-mutator":
                    raise ValueError("mutation generator must be source-mutator")
                if solution.generator.model != "not-applicable":
                    raise ValueError("mutation generator model must be not-applicable")
                if solution.generator.attempt != 1:
                    raise ValueError("mutation generator attempt must be one")
            else:
                raise ValueError("solution origin is not recognized")

    def _validate_oracle(self, gold_solution: SolutionV4) -> None:
        definition = self.oracle.definition()
        if is_suite_wide_oracle(definition):
            raise ValueError("v4 manifests cannot use a synthetic suite-wide oracle")
        if self.oracle.f2p_tests != definition.f2p:
            raise ValueError("F2P tests do not match oracle definition")
        if self.oracle.p2p_tests != definition.p2p:
            raise ValueError("P2P tests do not match oracle definition")
        descriptor = self.diversity.oracle_descriptor
        if descriptor.baseline_tree_hash != self.baseline_tree_hash:
            raise ValueError("oracle descriptor baseline hash mismatch")
        if descriptor.gold_sha256 != gold_solution.sha256:
            raise ValueError("oracle descriptor Gold hash mismatch")
        if (
            canonical_hash(descriptor.model_dump(mode="json"))
            != self.oracle_definition_hash
        ):
            raise ValueError("oracle definition hash mismatch")

        for solution in self.solutions:
            evidence = self.oracle.patch_results[solution.id]
            result = evidence.result
            if result.run_status is not RunStatus.COMPLETED:
                raise ValueError(
                    f"oracle run is not completed for solution {solution.id}"
                )
            if set(result.tests) != set(definition.test_ids):
                raise ValueError(f"oracle test IDs differ for solution {solution.id}")
            assessment = classify_candidate(
                definition, result, require_f2p_progress=False
            )
            expected = (
                CandidateLabel.ORACLE_VALID
                if solution.label is PatchLabel.ORACLE_VALID
                else CandidateLabel.ORACLE_INVALID
            )
            if assessment.label is not expected:
                raise ValueError(f"oracle label mismatch for solution {solution.id}")
            if solution.label is PatchLabel.ORACLE_VALID:
                if evidence.invalid_signature is not None:
                    raise ValueError("valid patches cannot have an invalid signature")
            else:
                expected_signature = invalid_behavior_signature(definition, result)
                if evidence.invalid_signature != expected_signature:
                    raise ValueError(
                        "invalid behavior signature mismatch for solution "
                        f"{solution.id}"
                    )
        if self.oracle.patch_results[gold_solution.id].result != self.oracle.gold:
            raise ValueError("gold result does not match canonical gold run")

    def _validate_diversity(
        self, valid: list[SolutionV4], invalid: list[SolutionV4]
    ) -> None:
        artifact_ids = {solution.id for solution in self.solutions}
        if set(self.diversity.artifacts) != artifact_ids:
            raise ValueError("diversity artifacts must cover every solution exactly")
        by_id = {solution.id: solution for solution in self.solutions}
        for solution in self.solutions:
            artifact = self.diversity.artifacts[solution.id]
            if artifact.artifact_sha256 != solution.sha256:
                raise ValueError(f"artifact hash mismatch for solution {solution.id}")
            if artifact.effect_descriptor is not None:
                effect_descriptor = artifact.effect_descriptor
                if effect_descriptor.baseline_tree_hash != self.baseline_tree_hash:
                    raise ValueError("effect descriptor baseline hash mismatch")
                if effect_descriptor.scope_id != artifact.effect_scope_id:
                    raise ValueError("effect descriptor scope mismatch")
                if artifact.effect_sha256 != canonical_hash(
                    effect_descriptor.model_dump(mode="json")
                ):
                    raise ValueError("effect descriptor hash mismatch")

        complete_effects: dict[tuple[str, int, str], str] = {}
        for solution_id, artifact in self.diversity.artifacts.items():
            if artifact.effect_status is not EffectStatus.COMPLETE:
                continue
            assert artifact.effect_descriptor is not None
            key = (
                artifact.effect_scope_id,
                artifact.effect_descriptor.version,
                artifact.effect_sha256 or "",
            )
            previous = complete_effects.get(key)
            if previous is not None:
                raise ValueError(
                    f"complete effect duplicate between {previous} and {solution_id}"
                )
            complete_effects[key] = solution_id

        invalid_behaviors = {
            solution.id: dict(self.oracle.patch_results[solution.id].result.tests)
            for solution in invalid
        }
        expected_matrix = behavior_matrix(
            invalid_behaviors, self.oracle.definition().f2p
        )
        if self.diversity.invalid_behavior_matrix_f2p != expected_matrix:
            raise ValueError("invalid F2P behavior matrix mismatch")

        expected_pairs: set[frozenset[str]] = set()
        for group in (valid, invalid):
            for index, left in enumerate(group):
                for right in group[index + 1 :]:
                    expected_pairs.add(frozenset((left.id, right.id)))
        actual_pairs: set[frozenset[str]] = set()
        for pair in self.diversity.pairs:
            if pair.left_id not in by_id or pair.right_id not in by_id:
                raise ValueError("diversity pair references an unknown solution")
            key = frozenset((pair.left_id, pair.right_id))
            if key in actual_pairs:
                raise ValueError("diversity pairs contain a duplicate")
            actual_pairs.add(key)
            left = by_id[pair.left_id]
            right = by_id[pair.right_id]
            if left.label is not right.label:
                raise ValueError("diversity pairs must compare same-label solutions")
            if pair.left_sha256 != left.sha256 or pair.right_sha256 != right.sha256:
                raise ValueError("diversity pair solution hash mismatch")
            left_evidence = self.oracle.patch_results[left.id]
            right_evidence = self.oracle.patch_results[right.id]
            left_signature = left_evidence.invalid_signature
            right_signature = right_evidence.invalid_signature
            if pair.oracle_left_signature != left_signature:
                raise ValueError("left oracle signature mismatch in diversity pair")
            if pair.oracle_right_signature != right_signature:
                raise ValueError("right oracle signature mismatch in diversity pair")
            if pair.tier is PairTier.ORACLE_SIGNATURE:
                if left_signature is None or right_signature is None:
                    raise ValueError(
                        "oracle-signature tier requires invalid signatures"
                    )
                if left_signature == right_signature:
                    raise ValueError(
                        "oracle-signature tier requires different signatures"
                    )
            else:
                if left_signature != right_signature:
                    raise ValueError("static tier requires equal oracle signatures")
                left_artifact = self.diversity.artifacts[left.id]
                right_artifact = self.diversity.artifacts[right.id]
                if (
                    left_artifact.static_status is not StaticStatus.COMPLETE
                    or right_artifact.static_status is not StaticStatus.COMPLETE
                ):
                    raise ValueError("static tier requires complete static evidence")
                left_feature_hash = canonical_hash(left_artifact.features)
                right_feature_hash = canonical_hash(right_artifact.features)
                if pair.feature_left_sha256 != left_feature_hash:
                    raise ValueError("left feature hash mismatch in diversity pair")
                if pair.feature_right_sha256 != right_feature_hash:
                    raise ValueError("right feature hash mismatch in diversity pair")
                distance = weighted_jaccard_distance(
                    left_artifact.features, right_artifact.features
                )
                if pair.static_distance is None or not math.isclose(
                    pair.static_distance, round(distance, 6), abs_tol=1e-9
                ):
                    raise ValueError("static distance mismatch in diversity pair")
                if distance < self.diversity.static_policy.min_distance:
                    raise ValueError("static diversity distance is below threshold")
        if actual_pairs != expected_pairs:
            raise ValueError("diversity evidence must cover every same-label pair")

    def _validate_mutations(
        self, valid: list[SolutionV4], invalid: list[SolutionV4]
    ) -> None:
        valid_ids = {solution.id for solution in valid}
        invalid_by_id = {solution.id: solution for solution in invalid}
        if len(self.mutations) != len(valid):
            raise ValueError(
                "every valid solution must have exactly one mutation record"
            )
        jobs: dict[str, MutationRecordV4] = {}
        for record in self.mutations:
            if record.job_id in jobs:
                raise ValueError("mutation job IDs must be unique")
            jobs[record.job_id] = record
            if record.parent_solution_id not in valid_ids:
                raise ValueError("mutation parent must be a valid solution")
            parent = next(
                solution
                for solution in valid
                if solution.id == record.parent_solution_id
            )
            if record.parent_sha256 != parent.sha256:
                raise ValueError("mutation parent hash mismatch")
        if {record.parent_solution_id for record in self.mutations} != valid_ids:
            raise ValueError("mutation records must cover every valid solution")

        for solution in invalid:
            if not isinstance(solution.origin, MutationOrigin):
                continue
            record = jobs.get(solution.origin.job_id)
            if record is None:
                raise ValueError("mutation-origin solution references an unknown job")
            if record.selection_outcome is not MutationSelectionOutcome.ACCEPTED:
                raise ValueError("only accepted mutation jobs may publish a solution")
            if record.accepted_solution_id != solution.id:
                raise ValueError("mutation accepted solution reference mismatch")
            if solution.origin.parent_solution_id != record.parent_solution_id:
                raise ValueError("mutation origin parent mismatch")
            if solution.origin.parent_sha256 != record.parent_sha256:
                raise ValueError("mutation origin parent hash mismatch")
            if solution.origin.operator != record.operator:
                raise ValueError("mutation origin operator mismatch")
            if solution.origin.policy_version != record.policy_version:
                raise ValueError("mutation origin policy mismatch")
            if record.mutant_sha256 != solution.sha256:
                raise ValueError("mutation mutant hash mismatch")

        for record in self.mutations:
            if record.selection_outcome is not MutationSelectionOutcome.ACCEPTED:
                continue
            accepted = invalid_by_id.get(record.accepted_solution_id or "")
            if accepted is None or not isinstance(accepted.origin, MutationOrigin):
                raise ValueError(
                    "accepted mutation must reference a mutation-origin invalid"
                )

    def validate_files(self, root: str | Path) -> None:
        """Recheck all original solution bytes under a publication root."""

        root_path = Path(root).resolve(strict=True)
        if not root_path.is_dir():
            raise ValueError(f"patch-set root is not a directory: {root_path}")
        for solution in self.solutions:
            artifact = _resolve_artifact(root_path, solution.path)
            actual_hash = hashlib.sha256(artifact.read_bytes()).hexdigest()
            if actual_hash != solution.sha256:
                raise ValueError(f"SHA-256 mismatch for solution {solution.id}")
            try:
                script = artifact.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(f"solution {solution.id} is not UTF-8 text") from exc
            if not script.strip() or "\x00" in script:
                raise ValueError(f"solution {solution.id} is not an executable script")


def canonical_json(value: Any) -> str:
    """Return the canonical JSON representation used for evidence hashes."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def weighted_jaccard_distance(left: dict[str, int], right: dict[str, int]) -> float:
    """Recompute the static-v1 weighted multiset distance from embedded features."""

    keys = set(left) | set(right)
    denominator = sum(max(left.get(key, 0), right.get(key, 0)) for key in keys)
    if denominator == 0:
        return 0.0
    numerator = sum(min(left.get(key, 0), right.get(key, 0)) for key in keys)
    return 1.0 - numerator / denominator


def _validate_relative_path(value: str, label: str) -> None:
    if not value or "\\" in value:
        raise ValueError(f"{label} must stay within patch-set root")
    path = PurePosixPath(value)
    if path.is_absolute() or path == PurePosixPath(".") or ".." in path.parts:
        raise ValueError(f"{label} must stay within patch-set root")


def _resolve_artifact(root: Path, relative: str) -> Path:
    candidate = root / relative
    if candidate.is_symlink():
        raise ValueError(f"solution path is a symbolic link: {relative}")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, ValueError) as exc:
        raise ValueError(
            f"solution path escapes solution-set root: {relative}"
        ) from exc
    if not resolved.is_file():
        raise ValueError(f"solution artifact is not a file: {relative}")
    return resolved


# Friendly aliases used by consumers and tests.  The explicit V4 suffixes keep
# the names unambiguous next to the historical classes in ``models.py``.
Completion = CompletionV4
StopReason = V4StopReason
PatchEntryV4 = SolutionV4
PatchOracleEvidence = PatchOracleEvidenceV4
OracleEvidence = OracleEvidenceV4
DiversityEvidence = DiversityEvidenceV4
PairEvidence = PairEvidenceV4
MutationRecord = MutationRecordV4
MutationSite = MutationSiteV4
EffectDescriptor = EffectDescriptorV4
ArtifactDiversity = ArtifactDiversityV4
