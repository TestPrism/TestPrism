import pytest

from testprism.patches.diversity import (
    behavior_distance,
    behavior_matrix,
    invalid_signature_quota_met,
    pairwise_matrix,
    required_invalid_signature_count,
)


def test_behavior_distance_and_matrix_are_stable_and_complete() -> None:
    behaviors = {
        "patch-b": {"fix-2": "failed", "fix-1": "passed"},
        "patch-a": {"fix-1": "failed", "fix-2": "failed"},
    }

    assert behavior_distance(
        behaviors["patch-a"], behaviors["patch-b"], ["fix-2", "fix-1"]
    ) == 0.5
    assert behavior_matrix(behaviors, ["fix-2", "fix-1"]) == {
        "patch-a": {"patch-a": 0.0, "patch-b": 0.5},
        "patch-b": {"patch-a": 0.5, "patch-b": 0.0},
    }


def test_behavior_distance_rejects_missing_test_results() -> None:
    with pytest.raises(ValueError, match="missing test IDs"):
        behavior_distance({"fix": "passed"}, {}, ["fix"])


def test_pairwise_matrix_is_order_independent() -> None:
    values = {"z": 3, "a": 1, "m": 2}
    reordered = {key: values[key] for key in reversed(values)}

    def distance(left: int, right: int) -> float:
        return abs(left - right) / 2

    first = pairwise_matrix(values, distance)
    second = pairwise_matrix(reordered, distance)

    assert first == second
    assert list(first) == ["a", "m", "z"]
    assert all(list(row) == ["a", "m", "z"] for row in first.values())


@pytest.mark.parametrize(
    ("configured", "f2p_count", "expected"),
    [(3, 0, 0), (3, 1, 1), (3, 2, 3), (5, 2, 3), (3, 10, 3)],
)
def test_required_invalid_signatures_respect_reachable_upper_bound(
    configured: int, f2p_count: int, expected: int
) -> None:
    assert required_invalid_signature_count(configured, f2p_count) == expected


def test_invalid_signature_quota_counts_distinct_signatures() -> None:
    assert invalid_signature_quota_met(["a", "a", "b", "c"], 3, 2)
    assert not invalid_signature_quota_met(["a", "a", "b"], 3, 2)


@pytest.mark.parametrize("configured,f2p_count", [(-1, 1), (1, -1)])
def test_invalid_signature_requirement_rejects_negative_values(
    configured: int, f2p_count: int
) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        required_invalid_signature_count(configured, f2p_count)
