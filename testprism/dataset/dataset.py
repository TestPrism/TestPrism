"""Minimal Dataset class for loading local task directories.

Stripped-down version of the original Terminal-Bench Dataset:
only supports local path-based loading (no remote registry).
"""

import fnmatch
from pathlib import Path
from typing import Iterator, Self

from pydantic import BaseModel, Field, model_validator

from testprism.handlers.trial_handler import Task, TaskPaths
from testprism.patches.task_identity import (
    AmbiguousTaskSelectorError,
    TaskIdentity,
    TaskSelectorNotFoundError,
    discover_tasks,
    select_tasks,
)
from testprism.utils.logger import logger

_GLOB_META_CHARS = frozenset("*?[")


class DatasetConfig(BaseModel):
    """Configuration for loading a dataset."""

    path: Path
    task_ids: list[str] | None = None
    n_tasks: int | None = None
    exclude_task_ids: list[str] = Field(default_factory=list)
    name: str | None = None
    version: str | None = None
    registry_url: str | None = None
    local_registry_path: Path | None = None

    @model_validator(mode="after")
    def validate_config(self) -> Self:
        if self.task_ids is not None and self.n_tasks is not None:
            raise ValueError("Cannot specify both task_ids and n_tasks")
        return self


class Dataset:
    """Load and iterate over tasks in a local directory."""

    def __init__(
        self,
        path: Path,
        task_ids: list[str] | None = None,
        n_tasks: int | None = None,
        exclude_task_ids: list[str] | None = None,
        name: str | None = None,
        version: str | None = None,
        registry_url: str | None = None,
        local_registry_path: Path | None = None,
    ):
        self.config = DatasetConfig(
            path=path,
            task_ids=task_ids,
            n_tasks=n_tasks,
            exclude_task_ids=exclude_task_ids or [],
            name=name,
            version=version,
            registry_url=registry_url,
            local_registry_path=local_registry_path,
        )
        self._logger = logger.getChild(__name__)
        self._path = self.config.path.expanduser().resolve(strict=True)
        self._tasks: list[Path] = []
        self._identities: list[TaskIdentity] = []
        self._task_keys_by_path: dict[Path, str] = {}

        self._init_dataset()
        self._validate_task_paths()

    @staticmethod
    def _is_glob_selector(selector: str) -> bool:
        return any(char in selector for char in _GLOB_META_CHARS)

    @staticmethod
    def _legacy_task_id(identity: TaskIdentity) -> str:
        return identity.task_key.replace("/", "__")

    def _discover_identities(self) -> list[TaskIdentity]:
        identities = discover_tasks(self._path)
        if not identities:
            raise ValueError(f"No tasks found under {self._path}")
        return identities

    def _match_glob_selector(self, selector: str) -> list[TaskIdentity]:
        matches = [
            identity
            for identity in self._identities
            if fnmatch.fnmatch(identity.task_key, selector)
            or fnmatch.fnmatch(identity.task_id, selector)
            or fnmatch.fnmatch(self._legacy_task_id(identity), selector)
        ]
        if not matches:
            raise ValueError(f"No tasks found matching pattern: {selector}")
        return matches

    def _match_exact_selector(self, selector: str) -> list[TaskIdentity]:
        exact_matches = [
            identity
            for identity in self._identities
            if identity.task_key == selector
            or self._legacy_task_id(identity) == selector
        ]
        if exact_matches:
            if len(exact_matches) > 1:
                candidates = ", ".join(
                    identity.task_key for identity in sorted(
                        exact_matches, key=lambda item: item.task_key
                    )
                )
                raise ValueError(
                    f"task selector {selector!r} is ambiguous; use one of: "
                    f"{candidates}"
                )
            return exact_matches
        try:
            return select_tasks(self._identities, [selector])
        except TaskSelectorNotFoundError as exc:
            raise ValueError(f"No tasks found matching pattern: {selector}") from exc
        except AmbiguousTaskSelectorError as exc:
            raise ValueError(str(exc)) from exc

    def _resolve_selector(self, selector: str) -> list[TaskIdentity]:
        if self._is_glob_selector(selector):
            return self._match_glob_selector(selector)
        return self._match_exact_selector(selector)

    def _resolve_task_keys(
        self, selectors: list[str] | None, *, default_all: bool
    ) -> set[str]:
        if selectors is None:
            return (
                {identity.task_key for identity in self._identities}
                if default_all
                else set()
            )
        if not selectors:
            return set()

        resolved: set[str] = set()
        for selector in selectors:
            resolved.update(
                identity.task_key for identity in self._resolve_selector(selector)
            )
        return resolved

    def _validate_task_paths(self) -> None:
        for task_path in self._tasks:
            if not task_path.exists():
                raise FileNotFoundError(f"Task path {task_path} does not exist")

    def _init_dataset(self) -> None:
        self._identities = self._discover_identities()
        included = self._resolve_task_keys(self.config.task_ids, default_all=True)
        excluded = self._resolve_task_keys(
            self.config.exclude_task_ids, default_all=False
        )
        filtered = [
            identity
            for identity in self._identities
            if identity.task_key in included and identity.task_key not in excluded
        ]
        limited = self._limit_tasks([identity.task_path for identity in filtered])
        self._tasks = limited
        limited_paths = set(limited)
        self._task_keys_by_path = {
            identity.task_path: identity.task_key
            for identity in filtered
            if identity.task_path in limited_paths
        }

    def _limit_tasks(self, task_paths: list[Path]) -> list[Path]:
        if self.config.n_tasks is None:
            return task_paths
        limited = task_paths[: self.config.n_tasks]
        if len(limited) < self.config.n_tasks:
            self._logger.warning(
                f"Expected >= {self.config.n_tasks} tasks, "
                f"found {len(limited)}. Running all."
            )
        return limited

    def __iter__(self) -> Iterator[Path]:
        return iter(self._tasks)

    def __len__(self) -> int:
        return len(self._tasks)

    @property
    def tasks(self) -> list[Path]:
        return self._tasks

    @property
    def task_ids(self) -> list[str]:
        return [self.task_id_for_path(task_path) for task_path in self._tasks]

    def task_id_for_path(self, task_path: Path) -> str:
        task_id = self._task_keys_by_path.get(task_path)
        if task_id is not None:
            return task_id

        resolved = task_path.expanduser().resolve(strict=True)
        task_id = self._task_keys_by_path.get(resolved)
        if task_id is not None:
            return task_id
        raise KeyError(f"Task path {task_path} is not part of this dataset")

    def sort_by_duration(self) -> None:
        """Sort tasks by estimated duration (longest first)."""
        durations = []
        for tp in self._tasks:
            try:
                tpo = TaskPaths(tp)
                task = Task.from_yaml(tpo.task_config_path)
                dur = task.effective_estimated_duration_sec
            except Exception:
                dur = 210.0
            durations.append((tp, dur))
        durations.sort(key=lambda x: x[1], reverse=True)
        self._tasks = [tp for tp, _ in durations]
