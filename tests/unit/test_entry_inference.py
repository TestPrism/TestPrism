from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from testprism.entry_inference import _is_test_path, infer_test_entries
from testprism.handlers.trial_handler import (
    Task,
)
from testprism.handlers.trial_handler import TestEntry as EntryModel
from testprism.handlers.trial_handler import (
    TestEntryInferences as InferenceMetadata,
)
from scripts.generation.infer_test_entries import main as infer_cli


def _write_fixture(root: Path) -> Path:
    (root / "pkg").mkdir()
    (root / "tests").mkdir()
    (root / "pkg" / "widget.py").write_text(
        "\n".join(
            [
                "raise RuntimeError('must never execute')",
                "",
                "def add(value):",
                "    return value + 1",
                "",
                "class Widget:",
                "    def __init__(self, name):",
                "        self.name = name",
                "    def render(self):",
                "        return self.name",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    test_path = root / "tests" / "test_widget.py"
    test_path.write_text(
        "\n".join(
            [
                "from pkg.widget import add, Widget as WidgetAlias",
                "import pkg.widget as widget_module",
                "",
                "def helper():",
                "    return add(100)",
                "",
                "def test_widget():",
                "    assert add(1) == 2",
                "    widget = WidgetAlias('hello')",
                "    widget.render()",
                "    widget_module.add(2)",
                "    unresolved_call()",
                "    helper()",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return test_path


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("pkg/numpy_test.py", True),
        ("pkg/test_api.py", True),
        ("tests/helpers.py", True),
        ("pkg/conftest.py", True),
        ("pkg/contest_utils.py", False),
        ("pkg/latest.py", False),
    ],
)
def test_test_path_predicate_accepts_conventional_suffix_without_substring(
    path: str, expected: bool
):
    assert _is_test_path(path) is expected


def test_inference_resolves_alias_constructor_and_method_without_execution(tmp_path):
    test_path = _write_fixture(tmp_path)

    result = infer_test_entries(tmp_path, [test_path])

    assert [entry.symbol for entry in result.entries] == [
        "pkg/widget.py:Widget.__init__",
        "pkg/widget.py:Widget.render",
        "pkg/widget.py:add",
    ]
    assert all(entry.path == "pkg/widget.py" for entry in result.entries)
    assert result.entries[0].start_line == 7
    assert result.entries[0].end_line == 8
    assert {diagnostic.expression for diagnostic in result.diagnostics} == {
        "helper",
        "unresolved_call",
    }
    assert len(result.diagnostics) == 2
    assert (
        sum(
            diagnostic.expression == "unresolved_call"
            for diagnostic in result.diagnostics
        )
        == 1
    )


def test_inference_treats_workspace_as_an_import_root(tmp_path):
    (tmp_path / "workspace").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "workspace" / "bowling.py").write_text(
        "class BowlingGame:\n    def roll(self, pins):\n        return pins\n",
        encoding="utf-8",
    )
    test_path = tmp_path / "tests" / "bowling_test.py"
    test_path.write_text(
        "from bowling import BowlingGame\n\n"
        "def test_roll():\n"
        "    game = BowlingGame()\n"
        "    assert game.roll(1) == 1\n",
        encoding="utf-8",
    )

    result = infer_test_entries(tmp_path, [test_path])

    assert [entry.symbol for entry in result.entries] == [
        "workspace/bowling.py:BowlingGame.roll"
    ]


def test_inference_aliases_modules_below_invalid_leading_path_prefix(tmp_path):
    package = tmp_path / "repo-core" / "pkg"
    tests = tmp_path / "tests"
    package.mkdir(parents=True)
    tests.mkdir()
    (package / "__init__.py").write_text(
        "from pkg.api import Widget as Widget, make as make\n",
        encoding="utf-8",
    )
    (package / "api.py").write_text(
        "def make():\n"
        "    return object()\n"
        "\n"
        "class Widget:\n"
        "    def __init__(self):\n"
        "        self.value = 1\n",
        encoding="utf-8",
    )
    test_path = tests / "test_api.py"
    test_path.write_text(
        "import pkg\n"
        "import pkg.api as api\n"
        "\n"
        "def test_api():\n"
        "    pkg.make()\n"
        "    pkg.Widget()\n"
        "    api.make()\n",
        encoding="utf-8",
    )

    result = infer_test_entries(tmp_path, [test_path])

    assert [entry.symbol for entry in result.entries] == [
        "repo-core/pkg/api.py:Widget.__init__",
        "repo-core/pkg/api.py:make",
    ]
    assert result.diagnostics == ()


@pytest.mark.parametrize(
    "production_paths",
    [
        ("monorepo/pkg/api.py",),
        ("monorepo/repo-core/pkg/api.py",),
        ("repo-core/pkg-name/api.py",),
        ("repo-one/pkg/api.py", "repo-two/pkg/api.py"),
    ],
    ids=[
        "valid-leading-directory",
        "interior-invalid-component",
        "invalid-importable-suffix",
        "duplicate-suffix-alias",
    ],
)
def test_invalid_prefix_alias_keeps_unproven_or_ambiguous_paths_unresolved(
    tmp_path, production_paths
):
    tests = tmp_path / "tests"
    tests.mkdir()
    for relative in production_paths:
        source = tmp_path / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("def run():\n    return 1\n", encoding="utf-8")
    test_path = tests / "test_api.py"
    test_path.write_text(
        "from pkg.api import run\n\ndef test_api():\n    run()\n",
        encoding="utf-8",
    )

    result = infer_test_entries(tmp_path, [test_path])

    assert result.entries == []
    assert len(result.diagnostics) == 1
    if len(production_paths) == 2:
        assert "ambiguous" in result.diagnostics[0].reason
    else:
        assert "unresolved" in result.diagnostics[0].reason


def test_invalid_prefix_alias_does_not_index_excluded_vendor_tree(tmp_path):
    source = tmp_path / "repo-core" / "vendor" / "pkg" / "api.py"
    source.parent.mkdir(parents=True)
    source.write_text("def run():\n    return 1\n", encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    test_path = tests / "test_api.py"
    test_path.write_text(
        "from vendor.pkg.api import run\n\ndef test_api():\n    run()\n",
        encoding="utf-8",
    )

    result = infer_test_entries(tmp_path, [test_path])

    assert result.entries == []
    assert len(result.diagnostics) == 1
    assert "unresolved" in result.diagnostics[0].reason


def test_invalid_prefix_alias_preserves_ambiguous_reexport(tmp_path):
    package = tmp_path / "repo-core" / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "from pkg.one import run as run\nfrom pkg.two import run as run\n",
        encoding="utf-8",
    )
    for name in ("one", "two"):
        (package / f"{name}.py").write_text(
            f"def run():\n    return {name!r}\n", encoding="utf-8"
        )
    tests = tmp_path / "tests"
    tests.mkdir()
    test_path = tests / "test_api.py"
    test_path.write_text(
        "from pkg import run\n\ndef test_api():\n    run()\n",
        encoding="utf-8",
    )

    result = infer_test_entries(tmp_path, [test_path])

    assert result.entries == []
    assert len(result.diagnostics) == 1
    assert "ambiguous" in result.diagnostics[0].reason


def test_selected_test_parameter_annotation_seeds_instance_and_helper_return(
    tmp_path,
):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "widget.py").write_text(
        "class Widget:\n    def render(self):\n        return 'rendered'\n",
        encoding="utf-8",
    )
    test_path = tmp_path / "tests" / "test_widget.py"
    test_path.write_text(
        "from pkg.widget import Widget\n"
        "\n"
        "def identity(value):\n"
        "    return value\n"
        "\n"
        "def test_typed(pos: Widget, /, value: Widget = None, *, keyword: Widget):\n"
        "    pos.render()\n"
        "    assigned = value\n"
        "    assigned.render()\n"
        "    returned = identity(keyword)\n"
        "    returned.render()\n",
        encoding="utf-8",
    )

    result = infer_test_entries(tmp_path, [test_path])

    assert [entry.symbol for entry in result.entries] == ["pkg/widget.py:Widget.render"]
    assert result.entries[0].test_id == "tests/test_widget.py::test_typed"


def _write_matplotlib_figure_fixture(
    tmp_path,
    *,
    test_import="from matplotlib.testing.decorators import check_figures_equal",
    test_decorator="@check_figures_equal(extensions=['png'])",
    test_parameters="fig_test, fig_ref",
    wrapper_test_assignment='fig_test = plt.figure("test")',
    wrapper_ref_assignment='fig_ref = plt.figure("reference")',
    wrapper_forwarding=("func(*args, fig_test=fig_test, fig_ref=fig_ref, **kwargs)"),
    decorator_suffix="",
    test_import_after="",
    figure_signature="name=None, FigureClass=Figure",
):
    (tmp_path / "matplotlib" / "testing").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "matplotlib" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "matplotlib" / "testing" / "__init__.py").write_text(
        "", encoding="utf-8"
    )
    (tmp_path / "matplotlib" / "figure.py").write_text(
        "class FigureBase:\n"
        "    def subplots(self):\n"
        "        return object()\n"
        "\n"
        "class Figure(FigureBase):\n"
        "    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "matplotlib" / "axes.py").write_text(
        "class Axes:\n    def plot(self):\n        return None\n",
        encoding="utf-8",
    )
    (tmp_path / "matplotlib" / "pyplot.py").write_text(
        "from matplotlib.figure import Figure\n"
        "\n"
        f"def figure({figure_signature}):\n"
        "    return Figure()\n",
        encoding="utf-8",
    )
    (tmp_path / "matplotlib" / "testing" / "decorators.py").write_text(
        "from matplotlib import pyplot as plt\n"
        "\n"
        "def check_figures_equal(*, extensions=('png',), tol=0):\n"
        "    def decorator(func):\n"
        "        def wrapper(*args, **kwargs):\n"
        f"            {wrapper_test_assignment}\n"
        f"            {wrapper_ref_assignment}\n"
        f"            {wrapper_forwarding}\n"
        "        return wrapper\n"
        "    return decorator\n"
        f"{decorator_suffix}",
        encoding="utf-8",
    )
    test_path = tmp_path / "tests" / "test_plot.py"
    test_path.write_text(
        f"{test_import}\n"
        f"{test_decorator}\n"
        f"def test_plot({test_parameters}):\n"
        "    axes = fig_test.subplots()\n"
        "    axes.plot()\n"
        "    fig_ref.subplots()\n"
        "    unrelated.subplots()\n"
        f"{test_import_after}",
        encoding="utf-8",
    )
    return test_path


def test_check_figures_equal_seeds_only_proven_figure_parameters(tmp_path):
    test_path = _write_matplotlib_figure_fixture(tmp_path)

    result = infer_test_entries(tmp_path, [test_path])

    assert [entry.symbol for entry in result.entries] == [
        "matplotlib/figure.py:FigureBase.subplots"
    ]
    assert result.entries[0].test_id == "tests/test_plot.py::test_plot"


@pytest.mark.parametrize(
    "changes",
    [
        {"test_parameters": "fig_test"},
        {"test_parameters": "fig_test, fig_ref, unrelated"},
        {"test_decorator": "@check_figures_equal"},
        {
            "test_import": (
                "from matplotlib.testing.decorators import "
                "check_figures_equal as compare"
            ),
            "test_decorator": "@compare()",
        },
        {
            "test_import": (
                "from matplotlib.testing.decorators import check_figures_equal\n"
                "check_figures_equal = lambda: None"
            )
        },
        {
            "test_import": (
                "from matplotlib.testing.decorators import check_figures_equal\n"
                "from matplotlib.testing.decorators import check_figures_equal"
            )
        },
        {
            "test_import": "",
            "test_import_after": (
                "\nfrom matplotlib.testing.decorators import check_figures_equal\n"
            ),
        },
        {
            "decorator_suffix": (
                "\ndef check_figures_equal(*, extensions=('png',), tol=0):\n"
                "    return lambda func: func\n"
            )
        },
        {"wrapper_test_assignment": "fig_test = object()"},
        {"wrapper_ref_assignment": "pass"},
        {"wrapper_ref_assignment": 'fig_ref = plt.subplots("reference")'},
        {"wrapper_forwarding": "func(*args, fig_test=fig_test, **kwargs)"},
        {"figure_signature": ""},
        {"figure_signature": "name=None"},
        {"figure_signature": "name, FigureClass"},
    ],
)
def test_check_figures_equal_rejects_unproven_wrapper_or_test_shapes(tmp_path, changes):
    test_path = _write_matplotlib_figure_fixture(tmp_path, **changes)

    result = infer_test_entries(tmp_path, [test_path])

    assert result.entries == []


@pytest.mark.parametrize(
    "annotation",
    [
        "'Widget'",
        "list[Widget]",
        "Widget | None",
        "module.Widget",
        "str",
        "Missing",
        None,
    ],
)
def test_non_simple_or_unresolved_parameter_annotations_do_not_seed_instances(
    tmp_path,
    annotation,
):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "widget.py").write_text(
        "class Widget:\n    def render(self):\n        return 'rendered'\n",
        encoding="utf-8",
    )
    annotation_text = f": {annotation}" if annotation is not None else ""
    test_path = tmp_path / "tests" / "test_widget.py"
    test_path.write_text(
        "from pkg.widget import Widget\n"
        "import pkg.widget as module\n"
        "\n"
        f"def test_typed(value{annotation_text}):\n"
        "    value.render()\n",
        encoding="utf-8",
    )

    result = infer_test_entries(tmp_path, [test_path])

    assert result.entries == []


def test_ambiguous_import_local_class_and_self_annotations_are_not_seeded(tmp_path):
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    (tmp_path / "tests").mkdir()
    source = "class Widget:\n    def render(self):\n        return 'rendered'\n"
    (tmp_path / "one" / "widget.py").write_text(source, encoding="utf-8")
    (tmp_path / "two" / "widget.py").write_text(source, encoding="utf-8")
    test_path = tmp_path / "tests" / "test_widget.py"
    test_path.write_text(
        "from one.widget import Widget\n"
        "from two.widget import Widget\n"
        "from one.widget import Widget as ImportedWidget\n"
        "\n"
        "class ImportedWidget:\n"
        "    pass\n"
        "\n"
        "class TestTyped:\n"
        "    def test_rejected(\n"
        "        self: ImportedWidget, cls: ImportedWidget, value: Widget\n"
        "    ):\n"
        "        self.render()\n"
        "        cls.render()\n"
        "        value.render()\n",
        encoding="utf-8",
    )

    result = infer_test_entries(tmp_path, [test_path])

    assert result.entries == []


def test_nested_selected_test_class_keeps_typed_parameter_resolution(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "widget.py").write_text(
        "class Widget:\n    def render(self):\n        return 'rendered'\n",
        encoding="utf-8",
    )
    test_path = tmp_path / "tests" / "test_widget.py"
    test_path.write_text(
        "from pkg.widget import Widget\n"
        "\n"
        "class Outer:\n"
        "    class Inner:\n"
        "        def test_typed(self, value: Widget):\n"
        "            value.render()\n",
        encoding="utf-8",
    )

    result = infer_test_entries(
        tmp_path,
        selected_test_ids=["tests/test_widget.py::Outer::Inner::test_typed"],
    )

    assert [entry.symbol for entry in result.entries] == ["pkg/widget.py:Widget.render"]


def test_fixture_return_annotation_and_imported_test_class_do_not_seed_parameters(
    tmp_path,
):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "widget.py").write_text(
        "class Widget:\n    def render(self):\n        return 'rendered'\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "helper_types.py").write_text(
        "class TestValue:\n    def render(self):\n        return 'test helper'\n",
        encoding="utf-8",
    )
    test_path = tmp_path / "tests" / "test_widget.py"
    test_path.write_text(
        "import pytest\n"
        "from pkg.widget import Widget\n"
        "from tests.helper_types import TestValue\n"
        "\n"
        "@pytest.fixture\n"
        "def value() -> Widget:\n"
        "    return Widget()\n"
        "\n"
        "def test_untyped(value, helper: TestValue):\n"
        "    value.render()\n"
        "    helper.render()\n",
        encoding="utf-8",
    )

    result = infer_test_entries(tmp_path, [test_path])

    assert result.entries == []


def _write_fixture_instance_project(
    tmp_path,
    *,
    fixture_import="import pytest",
    fixture_decorator="@pytest.fixture",
    fixture_body="    value = Widget()\n    return value\n",
    production_import="from pkg.widget import Widget",
    production_extra="",
    fixture_in_conftest=False,
    test_parameters="widget",
    test_body="    widget.render()\n",
):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "widget.py").write_text(
        "from typing import Self\n"
        "class Widget:\n"
        "    def prepare(self) -> Self:\n"
        "        return self\n"
        "    def render(self):\n"
        "        return 'rendered'\n"
        f"{production_extra}",
        encoding="utf-8",
    )
    fixture_source = (
        f"{fixture_import}\n"
        f"{production_import}\n\n"
        f"{fixture_decorator}\n"
        "def widget():\n"
        f"{fixture_body}"
    )
    test_path = tmp_path / "tests" / "test_widget.py"
    if fixture_in_conftest:
        (tmp_path / "tests" / "conftest.py").write_text(
            fixture_source, encoding="utf-8"
        )
        test_source = ""
    else:
        test_source = fixture_source + "\n"
    test_path.write_text(
        test_source + f"def test_fixture({test_parameters}):\n" + test_body,
        encoding="utf-8",
    )
    return test_path


def test_same_module_fixture_local_construction_seeds_only_selected_parameter(
    tmp_path,
):
    test_path = _write_fixture_instance_project(
        tmp_path,
        test_parameters="widget, peer",
        test_body="    widget.render()\n    peer.render()\n",
    )

    result = infer_test_entries(tmp_path, [test_path])

    assert [entry.symbol for entry in result.entries] == ["pkg/widget.py:Widget.render"]
    assert result.entries[0].test_id == "tests/test_widget.py::test_fixture"


@pytest.mark.parametrize(
    "self_import",
    ["from typing import Self", "from typing_extensions import Self"],
)
def test_applicable_conftest_fixture_preserves_instance_through_self_method(
    tmp_path, self_import
):
    test_path = _write_fixture_instance_project(
        tmp_path,
        fixture_in_conftest=True,
        fixture_body="    value = Widget()\n    return value.prepare()\n",
    )
    widget_path = tmp_path / "pkg" / "widget.py"
    widget_path.write_text(
        widget_path.read_text(encoding="utf-8").replace(
            "from typing import Self", self_import
        ),
        encoding="utf-8",
    )

    result = infer_test_entries(tmp_path, [test_path])

    assert [entry.symbol for entry in result.entries] == ["pkg/widget.py:Widget.render"]


def test_same_class_fixture_is_applicable_but_peer_class_fixture_is_not(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "widget.py").write_text(
        "class Widget:\n    def render(self):\n        return 'rendered'\n",
        encoding="utf-8",
    )
    test_path = tmp_path / "tests" / "test_widget.py"
    test_path.write_text(
        "import pytest\n"
        "from pkg.widget import Widget\n\n"
        "class TestOwner:\n"
        "    @pytest.fixture\n"
        "    def widget(self):\n"
        "        value = Widget()\n"
        "        return value\n\n"
        "    def test_owner(self, widget):\n"
        "        widget.render()\n\n"
        "class TestPeer:\n"
        "    def test_peer(self, widget):\n"
        "        widget.render()\n",
        encoding="utf-8",
    )

    result = infer_test_entries(tmp_path, [test_path])

    assert [(entry.test_id, entry.symbol) for entry in result.entries] == [
        (
            "tests/test_widget.py::TestOwner.test_owner",
            "pkg/widget.py:Widget.render",
        )
    ]


@pytest.mark.parametrize(
    ("changes", "extra_conftest"),
    [
        ({"fixture_import": "import pytest as testing"}, ""),
        (
            {
                "fixture_import": "from pytest import fixture",
                "fixture_decorator": "@fixture",
            },
            "",
        ),
        ({"fixture_import": "import pytest\npytest = object()"}, ""),
        ({"fixture_decorator": ""}, ""),
        ({"fixture_decorator": "@other\n@pytest.fixture"}, ""),
        ({"fixture_decorator": "@pytest.fixture(name='widget')"}, ""),
        (
            {"production_import": ("from pkg.widget import Widget\nWidget = object()")},
            "",
        ),
        ({"fixture_body": "    return Widget()\n"}, ""),
        (
            {
                "fixture_body": (
                    "    value = Widget()\n"
                    "    value = unknown_factory()\n"
                    "    return value\n"
                )
            },
            "",
        ),
        (
            {
                "fixture_body": (
                    "    value = Widget()\n    value = Other()\n    return value\n"
                ),
                "production_import": ("from pkg.widget import Widget, Other"),
                "production_extra": (
                    "class Other:\n    def render(self):\n        return 'other'\n"
                ),
            },
            "",
        ),
        ({"fixture_body": "    return missing\n"}, ""),
        (
            {},
            "import pytest\n"
            "from pkg.widget import Widget\n\n"
            "@pytest.fixture\n"
            "def widget():\n"
            "    value = Widget()\n"
            "    return value\n",
        ),
    ],
)
def test_fixture_instance_proof_rejects_unproven_shapes(
    tmp_path, changes, extra_conftest
):
    test_path = _write_fixture_instance_project(tmp_path, **changes)
    if extra_conftest:
        (tmp_path / "tests" / "conftest.py").write_text(
            extra_conftest, encoding="utf-8"
        )

    result = infer_test_entries(tmp_path, [test_path])

    assert result.entries == []


@pytest.mark.parametrize(
    "annotation",
    ["Widget", "'Self'", "typing.Self", "Missing"],
)
def test_fixture_method_requires_direct_uniquely_imported_self(tmp_path, annotation):
    test_path = _write_fixture_instance_project(
        tmp_path,
        fixture_body="    value = Widget()\n    return value.prepare()\n",
    )
    source_path = tmp_path / "pkg" / "widget.py"
    source = source_path.read_text(encoding="utf-8")
    source = source.replace("-> Self", f"-> {annotation}")
    if annotation == "typing.Self":
        source = source.replace("from typing import Self", "import typing")
    source_path.write_text(source, encoding="utf-8")

    result = infer_test_entries(tmp_path, [test_path])

    assert result.entries == []


def _xarray_style_self_shim(
    *,
    condition="sys.version_info >= (3, 11)",
    error="ImportError",
    fallback="None",
    sys_binding="import sys",
    type_checking_binding="from typing import TYPE_CHECKING, Any",
):
    return (
        f"{sys_binding}\n"
        f"{type_checking_binding}\n\n"
        "try:\n"
        f"    if {condition}:\n"
        "        from typing import Self\n"
        "    else:\n"
        "        from typing_extensions import Self\n"
        f"except {error}:\n"
        "    if TYPE_CHECKING:\n"
        "        raise\n"
        "    else:\n"
        f"        Self: Any = {fallback}\n"
    )


def test_fixture_method_accepts_exact_xarray_style_self_compatibility_shim(tmp_path):
    test_path = _write_fixture_instance_project(
        tmp_path,
        fixture_body="    value = Widget()\n    return value.prepare()\n",
    )
    (tmp_path / "pkg" / "compat.py").write_text(
        _xarray_style_self_shim(), encoding="utf-8"
    )
    widget_path = tmp_path / "pkg" / "widget.py"
    widget_path.write_text(
        widget_path.read_text(encoding="utf-8").replace(
            "from typing import Self", "from pkg.compat import Self"
        ),
        encoding="utf-8",
    )

    result = infer_test_entries(tmp_path, [test_path])

    assert [entry.symbol for entry in result.entries] == ["pkg/widget.py:Widget.render"]


@pytest.mark.parametrize(
    "shim",
    [
        _xarray_style_self_shim(condition="FLAG"),
        _xarray_style_self_shim(error="Exception"),
        _xarray_style_self_shim(fallback="object()"),
        _xarray_style_self_shim(sys_binding="sys = fake"),
        _xarray_style_self_shim(sys_binding="import other as sys"),
        _xarray_style_self_shim(
            type_checking_binding=(
                "from typing import TYPE_CHECKING, Any\nTYPE_CHECKING = True"
            )
        ),
        _xarray_style_self_shim(
            type_checking_binding=(
                "from other import TYPE_CHECKING\nfrom typing import Any"
            )
        ),
        _xarray_style_self_shim(
            type_checking_binding=("from typing import TYPE_CHECKING as CHECKING, Any")
        ),
        _xarray_style_self_shim() + "Self = object()\n",
        (
            "from typing import TYPE_CHECKING\n"
            "if TYPE_CHECKING:\n"
            "    from typing import Self\n"
            "else:\n"
            "    from typing_extensions import Self\n"
        ),
    ],
)
def test_fixture_method_rejects_unrelated_self_conditional_or_fallback(tmp_path, shim):
    test_path = _write_fixture_instance_project(
        tmp_path,
        fixture_body="    value = Widget()\n    return value.prepare()\n",
    )
    (tmp_path / "pkg" / "compat.py").write_text(shim, encoding="utf-8")
    widget_path = tmp_path / "pkg" / "widget.py"
    widget_path.write_text(
        widget_path.read_text(encoding="utf-8").replace(
            "from typing import Self", "from pkg.compat import Self"
        ),
        encoding="utf-8",
    )

    result = infer_test_entries(tmp_path, [test_path])

    assert result.entries == []


@pytest.mark.parametrize(
    "self_import",
    [
        "if FLAG:\n    from typing import Self",
        "try:\n    from typing import Self\nexcept ImportError:\n    Self = object()",
        (
            "if TYPE_CHECKING:\n"
            "    from typing_extensions import Self\n"
            "else:\n"
            "    Self = object()"
        ),
    ],
)
def test_fixture_method_rejects_unproven_method_module_self_scope(
    tmp_path, self_import
):
    test_path = _write_fixture_instance_project(
        tmp_path,
        fixture_body="    value = Widget()\n    return value.prepare()\n",
    )
    widget_path = tmp_path / "pkg" / "widget.py"
    source = widget_path.read_text(encoding="utf-8").replace(
        "from typing import Self", "from typing import TYPE_CHECKING\n" + self_import
    )
    widget_path.write_text(source, encoding="utf-8")

    result = infer_test_entries(tmp_path, [test_path])

    assert result.entries == []


def test_fixture_method_rejects_missing_or_ambiguous_method(tmp_path):
    test_path = _write_fixture_instance_project(
        tmp_path,
        fixture_body="    value = Widget()\n    return value.prepare()\n",
    )
    widget_path = tmp_path / "pkg" / "widget.py"
    source = widget_path.read_text(encoding="utf-8")
    source = source.replace(
        "    def prepare(self) -> Self:\n        return self\n",
        "    def prepare(self) -> Self:\n"
        "        return self\n"
        "    def prepare(self) -> Self:\n"
        "        return self\n",
    )
    widget_path.write_text(source, encoding="utf-8")

    result = infer_test_entries(tmp_path, [test_path])

    assert result.entries == []


def test_fixture_dependency_return_is_not_inferred(tmp_path):
    test_path = _write_fixture_instance_project(
        tmp_path,
        fixture_body="    return dependency\n",
    )
    fixture_source = test_path.read_text(encoding="utf-8").replace(
        "def widget():", "def widget(dependency):"
    )
    test_path.write_text(fixture_source, encoding="utf-8")

    result = infer_test_entries(tmp_path, [test_path])

    assert result.entries == []


def _write_unittest_setup_project(tmp_path: Path, test_source: str) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "api.py").write_text(
        "class Client:\n"
        "    def __init__(self, db=0):\n"
        "        self.db = db\n"
        "    def ping(self):\n"
        "        return True\n"
        "\n"
        "class Other:\n"
        "    def __init__(self):\n"
        "        pass\n"
        "    def ping(self):\n"
        "        return False\n",
        encoding="utf-8",
    )
    test_path = tmp_path / "tests" / "test_api.py"
    test_path.write_text(test_source, encoding="utf-8")
    return test_path


def test_same_class_unittest_setup_seeds_only_selected_direct_calls(tmp_path):
    test_path = _write_unittest_setup_project(
        tmp_path,
        "import pkg.api as api\n"
        "\n"
        "class TestClient:\n"
        "    def setUp(self):\n"
        "        self.client = self.make_client()\n"
        "\n"
        "    def make_client(self, db=0):\n"
        "        if db:\n"
        "            return api.Client(db=db)\n"
        "        return api.Client(db=db)\n"
        "\n"
        "    def test_selected(self):\n"
        "        self.client.ping()\n"
        "        self.client.ping()\n"
        "\n"
        "    def test_peer(self):\n"
        "        self.client.ping()\n"
        "\n"
        "class TestPeer:\n"
        "    def test_unknown(self):\n"
        "        self.client.ping()\n",
    )

    result = infer_test_entries(
        tmp_path,
        [test_path],
        selected_test_ids=["tests/test_api.py::TestClient::test_selected"],
    )

    assert [(entry.test_id, entry.symbol) for entry in result.entries] == [
        ("tests/test_api.py::TestClient.test_selected", "pkg/api.py:Client.ping")
    ]
    assert result.entries[0].start_line == 4
    assert result.entries[0].end_line == 5
    assert all("__init__" not in entry.symbol for entry in result.entries)


def test_unittest_setup_rejects_inherited_selected_tests_and_subclass_helpers(
    tmp_path,
):
    test_path = _write_unittest_setup_project(
        tmp_path,
        "from pkg.api import Client, Other\n"
        "\n"
        "class TestBase:\n"
        "    def setUp(self):\n"
        "        self.client = self.make_client()\n"
        "    def make_client(self):\n"
        "        return Client()\n"
        "    def test_inherited(self):\n"
        "        self.client.ping()\n"
        "\n"
        "class TestChild(TestBase):\n"
        "    def make_client(self):\n"
        "        return Other()\n"
        "    def test_direct(self):\n"
        "        self.client.ping()\n",
    )

    result = infer_test_entries(
        tmp_path,
        [test_path],
        selected_test_ids=[
            "tests/test_api.py::TestChild::test_inherited",
            "tests/test_api.py::TestChild::test_direct",
        ],
    )

    assert result.entries == []
    assert any(
        diagnostic.test_id == "tests/test_api.py::TestChild.test_inherited"
        and diagnostic.reason
        == "requested test selector was not found in parsed test source"
        for diagnostic in result.diagnostics
    )


@pytest.mark.parametrize(
    "class_body",
    [
        "    def make(self):\n        return Client()\n",
        "    @staticmethod\n    def setUp(self):\n        self.client = self.make()\n"
        "    def make(self):\n        return Client()\n",
        "    async def setUp(self):\n        self.client = self.make()\n"
        "    def make(self):\n        return Client()\n",
        "    def setUp(self):\n        self.client = self.make()\n"
        "    def setUp(self):\n        self.client = self.make()\n"
        "    def make(self):\n        return Client()\n",
        "    def setUp(self):\n        self.client = self.make(1)\n"
        "    def make(self, db=0):\n        return Client(db=db)\n",
        "    def setUp(self):\n        self.client = self.make()\n"
        "    def make(self, db):\n        return Client(db=db)\n",
        "    def setUp(self):\n        self.client = self.missing()\n",
        "    def setUp(self):\n        self.client = self.make()\n"
        "    def make(self):\n        return Client()\n"
        "    def make(self):\n        return Client()\n",
        "    def setUp(self):\n        self.client = self.make()\n"
        "    async def make(self):\n        return Client()\n",
        "    def setUp(self):\n"
        "        self.client = self.make()\n"
        "        self.client = self.make()\n"
        "    def make(self):\n        return Client()\n",
        "    def setUp(self):\n"
        "        self.client = self.make()\n"
        "        del self.client\n"
        "    def make(self):\n        return Client()\n",
        "    def setUp(self):\n        self.client: Client = self.make()\n"
        "    def make(self):\n        return Client()\n",
        "    def setUp(self):\n"
        "        self.client = self.make()\n"
        "        self.client += 1\n"
        "    def make(self):\n        return Client()\n",
        "    def setUp(self):\n"
        "        if flag:\n"
        "            self.client = self.make()\n"
        "    def make(self):\n        return Client()\n",
        "    def setUp(self):\n"
        "        for _ in values:\n"
        "            self.client = self.make()\n"
        "    def make(self):\n        return Client()\n",
        "    def setUp(self):\n"
        "        try:\n"
        "            self.client = self.make()\n"
        "        except Exception:\n"
        "            pass\n"
        "    def make(self):\n        return Client()\n",
        "    def setUp(self):\n        self.client = self.make()\n"
        "    @staticmethod\n    def make(self):\n        return Client()\n",
        "    def setUp(self):\n        self.client = self.make()\n"
        "    def make(self):\n        if flag:\n            return Client()\n",
        "    def setUp(self):\n        self.client = self.make()\n"
        "    def make(self):\n        yield Client()\n",
        "    def setUp(self):\n        self.client = self.make()\n"
        "    def make(self):\n        return self.make()\n",
        "    def setUp(self):\n        self.client = self.make()\n"
        "    def make(self):\n        return factory()\n",
        "    def setUp(self):\n        self.client = self.make()\n"
        "    def make(self):\n"
        "        if flag:\n"
        "            return Client()\n"
        "        return Other()\n",
        "    def setUp(self):\n        self.client = self.make()\n"
        "    def make(self):\n        helper()\n        return Client()\n",
        "    def setUp(self):\n        self.client = self.make()\n"
        "    def make(self):\n        return LocalClient()\n",
        "    def setUp(self):\n        self.client = self.make()\n"
        "    def make(self):\n        return Client()\n"
        "    def test_selected(self):\n"
        "        self.client = object()\n"
        "        self.client.ping()\n",
    ],
    ids=[
        "missing-setup",
        "decorated-setup",
        "async-setup",
        "multiple-setup",
        "setup-arguments",
        "required-helper-argument",
        "missing-helper",
        "multiple-helper",
        "async-helper",
        "competing-write",
        "delete",
        "annotated-write",
        "augmented-write",
        "conditional-write",
        "loop-write",
        "try-write",
        "decorated-helper",
        "fallthrough-return",
        "yield-return",
        "recursive-return",
        "unknown-factory",
        "mixed-returns",
        "arbitrary-helper-call",
        "test-local-class",
        "selected-method-rebinding",
    ],
)
def test_unittest_setup_receiver_proof_fails_closed(tmp_path, class_body):
    if "def test_selected" not in class_body:
        class_body += "    def test_selected(self):\n        self.client.ping()\n"
    test_path = _write_unittest_setup_project(
        tmp_path,
        "from pkg.api import Client, Other\n"
        "\n"
        "class LocalClient:\n"
        "    def ping(self):\n"
        "        return True\n"
        "\n"
        "class TestClient:\n" + class_body,
    )

    result = infer_test_entries(
        tmp_path,
        [test_path],
        selected_test_ids=["tests/test_api.py::TestClient::test_selected"],
    )

    assert result.entries == []


@pytest.mark.parametrize(
    "binding",
    [
        "from pkg.api import Client\nClient = object\n",
        "from pkg.api import Client\nfrom pkg.api import Other as Client\n",
    ],
    ids=["rebound-constructor", "ambiguous-constructor-import"],
)
def test_unittest_setup_receiver_rejects_unproven_constructor_binding(
    tmp_path, binding
):
    test_path = _write_unittest_setup_project(
        tmp_path,
        binding + "class TestClient:\n"
        "    def setUp(self):\n"
        "        self.client = self.make()\n"
        "    def make(self):\n"
        "        return Client()\n"
        "    def test_selected(self):\n"
        "        self.client.ping()\n",
    )

    result = infer_test_entries(tmp_path, [test_path])

    assert result.entries == []


def test_same_module_helper_calls_and_returned_instance_keep_selected_test_id(
    tmp_path,
):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "widget.py").write_text(
        "class Widget:\n"
        "    def __init__(self, name):\n"
        "        self.name = name\n"
        "\n"
        "    def render(self):\n"
        "        return self.name\n",
        encoding="utf-8",
    )
    test_path = tmp_path / "tests" / "test_widget.py"
    test_path.write_text(
        "from pkg.widget import Widget\n"
        "\n"
        "def make_widget(name):\n"
        "    widget = Widget(name)\n"
        "    widget.render()\n"
        "    return widget\n"
        "\n"
        "def test_helper_return():\n"
        "    widget = make_widget('fixture')\n"
        "    widget.render()\n",
        encoding="utf-8",
    )

    result = infer_test_entries(tmp_path, [test_path])

    assert [entry.symbol for entry in result.entries] == [
        "pkg/widget.py:Widget.__init__",
        "pkg/widget.py:Widget.render",
    ]
    assert {entry.test_id for entry in result.entries} == {
        "tests/test_widget.py::test_helper_return"
    }
    assert all("make_widget" not in entry.symbol for entry in result.entries)


def test_referenced_module_factory_calls_keep_each_selected_test_id_and_opaque_return(
    tmp_path,
):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "factory.py").write_text(
        "def make_client():\n"
        "    return object()\n"
        "\n"
        "class Client:\n"
        "    def __init__(self):\n"
        "        pass\n"
        "\n"
        "    def request(self):\n"
        "        pass\n",
        encoding="utf-8",
    )
    test_path = tmp_path / "tests" / "test_client.py"
    test_path.write_text(
        "from pkg.factory import Client, make_client\n"
        "\n"
        "FUNCTION_CLIENT = make_client()\n"
        "CONSTRUCTED_CLIENT = Client()\n"
        "\n"
        "def use_function_client():\n"
        "    return FUNCTION_CLIENT\n"
        "\n"
        "def test_direct():\n"
        "    FUNCTION_CLIENT.request()\n"
        "\n"
        "def test_helper():\n"
        "    use_function_client()\n"
        "\n"
        "def test_constructor():\n"
        "    CONSTRUCTED_CLIENT.request()\n",
        encoding="utf-8",
    )

    result = infer_test_entries(tmp_path, [test_path])

    assert [(entry.test_id, entry.symbol) for entry in result.entries] == [
        ("tests/test_client.py::test_constructor", "pkg/factory.py:Client.__init__"),
        ("tests/test_client.py::test_constructor", "pkg/factory.py:Client.request"),
        ("tests/test_client.py::test_direct", "pkg/factory.py:make_client"),
        ("tests/test_client.py::test_helper", "pkg/factory.py:make_client"),
    ]
    assert all(
        entry.symbol != "pkg/factory.py:Client.request"
        for entry in result.entries
        if entry.test_id != "tests/test_client.py::test_constructor"
    )


@pytest.mark.parametrize(
    "module_source",
    [
        # Unreferenced setup and peer-test-only loads are outside a selected root.
        "from pkg.factory import make_client\nCLIENT = make_client()\n\n"
        "def test_selected():\n    pass\n\n"
        "def test_peer():\n    assert CLIENT\n",
        # A second binding, even after the candidate assignment, invalidates it.
        "from pkg.factory import make_client\n"
        "CLIENT = make_client()\nCLIENT = object()\n\n"
        "def test_selected():\n    assert CLIENT\n",
        # Conditional module assignments are not unconditional setup authority.
        "from pkg.factory import make_client\nif FLAG:\n    CLIENT = make_client()\n\n"
        "def test_selected():\n    assert CLIENT\n",
        # Conditional imports do not establish a stable factory binding.
        "if FLAG:\n    from pkg.factory import make_client\n"
        "CLIENT = make_client()\n\ndef test_selected():\n    assert CLIENT\n",
        # The import must textually precede the setup call.
        "CLIENT = make_client()\nfrom pkg.factory import make_client\n\n"
        "def test_selected():\n    assert CLIENT\n",
        # Unresolved and multiply imported call targets remain unproven.
        "CLIENT = missing_factory()\n\ndef test_selected():\n    assert CLIENT\n",
        "from pkg.factory import make_client\nfrom pkg.other import make_client\n"
        "CLIENT = make_client()\n\ndef test_selected():\n    assert CLIENT\n",
        # A class without one explicit constructor has no entry span.
        "from pkg.factory import Client\nCLIENT = Client()\n\n"
        "def test_selected():\n    assert CLIENT\n",
        # Only one direct simple-name Assign target is admitted.
        "from pkg.factory import make_client\nCLIENT, OTHER = make_client()\n\n"
        "def test_selected():\n    assert CLIENT\n",
        "from pkg.factory import make_client\nHOLDER.CLIENT = make_client()\n\n"
        "def test_selected():\n    assert HOLDER.CLIENT\n",
        # A selected function-local binding means the module global is not loaded.
        "from pkg.factory import make_client\nCLIENT = make_client()\n\n"
        "def test_selected():\n    CLIENT = object()\n    assert CLIENT\n",
    ],
)
def test_module_factory_calls_require_unique_unconditional_referenced_setup(
    tmp_path, module_source
):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "factory.py").write_text(
        "def make_client():\n    return object()\n\nclass Client:\n    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "pkg" / "other.py").write_text(
        "def make_client():\n    return object()\n",
        encoding="utf-8",
    )
    test_path = tmp_path / "tests" / "test_client.py"
    test_path.write_text(module_source, encoding="utf-8")

    result = infer_test_entries(
        tmp_path,
        [test_path],
        selected_test_ids=["tests/test_client.py::test_selected"],
    )

    assert result.entries == []


def test_nested_helpers_are_bounded_and_recursive_or_ambiguous_returns_are_rejected(
    tmp_path,
):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "widget.py").write_text(
        "class Widget:\n"
        "    def __init__(self):\n"
        "        pass\n"
        "\n"
        "    def render(self):\n"
        "        pass\n",
        encoding="utf-8",
    )
    test_path = tmp_path / "tests" / "test_widget.py"
    test_path.write_text(
        "from pkg.widget import Widget\n"
        "\n"
        "def nested_make():\n"
        "    return Widget()\n"
        "\n"
        "def make_widget(flag):\n"
        "    if flag:\n"
        "        return nested_make()\n"
        "    return 0\n"
        "\n"
        "def recurse():\n"
        "    return recurse()\n"
        "\n"
        "def test_ambiguous_helper():\n"
        "    widget = make_widget(flag)\n"
        "    widget.render()\n"
        "\n"
        "def test_recursive_helper():\n"
        "    recurse()\n",
        encoding="utf-8",
    )

    result = infer_test_entries(tmp_path, [test_path])

    # The nested helper's constructor is visible under the selected caller,
    # but the incompatible return prevents guessing a caller-side method.
    ambiguous_id = "tests/test_widget.py::test_ambiguous_helper"
    recursive_id = "tests/test_widget.py::test_recursive_helper"
    assert [entry.symbol for entry in result.entries] == [
        "pkg/widget.py:Widget.__init__"
    ]
    assert {entry.test_id for entry in result.entries} == {ambiguous_id}
    assert any(
        diagnostic.test_id == ambiguous_id and "ambiguous" in diagnostic.reason
        for diagnostic in result.diagnostics
    )
    assert any(
        diagnostic.test_id == recursive_id and "recursive" in diagnostic.reason
        for diagnostic in result.diagnostics
    )


def test_helper_conditional_fallthrough_does_not_propagate_return_instance(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "widget.py").write_text(
        "class Widget:\n"
        "    def __init__(self):\n"
        "        pass\n"
        "\n"
        "    def render(self):\n"
        "        pass\n",
        encoding="utf-8",
    )
    test_path = tmp_path / "tests" / "test_widget.py"
    test_path.write_text(
        "from pkg.widget import Widget\n"
        "\n"
        "def maybe_widget(flag):\n"
        "    if flag:\n"
        "        return Widget()\n"
        "\n"
        "def test_fallthrough():\n"
        "    widget = maybe_widget(flag)\n"
        "    widget.render()\n",
        encoding="utf-8",
    )

    result = infer_test_entries(tmp_path, [test_path])

    assert [entry.symbol for entry in result.entries] == [
        "pkg/widget.py:Widget.__init__"
    ]
    assert any(
        "ambiguous" in diagnostic.reason or "fall through" in diagnostic.reason
        for diagnostic in result.diagnostics
    )


def test_helper_branch_only_assignment_does_not_propagate_instance(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "widget.py").write_text(
        "class Widget:\n"
        "    def __init__(self):\n"
        "        pass\n"
        "\n"
        "    def render(self):\n"
        "        pass\n",
        encoding="utf-8",
    )
    test_path = tmp_path / "tests" / "test_widget.py"
    test_path.write_text(
        "from pkg.widget import Widget\n"
        "\n"
        "def maybe_widget(flag):\n"
        "    if flag:\n"
        "        widget = Widget()\n"
        "    return widget\n"
        "\n"
        "def test_branch_assignment():\n"
        "    widget = maybe_widget(flag)\n"
        "    widget.render()\n",
        encoding="utf-8",
    )

    result = infer_test_entries(tmp_path, [test_path])

    assert [entry.symbol for entry in result.entries] == [
        "pkg/widget.py:Widget.__init__"
    ]
    assert any(
        diagnostic.expression == "widget.render" and "ambiguous" in diagnostic.reason
        for diagnostic in result.diagnostics
    )


def test_helper_branch_stops_after_return_without_unreachable_entry_leakage(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "widget.py").write_text(
        "class Widget:\n"
        "    def __init__(self):\n"
        "        pass\n"
        "\n"
        "    def render(self):\n"
        "        pass\n"
        "\n"
        "class Other:\n"
        "    def __init__(self):\n"
        "        pass\n",
        encoding="utf-8",
    )
    test_path = tmp_path / "tests" / "test_widget.py"
    test_path.write_text(
        "from pkg.widget import Other, Widget\n"
        "\n"
        "def make_widget(flag):\n"
        "    if flag:\n"
        "        return Widget()\n"
        "        Other()\n"
        "        return Other()\n"
        "    return Widget()\n"
        "\n"
        "def test_unreachable_helper_calls():\n"
        "    widget = make_widget(flag)\n"
        "    widget.render()\n",
        encoding="utf-8",
    )

    result = infer_test_entries(tmp_path, [test_path])

    assert [entry.symbol for entry in result.entries] == [
        "pkg/widget.py:Widget.__init__",
        "pkg/widget.py:Widget.render",
    ]
    assert all("Other" not in entry.symbol for entry in result.entries)


def test_constructor_uses_explicit_new_without_class_span(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "factory.py").write_text(
        "class Factory:\n"
        "    def __new__(cls, value):\n"
        "        return object.__new__(cls)\n"
        "\n"
        "    def unrelated(self):\n"
        "        return 1\n",
        encoding="utf-8",
    )
    test_path = tmp_path / "tests" / "test_factory.py"
    test_path.write_text(
        "from pkg.factory import Factory\n\ndef test_factory():\n    Factory(1)\n",
        encoding="utf-8",
    )

    result = infer_test_entries(tmp_path, [test_path])

    assert [entry.symbol for entry in result.entries] == [
        "pkg/factory.py:Factory.__new__"
    ]
    assert result.entries[0].start_line == 2
    assert result.entries[0].end_line == 3


def test_proven_overload_imports_leave_exact_concrete_function_and_method_spans(
    tmp_path,
):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "api.py").write_text(
        "from typing import overload\n"
        "from typing_extensions import overload as signature\n"
        "\n"
        "@overload\n"
        "def convert(value: int) -> int: ...\n"
        "\n"
        "def convert(value):\n"
        "    return value\n"
        "\n"
        "class Renderer:\n"
        "    @signature\n"
        "    def render(self, value: int) -> int:\n"
        "        ...\n"
        "\n"
        "    def render(self, value):\n"
        "        return value\n",
        encoding="utf-8",
    )
    test_path = tmp_path / "tests" / "test_api.py"
    test_path.write_text(
        "from pkg.api import Renderer, convert\n"
        "\n"
        "def test_runtime_definitions(renderer: Renderer):\n"
        "    convert(1)\n"
        "    renderer.render(1)\n",
        encoding="utf-8",
    )

    result = infer_test_entries(tmp_path, [test_path])

    actual_spans = [
        (entry.symbol, entry.start_line, entry.end_line) for entry in result.entries
    ]
    assert actual_spans == [
        ("pkg/api.py:Renderer.render", 15, 16),
        ("pkg/api.py:convert", 7, 8),
    ]
    assert result.diagnostics == ()


@pytest.mark.parametrize(
    "source",
    [
        "import typing\n@typing.overload\ndef chosen(value: int) -> int: ...\n",
        (
            "from typing import overload\n"
            "from unknown import *\n"
            "@overload\n"
            "def chosen(value: int) -> int: ...\n"
        ),
        (
            "from typing import overload\n"
            "overload = lambda function: function\n"
            "@overload\n"
            "def chosen(value: int) -> int: ...\n"
        ),
        (
            "from typing import overload as marker\n"
            "from typing_extensions import overload as marker\n"
            "@marker\n"
            "def chosen(value: int) -> int: ...\n"
        ),
        (
            "def marker(function): return function\n"
            "@marker\n"
            "def chosen(value: int) -> int: ...\n"
        ),
        (
            "@overload\n"
            "def chosen(value: int) -> int: ...\n"
            "from typing import overload\n"
        ),
        (
            "@marker\n"
            "def chosen(value: int) -> int: ...\n"
            "from typing_extensions import overload as marker\n"
        ),
    ],
    ids=[
        "qualified",
        "wildcard",
        "rebound",
        "ambiguous-alias",
        "unrelated",
        "later-direct-import",
        "later-aliased-import",
    ],
)
def test_unproven_overload_decorators_remain_ordinary_definitions(tmp_path, source):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "api.py").write_text(
        source + "\ndef chosen(value):\n    return value\n",
        encoding="utf-8",
    )
    test_path = tmp_path / "tests" / "test_api.py"
    test_path.write_text(
        "from pkg.api import chosen\n\ndef test_chosen():\n    chosen(1)\n",
        encoding="utf-8",
    )

    result = infer_test_entries(tmp_path, [test_path])

    assert result.entries == []
    assert any("ambiguous" in diagnostic.reason for diagnostic in result.diagnostics)


def test_shadowed_overload_and_nonunique_concrete_definitions_are_omitted(
    tmp_path,
):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "api.py").write_text(
        "from typing import overload\n"
        "\n"
        "class Shadowed:\n"
        "    overload = staticmethod(lambda function: function)\n"
        "    @overload\n"
        "    def run(self, value: int) -> int: ...\n"
        "    def run(self, value):\n"
        "        return value\n"
        "\n"
        "@overload\n"
        "def missing(value: int) -> int: ...\n"
        "\n"
        "@overload\n"
        "def multiple(value: int) -> int: ...\n"
        "def multiple(value):\n"
        "    return value\n"
        "def multiple(value):\n"
        "    return value + 1\n",
        encoding="utf-8",
    )
    test_path = tmp_path / "tests" / "test_api.py"
    test_path.write_text(
        "from pkg.api import Shadowed, missing, multiple\n"
        "\n"
        "def test_conservative_results(value: Shadowed):\n"
        "    value.run(1)\n"
        "    missing(1)\n"
        "    multiple(1)\n",
        encoding="utf-8",
    )

    result = infer_test_entries(tmp_path, [test_path])

    assert result.entries == []
    reasons = {
        diagnostic.expression: diagnostic.reason for diagnostic in result.diagnostics
    }
    assert "ambiguous" in reasons["value.run"]
    assert "unresolved" in reasons["missing"]
    assert "ambiguous" in reasons["multiple"]


def test_literal_false_module_flag_prunes_inactive_definitions(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "api.py").write_text(
        "FLAG = False\n"
        "\n"
        "if FLAG:\n"
        "    def choose():\n"
        "        return 'inactive'\n"
        "else:\n"
        "    def choose():\n"
        "        return 'active'\n"
        "\n"
        "class Widget:\n"
        "    def __init__(self):\n"
        "        self.value = 1\n"
        "\n"
        "    if FLAG:\n"
        "        def __init__(self):\n"
        "            self.value = 2\n"
        "\n"
        "    if FLAG:\n"
        "        def render(self):\n"
        "            return 'inactive'\n"
        "    else:\n"
        "        def render(self):\n"
        "            return 'active'\n",
        encoding="utf-8",
    )
    test_path = tmp_path / "tests" / "test_api.py"
    test_path.write_text(
        "from pkg.api import Widget, choose\n"
        "\n"
        "def test_active_definitions(widget: Widget):\n"
        "    choose()\n"
        "    Widget()\n"
        "    widget.render()\n",
        encoding="utf-8",
    )

    result = infer_test_entries(tmp_path, [test_path])

    assert [
        (entry.symbol, entry.start_line, entry.end_line) for entry in result.entries
    ] == [
        ("pkg/api.py:Widget.__init__", 11, 12),
        ("pkg/api.py:Widget.render", 22, 23),
        ("pkg/api.py:choose", 7, 8),
    ]
    assert result.diagnostics == ()


def test_literal_false_module_flag_selects_else_class_definition(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "api.py").write_text(
        "FLAG = False\n"
        "if FLAG:\n"
        "    class Factory:\n"
        "        def __init__(self): pass\n"
        "else:\n"
        "    class Factory:\n"
        "        def __init__(self): pass\n",
        encoding="utf-8",
    )
    test_path = tmp_path / "tests" / "test_api.py"
    test_path.write_text(
        "from pkg.api import Factory\n\ndef test_factory():\n    Factory()\n",
        encoding="utf-8",
    )

    result = infer_test_entries(tmp_path, [test_path])

    assert [
        (entry.symbol, entry.start_line, entry.end_line) for entry in result.entries
    ] == [("pkg/api.py:Factory.__init__", 7, 7)]
    assert result.diagnostics == ()


@pytest.mark.parametrize(
    "source",
    [
        ("if FLAG:\n    def choose(): return 'guarded'\nFLAG = False\n"),
        (
            "if setting:\n"
            "    FLAG = False\n"
            "if FLAG:\n"
            "    def choose(): return 'guarded'\n"
        ),
        (
            "from pkg.flags import FLAG\n"
            "FLAG = False\n"
            "if FLAG:\n"
            "    def choose(): return 'guarded'\n"
        ),
        ("FLAG = False\nFLAG = False\nif FLAG:\n    def choose(): return 'guarded'\n"),
        ("FLAG = False\nFLAG = value\nif FLAG:\n    def choose(): return 'guarded'\n"),
        ("FLAG = value\nif FLAG:\n    def choose(): return 'guarded'\n"),
        ("FLAG = False\nif settings.FLAG:\n    def choose(): return 'guarded'\n"),
        ("FLAG = False\nif not FLAG:\n    def choose(): return 'guarded'\n"),
        ("FLAG = False\nif FLAG and setting:\n    def choose(): return 'guarded'\n"),
        ("FLAG = False\nif FLAG is False:\n    def choose(): return 'guarded'\n"),
        ("FLAG = False\nif FLAG():\n    def choose(): return 'guarded'\n"),
    ],
    ids=[
        "later",
        "conditional",
        "imported",
        "multiply-assigned",
        "rebound",
        "non-literal",
        "attribute",
        "negated",
        "boolean",
        "comparison",
        "call",
    ],
)
def test_unproven_false_conditions_keep_both_definition_candidates(tmp_path, source):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "api.py").write_text(
        source + "\ndef choose():\n    return 'outer'\n",
        encoding="utf-8",
    )
    test_path = tmp_path / "tests" / "test_api.py"
    test_path.write_text(
        "from pkg.api import choose\n\ndef test_choose():\n    choose()\n",
        encoding="utf-8",
    )

    result = infer_test_entries(tmp_path, [test_path])

    assert result.entries == []
    assert any("ambiguous" in diagnostic.reason for diagnostic in result.diagnostics)


def test_literal_false_module_flag_does_not_override_class_local_shadow(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "api.py").write_text(
        "FLAG = False\n"
        "\n"
        "class Widget:\n"
        "    FLAG = True\n"
        "    if FLAG:\n"
        "        def render(self): return 'guarded'\n"
        "    def render(self): return 'outer'\n",
        encoding="utf-8",
    )
    test_path = tmp_path / "tests" / "test_api.py"
    test_path.write_text(
        "from pkg.api import Widget\n"
        "\n"
        "def test_render(widget: Widget):\n"
        "    widget.render()\n",
        encoding="utf-8",
    )

    result = infer_test_entries(tmp_path, [test_path])

    assert result.entries == []
    assert any("ambiguous" in diagnostic.reason for diagnostic in result.diagnostics)


def test_inference_excludes_ambiguous_control_flow_binding_with_diagnostic(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "one.py").write_text(
        "def run():\n    return 'one'\n", encoding="utf-8"
    )
    (tmp_path / "pkg" / "two.py").write_text(
        "def run():\n    return 'two'\n", encoding="utf-8"
    )
    test_path = tmp_path / "tests" / "test_ambiguous.py"
    test_path.write_text(
        "def test_ambiguous():\n"
        "    if True:\n"
        "        from pkg.one import run\n"
        "    else:\n"
        "        from pkg.two import run\n"
        "    run()\n",
        encoding="utf-8",
    )

    result = infer_test_entries(tmp_path, [test_path])

    assert result.entries == []
    assert len(result.diagnostics) == 1
    assert result.diagnostics[0].expression == "run"
    assert "ambiguous" in result.diagnostics[0].reason


def test_inference_is_deterministic_and_deduplicates_repeated_calls(tmp_path):
    test_path = _write_fixture(tmp_path)
    test_path.write_text(
        test_path.read_text(encoding="utf-8").replace(
            "    widget_module.add(2)\n", "    widget_module.add(2)\n    add(3)\n"
        ),
        encoding="utf-8",
    )

    first = infer_test_entries(tmp_path, [test_path])
    second = infer_test_entries(tmp_path, [test_path])

    assert first.as_dict() == second.as_dict()
    assert first.metadata.model_dump_json() == second.metadata.model_dump_json()
    assert len(first.entries) == 3


def test_inference_can_target_one_pytest_selector_without_pulling_peer_tests(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "api.py").write_text(
        "def alpha():\n    return 1\n\ndef beta():\n    return 2\n",
        encoding="utf-8",
    )
    test_path = tmp_path / "tests" / "test_api.py"
    test_path.write_text(
        "from pkg.api import alpha, beta\n\n"
        "def test_alpha_only():\n"
        "    alpha()\n\n"
        "def test_beta_only():\n"
        "    beta()\n",
        encoding="utf-8",
    )

    result = infer_test_entries(
        tmp_path,
        test_ids=["tests/test_api.py::test_alpha_only"],
    )

    assert [entry.symbol for entry in result.entries] == ["pkg/api.py:alpha"]
    assert result.diagnostics == ()


def test_selector_closure_excludes_peer_test_file(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "api.py").write_text(
        "def selected():\n    return 1\n\ndef peer():\n    return 2\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_selected.py").write_text(
        "from pkg.api import selected\n\ndef test_selected():\n    selected()\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_peer.py").write_text(
        "from pkg.api import peer\n\ndef test_peer():\n    peer()\n",
        encoding="utf-8",
    )

    result = infer_test_entries(
        tmp_path,
        test_ids=["tests/test_selected.py::test_selected"],
    )

    assert [entry.test_id for entry in result.entries] == [
        "tests/test_selected.py::test_selected"
    ]
    assert [entry.symbol for entry in result.entries] == ["pkg/api.py:selected"]


def test_unresolved_selectors_do_not_fall_back_to_explicit_test_path(tmp_path):
    test_path = _write_fixture(tmp_path)

    result = infer_test_entries(
        tmp_path,
        [test_path],
        test_ids=[
            "../tests/test_widget.py::test_widget",
            "not-a-test-selector",
        ],
    )

    assert result.entries == []
    assert {diagnostic.expression for diagnostic in result.diagnostics} == {
        "../tests/test_widget.py::test_widget",
        "not-a-test-selector",
    }
    assert all("selector" in diagnostic.reason for diagnostic in result.diagnostics)


def test_mixed_resolved_and_unresolved_selectors_remain_closed(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "api.py").write_text(
        "def selected():\n    return 1\n\ndef peer():\n    return 2\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_selected.py").write_text(
        "from pkg.api import selected\n\ndef test_selected():\n    selected()\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_peer.py").write_text(
        "from pkg.api import peer\n\ndef test_peer():\n    peer()\n",
        encoding="utf-8",
    )

    result = infer_test_entries(
        tmp_path,
        [tmp_path / "tests"],
        test_ids=[
            "tests/test_selected.py::test_selected",
            "../tests/test_peer.py::test_peer",
        ],
    )

    assert [entry.symbol for entry in result.entries] == ["pkg/api.py:selected"]
    assert [diagnostic.expression for diagnostic in result.diagnostics] == [
        "../tests/test_peer.py::test_peer"
    ]


def test_inference_can_resolve_unittest_style_selector_without_explicit_path(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "validators").mkdir()
    (tmp_path / "pkg" / "api.py").write_text(
        "def chosen():\n    return 1\n\ndef ignored():\n    return 2\n",
        encoding="utf-8",
    )
    (tmp_path / "validators" / "tests.py").write_text(
        "from pkg.api import chosen, ignored\n\n"
        "class TestSelected:\n"
        "    def test_selected(self):\n"
        "        chosen()\n\n"
        "class TestOther:\n"
        "    def test_other(self):\n"
        "        ignored()\n",
        encoding="utf-8",
    )

    result = infer_test_entries(
        tmp_path,
        test_ids=["test_selected (validators.tests.TestSelected)"],
    )

    assert [entry.symbol for entry in result.entries] == ["pkg/api.py:chosen"]
    assert result.diagnostics == ()


def test_inference_follows_package_reexports_for_module_aliases(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text(
        "from .widget import Widget\n",
        encoding="utf-8",
    )
    (tmp_path / "pkg" / "widget.py").write_text(
        "class Widget:\n    def render(self):\n        return 'ok'\n",
        encoding="utf-8",
    )
    test_path = tmp_path / "tests" / "test_api.py"
    test_path.write_text(
        "import pkg as api\n\ndef test_render():\n    api.Widget().render()\n",
        encoding="utf-8",
    )

    result = infer_test_entries(tmp_path, [test_path])

    assert [entry.symbol for entry in result.entries] == [
        "pkg/widget.py:Widget.render",
    ]
    assert [diagnostic.expression for diagnostic in result.diagnostics] == [
        "api.Widget"
    ]


def test_inference_resolves_proven_package_self_submodule(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text(
        "from pkg import feature\n",
        encoding="utf-8",
    )
    (tmp_path / "pkg" / "feature.py").write_text(
        "def run(value):\n    return value + 1\n",
        encoding="utf-8",
    )
    test_path = tmp_path / "tests" / "test_feature.py"
    test_path.write_text(
        "from pkg import feature\n\ndef test_run():\n    feature.run(1)\n",
        encoding="utf-8",
    )

    result = infer_test_entries(tmp_path, [test_path])

    assert [entry.symbol for entry in result.entries] == ["pkg/feature.py:run"]
    assert result.diagnostics == ()


@pytest.mark.parametrize(
    "initializer",
    [
        "from pkg import feature as feature\n",
        "from pkg import feature as renamed\n",
        "from . import feature\n",
        "from . import feature as renamed\n",
        "from other import feature\n",
        "from pkg import *\nfrom pkg import feature\n",
        "from pkg import feature\nfeature = object()\n",
        "from pkg import feature\ndef feature():\n    pass\n",
        "if enabled:\n    from pkg import feature\n",
        "from pkg import feature\nfrom pkg import feature\n",
    ],
)
def test_inference_rejects_unproven_package_self_submodule(tmp_path, initializer):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text(initializer, encoding="utf-8")
    (tmp_path / "pkg" / "feature.py").write_text(
        "def run(value):\n    return value + 1\n",
        encoding="utf-8",
    )
    if "from other import feature" in initializer:
        (tmp_path / "other.py").write_text(
            "def feature(value):\n    return value\n",
            encoding="utf-8",
        )
    test_path = tmp_path / "tests" / "test_feature.py"
    test_path.write_text(
        "from pkg import feature\n\ndef test_run():\n    feature.run(1)\n",
        encoding="utf-8",
    )

    result = infer_test_entries(tmp_path, [test_path])

    assert result.entries == []


def test_inference_rejects_self_import_without_physical_submodule(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text(
        "from pkg import feature\n",
        encoding="utf-8",
    )
    test_path = tmp_path / "tests" / "test_feature.py"
    test_path.write_text(
        "from pkg import feature\n\ndef test_run():\n    feature.run(1)\n",
        encoding="utf-8",
    )

    result = infer_test_entries(tmp_path, [test_path])

    assert result.entries == []


def test_inference_rejects_duplicate_physical_self_submodule(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text(
        "from pkg import feature\n",
        encoding="utf-8",
    )
    source = "def run(value):\n    return value + 1\n"
    (tmp_path / "pkg" / "feature.py").write_text(source, encoding="utf-8")
    (tmp_path / "src" / "pkg" / "feature.py").write_text(source, encoding="utf-8")
    test_path = tmp_path / "tests" / "test_feature.py"
    test_path.write_text(
        "from pkg import feature\n\ndef test_run():\n    feature.run(1)\n",
        encoding="utf-8",
    )

    result = infer_test_entries(tmp_path, [test_path])

    assert result.entries == []


def test_inference_follows_wildcard_package_reexports(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text(
        "from .api import *\n",
        encoding="utf-8",
    )
    (tmp_path / "pkg" / "api.py").write_text(
        "def chosen(value):\n    return value + 1\n",
        encoding="utf-8",
    )
    test_path = tmp_path / "tests" / "test_api.py"
    test_path.write_text(
        "from pkg import chosen\n\ndef test_chosen():\n    chosen(1)\n",
        encoding="utf-8",
    )

    result = infer_test_entries(tmp_path, [test_path])

    assert [entry.symbol for entry in result.entries] == ["pkg/api.py:chosen"]
    assert result.diagnostics == ()


def test_inference_traces_explicit_test_resource_to_its_setup_block(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "widget.py").write_text(
        "class Widget:\n"
        "    def __call__(self, value):\n"
        "        return value\n"
        "\n"
        "class Unrelated:\n"
        "    pass\n",
        encoding="utf-8",
    )
    selected_resource = tmp_path / "tests" / "cases.txt"
    selected_resource.write_text("one\n", encoding="utf-8")
    (tmp_path / "tests" / "other.txt").write_text("two\n", encoding="utf-8")
    (tmp_path / "tests" / "foreign").mkdir()
    (tmp_path / "tests" / "foreign" / "cases.txt").write_text(
        "three\n", encoding="utf-8"
    )
    (tmp_path / "tests" / "test_data_driven.py").write_text(
        "from pkg.widget import Unrelated, Widget\n"
        "\n"
        "def resource_path(name):\n"
        "    return name\n"
        "\n"
        "with open(resource_path('cases.txt')) as stream:\n"
        "    for value in stream:\n"
        "        DATA.append((Widget(), value))\n"
        "\n"
        "with open(resource_path('other.txt')) as stream:\n"
        "    OTHER.append(Unrelated())\n"
        "\n"
        "def test_unrelated_explicit_test():\n"
        "    Unrelated()\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "foreign" / "test_foreign.py").write_text(
        "from pkg.widget import Unrelated\n"
        "with open('cases.txt') as stream:\n"
        "    FOREIGN.append(Unrelated())\n",
        encoding="utf-8",
    )

    result = infer_test_entries(tmp_path, [selected_resource])

    # A class with no explicit constructor is deliberately omitted rather than
    # widening the entry to its whole ClassDef (or guessing inherited methods).
    assert result.entries == []


def test_call_form_assert_raises_resolves_function_and_explicit_constructors(
    tmp_path,
):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "pkg" / "api.py").write_text(
        "def rejected(value, *, reason=None):\n"
        "    raise ValueError(reason)\n"
        "\n"
        "class WithInit:\n"
        "    def __init__(self, value):\n"
        "        raise ValueError(value)\n"
        "\n"
        "class WithNew:\n"
        "    def __new__(cls, value):\n"
        "        raise ValueError(value)\n",
        encoding="utf-8",
    )
    test_path = tmp_path / "tests" / "test_api.py"
    test_path.write_text(
        "from pkg.api import WithInit, WithNew, rejected\n"
        "import pkg.api as api\n"
        "\n"
        "class TestCalls:\n"
        "    def test_attribute(self):\n"
        "        self.assertRaises(ValueError, api.rejected, 1)\n"
        "\n"
        "    def test_function(self):\n"
        "        self.assertRaises(ValueError, rejected, 1, reason='bad')\n"
        "\n"
        "    def test_init(self):\n"
        "        self.assertRaises(ValueError, WithInit, 1)\n"
        "\n"
        "    def test_new(self):\n"
        "        self.assertRaises(ValueError, WithNew, 1)\n",
        encoding="utf-8",
    )

    result = infer_test_entries(tmp_path, [test_path])

    assert [(entry.test_id, entry.symbol) for entry in result.entries] == [
        ("tests/test_api.py::TestCalls.test_attribute", "pkg/api.py:rejected"),
        ("tests/test_api.py::TestCalls.test_function", "pkg/api.py:rejected"),
        ("tests/test_api.py::TestCalls.test_init", "pkg/api.py:WithInit.__init__"),
        ("tests/test_api.py::TestCalls.test_new", "pkg/api.py:WithNew.__new__"),
    ]
    assert result.diagnostics == ()


def test_call_form_assert_raises_rejects_non_exact_or_unsafe_protocols(tmp_path):
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    (tmp_path / "tests").mkdir()
    source = "def rejected(value):\n    raise ValueError(value)\n"
    (tmp_path / "one" / "api.py").write_text(source, encoding="utf-8")
    (tmp_path / "two" / "api.py").write_text(source, encoding="utf-8")
    test_path = tmp_path / "tests" / "test_api.py"
    test_path.write_text(
        "from one.api import rejected as chosen\n"
        "from one.api import rejected as ambiguous\n"
        "from two.api import rejected as ambiguous\n"
        "\n"
        "class TestRejectedShapes:\n"
        "    def test_context_manager(self):\n"
        "        with self.assertRaises(ValueError):\n"
        "            pass\n"
        "\n"
        "    def test_missing_callable(self):\n"
        "        self.assertRaises(ValueError)\n"
        "\n"
        "    def test_keyword_callable(self):\n"
        "        self.assertRaises(ValueError, callable=chosen)\n"
        "\n"
        "    def test_lambda_callable(self):\n"
        "        self.assertRaises(ValueError, lambda: chosen(1))\n"
        "\n"
        "    def test_nested_call_callable(self):\n"
        "        self.assertRaises(ValueError, chosen(1))\n"
        "\n"
        "    def test_subscript_callable(self):\n"
        "        self.assertRaises(ValueError, [chosen][0], 1)\n"
        "\n"
        "    def test_ambiguous_callable(self):\n"
        "        self.assertRaises(ValueError, ambiguous, 1)\n"
        "\n"
        "    def test_unresolved_callable(self):\n"
        "        self.assertRaises(ValueError, missing, 1)\n"
        "\n"
        "    def test_other_receiver(self, case):\n"
        "        case.assertRaises(ValueError, chosen, 1)\n",
        encoding="utf-8",
    )

    result = infer_test_entries(tmp_path, [test_path])

    assert result.entries == []
    assert {
        (diagnostic.test_id, diagnostic.expression, diagnostic.reason)
        for diagnostic in result.diagnostics
    } == {
        (
            "tests/test_api.py::TestRejectedShapes.test_ambiguous_callable",
            "ambiguous",
            "ambiguous import binding",
        ),
        (
            "tests/test_api.py::TestRejectedShapes.test_other_receiver",
            "case.assertRaises",
            "unresolved attribute",
        ),
        (
            "tests/test_api.py::TestRejectedShapes.test_unresolved_callable",
            "missing",
            "unresolved name",
        ),
    }


def test_task_schema_is_optional_and_rejects_unsafe_present_metadata(tmp_path):
    historical = tmp_path / "historical.yaml"
    historical.write_text("instruction: old\ndifficulty: easy\n", encoding="utf-8")
    assert Task.from_yaml(historical).test_entry_inferences is None

    valid = InferenceMetadata(
        entries=[
            EntryModel(
                test_id="tests/test.py::test_x",
                path="pkg/widget.py",
                symbol="pkg/widget.py:Widget",
                start_line=1,
                end_line=3,
            )
        ]
    )
    assert valid.schema_version == 1
    assert (
        InferenceMetadata.model_validate(
            {
                "schema_version": 1,
                "language": "javascript",
                "source": "static_test_invocation",
                "entries": [],
            }
        ).language
        == "javascript"
    )
    assert (
        InferenceMetadata.model_validate(
            {
                "schema_version": 1,
                "language": "typescript",
                "source": "static_test_invocation",
                "entries": [],
            }
        ).language
        == "typescript"
    )
    with pytest.raises(ValidationError):
        EntryModel(
            test_id="tests/test.py::test_x",
            path="../widget.py",
            symbol="widget",
            start_line=1,
            end_line=3,
        )
    with pytest.raises(ValidationError):
        InferenceMetadata.model_validate(
            {
                "schema_version": 2,
                "language": "python",
                "source": "static_test_invocation",
                "entries": [],
            }
        )
    with pytest.raises(ValidationError):
        InferenceMetadata.model_validate(
            {
                "schema_version": 1,
                "language": "python",
                "source": "static_test_invocation",
                "entries": [
                    {
                        "test_id": "t",
                        "path": "pkg/widget.py",
                        "symbol": "widget",
                        "start_line": 4,
                        "end_line": 3,
                    }
                ],
            }
        )


def test_cli_write_check_and_preserve_unrelated_yaml(tmp_path):
    test_path = _write_fixture(tmp_path)
    task_yaml = tmp_path / "task.yaml"
    task_yaml.write_text(
        "instruction: |\n  Keep this canary text\n# unrelated comment\n"
        "difficulty: easy\ncanary: untouched\n",
        encoding="utf-8",
    )

    assert (
        infer_cli(
            [
                "--repo",
                str(tmp_path),
                "--tests",
                str(test_path),
                str(task_yaml),
            ]
        )
        == 0
    )
    written = task_yaml.read_text(encoding="utf-8")
    assert "# unrelated comment" in written
    assert "canary: untouched" in written
    assert yaml.safe_load(written)["instruction"] == "Keep this canary text\n"

    before_check = task_yaml.read_bytes()
    assert (
        infer_cli(
            [
                "--repo",
                str(tmp_path),
                "--tests",
                str(test_path),
                "--task-yaml",
                str(task_yaml),
                "--check",
            ]
        )
        == 0
    )
    assert task_yaml.read_bytes() == before_check

    task_yaml.write_text(
        written.replace("start_line: 7", "start_line: 8"), encoding="utf-8"
    )
    before_stale_check = task_yaml.read_bytes()
    assert (
        infer_cli(
            [
                "--repo",
                str(tmp_path),
                "--tests",
                str(test_path),
                "--task-yaml",
                str(task_yaml),
                "--check",
            ]
        )
        == 1
    )
    assert task_yaml.read_bytes() == before_stale_check

    task_yaml.write_text(
        written.replace("schema_version: 1", "schema_version: 99"),
        encoding="utf-8",
    )
    before_invalid_check = task_yaml.read_bytes()
    assert (
        infer_cli(
            [
                "--repo",
                str(tmp_path),
                "--tests",
                str(test_path),
                "--task-yaml",
                str(task_yaml),
                "--check",
            ]
        )
        == 1
    )
    assert task_yaml.read_bytes() == before_invalid_check
