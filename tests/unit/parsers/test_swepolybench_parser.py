from testprism.parsers.base_parser import UnitTestStatus
from testprism.parsers.swepolybench_parser import SWEPolyBenchParser


def _content(block: str) -> str:
    return (
        f"noise\n{SWEPolyBenchParser.START_MARKER}\n"
        f"{block}\n"
        f"{SWEPolyBenchParser.END_MARKER}\nmore noise"
    )


def test_parses_per_test_lines_with_double_colon_separator() -> None:
    result = SWEPolyBenchParser().parse(
        _content(
            "pkg.Test.test_a :: PASSED\n"
            "pkg.Test.test_b :: FAILED"
        )
    )

    assert result == {
        "pkg.Test.test_a": UnitTestStatus.PASSED,
        "pkg.Test.test_b": UnitTestStatus.FAILED,
    }


def test_rejoins_terminal_wrapped_status_tokens() -> None:
    result = SWEPolyBenchParser().parse(
        _content(
            "pkg.Test.test_a :: PAS\n"
            "SED\n"
            "pkg.Test.test_b :: FAI\n"
            "LED"
        )
    )

    assert result == {
        "pkg.Test.test_a": UnitTestStatus.PASSED,
        "pkg.Test.test_b": UnitTestStatus.FAILED,
    }


def test_rejoins_wrapped_single_status_result() -> None:
    result = SWEPolyBenchParser().parse(_content("PAS\nSED"))

    assert result == {"tests": UnitTestStatus.PASSED}


def test_rejoins_wrapped_separator_before_status() -> None:
    result = SWEPolyBenchParser().parse(
        _content(
            "pkg.Test.test_long_name :\n"
            ": PASSED"
        )
    )

    assert result == {"pkg.Test.test_long_name": UnitTestStatus.PASSED}

def test_skips_non_result_noise_after_marker() -> None:
    result = SWEPolyBenchParser().parse(
        _content(
            "ionHelper.java:104)\n"
            "        at org.testng.internal.Invoker.invokeMethod(Invoker.java:645)\n"
            "</failure>\n"
            "</testsuite>pkg.Test.test_long_name_esca\n"
            "pe :: PASSED\n"
            "pkg.Test.test_other :: PASSED"
        )
    )

    assert result == {
        "pkg.Test.test_long_name_escape": UnitTestStatus.PASSED,
        "pkg.Test.test_other": UnitTestStatus.PASSED,
    }


def test_rejoins_wrapped_status_after_complete_separator() -> None:
    result = SWEPolyBenchParser().parse(
        _content(
            "pkg.Test.test_a ::\n"
            " PASSED"
        )
    )

    assert result == {"pkg.Test.test_a": UnitTestStatus.PASSED}
