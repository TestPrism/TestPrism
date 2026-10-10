from __future__ import annotations

from pathlib import Path

from scripts.generation import audit_hardest500_test_scope as audit


def test_family_contract_has_the_five_expected_counts() -> None:
    assert {
        name: spec.expected_count for name, spec in audit.FAMILY_CONTRACT.items()
    } == {
        "from-swe-bench-live": 50,
        "from-swe-bench-multimodal": 16,
        "from-swepolybench": 39,
        "from-swesmith": 20,
        "from-swe-rebench": 2,
    }


def test_real_inventory_is_exactly_127_without_scope_violations() -> None:
    report = audit.audit_repository(audit.DEFAULT_TASKS_ROOT)

    assert [family.actual_count for family in report.families] == [50, 16, 39, 20, 2]
    assert sum(family.actual_count for family in report.families) == 127
    assert report.ok
    assert all(not family.violations for family in report.families)


def test_detects_broad_commands_for_each_high_risk_runner_kind() -> None:
    cases = {
        "from-swe-bench-live": "pytest -rA",
        "from-swesmith": "pytest --disable-warnings --verbose",
        "from-swe-rebench": "python3 -m pytest -rA tests",
        "from-swe-bench-multimodal": "NODE_OPTIONS=x ./node_modules/.bin/jest --no-color",
        "from-swepolybench": "npm run test -- --reporter json --exit",
    }

    for family, script in cases.items():
        reasons = audit.audit_script(script, family)
        assert reasons, (family, reasons)
        assert any("broad" in reason for reason in reasons), (family, reasons)


def test_availability_checks_are_not_test_commands_but_bare_jest_is() -> None:
    availability = (
        "test -x ./node_modules/.bin/jest\n"
        "command -v ./node_modules/.bin/grunt\n"
        "[[ -x ./node_modules/.bin/mocha ]]\n"
    )
    assert audit.audit_script(availability, "from-swe-bench-multimodal") == (
        "no scoped test command found",
    )

    assert audit.audit_script(
        "test -x ./node_modules/.bin/jest\n./node_modules/.bin/jest --no-color",
        "from-swe-bench-multimodal",
    ) == ("broad jest invocation at line 2: no explicit path",)


def test_allows_explicit_file_and_nodeid_selectors() -> None:
    scripts = {
        "pytest tests/test_api.py::test_one -q": "from-swe-bench-live",
        "python -m pytest src/pkg/test_math.py::test_add": "from-swesmith",
        "./node_modules/.bin/jest --runTestsByPath packages/a/a.test.js": "from-swe-bench-multimodal",
        "npm run test -- test/runtime/sample --reporter json": "from-swepolybench",
        "npm run test -- --grep 'specific regression'": "from-swepolybench",
        "grunt test --grep 'specific regression'": "from-swe-bench-multimodal",
        "mocha --grep 'specific regression'": "from-swe-bench-multimodal",
    }

    for script, family in scripts.items():
        assert audit.audit_script(script, family) == (), (family, script)


def test_empty_selector_is_rejected_but_fail_closed_guard_is_allowed() -> None:
    empty = 'SELECTORS=(); pytest "${SELECTORS[@]}"'
    reasons = audit.audit_script(empty, "from-swe-bench-live")
    assert any("empty selector" in reason for reason in reasons), reasons

    guarded = (
        'SELECTORS=(tests/test_api.py::test_one)\n'
        'if [ "${#SELECTORS[@]}" -eq 0 ]; then exit 1; fi\n'
        'pytest "${SELECTORS[@]}"'
    )
    assert audit.audit_script(guarded, "from-swe-bench-live") == ()


def test_broad_fallback_is_reported_even_after_a_narrow_attempt() -> None:
    script = "pytest tests/test_api.py::test_one || pytest"
    reasons = audit.audit_script(script, "from-swe-bench-live")
    assert any("fallback" in reason for reason in reasons), reasons


def test_family_count_mismatch_is_a_failure(tmp_path: Path) -> None:
    task = tmp_path / "from-swe-bench-live" / "only-task"
    task.mkdir(parents=True)
    (task / "run-tests.sh").write_text(
        "pytest tests/test_api.py::test_one\n", encoding="utf-8"
    )

    result = audit.audit_family(tmp_path, "from-swe-bench-live")

    assert result.actual_count == 1
    assert not result.ok
    assert any(
        violation.task_id == "<family>"
        and "expected 50, found 1" in violation.reason
        for violation in result.violations
    )
