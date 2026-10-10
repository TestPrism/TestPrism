"""Public TestPrism API with side-effect-free package discovery."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from testprism.agents.base_agent import BaseAgent
    from testprism.harness.harness import Harness
    from testprism.harness.models import BenchmarkResults, TestBenchTrialResults
    from testprism.harness.testbench_harness import TestBenchHarness

__all__ = [
    "Harness",
    "TestBenchHarness",
    "BenchmarkResults",
    "TestBenchTrialResults",
    "BaseAgent",
]

_LAZY_EXPORTS = {
    "Harness": ("testprism.harness.harness", "Harness"),
    "TestBenchHarness": (
        "testprism.harness.testbench_harness",
        "TestBenchHarness",
    ),
    "BenchmarkResults": ("testprism.harness.models", "BenchmarkResults"),
    "TestBenchTrialResults": (
        "testprism.harness.models",
        "TestBenchTrialResults",
    ),
    "BaseAgent": ("testprism.agents.base_agent", "BaseAgent"),
}


def __getattr__(name: str) -> Any:
    """Load a public runtime only when that public attribute is requested."""

    try:
        module_name, attribute = _LAZY_EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
