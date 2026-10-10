"""Oracle evidence models and deterministic downstream revalidation."""

from __future__ import annotations

import hashlib
import json
from enum import Enum, StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _OracleModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TestStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"


class RunStatus(StrEnum):
    COMPLETED = "completed"
    TIMEOUT = "timeout"
    PARSE_ERROR = "parse_error"
    COMMAND_ERROR = "command_error"
    CONTAINER_ERROR = "container_error"
    EXECUTION_ERROR = "execution_error"


class CandidateLabel(StrEnum):
    ORACLE_VALID = "oracle_valid"
    ORACLE_INVALID = "oracle_invalid"


SUITE_WIDE_TEST_IDS = (
    "__testprism_suite_wide_1__",
    "__testprism_suite_wide_2__",
)


class ClassificationReason(StrEnum):
    VALID = "valid"
    INVALID = "invalid"
    BASELINE_EQUIVALENT = "baseline_equivalent"
    P2P_REGRESSION = "p2p_regression"
    TEST_SET_MISMATCH = "test_set_mismatch"
    INFRA_ERROR = "infra_error"


class OracleValidationError(ValueError):
    """Raised when baseline/gold evidence cannot establish a sound oracle."""


class TestRunResult(_OracleModel):
    """A test run whose infrastructure status is separate from assertions."""

    run_status: RunStatus
    tests: dict[str, TestStatus] = Field(default_factory=dict)
    exit_code: int | None = None
    log_path: str | None = None
    output_excerpt: str = ""
    output_truncated: bool = False
    error: str | None = None

    @field_validator("tests", mode="before")
    @classmethod
    def _coerce_parser_statuses(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        return {
            test_id: status.value if isinstance(status, Enum) else status
            for test_id, status in value.items()
        }

    @field_validator("tests")
    @classmethod
    def _validate_test_ids(cls, value: dict[str, TestStatus]) -> dict[str, TestStatus]:
        empty_ids = [test_id for test_id in value if not test_id.strip()]
        if empty_ids:
            raise ValueError("test IDs must not be empty")
        return value


class OracleTransitions(_OracleModel):
    """Stable baseline-to-gold transition sets."""

    f2p: tuple[str, ...]
    p2p: tuple[str, ...]
    p2f: tuple[str, ...]


class OracleDefinition(_OracleModel):
    """Validated baseline/gold evidence used to classify candidates."""

    baseline: TestRunResult
    gold: TestRunResult
    test_ids: tuple[str, ...]
    f2p: tuple[str, ...]
    p2p: tuple[str, ...]
    p2f: tuple[str, ...]


def suite_wide_oracle() -> OracleDefinition:
    """Return a prevalidated oracle whose candidate test set is discovered later."""

    baseline = TestRunResult(
        run_status=RunStatus.COMPLETED,
        tests={test_id: TestStatus.FAILED for test_id in SUITE_WIDE_TEST_IDS},
    )
    gold = TestRunResult(
        run_status=RunStatus.COMPLETED,
        tests={test_id: TestStatus.PASSED for test_id in SUITE_WIDE_TEST_IDS},
    )
    return build_oracle(baseline, gold)


def is_suite_wide_oracle(oracle: OracleDefinition) -> bool:
    """Whether the oracle should classify the whole observed test suite."""

    return oracle.test_ids == SUITE_WIDE_TEST_IDS and oracle.f2p == SUITE_WIDE_TEST_IDS


class CandidateAssessment(_OracleModel):
    """An oracle label or a deterministic rejection reason."""

    label: CandidateLabel | None
    reason: ClassificationReason
    detail: str
    invalid_signature: str | None = None


def _require_completed(result: TestRunResult, name: str) -> None:
    if result.run_status is not RunStatus.COMPLETED:
        raise OracleValidationError(
            f"{name} run is not completed: {result.run_status.value}"
        )


def derive_transitions(
    baseline: TestRunResult, gold: TestRunResult
) -> OracleTransitions:
    """Derive sorted F2P/P2P/P2F sets from complete, stable test results."""

    _require_completed(baseline, "baseline")
    _require_completed(gold, "gold")
    baseline_ids = set(baseline.tests)
    gold_ids = set(gold.tests)
    if baseline_ids != gold_ids:
        missing_from_gold = sorted(baseline_ids - gold_ids)
        added_in_gold = sorted(gold_ids - baseline_ids)
        raise OracleValidationError(
            "baseline and gold test IDs differ; "
            f"missing_from_gold={missing_from_gold}, added_in_gold={added_in_gold}"
        )

    f2p: list[str] = []
    p2p: list[str] = []
    p2f: list[str] = []
    for test_id in sorted(baseline_ids):
        before = baseline.tests[test_id]
        after = gold.tests[test_id]
        if before is TestStatus.FAILED and after is TestStatus.PASSED:
            f2p.append(test_id)
        elif before is TestStatus.PASSED and after is TestStatus.PASSED:
            p2p.append(test_id)
        elif before is TestStatus.PASSED and after is TestStatus.FAILED:
            p2f.append(test_id)
    return OracleTransitions(f2p=tuple(f2p), p2p=tuple(p2p), p2f=tuple(p2f))


def build_oracle(baseline: TestRunResult, gold: TestRunResult) -> OracleDefinition:
    """Validate baseline/gold evidence and establish a non-empty F2P oracle."""

    transitions = derive_transitions(baseline, gold)
    failed_gold = sorted(
        test_id for test_id, status in gold.tests.items() if status is TestStatus.FAILED
    )
    if failed_gold:
        raise OracleValidationError(
            f"gold run must pass every test; failed={failed_gold}"
        )
    if not transitions.f2p:
        raise OracleValidationError("oracle F2P must not be empty")

    return OracleDefinition(
        baseline=baseline,
        gold=gold,
        test_ids=tuple(sorted(baseline.tests)),
        f2p=transitions.f2p,
        p2p=transitions.p2p,
        p2f=transitions.p2f,
    )


def _candidate_test_set_error(
    oracle: OracleDefinition, candidate: TestRunResult
) -> str | None:
    expected = set(oracle.test_ids)
    actual = set(candidate.tests)
    if expected == actual:
        return None
    return (
        f"candidate test IDs differ; missing={sorted(expected - actual)}, "
        f"unexpected={sorted(actual - expected)}"
    )


def invalid_behavior_signature(
    oracle: OracleDefinition, candidate: TestRunResult
) -> str:
    """Hash the sorted full pass/fail vector into a stable behavior signature."""

    if candidate.run_status is not RunStatus.COMPLETED:
        raise ValueError("invalid behavior signature requires a completed run")
    if is_suite_wide_oracle(oracle):
        if not candidate.tests:
            raise ValueError("suite-wide invalid signature requires parsed tests")
        vector = [
            [test_id, candidate.tests[test_id].value]
            for test_id in sorted(candidate.tests)
        ]
        canonical = json.dumps(vector, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    mismatch = _candidate_test_set_error(oracle, candidate)
    if mismatch is not None:
        raise ValueError(mismatch)

    vector = [[test_id, candidate.tests[test_id].value] for test_id in oracle.test_ids]
    canonical = json.dumps(vector, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def classify_candidate(
    oracle: OracleDefinition,
    candidate: TestRunResult,
    *,
    require_f2p_progress: bool = True,
) -> CandidateAssessment:
    """Classify a candidate, rejecting infrastructure and test-set errors."""

    if candidate.run_status is not RunStatus.COMPLETED:
        return CandidateAssessment(
            label=None,
            reason=ClassificationReason.INFRA_ERROR,
            detail=f"candidate run is not completed: {candidate.run_status.value}",
        )

    if is_suite_wide_oracle(oracle):
        if not candidate.tests:
            return CandidateAssessment(
                label=None,
                reason=ClassificationReason.TEST_SET_MISMATCH,
                detail="candidate produced no parseable test results",
            )
        failed_tests = tuple(
            test_id
            for test_id in sorted(candidate.tests)
            if candidate.tests[test_id] is TestStatus.FAILED
        )
        if not failed_tests:
            return CandidateAssessment(
                label=CandidateLabel.ORACLE_VALID,
                reason=ClassificationReason.VALID,
                detail="candidate passes every observed test",
            )
        signature = invalid_behavior_signature(oracle, candidate)
        return CandidateAssessment(
            label=CandidateLabel.ORACLE_INVALID,
            reason=ClassificationReason.INVALID,
            detail="candidate test run completed but at least one observed test fails",
            invalid_signature=signature,
        )

    mismatch = _candidate_test_set_error(oracle, candidate)
    if mismatch is not None:
        return CandidateAssessment(
            label=None,
            reason=ClassificationReason.TEST_SET_MISMATCH,
            detail=mismatch,
        )

    passed_f2p = tuple(
        test_id
        for test_id in oracle.f2p
        if candidate.tests[test_id] is TestStatus.PASSED
    )
    failed_tests = tuple(
        test_id
        for test_id in oracle.test_ids
        if candidate.tests[test_id] is TestStatus.FAILED
    )
    if not failed_tests:
        return CandidateAssessment(
            label=CandidateLabel.ORACLE_VALID,
            reason=ClassificationReason.VALID,
            detail="candidate passes every oracle test",
        )

    if require_f2p_progress and len(oracle.f2p) > 1 and not passed_f2p:
        return CandidateAssessment(
            label=None,
            reason=ClassificationReason.BASELINE_EQUIVALENT,
            detail="candidate fixes none of the multiple F2P tests",
        )

    signature = invalid_behavior_signature(oracle, candidate)
    return CandidateAssessment(
        label=CandidateLabel.ORACLE_INVALID,
        reason=ClassificationReason.INVALID,
        detail="candidate test run completed but at least one oracle test still fails",
        invalid_signature=signature,
    )
