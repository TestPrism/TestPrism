import copy
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from testprism.patches.diversity import (
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
)
from testprism.patches.oracle import (
    RunStatus,
    build_oracle,
    invalid_behavior_signature,
)
from testprism.patches.oracle import TestRunResult as RunResult


def _diff(index: int) -> str:
    return f"#!/bin/sh\nprintf '%s\\n' strategy-{index} > /tmp/result-{index}\n"


def _manifest(root: Path) -> PatchSetManifest:
    baseline = RunResult(
        run_status=RunStatus.COMPLETED,
        tests={"fix-a": "failed", "fix-b": "failed", "stable": "passed"},
    )
    gold = RunResult(
        run_status=RunStatus.COMPLETED,
        tests={"fix-a": "passed", "fix-b": "passed", "stable": "passed"},
    )
    oracle = build_oracle(baseline, gold)
    entries = []
    results = {}
    diffs = {}
    invalid_vectors = [
        ("passed", "failed"),
        ("failed", "passed"),
        ("failed", "failed"),
        ("passed", "failed"),
        ("failed", "passed"),
    ]
    for index in range(10):
        patch_id = f"patch-{index + 1:02d}"
        relative = f"solutions/{patch_id}/solution.sh"
        content = _diff(index + 1)
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        label = (
            PatchLabel.ORACLE_VALID if index < 5 else PatchLabel.ORACLE_INVALID
        )
        run = gold
        signature = None
        if label is PatchLabel.ORACLE_INVALID:
            first, second = invalid_vectors[index - 5]
            run = RunResult(
                run_status="completed",
                tests={"fix-a": first, "fix-b": second, "stable": "passed"},
            )
            signature = invalid_behavior_signature(oracle, run)
        results[patch_id] = PatchOracleEvidence(
            result=run, invalid_signature=signature
        )
        diffs[patch_id] = content
        entries.append(
            PatchEntry(
                id=patch_id,
                label=label,
                is_gold=index == 0,
                path=relative,
                sha256=hashlib.sha256(content.encode()).hexdigest(),
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
    for group in (entries[:5], entries[5:]):
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
                        similarities=["both address the same issue"],
                        material_differences=["different files and strategy"],
                        confidence=0.95,
                    )
                )

    invalid_entries = entries[5:]
    invalid_behaviors = {
        entry.id: results[entry.id].result.tests for entry in invalid_entries
    }
    return PatchSetManifest(
        schema_version=3,
        patch_set_id="set-001",
        task_key="from-source/task-1",
        source="from-source",
        task_id="task-1",
        status="ready",
        input_task_path="from-source/task-1",
        baseline_tree_hash="a" * 40,
        original_instruction_hash="b" * 64,
        effective_instruction_hash="c" * 64,
        config_hash="d" * 64,
        generated_at="2026-07-15T00:00:00Z",
        tool_version="1.0.0",
        oracle=OracleEvidence(
            baseline=baseline,
            gold=gold,
            f2p_tests=["fix-a", "fix-b"],
            p2p_tests=["stable"],
            patch_results=results,
            canonical_gold_provenance="task solution",
        ),
        solutions=entries,
        diversity=DiversityEvidence(
            configured_invalid_signatures=3,
            behavior_matrix=behavior_matrix(
                invalid_behaviors, ["fix-a", "fix-b"]
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


def _data(manifest: PatchSetManifest) -> dict:
    return manifest.model_dump(mode="json")


def test_ready_manifest_round_trips_and_validates_files(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)

    restored = PatchSetManifest.model_validate_json(manifest.model_dump_json())
    restored.validate_files(tmp_path)

    assert restored == manifest
    assert json.loads(restored.model_dump_json())["schema_version"] == 3


def test_task_key_allows_recursive_dataset_relative_path(tmp_path: Path) -> None:
    data = _data(_manifest(tmp_path))
    data["task_key"] = "from-source/group/nested/task-1"

    manifest = PatchSetManifest.model_validate(data)

    assert manifest.task_key == "from-source/group/nested/task-1"


@pytest.mark.parametrize(
    ("task_key", "message"),
    [
        ("/from-source/task-1", "safe relative POSIX path"),
        ("from-source/../task-1", "safe relative POSIX path"),
        ("from-source", "at least source and task ID"),
        ("other/group/task-1", "first task_key segment must equal source"),
        ("from-source/group/other", "last task_key segment must equal task_id"),
    ],
)
def test_task_key_rejects_unsafe_or_inconsistent_paths(
    tmp_path: Path, task_key: str, message: str
) -> None:
    data = _data(_manifest(tmp_path))
    data["task_key"] = task_key

    with pytest.raises(ValidationError, match=message):
        PatchSetManifest.model_validate(data)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda data: data["solutions"].pop(), "exactly 5 valid and 5 invalid"),
        (
            lambda data: data["solutions"][1].update(is_gold=True),
            "exactly one gold",
        ),
        (
            lambda data: data["solutions"][1].update(id=data["solutions"][0]["id"]),
            "solution IDs must be unique",
        ),
        (
            lambda data: data["solutions"][1].update(
                sha256=data["solutions"][0]["sha256"]
            ),
            "solution SHA-256 values must be unique",
        ),
        (
            lambda data: data["solutions"][1].update(path="../outside/solution.sh"),
            "path must stay within patch-set root",
        ),
        (
            lambda data: data["reviewer"]["pairs"].pop(),
            "reviewer evidence must cover every same-label peer pair",
        ),
        (
            lambda data: [
                data["oracle"]["patch_results"][entry["id"]].update(
                    invalid_signature="f" * 64
                )
                for entry in data["solutions"][5:]
            ],
            "invalid behavior signature quota is not met",
        ),
    ],
)
def test_rejects_invalid_ready_contract(tmp_path: Path, mutate, message: str) -> None:
    data = _data(_manifest(tmp_path))
    mutate(data)

    with pytest.raises(ValidationError, match=message):
        PatchSetManifest.model_validate(data)


def test_validate_files_recomputes_hash(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    (tmp_path / manifest.solutions[1].path).write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        manifest.validate_files(tmp_path)

def test_validate_files_recomputes_invalid_signature_and_behavior_matrix(
    tmp_path: Path,
) -> None:
    data = _data(_manifest(tmp_path))
    invalid_id = data["solutions"][5]["id"]
    data["oracle"]["patch_results"][invalid_id]["invalid_signature"] = "f" * 64
    forged = PatchSetManifest.model_validate(data)
    with pytest.raises(ValueError, match="invalid behavior signature mismatch"):
        forged.validate_files(tmp_path)

    data = _data(_manifest(tmp_path))
    invalid_ids = [entry["id"] for entry in data["solutions"][5:]]
    data["diversity"]["behavior_matrix"][invalid_ids[0]][invalid_ids[1]] = 0.123
    forged = PatchSetManifest.model_validate(data)
    with pytest.raises(ValueError, match="behavior matrix mismatch"):
        forged.validate_files(tmp_path)


def test_validate_files_rejects_runtime_symlink_escape(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    outside = tmp_path.parent / "outside/solution.sh"
    outside.parent.mkdir(exist_ok=True)
    outside.write_text(_diff(99), encoding="utf-8")
    artifact = tmp_path / manifest.solutions[0].path
    artifact.unlink()
    artifact.symlink_to(outside)

    with pytest.raises(ValueError, match="escapes solution-set root"):
        manifest.validate_files(tmp_path)


def test_rejects_secret_bearing_serialized_values(tmp_path: Path) -> None:
    data = _data(_manifest(tmp_path))
    data["tool_version"] = "Bearer abcdefghijklmnopqrstuvwxyz"

    with pytest.raises(ValidationError, match="secret-like value"):
        PatchSetManifest.model_validate(data)


def test_reviewer_pair_hashes_must_match_entries(tmp_path: Path) -> None:
    data = copy.deepcopy(_data(_manifest(tmp_path)))
    data["reviewer"]["pairs"][0]["peer_sha256"] = "0" * 64

    with pytest.raises(ValidationError, match="reviewer peer hash mismatch"):
        PatchSetManifest.model_validate(data)
