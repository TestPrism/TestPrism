from __future__ import annotations


def test_lazy_package_exports_preserve_public_import_api() -> None:
    import testprism
    from testprism import (
        BaseAgent,
        BenchmarkResults,
        Harness,
        TestBenchHarness,
        TestBenchTrialResults,
    )

    assert set(testprism.__all__) == {
        "BaseAgent",
        "BenchmarkResults",
        "Harness",
        "TestBenchHarness",
        "TestBenchTrialResults",
    }
    assert BaseAgent.__name__ == "BaseAgent"
    assert BenchmarkResults.__name__ == "BenchmarkResults"
    assert Harness.__name__ == "Harness"
    assert TestBenchHarness.__name__ == "TestBenchHarness"
    assert TestBenchTrialResults.__name__ == "TestBenchTrialResults"
