from textwrap import dedent

import pytest

from testprism.parsers.pytest_parser import (
    PytestParser,
    PytestTestStatus,
    UnitTestStatus,
)


@pytest.fixture
def parser() -> PytestParser:
    return PytestParser()


def test_parse_result_line(parser: PytestParser):
    # Test successful parsing
    test_name, status = parser._parse_result_line(
        "FAILED test_something::test_case - additional info"
    )
    assert test_name == "test_case"
    assert status == PytestTestStatus.FAILED

    # Test passed status
    test_name, status = parser._parse_result_line("PASSED test_module::test_success")
    assert test_name == "test_success"
    assert status == PytestTestStatus.PASSED

    # Test unittest-style node IDs keep class-qualified name
    test_name, status = parser._parse_result_line(
        "FAILED test_module.py::Suite::test_case - additional info"
    )
    assert test_name == "Suite::test_case"
    assert status == PytestTestStatus.FAILED

    # Test invalid input
    test_name, status = parser._parse_result_line("INVALID test_case")
    assert test_name == ""
    assert status == PytestTestStatus.UNKNOWN

    # Test empty input
    test_name, status = parser._parse_result_line("")
    assert test_name == ""
    assert status == PytestTestStatus.UNKNOWN


def test_parse_test_results(parser: PytestParser):
    test_output = dedent("""
        ====================== short test summary info ======================
        FAILED test_module.py::test_failure - assert 1 == 2
        PASSED test_module.py::test_success
        SKIPPED test_module.py::test_skip
        XFAIL test_module.py::test_expected_failure
        XPASS test_module.py::test_unexpected_pass
        ERROR test_module.py::test_error
    """)

    results = parser.parse(test_output)

    assert results == {
        "test_failure": UnitTestStatus.FAILED,
        "test_success": UnitTestStatus.PASSED,
        "test_skip": UnitTestStatus.PASSED,
        "test_expected_failure": UnitTestStatus.PASSED,
        "test_unexpected_pass": UnitTestStatus.FAILED,
        "test_error": UnitTestStatus.FAILED,
    }


def test_pytest_test_status_conversion():
    assert PytestTestStatus.PASSED.to_test_status() == UnitTestStatus.PASSED
    assert PytestTestStatus.FAILED.to_test_status() == UnitTestStatus.FAILED
    assert PytestTestStatus.SKIPPED.to_test_status() == UnitTestStatus.PASSED
    assert PytestTestStatus.XFAIL.to_test_status() == UnitTestStatus.PASSED
    assert PytestTestStatus.XPASS.to_test_status() == UnitTestStatus.FAILED
    assert PytestTestStatus.ERROR.to_test_status() == UnitTestStatus.FAILED
    assert PytestTestStatus.UNKNOWN.to_test_status() == UnitTestStatus.FAILED


def test_clean_line(parser: PytestParser):
    cleaned = parser._clean_line("FAILED test_something::test_case - assert 1 == 2")
    assert cleaned == "FAILED test_something::test_case"

    cleaned = parser._clean_line("PASSED test_something::test_case")
    assert cleaned == "PASSED test_something::test_case"


def test_parse_verbose_output_with_all_tests_passing_and_wrapper_summary(
    parser: PytestParser,
):
    test_output = dedent("""
        ============================= test session starts ==============================
        collected 2 items

        bowling_test.py::BowlingTest::test_scores_strike PASSED                  [ 50%]
        bowling_test.py::BowlingTest::test_scores_spare PASSED                   [100%]

        ============================== 2 passed in 0.04s ===============================
        === TEST EXECUTION COMPLETED ===
        Exit code: 0
        =============== short test summary info ===============
        PASSED aider_polyglot_test
    """)

    results = parser.parse(test_output)

    assert results == {
        "BowlingTest::test_scores_strike": UnitTestStatus.PASSED,
        "BowlingTest::test_scores_spare": UnitTestStatus.PASSED,
    }


def test_parse_strips_ansi_sequences_from_verbose_and_summary_output(
    parser: PytestParser,
):
    test_output = (
        "\x1b[32mbowling_test.py::BowlingTest::test_scores_strike PASSED\x1b[0m\n"
        "\x1b[36m================ short test summary info ================\x1b[0m\n"
        "\x1b[31mFAILED bowling_test.py::BowlingTest::test_scores_spare"
        " - AssertionError: nope\x1b[0m\n"
    )

    results = parser.parse(test_output)

    assert results == {
        "BowlingTest::test_scores_strike": UnitTestStatus.PASSED,
        "BowlingTest::test_scores_spare": UnitTestStatus.FAILED,
    }


def test_parse_verbose_output_with_skipped_reason(parser: PytestParser):
    test_output = dedent("""
        ============================= test session starts ==============================
        collected 2 items

        ../tests/test_outputs.py::test_service_process_running FAILED           [ 50%]
        ../tests/test_outputs.py::test_service_responding SKIPPED (service is offline) [100%]

        =========================== short test summary info ============================
        SKIPPED [1] ../tests/test_outputs.py:48: service is offline
        FAILED ../tests/test_outputs.py::test_service_process_running - AssertionError: nope
    """)

    results = parser.parse(test_output)

    assert results == {
        "test_service_process_running": UnitTestStatus.FAILED,
        "test_service_responding": UnitTestStatus.PASSED,
    }


def test_parse_empty_content(parser: PytestParser):
    test_output = ""

    with pytest.raises(ValueError, match="No short test summary info found"):
        parser.parse(test_output)


def test_parse_invalid_format(parser: PytestParser):
    test_output = "This is not a valid pytest output format"

    with pytest.raises(ValueError, match="No short test summary info found"):
        parser.parse(test_output)


def test_parse_rejects_wrapper_summary_without_real_pytest_node_ids(
    parser: PytestParser,
):
    test_output = dedent("""
        =============== short test summary info ===============
        PASSED aider_polyglot_test
    """)

    with pytest.raises(ValueError, match="No pytest test results found"):
        parser.parse(test_output)
