"""Deterministic behavioral diversity metrics for patch sets."""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterable, Mapping
from enum import Enum
from typing import TypeVar

_T = TypeVar("_T")
DistanceMatrix = dict[str, dict[str, float]]


def _normalized_status(value: object) -> object:
    return value.value if isinstance(value, Enum) else value


def behavior_distance(
    left: Mapping[str, object],
    right: Mapping[str, object],
    test_ids: Iterable[str],
) -> float:
    """Return normalized Hamming distance over the requested F2P tests."""

    ordered_ids = tuple(sorted(set(test_ids)))
    missing_left = [test_id for test_id in ordered_ids if test_id not in left]
    missing_right = [test_id for test_id in ordered_ids if test_id not in right]
    if missing_left or missing_right:
        raise ValueError(
            "behavior vectors have missing test IDs; "
            f"left={missing_left}, right={missing_right}"
        )
    if not ordered_ids:
        return 0.0
    mismatches = sum(
        _normalized_status(left[test_id]) != _normalized_status(right[test_id])
        for test_id in ordered_ids
    )
    return round(mismatches / len(ordered_ids), 6)


def pairwise_matrix(
    values: Mapping[str, _T], distance_fn: Callable[[_T, _T], float]
) -> DistanceMatrix:
    """Build a sorted, symmetric all-pairs matrix independent of mapping order."""

    ids = sorted(values)
    matrix = {patch_id: {peer_id: 0.0 for peer_id in ids} for patch_id in ids}
    for index, left_id in enumerate(ids):
        for right_id in ids[index + 1 :]:
            distance = round(float(distance_fn(values[left_id], values[right_id])), 6)
            if not 0.0 <= distance <= 1.0:
                raise ValueError("pairwise distance must be in [0, 1]")
            matrix[left_id][right_id] = distance
            matrix[right_id][left_id] = distance
    return matrix


def behavior_matrix(
    behaviors: Mapping[str, Mapping[str, object]], test_ids: Iterable[str]
) -> DistanceMatrix:
    """Build a deterministic pairwise behavior-distance matrix."""

    ordered_ids = tuple(sorted(set(test_ids)))
    return pairwise_matrix(
        behaviors,
        lambda left, right: behavior_distance(left, right, ordered_ids),
    )


def required_invalid_signature_count(configured: int, f2p_count: int) -> int:
    """Return ``min(configured, 2**f2p_count - 1)``."""

    if configured < 0 or f2p_count < 0:
        raise ValueError("configured quota and F2P count must be non-negative")
    return min(configured, (1 << f2p_count) - 1)


def invalid_signature_quota_met(
    signatures: Collection[str], configured: int, f2p_count: int
) -> bool:
    """Whether distinct invalid behavior signatures satisfy the reachable quota."""

    required = required_invalid_signature_count(configured, f2p_count)
    return len(set(signatures)) >= required
