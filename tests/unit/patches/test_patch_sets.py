import hashlib
from pathlib import Path

import pytest

from testprism.patches.diversity import (
    behavior_distance,
    behavior_matrix,
)
from testprism.patches.models import (
    DiversityEvidence,
    GeneratorProvenance,
    OracleEvidence,
    PatchEntry,
    PatchLabel,
    PatchOracleEvidence,
    PatchSetManifest,
    ReviewerEvidence,
    ReviewerPairEvidence,
    load_patch_set_manifest,
)
from testprism.patches.oracle import (
    RunStatus,
    build_oracle,
    invalid_behavior_signature,
)
from testprism.patches.oracle import TestRunResult as RunResult


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _manifest() -> PatchSetManifest:
    baseline = RunResult(
        run_status=RunStatus.COMPLETED,
        tests={"fix-a": "failed", "fix-b": "failed", "stable": "passed"},
    )
    gold = RunResult(
        run_status=RunStatus.COMPLETED,
        tests={"fix-a": "passed", "fix-b": "passed", "stable": "passed"},
    )
    oracle = build_oracle(baseline, gold)
    invalid_vectors = (
        ("passed", "failed"),
        ("failed", "passed"),
        ("failed", "failed"),
        ("passed", "failed"),
        ("failed", "passed"),
    )
    solutions: list[PatchEntry] = []
    patch_results: dict[str, PatchOracleEvidence] = {}
    invalid_behaviors: dict[str, dict[str, str]] = {}
    for index in range(10):
        patch_id = f"patch-{index + 1:02d}"
        label = (
            PatchLabel.ORACLE_VALID if index < 5 else PatchLabel.ORACLE_INVALID
        )
        content = f"#!/bin/sh\nprintf '%s\\n' {patch_id}\n"
        result = gold
        signature = None
        if label is PatchLabel.ORACLE_INVALID:
            first, second = invalid_vectors[index - 5]
            result = RunResult(
                run_status=RunStatus.COMPLETED,
                tests={"fix-a": first, "fix-b": second, "stable": "passed"},
            )
            signature = invalid_behavior_signature(oracle, result)
            invalid_behaviors[patch_id] = dict(result.tests)
        patch_results[patch_id] = PatchOracleEvidence(
            result=result,
            invalid_signature=signature,
        )
        solutions.append(
            PatchEntry(
                id=patch_id,
                label=label,
                is_gold=index == 0,
                path=f"solutions/{patch_id}/solution.sh",
                sha256=_hash(content),
                generator=GeneratorProvenance(
                    profile_id="gold" if index == 0 else "generator",
                    agent="canonical" if index == 0 else "codex",
                    model="fixture",
                    version="1",
                    attempt=index + 1,
                ),
            )
        )

    pairs = []
    for group in (solutions[:5], solutions[5:]):
        for position, left in enumerate(group):
            for right in group[position + 1 :]:
                pairs.append(
                    ReviewerPairEvidence(
                        candidate_id=left.id,
                        peer_id=right.id,
                        candidate_sha256=left.sha256,
                        peer_sha256=right.sha256,
                        strategy_family=f"strategy-{left.id}",
                        heterogeneous=True,
                        similarities=("same target behavior",),
                        material_differences=("different solution artifacts",),
                        confidence=0.95,
                    )
                )
    return PatchSetManifest(
        schema_version=3,
        patch_set_id="set-1",
        task_key="fixture/demo",
        source="fixture",
        task_id="demo",
        status="ready",
        input_task_path="fixture/demo",
        baseline_tree_hash="a" * 40,
        original_instruction_hash="b" * 64,
        effective_instruction_hash="c" * 64,
        config_hash="d" * 64,
        generated_at="2026-07-15T00:00:00Z",
        tool_version="1.0.0",
        oracle=OracleEvidence(
            baseline=baseline,
            gold=gold,
            f2p_tests=oracle.f2p,
            p2p_tests=oracle.p2p,
            patch_results=patch_results,
            canonical_gold_provenance="fixture solution",
        ),
        solutions=solutions,
        diversity=DiversityEvidence(
            configured_invalid_signatures=3,
            behavior_matrix=behavior_matrix(
                invalid_behaviors,
                oracle.f2p,
            ),
        ),
        reviewer=ReviewerEvidence(
            reviewer=GeneratorProvenance(
                profile_id="reviewer",
                agent="review-agent",
                model="review-model",
                version="1",
                attempt=1,
            ),
            min_confidence=0.7,
            pairs=pairs,
            final_conclusion=True,
            reason="all same-label pairs are materially different",
        ),
    )


def test_manifest_enforces_fixed_balanced_patch_set() -> None:
    manifest = _manifest()
    assert len(manifest.solutions) == 10
    assert sum(
        patch.label is PatchLabel.ORACLE_VALID for patch in manifest.solutions
    ) == 5
    assert sum(
        patch.label is PatchLabel.ORACLE_INVALID for patch in manifest.solutions
    ) == 5
    with pytest.raises(ValueError, match="exactly 5 valid and 5 invalid"):
        PatchSetManifest.model_validate(
            {
                **manifest.model_dump(),
                "solutions": manifest.model_dump()["solutions"][:-1],
            }
        )


def test_manifest_rejects_reviewer_evidence_outside_current_contract() -> None:
    manifest = _manifest()
    payload = manifest.model_dump()
    payload["reviewer"]["pairs"][0]["heterogeneous"] = False
    with pytest.raises(ValueError, match="must be heterogeneous"):
        PatchSetManifest.model_validate(payload)

    payload = _manifest().model_dump()
    payload["reviewer"]["pairs"][0]["confidence"] = 0.6
    with pytest.raises(ValueError, match="confidence is below threshold"):
        PatchSetManifest.model_validate(payload)


def test_manifest_validates_hashes_and_generator_independence(tmp_path: Path) -> None:
    task_root = tmp_path / "sets" / "demo"
    manifest = _manifest()
    for patch in manifest.solutions:
        artifact = task_root / patch.path
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(f"#!/bin/sh\nprintf '%s\\n' {patch.id}\n")
    (task_root / "manifest.json").write_text(manifest.model_dump_json())

    loaded, _ = load_patch_set_manifest(tmp_path / "sets", "demo")
    gold = next(patch for patch in loaded.solutions if patch.is_gold)
    generated = [patch for patch in loaded.solutions if not patch.is_gold]
    assert all(
        patch.generator.profile_id != gold.generator.profile_id
        for patch in generated
    )
    assert all(
        patch.generator.agent != gold.generator.agent for patch in generated
    )


def test_manifest_load_uses_task_key_to_disambiguate_duplicate_leaf_ids(
    tmp_path: Path,
) -> None:
    patch_roots = {
        "from-alpha/shared": tmp_path / "sets" / "from-alpha" / "shared",
        "from-beta/shared": tmp_path / "sets" / "from-beta" / "shared",
    }
    for task_key, task_root in patch_roots.items():
        payload = _manifest().model_dump(mode="json")
        payload["patch_set_id"] = task_key.replace("/", "-")
        payload["task_key"] = task_key
        payload["source"] = task_key.split("/", 1)[0]
        payload["task_id"] = "shared"
        payload["input_task_path"] = task_key
        manifest = PatchSetManifest.model_validate(payload)
        for patch in manifest.solutions:
            artifact = task_root / patch.path
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text(f"#!/bin/sh\nprintf '%s\\n' {patch.id}\n")
        (task_root / "manifest.json").write_text(manifest.model_dump_json())

    with pytest.raises(ValueError, match="multiple patch-set manifests"):
        load_patch_set_manifest(tmp_path / "sets", "shared")

    loaded, root = load_patch_set_manifest(
        tmp_path / "sets", "shared", task_key="from-beta/shared"
    )

    assert loaded.task_key == "from-beta/shared"
    assert root == patch_roots["from-beta/shared"]


def test_manifest_load_uses_direct_task_key_path_without_recursive_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_key = "from-source/nested/demo"
    task_root = tmp_path / "sets" / "from-source" / "nested" / "demo"
    manifest = _manifest()
    payload = manifest.model_dump(mode="json")
    payload["task_key"] = task_key
    payload["source"] = "from-source"
    payload["task_id"] = "demo"
    payload["input_task_path"] = task_key
    manifest = PatchSetManifest.model_validate(payload)
    for patch in manifest.solutions:
        artifact = task_root / patch.path
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(f"#!/bin/sh\nprintf '%s\\n' {patch.id}\n")
    (task_root / "manifest.json").write_text(manifest.model_dump_json())

    def fail_rglob(_self: Path, _pattern: str):
        raise AssertionError("rglob should not run for an exact task_key hit")

    monkeypatch.setattr(Path, "rglob", fail_rglob)

    loaded, root = load_patch_set_manifest(
        tmp_path / "sets", "demo", task_key=task_key
    )

    assert loaded.task_key == task_key
    assert root == task_root


def test_manifest_load_accepts_task_key_relative_to_scoped_source_root(
    tmp_path: Path,
) -> None:
    task_id = "demo"
    task_key = f"from-source/{task_id}"
    task_root = tmp_path / "sets" / "from-source" / task_id
    manifest = _manifest()
    payload = manifest.model_dump(mode="json")
    payload["patch_set_id"] = "from-source-demo"
    payload["task_key"] = task_key
    payload["source"] = "from-source"
    payload["task_id"] = task_id
    payload["input_task_path"] = task_key
    manifest = PatchSetManifest.model_validate(payload)
    for patch in manifest.solutions:
        artifact = task_root / patch.path
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(f"#!/bin/sh\nprintf '%s\\n' {patch.id}\n")
    (task_root / "manifest.json").write_text(manifest.model_dump_json())

    loaded, root = load_patch_set_manifest(
        tmp_path / "sets" / "from-source",
        task_id,
        task_key=task_id,
    )

    assert loaded.task_key == task_key
    assert root == task_root


def test_manifest_rejects_sidecar_path_escape(tmp_path: Path) -> None:
    manifest = _manifest()
    payload = manifest.model_dump()
    payload["solutions"][1]["path"] = "../outside/solution.sh"
    with pytest.raises(
        ValueError,
        match="solution path must stay within patch-set root",
    ):
        PatchSetManifest.model_validate(payload)


def test_diversity_metrics_capture_behavioral_difference() -> None:
    assert (
        behavior_distance(
            {"a": "passed", "b": "failed"},
            {"a": "failed", "b": "failed"},
            ["a", "b"],
        )
        == 0.5
    )
    matrix = behavior_matrix(
        {
            "first": {"a": "passed", "b": "failed"},
            "second": {"a": "failed", "b": "failed"},
        },
        ["a", "b"],
    )
    assert matrix["first"]["first"] == 0.0
    assert matrix["first"]["second"] == matrix["second"]["first"] == 0.5
