import re
from pathlib import Path

import pytest

from testprism.patches.task_identity import (
    AmbiguousTaskSelectorError,
    TaskSelectorNotFoundError,
    discover_tasks,
    safe_task_slug,
    select_tasks,
)


def _task(dataset_root: Path, task_key: str) -> Path:
    task_dir = dataset_root / task_key
    task_dir.mkdir(parents=True)
    (task_dir / "task.yaml").write_text("instruction: demo\n", encoding="utf-8")
    return task_dir


def test_discovers_tasks_recursively_with_sorted_relative_task_keys(tmp_path) -> None:
    expected_paths = {
        "from-swebench-pro/instance-z": _task(
            tmp_path, "from-swebench-pro/instance-z"
        ),
        "from-appworld/group/instance-a": _task(
            tmp_path, "from-appworld/group/instance-a"
        ),
        "from-usaco/problem-b": _task(tmp_path, "from-usaco/problem-b"),
    }
    (tmp_path / "from-usaco" / "metadata.yaml").write_text("ignored: true\n")

    tasks = discover_tasks(tmp_path)

    assert [task.task_key for task in tasks] == sorted(expected_paths)
    for task in tasks:
        assert task.task_path == expected_paths[task.task_key]
        assert task.task_id == task.task_path.name
        assert task.source == task.task_key.split("/", 1)[0]


def test_exact_and_unique_leaf_selectors_filter_and_sort_tasks(tmp_path) -> None:
    _task(tmp_path, "source-z/task-c")
    _task(tmp_path, "source-a/task-b")
    _task(tmp_path, "source-a/task-a")
    tasks = discover_tasks(tmp_path)

    selected = select_tasks(tasks, ["task-c", "source-a/task-a"])

    assert [task.task_key for task in selected] == [
        "source-a/task-a",
        "source-z/task-c",
    ]


def test_ambiguous_leaf_selector_reports_all_candidate_task_keys(tmp_path) -> None:
    _task(tmp_path, "source-b/shared-task")
    _task(tmp_path, "source-a/shared-task")
    tasks = discover_tasks(tmp_path)

    with pytest.raises(AmbiguousTaskSelectorError) as raised:
        select_tasks(tasks, ["shared-task"])

    assert raised.value.selector == "shared-task"
    assert raised.value.candidates == (
        "source-a/shared-task",
        "source-b/shared-task",
    )
    assert "source-a/shared-task" in str(raised.value)
    assert "source-b/shared-task" in str(raised.value)


def test_unknown_selector_fails_clearly(tmp_path) -> None:
    _task(tmp_path, "source/task-a")

    with pytest.raises(TaskSelectorNotFoundError, match="missing"):
        select_tasks(discover_tasks(tmp_path), ["missing"])


def test_slug_is_stable_safe_hashed_and_bounded_for_docker(tmp_path) -> None:
    task_key = "Source With Spaces/" + "/".join(
        ["Very.Long_任务+Name"] * 20
    )
    _task(tmp_path, task_key)
    identity = discover_tasks(tmp_path)[0]

    rediscovered = discover_tasks(identity.dataset_root)[0]

    assert identity.slug == rediscovered.slug
    assert len(identity.slug) <= 63
    assert re.fullmatch(r"[a-z0-9][a-z0-9_-]*", identity.slug)
    assert re.search(r"-[0-9a-f]{10}$", identity.slug)
    assert identity.task_key == task_key


def test_slug_rewrites_dots_to_remain_compose_safe() -> None:
    slug = safe_task_slug(
        "from-swe-bench-multimodal/processing__p5.js-5555/validation/attempt-01"
    )

    assert "." not in slug
    assert re.fullmatch(r"[a-z0-9][a-z0-9_-]*", slug)


def test_slug_rewrites_from_swesmith_runtime_keys_to_remain_compose_safe() -> None:
    slug = safe_task_slug(
        "from-swesmith/oauthlib__oauthlib.1fd52536.combine_file__0q5tya4o/"
        "validation/oracle-baseline"
    )

    assert "." not in slug
    assert re.fullmatch(r"[a-z0-9][a-z0-9_-]*", slug)
    assert "oauthlib__oauthlib-1fd52536" in slug


def test_sanitization_collisions_still_have_distinct_hashes(tmp_path) -> None:
    _task(tmp_path, "source/task+a")
    _task(tmp_path, "source/task-a")

    first, second = discover_tasks(tmp_path)

    assert first.slug != second.slug


def test_same_leaf_id_in_two_sources_has_isolated_runtime_names(tmp_path) -> None:
    _task(tmp_path, "from-alpha/shared")
    _task(tmp_path, "from-beta/shared")
    first, second = discover_tasks(tmp_path)

    assert first.task_id == second.task_id == "shared"
    assert first.image_key != second.image_key
    assert first.container_prefix != second.container_prefix
    assert first.output_relative_path != second.output_relative_path
    assert first.output_relative_path == Path("from-alpha/shared")
    assert second.output_relative_path == Path("from-beta/shared")
    assert len(first.image_key) <= 63
    assert len(first.container_prefix) <= 63
