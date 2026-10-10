"""Contract tests for the pure schema-v4 patch-set consumer."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from testprism.patches.diversity import behavior_matrix
from testprism.patches.models import (
    GeneratorProvenance,
    PatchLabel,
    PatchSetManifest,
    load_patch_set_manifest,
)
from testprism.patches.models_v4 import (
    AgentOrigin,
    ArtifactDiversityV4,
    CompletionV4,
    DiversityEvidenceV4,
    EffectStatus,
    GoldOrigin,
    MutationOracleOutcome,
    MutationRecordV4,
    MutationSelectionOutcome,
    OracleDescriptorV4,
    OracleEvidenceV4,
    PairEvidenceV4,
    PairTier,
    PatchOracleEvidenceV4,
    PatchSetManifestV4,
    SolutionV4,
    StaticPolicyV4,
    StaticStatus,
    V4StopReason,
    canonical_hash,
)
from testprism.patches.oracle import (
    RunStatus,
    build_oracle,
    invalid_behavior_signature,
)
from testprism.patches.oracle import TestRunResult as RunResult


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _manifest(
    tmp_path: Path,
    *,
    valid_count: int,
    invalid_count: int,
    target_valid: int | None = None,
    target_invalid: int | None = None,
) -> PatchSetManifestV4:
    baseline = RunResult(
        run_status=RunStatus.COMPLETED,
        tests={
            **{f"fix-{index}": "failed" for index in range(4)},
            "keep": "passed",
        },
    )
    gold = RunResult(
        run_status=RunStatus.COMPLETED,
        tests={test_id: "passed" for test_id in baseline.tests},
    )
    oracle = build_oracle(baseline, gold)
    solutions: list[SolutionV4] = []
    patch_results: dict[str, PatchOracleEvidenceV4] = {}
    artifacts: dict[str, ArtifactDiversityV4] = {}
    invalid_behaviors: dict[str, dict[str, str]] = {}

    for index in range(valid_count + invalid_count):
        is_valid = index < valid_count
        solution_id = (
            f"oracle-valid-{index + 1:02d}"
            if is_valid
            else f"oracle-invalid-{index - valid_count + 1:02d}"
        )
        if index == 0:
            solution_id += "-gold"
        content = f"#!/bin/sh\nprintf '%s\\n' {solution_id}\n"
        solution_hash = _hash(content)
        if index == 0:
            generator = GeneratorProvenance(
                profile_id="gold",
                agent="canonical-gold",
                model="not-applicable",
                version="fixture",
                attempt=1,
            )
            origin = GoldOrigin(
                canonical_source="fixture/gold.sh",
                source_sha256=_hash("canonical gold"),
            )
            result = gold
        else:
            generator = GeneratorProvenance(
                profile_id="valid" if is_valid else "invalid",
                agent="codex",
                model="fixture",
                version="fixture",
                attempt=index + 1,
            )
            origin = AgentOrigin(
                lane="valid" if is_valid else "invalid",
                attempt_id=f"attempt-{index + 1}",
                request_sha256=_hash(f"request-{index}"),
                oracle_access="source_visible" if is_valid else "hidden",
            )
            if is_valid:
                result = gold
            else:
                vector = index - valid_count
                result = RunResult(
                    run_status=RunStatus.COMPLETED,
                    tests={
                        **{
                            f"fix-{bit}": (
                                "passed" if vector & (1 << bit) else "failed"
                            )
                            for bit in range(4)
                        },
                        "keep": "passed",
                    },
                )
                invalid_behaviors[solution_id] = dict(result.tests)
        solution = SolutionV4(
            id=solution_id,
            label=PatchLabel.ORACLE_VALID if is_valid else PatchLabel.ORACLE_INVALID,
            is_gold=index == 0,
            path=f"solutions/{solution_id}/solution.sh",
            sha256=solution_hash,
            generator=generator,
            origin=origin,
        )
        solutions.append(solution)
        patch_results[solution_id] = PatchOracleEvidenceV4(
            result=result,
            invalid_signature=(
                None if is_valid else invalid_behavior_signature(oracle, result)
            ),
        )
        feature_hash = _hash(f"feature-{solution_id}")
        artifacts[solution_id] = ArtifactDiversityV4(
            artifact_sha256=solution_hash,
            effect_status=EffectStatus.UNKNOWN,
            effect_scope_id="effects-v1",
            static_status=(
                StaticStatus.COMPLETE
                if is_valid and valid_count > 1
                else StaticStatus.NOT_COMPUTED
            ),
            features={feature_hash: 1} if is_valid and valid_count > 1 else {},
            source_units=[],
        )

    descriptor = OracleDescriptorV4(
        baseline_tree_hash="a" * 40,
        gold_sha256=solutions[0].sha256,
        tests_tree_sha256="b" * 64,
        runner_sha256="c" * 64,
        parser_id="fixture-parser",
        parser_version="1",
        image_digest="sha256:" + "d" * 64,
    )
    valid = [
        solution for solution in solutions if solution.label is PatchLabel.ORACLE_VALID
    ]
    invalid = [
        solution
        for solution in solutions
        if solution.label is PatchLabel.ORACLE_INVALID
    ]
    pairs: list[PairEvidenceV4] = []
    for group in (valid, invalid):
        for index, left in enumerate(group):
            for right in group[index + 1 :]:
                left_signature = patch_results[left.id].invalid_signature
                right_signature = patch_results[right.id].invalid_signature
                if left_signature != right_signature:
                    pairs.append(
                        PairEvidenceV4(
                            left_id=left.id,
                            right_id=right.id,
                            left_sha256=left.sha256,
                            right_sha256=right.sha256,
                            tier=PairTier.ORACLE_SIGNATURE,
                            decision="different",
                            oracle_left_signature=left_signature,
                            oracle_right_signature=right_signature,
                            material_differences=("fix-0 status differs",),
                        )
                    )
                else:
                    left_features = artifacts[left.id].features
                    right_features = artifacts[right.id].features
                    pairs.append(
                        PairEvidenceV4(
                            left_id=left.id,
                            right_id=right.id,
                            left_sha256=left.sha256,
                            right_sha256=right.sha256,
                            tier=PairTier.STATIC,
                            decision="different",
                            static_distance=1.0,
                            feature_left_sha256=canonical_hash(left_features),
                            feature_right_sha256=canonical_hash(right_features),
                            material_differences=("src/app.py bytes differ",),
                        )
                    )
    mutations = [
        MutationRecordV4(
            job_id=f"mutation-{solution.id}",
            parent_solution_id=solution.id,
            parent_sha256=solution.sha256,
            policy_version="first-order-v1",
            seed=index,
            reason="fixture has no supported mutation site",
            oracle_outcome=MutationOracleOutcome.NOT_RUN,
            selection_outcome=MutationSelectionOutcome.NO_SITE,
        )
        for index, solution in enumerate(valid)
    ]
    actual_target_valid = target_valid if target_valid is not None else valid_count
    actual_target_invalid = (
        target_invalid if target_invalid is not None else invalid_count
    )
    manifest = PatchSetManifestV4(
        patch_set_id="set-fixture",
        task_key="fixture/demo",
        source="fixture",
        task_id="demo",
        input_task_path="fixture/demo",
        baseline_tree_hash="a" * 40,
        original_instruction_hash="e" * 64,
        effective_instruction_hash="f" * 64,
        config_hash="1" * 64,
        generated_at="2026-09-19T00:00:00Z",
        tool_version="fixture",
        oracle_definition_hash=canonical_hash(descriptor.model_dump(mode="json")),
        status=(
            "ready"
            if actual_target_valid == valid_count
            and actual_target_invalid == invalid_count
            else "partial"
        ),
        completion=CompletionV4(
            target_valid=actual_target_valid,
            target_invalid=actual_target_invalid,
            actual_valid=valid_count,
            actual_invalid=invalid_count,
            stop_reason=V4StopReason.QUOTA_REACHED,
            attempts_started=0,
            max_agent_attempts=0,
        ),
        oracle=OracleEvidenceV4(
            baseline=baseline,
            gold=gold,
            f2p_tests=oracle.f2p,
            p2p_tests=oracle.p2p,
            patch_results=patch_results,
            canonical_gold_provenance="fixture/gold.sh",
        ),
        solutions=solutions,
        diversity=DiversityEvidenceV4(
            policy_version="static-v1",
            oracle_descriptor=descriptor,
            static_policy=StaticPolicyV4(
                min_distance=0.15,
                grammar_versions={"python": "0.25.0"},
            ),
            artifacts=artifacts,
            pairs=pairs,
            invalid_behavior_matrix_f2p=behavior_matrix(invalid_behaviors, oracle.f2p),
        ),
        mutations=mutations,
    )
    for solution in manifest.solutions:
        path = tmp_path / solution.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"#!/bin/sh\nprintf '%s\\n' {solution.id}\n", encoding="utf-8")
    return manifest


def _accepted_mutation_payload(tmp_path: Path) -> dict:
    """Return a valid one-parent/one-accepted-mutant manifest payload."""

    manifest = _manifest(tmp_path, valid_count=1, invalid_count=1)
    payload = manifest.model_dump(mode="json")
    parent, mutant = payload["solutions"]
    record = payload["mutations"][0]
    job_id = record["job_id"]
    record.update(
        {
            "operator": "replace-expression",
            "site": {
                "path": "src/app.py",
                "start_byte": 0,
                "end_byte": 1,
                "before_sha256": "a" * 64,
                "after_sha256": "b" * 64,
            },
            "mutant_sha256": mutant["sha256"],
            "oracle_outcome": "oracle_invalid",
            "selection_outcome": "accepted",
            "accepted_solution_id": mutant["id"],
        }
    )
    mutant["generator"] = {
        "profile_id": "mutation",
        "agent": "source-mutator",
        "model": "not-applicable",
        "version": "fixture",
        "attempt": 1,
    }
    mutant["origin"] = {
        "kind": "mutation",
        "job_id": job_id,
        "parent_solution_id": parent["id"],
        "parent_sha256": parent["sha256"],
        "operator": record["operator"],
        "policy_version": record["policy_version"],
    }
    return payload


def _complete_effect_payload(manifest: PatchSetManifestV4) -> dict:
    payload = manifest.model_dump(mode="json")
    solution_id = payload["solutions"][0]["id"]
    descriptor = {
        "version": 1,
        "baseline_tree_hash": payload["baseline_tree_hash"],
        "scope_id": "effects-v1",
        "files": [],
        "non_file_effects": [],
    }
    artifact = payload["diversity"]["artifacts"][solution_id]
    artifact.update(
        {
            "effect_status": "complete",
            "effect_scope_id": "effects-v1",
            "effect_descriptor": descriptor,
            "effect_sha256": canonical_hash(descriptor),
        }
    )
    return payload


def _write_v4_manifest(
    root: Path,
    task_key: str,
    *,
    valid_count: int = 1,
    invalid_count: int = 0,
) -> PatchSetManifestV4:
    """Write a fixture below ``root`` after changing its dataset-relative key."""

    base = _manifest(
        root,
        valid_count=valid_count,
        invalid_count=invalid_count,
    )
    payload = base.model_dump(mode="json")
    payload.update(
        {
            "patch_set_id": task_key.replace("/", "-"),
            "task_key": task_key,
            "source": task_key.split("/", 1)[0],
            "task_id": task_key.rsplit("/", 1)[-1],
            "input_task_path": task_key,
        }
    )
    manifest = PatchSetManifestV4.model_validate(payload)
    (root / "manifest.json").write_text(manifest.model_dump_json(), encoding="utf-8")
    return manifest


@pytest.mark.parametrize(("valid", "invalid"), [(1, 0), (2, 1), (5, 5), (6, 7)])
def test_v4_ready_shapes_round_trip_and_revalidate_files(
    tmp_path: Path, valid: int, invalid: int
) -> None:
    manifest = _manifest(tmp_path, valid_count=valid, invalid_count=invalid)
    restored = PatchSetManifestV4.model_validate_json(manifest.model_dump_json())
    restored.validate_files(tmp_path)
    assert restored == manifest
    assert restored.status == "ready"


@pytest.mark.parametrize(("actual_valid", "actual_invalid"), [(1, 0), (2, 1)])
def test_v4_partial_shapes_are_formal_outputs(
    tmp_path: Path, actual_valid: int, actual_invalid: int
) -> None:
    manifest = _manifest(
        tmp_path,
        valid_count=actual_valid,
        invalid_count=actual_invalid,
        target_valid=5,
        target_invalid=5,
    )
    assert manifest.status == "partial"
    manifest.validate_files(tmp_path)


def test_v4_rejects_tampered_pair_quota_and_unknown_field(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, valid_count=2, invalid_count=1)
    payload = manifest.model_dump(mode="json")
    payload["diversity"]["pairs"][0]["static_distance"] = 0.0
    with pytest.raises(ValidationError, match="distance|threshold"):
        PatchSetManifestV4.model_validate(payload)

    payload = manifest.model_dump(mode="json")
    payload["completion"]["actual_valid"] = 1
    with pytest.raises(ValidationError, match="actual_valid"):
        PatchSetManifestV4.model_validate(payload)

    payload = manifest.model_dump(mode="json")
    payload["unexpected"] = True
    with pytest.raises(ValidationError):
        PatchSetManifestV4.model_validate(payload)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda payload: payload.update(solutions=[]), "at least one"),
        (
            lambda payload: [
                solution.update(is_gold=False) for solution in payload["solutions"]
            ],
            "exactly one gold",
        ),
        (
            lambda payload: payload["solutions"][1].update(is_gold=True),
            "exactly one gold",
        ),
    ],
)
def test_v4_rejects_empty_or_ambiguous_gold_sets(
    tmp_path: Path, mutate, message: str
) -> None:
    payload = _manifest(tmp_path, valid_count=2, invalid_count=1).model_dump(
        mode="json"
    )
    mutate(payload)

    with pytest.raises(ValidationError, match=message):
        PatchSetManifestV4.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("target_valid", 1, "actual valid solutions exceed target_valid"),
        ("target_invalid", 0, "actual invalid solutions exceed target_invalid"),
    ],
)
def test_v4_rejects_actual_counts_over_dynamic_quotas(
    tmp_path: Path, field: str, value: int, message: str
) -> None:
    payload = _manifest(tmp_path, valid_count=2, invalid_count=1).model_dump(
        mode="json"
    )
    payload["completion"][field] = value

    with pytest.raises(ValidationError, match=message):
        PatchSetManifestV4.model_validate(payload)


def test_v4_rejects_false_ready_status(tmp_path: Path) -> None:
    payload = _manifest(
        tmp_path,
        valid_count=1,
        invalid_count=0,
        target_valid=5,
        target_invalid=5,
    ).model_dump(mode="json")
    payload["status"] = "ready"

    with pytest.raises(ValidationError, match="status does not match quota"):
        PatchSetManifestV4.model_validate(payload)


def test_v4_rejects_missing_diversity_pair(tmp_path: Path) -> None:
    payload = _manifest(tmp_path, valid_count=2, invalid_count=1).model_dump(
        mode="json"
    )
    payload["diversity"]["pairs"] = []

    with pytest.raises(ValidationError, match="cover every same-label pair"):
        PatchSetManifestV4.model_validate(payload)


def test_v4_rejects_tampered_oracle_signature(tmp_path: Path) -> None:
    payload = _manifest(tmp_path, valid_count=1, invalid_count=1).model_dump(
        mode="json"
    )
    invalid_id = payload["solutions"][1]["id"]
    payload["oracle"]["patch_results"][invalid_id]["invalid_signature"] = "f" * 64

    with pytest.raises(ValidationError, match="behavior signature mismatch"):
        PatchSetManifestV4.model_validate(payload)


def test_v4_rejects_tampered_static_distance(tmp_path: Path) -> None:
    payload = _manifest(tmp_path, valid_count=2, invalid_count=1).model_dump(
        mode="json"
    )
    payload["diversity"]["pairs"][0]["static_distance"] = 0.0

    with pytest.raises(ValidationError, match="static distance mismatch"):
        PatchSetManifestV4.model_validate(payload)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda payload: payload["mutations"][0].update(
                parent_solution_id="missing-parent"
            ),
            "mutation parent must be a valid solution",
        ),
        (
            lambda payload: payload["mutations"][0].update(parent_sha256="f" * 64),
            "mutation parent hash mismatch",
        ),
        (
            lambda payload: payload["solutions"][1]["origin"].update(
                job_id="missing-job"
            ),
            "unknown job",
        ),
        (
            lambda payload: payload["mutations"][0].update(
                accepted_solution_id="other-mutant"
            ),
            "accepted solution reference mismatch",
        ),
    ],
)
def test_v4_rejects_mutation_lineage_tampering(
    tmp_path: Path, mutate, message: str
) -> None:
    payload = _accepted_mutation_payload(tmp_path)
    mutate(payload)

    with pytest.raises(ValidationError, match=message):
        PatchSetManifestV4.model_validate(payload)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda artifact: artifact["effect_descriptor"].update(
                scope_id="effects-other"
            ),
            "effect descriptor scope mismatch",
        ),
        (
            lambda artifact: artifact.update(effect_scope_id="effects-other"),
            "effect descriptor scope mismatch",
        ),
        (
            lambda artifact: artifact["effect_descriptor"].update(
                baseline_tree_hash="b" * 40
            ),
            "effect descriptor baseline hash mismatch",
        ),
        (
            lambda artifact: artifact["effect_descriptor"].update(version=2),
            "version",
        ),
        (
            lambda artifact: artifact.update(effect_sha256="f" * 64),
            "effect descriptor hash mismatch",
        ),
    ],
)
def test_v4_rejects_effect_descriptor_identity_tampering(
    tmp_path: Path, mutate, message: str
) -> None:
    payload = _complete_effect_payload(
        _manifest(tmp_path, valid_count=1, invalid_count=0)
    )
    solution_id = payload["solutions"][0]["id"]
    mutate(payload["diversity"]["artifacts"][solution_id])

    with pytest.raises(ValidationError, match=message):
        PatchSetManifestV4.model_validate(payload)


def test_v4_rejects_duplicate_complete_effect_identity(tmp_path: Path) -> None:
    payload = _complete_effect_payload(
        _manifest(tmp_path, valid_count=2, invalid_count=0)
    )
    first_id, second_id = (solution["id"] for solution in payload["solutions"])
    first = payload["diversity"]["artifacts"][first_id]
    second = payload["diversity"]["artifacts"][second_id]
    second.update(
        {
            "effect_status": "complete",
            "effect_scope_id": first["effect_scope_id"],
            "effect_descriptor": first["effect_descriptor"],
            "effect_sha256": first["effect_sha256"],
        }
    )

    with pytest.raises(ValidationError, match="complete effect duplicate"):
        PatchSetManifestV4.model_validate(payload)


def test_v4_rejects_unknown_nested_fields(tmp_path: Path) -> None:
    payload = _manifest(tmp_path, valid_count=1, invalid_count=0).model_dump(
        mode="json"
    )
    payload["solutions"][0]["generator"]["unexpected"] = True

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        PatchSetManifestV4.model_validate(payload)


def test_v4_loader_dispatches_without_converting_schema(tmp_path: Path) -> None:
    # Scope the loader root to this test's own dataset-relative path.  Using
    # ``tmp_path.parent`` lets pytest's shared temporary parent expose a
    # manifest left by another test with the same leaf task id, which makes
    # loader discovery correctly report an ambiguity rather than testing the
    # intended direct dispatch path.
    task_root = tmp_path / "fixture" / "demo"
    manifest = _manifest(task_root, valid_count=1, invalid_count=0)
    (task_root / "manifest.json").write_text(
        manifest.model_dump_json(), encoding="utf-8"
    )
    loaded, root = load_patch_set_manifest(tmp_path, "demo", task_key="fixture/demo")
    assert isinstance(loaded, PatchSetManifestV4)
    assert loaded.schema_version == 4
    assert root == task_root


def test_v4_loader_rejects_unknown_schema_version(tmp_path: Path) -> None:
    task_root = tmp_path / "demo"
    manifest = _manifest(task_root, valid_count=1, invalid_count=0)
    payload = manifest.model_dump(mode="json")
    payload["schema_version"] = 99
    (task_root / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported patch-set schema_version"):
        load_patch_set_manifest(tmp_path, "demo")


def test_v4_loader_disambiguates_duplicate_leaf_task_ids_by_task_key(
    tmp_path: Path,
) -> None:
    root = tmp_path / "sets"
    alpha_root = root / "from-alpha" / "shared"
    beta_root = root / "from-beta" / "shared"
    _write_v4_manifest(alpha_root, "from-alpha/shared")
    _write_v4_manifest(beta_root, "from-beta/shared")

    with pytest.raises(ValueError, match="multiple patch-set manifests"):
        load_patch_set_manifest(root, "shared")

    loaded, loaded_root = load_patch_set_manifest(
        root, "shared", task_key="from-beta/shared"
    )
    assert isinstance(loaded, PatchSetManifestV4)
    assert loaded.task_key == "from-beta/shared"
    assert loaded_root == beta_root


def test_v4_loader_accepts_task_key_relative_to_scoped_source_root(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "sets" / "from-source"
    task_root = source_root / "shared"
    _write_v4_manifest(task_root, "from-source/shared")

    loaded, loaded_root = load_patch_set_manifest(
        source_root, "shared", task_key="shared"
    )
    assert isinstance(loaded, PatchSetManifestV4)
    assert loaded.task_key == "from-source/shared"
    assert loaded_root == task_root


def test_v3_manifest_import_and_ready_reviewer_gate_are_unchanged(
    tmp_path: Path,
) -> None:
    # Reuse the established v3 fixture so this test exercises its exact fixed
    # 5+5 and all-same-label reviewer-pair contract through the shared loader.
    from tests.unit.patches.test_patch_sets import _manifest as make_v3_manifest

    manifest = make_v3_manifest()
    task_root = tmp_path / "demo"
    for solution in manifest.solutions:
        artifact = task_root / solution.path
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(
            f"#!/bin/sh\nprintf '%s\\n' {solution.id}\n", encoding="utf-8"
        )
    (task_root / "manifest.json").write_text(
        manifest.model_dump_json(), encoding="utf-8"
    )

    loaded, loaded_root = load_patch_set_manifest(tmp_path, "demo")
    assert type(loaded) is PatchSetManifest
    assert loaded.schema_version == 3
    assert loaded.status == "ready"
    assert (
        sum(solution.label is PatchLabel.ORACLE_VALID for solution in loaded.solutions)
        == 5
    )
    assert (
        sum(
            solution.label is PatchLabel.ORACLE_INVALID for solution in loaded.solutions
        )
        == 5
    )
    assert len(loaded.reviewer.pairs) == 20
    assert loaded_root == task_root
