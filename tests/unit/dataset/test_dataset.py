from pathlib import Path

import pytest

from testprism.dataset import Dataset


def _write_task(root: Path, task_key: str) -> Path:
    task_dir = root.joinpath(*task_key.split("/"))
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "task.yaml").write_text("instruction: demo\n", encoding="utf-8")
    return task_dir


def test_dataset_discovers_recursive_task_keys(tmp_path: Path) -> None:
    first = _write_task(tmp_path, "from-alpha/task-a")
    second = _write_task(tmp_path, "from-beta/group/task-b")

    dataset = Dataset(path=tmp_path)

    assert dataset.tasks == [first, second]
    assert dataset.task_ids == ["from-alpha/task-a", "from-beta/group/task-b"]
    assert dataset.task_id_for_path(first) == "from-alpha/task-a"
    assert dataset.task_id_for_path(second) == "from-beta/group/task-b"


def test_dataset_supports_exact_task_keys_unique_leafs_and_rejects_ambiguous_leafs(
    tmp_path: Path,
) -> None:
    _write_task(tmp_path, "from-alpha/shared")
    unique = _write_task(tmp_path, "from-alpha/unique")
    _write_task(tmp_path, "from-beta/shared")

    dataset = Dataset(path=tmp_path, task_ids=["from-alpha/shared", "unique"])

    assert dataset.tasks == [tmp_path / "from-alpha" / "shared", unique]
    assert dataset.task_ids == ["from-alpha/shared", "from-alpha/unique"]

    with pytest.raises(ValueError, match="ambiguous"):
        Dataset(path=tmp_path, task_ids=["shared"])


def test_dataset_globs_match_task_keys_leaf_ids_and_legacy_double_underscore_aliases(
    tmp_path: Path,
) -> None:
    _write_task(tmp_path, "from-quixbugs/task-a")
    _write_task(tmp_path, "from-quixbugs/task-b")
    _write_task(tmp_path, "from-other/task-c")

    by_task_key = Dataset(path=tmp_path, task_ids=["from-quixbugs/*"])
    by_legacy_alias = Dataset(path=tmp_path, task_ids=["from-quixbugs__*"])
    by_leaf_exclusion = Dataset(
        path=tmp_path,
        task_ids=["from-quixbugs/*"],
        exclude_task_ids=["task-b"],
    )

    expected = ["from-quixbugs/task-a", "from-quixbugs/task-b"]
    assert by_task_key.task_ids == expected
    assert by_legacy_alias.task_ids == expected
    assert by_leaf_exclusion.task_ids == ["from-quixbugs/task-a"]
