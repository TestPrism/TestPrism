"""Recursive task discovery and collision-safe runtime identities."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Iterable, Sequence

DOCKER_NAME_MAX_LENGTH: Final = 63
RUNTIME_NAME_PREFIX: Final = "ctb-patch-"
SLUG_HASH_LENGTH: Final = 10
_SLUG_MAX_LENGTH: Final = DOCKER_NAME_MAX_LENGTH - len(RUNTIME_NAME_PREFIX)
_UNSAFE_SLUG_CHARS = re.compile(r"[^a-z0-9_-]+")
_REPEATED_DASHES = re.compile(r"-+")


class TaskIdentityError(ValueError):
    """Base error for invalid task identities and selectors."""


class TaskDiscoveryError(TaskIdentityError):
    """Raised when a dataset cannot produce valid relative task keys."""


class TaskSelectorNotFoundError(TaskIdentityError):
    """Raised when a selector matches no discovered task."""

    def __init__(self, selector: str):
        self.selector = selector
        super().__init__(f"task selector {selector!r} matched no task")


class AmbiguousTaskSelectorError(TaskIdentityError):
    """Raised when a leaf selector matches tasks from multiple sources."""

    def __init__(self, selector: str, candidates: Iterable[str]):
        self.selector = selector
        self.candidates = tuple(sorted(candidates))
        joined = ", ".join(self.candidates)
        super().__init__(
            f"task selector {selector!r} is ambiguous; use one of: {joined}"
        )


def safe_task_slug(task_key: str, *, max_length: int = _SLUG_MAX_LENGTH) -> str:
    """Return a bounded compose-safe slug with a stable short hash."""

    minimum_length = SLUG_HASH_LENGTH + 2
    if max_length < minimum_length:
        raise ValueError(f"max_length must be at least {minimum_length}")

    digest = hashlib.sha256(task_key.encode("utf-8")).hexdigest()[:SLUG_HASH_LENGTH]
    normalized = _UNSAFE_SLUG_CHARS.sub("-", task_key.lower().replace("/", "-"))
    normalized = _REPEATED_DASHES.sub("-", normalized).strip("._-") or "task"
    base_length = max_length - SLUG_HASH_LENGTH - 1
    base = normalized[:base_length].rstrip("._-") or "task"
    return f"{base}-{digest}"


@dataclass(frozen=True, slots=True)
class TaskIdentity:
    """A dataset-relative task key and its runtime-safe derived names."""

    dataset_root: Path
    task_path: Path
    task_key: str

    @property
    def task_id(self) -> str:
        return self.task_path.name

    @property
    def source(self) -> str:
        return self.task_key.split("/", 1)[0]

    @property
    def slug(self) -> str:
        return safe_task_slug(self.task_key)

    @property
    def image_key(self) -> str:
        return f"{RUNTIME_NAME_PREFIX}{self.slug}"

    @property
    def container_prefix(self) -> str:
        return f"{RUNTIME_NAME_PREFIX}{self.slug}"

    @property
    def output_relative_path(self) -> Path:
        return Path(*self.task_key.split("/"))


def discover_tasks(dataset_root: str | Path) -> list[TaskIdentity]:
    """Recursively discover task directories beneath *dataset_root*."""

    root = Path(dataset_root).expanduser().resolve()
    if not root.is_dir():
        raise TaskDiscoveryError(f"dataset root is not a directory: {root}")

    tasks: list[TaskIdentity] = []
    for task_config in root.rglob("task.yaml"):
        if not task_config.is_file():
            continue
        task_path = task_config.parent
        relative_path = task_path.relative_to(root)
        if not relative_path.parts:
            raise TaskDiscoveryError(
                f"task.yaml must be below a source directory: {task_config}"
            )
        task_key = relative_path.as_posix()
        tasks.append(
            TaskIdentity(
                dataset_root=root,
                task_path=task_path,
                task_key=task_key,
            )
        )

    tasks.sort(key=lambda task: task.task_key)
    return tasks


def select_tasks(
    tasks: Iterable[TaskIdentity], selectors: Sequence[str] | None = None
) -> list[TaskIdentity]:
    """Select exact task keys or unambiguous leaf IDs and return sorted results."""

    discovered = tuple(tasks)
    if not selectors:
        return sorted(discovered, key=lambda task: task.task_key)

    by_key = {task.task_key: task for task in discovered}
    by_leaf: dict[str, list[TaskIdentity]] = {}
    for task in discovered:
        by_leaf.setdefault(task.task_id, []).append(task)

    selected: dict[str, TaskIdentity] = {}
    for selector in selectors:
        if selector in by_key:
            selected[selector] = by_key[selector]
            continue

        if "/" in selector:
            raise TaskSelectorNotFoundError(selector)

        matches = by_leaf.get(selector, [])
        if not matches:
            raise TaskSelectorNotFoundError(selector)
        if len(matches) > 1:
            raise AmbiguousTaskSelectorError(
                selector, (match.task_key for match in matches)
            )
        selected[matches[0].task_key] = matches[0]

    return [selected[key] for key in sorted(selected)]
