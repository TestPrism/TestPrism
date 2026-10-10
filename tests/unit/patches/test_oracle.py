from dataclasses import dataclass

import pytest

from testprism.parsers.base_parser import UnitTestStatus
from testprism.patches.oracle import (
    CandidateLabel,
    ClassificationReason,
    OracleValidationError,
    RunStatus,
    build_oracle,
    classify_candidate,
    derive_transitions,
    invalid_behavior_signature,
    is_suite_wide_oracle,
    suite_wide_oracle,
)
from testprism.patches.oracle import (
    TestRunResult as RunResult,
)


def _run(
    tests: dict[str, str | UnitTestStatus],
    status: RunStatus | str = RunStatus.COMPLETED,
) -> RunResult:
    return RunResult(run_status=status, tests=tests)


def _oracle():
    return build_oracle(
        _run({"fix-b": "failed", "stable": "passed", "fix-a": "failed"}),
        _run({"stable": "passed", "fix-a": "passed", "fix-b": "passed"}),
    )


def test_derives_sorted_f2p_p2p_and_p2f_sets() -> None:
    transitions = derive_transitions(
        _run({"z-f2p": "failed", "a-p2p": "passed", "m-p2f": "passed"}),
        _run({"m-p2f": "failed", "z-f2p": "passed", "a-p2p": "passed"}),
    )

    assert transitions.f2p == ("z-f2p",)
    assert transitions.p2p == ("a-p2p",)
    assert transitions.p2f == ("m-p2f",)


@pytest.mark.parametrize(
    ("baseline", "gold", "message"),
    [
        (
            _run({"fix": "failed", "stable": "passed"}),
            _run({"fix": "passed", "stable": "failed"}),
            "gold run must pass every test",
        ),
        (
            _run({"fix": "failed"}),
            _run({"fix": "passed", "new-test": "passed"}),
            "test IDs differ",
        ),
        (
            _run({"stable": "passed"}),
            _run({"stable": "passed"}),
            "F2P must not be empty",
        ),
        (
            _run({}, RunStatus.TIMEOUT),
            _run({}),
            "baseline run is not completed",
        ),
        (
            _run({"fix": "failed"}),
            _run({}, RunStatus.PARSE_ERROR),
            "gold run is not completed",
        ),
    ],
)
def test_rejects_invalid_oracle_inputs(
    baseline: RunResult, gold: RunResult, message: str
) -> None:
    with pytest.raises(OracleValidationError, match=message):
        build_oracle(baseline, gold)


@dataclass(frozen=True)
class ClassificationCase:
    name: str
    candidate: RunResult
    expected_label: CandidateLabel | None
    expected_reason: ClassificationReason


CLASSIFICATION_CASES = [
    ClassificationCase(
        "valid",
        _run({"fix-a": "passed", "fix-b": "passed", "stable": "passed"}),
        CandidateLabel.ORACLE_VALID,
        ClassificationReason.VALID,
    ),
    ClassificationCase(
        "invalid-near-miss",
        _run({"fix-a": "passed", "fix-b": "failed", "stable": "passed"}),
        CandidateLabel.ORACLE_INVALID,
        ClassificationReason.INVALID,
    ),
    ClassificationCase(
        "baseline-equivalent",
        _run({"fix-a": "failed", "fix-b": "failed", "stable": "passed"}),
        None,
        ClassificationReason.BASELINE_EQUIVALENT,
    ),
    ClassificationCase(
        "p2p-regression",
        _run({"fix-a": "passed", "fix-b": "failed", "stable": "failed"}),
        CandidateLabel.ORACLE_INVALID,
        ClassificationReason.INVALID,
    ),
    ClassificationCase(
        "missing-test",
        _run({"fix-a": "passed", "stable": "passed"}),
        None,
        ClassificationReason.TEST_SET_MISMATCH,
    ),
    ClassificationCase(
        "timeout",
        _run({}, RunStatus.TIMEOUT),
        None,
        ClassificationReason.INFRA_ERROR,
    ),
    ClassificationCase(
        "parse-error",
        _run({}, RunStatus.PARSE_ERROR),
        None,
        ClassificationReason.INFRA_ERROR,
    ),
]


@pytest.mark.parametrize("case", CLASSIFICATION_CASES, ids=lambda case: case.name)
def test_classifies_candidate_states(case: ClassificationCase) -> None:
    assessment = classify_candidate(_oracle(), case.candidate)

    assert assessment.label is case.expected_label
    assert assessment.reason is case.expected_reason
    assert (assessment.invalid_signature is not None) == (
        case.expected_label is CandidateLabel.ORACLE_INVALID
    )


def test_baseline_equivalent_can_be_invalid_when_progress_policy_disabled() -> None:
    assessment = classify_candidate(
        _oracle(),
        _run({"fix-a": "failed", "fix-b": "failed", "stable": "passed"}),
        require_f2p_progress=False,
    )

    assert assessment.label is CandidateLabel.ORACLE_INVALID
    assert assessment.reason is ClassificationReason.INVALID


def test_single_f2p_automatically_relaxes_progress_requirement() -> None:
    oracle = build_oracle(
        _run({"fix": "failed", "stable": "passed"}),
        _run({"fix": "passed", "stable": "passed"}),
    )

    assessment = classify_candidate(
        oracle, _run({"fix": "failed", "stable": "passed"})
    )

    assert assessment.label is CandidateLabel.ORACLE_INVALID


def test_invalid_behavior_signature_is_stable_across_mapping_order() -> None:
    oracle = _oracle()
    first = _run({"fix-a": "passed", "fix-b": "failed", "stable": "passed"})
    reordered = _run(
        {"stable": UnitTestStatus.PASSED, "fix-b": "failed", "fix-a": "passed"}
    )
    different = _run(
        {"fix-a": "failed", "fix-b": "passed", "stable": "passed"}
    )

    assert invalid_behavior_signature(oracle, first) == invalid_behavior_signature(
        oracle, reordered
    )
    assert invalid_behavior_signature(oracle, first) != invalid_behavior_signature(
        oracle, different
    )
    assert invalid_behavior_signature(oracle, first) != invalid_behavior_signature(
        oracle, _run({"fix-a": "passed", "fix-b": "failed", "stable": "failed"})
    )


def test_suite_wide_oracle_classifies_observed_test_suite() -> None:
    oracle = suite_wide_oracle()

    valid = classify_candidate(oracle, _run({"alpha": "passed", "beta": "passed"}))
    invalid = classify_candidate(
        oracle,
        _run({"alpha": "passed", "beta": "failed"}),
    )

    assert is_suite_wide_oracle(oracle)
    assert valid.label is CandidateLabel.ORACLE_VALID
    assert invalid.label is CandidateLabel.ORACLE_INVALID
    assert invalid.invalid_signature == invalid_behavior_signature(
        oracle,
        _run({"beta": "failed", "alpha": "passed"}),
    )
