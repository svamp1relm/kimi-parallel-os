from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

type _JSONObject = dict[str, object]
_TASK_ID_PATTERN = re.compile(r"[a-z][a-z0-9-]{0,63}")


@dataclass(frozen=True, slots=True)
class WorktreeConfig:
    enabled: bool
    base_ref: str | None = None
    allow_dirty_base: bool = False


@dataclass(frozen=True, slots=True)
class Task:
    id: str
    prompt: str
    allowed_paths: tuple[str, ...]
    worktree: WorktreeConfig


@dataclass(frozen=True, slots=True)
class Manifest:
    version: int
    project: Path
    max_parallel: int
    tasks: tuple[Task, ...]


def _check_fields(
    value: _JSONObject,
    *,
    required: frozenset[str],
    optional: frozenset[str],
    location: str,
) -> None:
    unknown = value.keys() - required - optional
    if unknown:
        fields = ", ".join(sorted(unknown))
        raise ValueError(f"{location} contains unexpected field(s): {fields}")
    missing = required - value.keys()
    if missing:
        fields = ", ".join(sorted(missing))
        raise ValueError(f"{location} is missing required field(s): {fields}")


def _as_object(value: object, location: str) -> _JSONObject:
    if not isinstance(value, dict):
        raise ValueError(f"{location} must be a JSON object")
    return value


def _as_string(value: object, field: str, *, allow_blank: bool = False) -> str:
    if not isinstance(value, str) or (not allow_blank and not value.strip()):
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _as_integer(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field} must be an integer")
    return value


def _as_boolean(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def _parse_worktree(value: object, task_id: str) -> WorktreeConfig:
    data = _as_object(value, f"worktree for task {task_id!r}")
    _check_fields(
        data,
        required=frozenset({"enabled"}),
        optional=frozenset({"base_ref", "allow_dirty_base"}),
        location=f"worktree for task {task_id!r}",
    )
    base_ref = None
    if "base_ref" in data:
        base_ref = _as_string(data["base_ref"], "worktree.base_ref")
    allow_dirty_base = _as_boolean(data.get("allow_dirty_base", False), "worktree.allow_dirty_base")
    return WorktreeConfig(
        enabled=_as_boolean(data["enabled"], "worktree.enabled"),
        base_ref=base_ref,
        allow_dirty_base=allow_dirty_base,
    )


def _resolve_allowed_path(project: Path, value: object) -> tuple[str, Path]:
    raw_path = _as_string(value, "allowed_paths")
    relative_path = Path(raw_path)
    if relative_path.is_absolute() or relative_path == Path(".") or ".." in relative_path.parts:
        raise ValueError(f"allowed_paths entry {raw_path!r} must be a safe relative path")
    try:
        resolved_path = (project / relative_path).resolve(strict=False)
        resolved_path.relative_to(project)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"allowed_paths entry {raw_path!r} resolves outside project") from exc
    return raw_path, resolved_path


def _parse_task(value: object, index: int, project: Path) -> tuple[Task, tuple[Path, ...]]:
    location = f"tasks[{index}]"
    data = _as_object(value, location)
    _check_fields(
        data,
        required=frozenset({"id", "prompt", "allowed_paths"}),
        optional=frozenset({"worktree"}),
        location=location,
    )
    task_id = _as_string(data["id"], "task.id")
    if _TASK_ID_PATTERN.fullmatch(task_id) is None:
        raise ValueError(f"task.id {task_id!r} has an invalid format")
    prompt = _as_string(data["prompt"], f"prompt for task {task_id!r}")

    raw_paths = data["allowed_paths"]
    if not isinstance(raw_paths, list) or not raw_paths:
        raise ValueError(f"allowed_paths for task {task_id!r} must be a non-empty list")
    parsed_paths = [_resolve_allowed_path(project, raw_path) for raw_path in raw_paths]
    allowed_paths = tuple(raw_path for raw_path, _ in parsed_paths)
    resolved_paths = tuple(resolved for _, resolved in parsed_paths)
    worktree = (
        _parse_worktree(data["worktree"], task_id)
        if "worktree" in data
        else WorktreeConfig(enabled=False)
    )
    task = Task(
        id=task_id,
        prompt=prompt,
        allowed_paths=allowed_paths,
        worktree=worktree,
    )
    return task, resolved_paths


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first.is_relative_to(second) or second.is_relative_to(first)


def _check_path_overlaps(
    parsed_tasks: list[tuple[Task, tuple[Path, ...]]],
) -> None:
    shared_paths: list[tuple[str, Path]] = []
    for task, resolved_paths in parsed_tasks:
        if task.worktree.enabled:
            continue
        for resolved_path in resolved_paths:
            for other_task_id, other_path in shared_paths:
                if _paths_overlap(resolved_path, other_path):
                    raise ValueError(
                        f"allowed_paths overlap between tasks {other_task_id!r} and {task.id!r}"
                    )
            shared_paths.append((task.id, resolved_path))


def load_manifest(path: str | Path) -> Manifest:
    manifest_path = Path(path)
    try:
        raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid manifest JSON: {exc.msg}") from exc

    data = _as_object(raw_manifest, "manifest")
    _check_fields(
        data,
        required=frozenset({"version", "project", "tasks"}),
        optional=frozenset({"max_parallel", "worktree"}),
        location="manifest",
    )
    version = _as_integer(data["version"], "version")
    if version != 1:
        raise ValueError(f"Unsupported manifest version: {version}")

    project_value = _as_string(data["project"], "project")
    try:
        project = Path(project_value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError("project must resolve to an existing directory") from exc
    if not project.is_dir():
        raise ValueError("project must be a directory")

    raw_tasks = data["tasks"]
    if not isinstance(raw_tasks, list) or not 1 <= len(raw_tasks) <= 64:
        raise ValueError("tasks must be a list containing between 1 and 64 tasks")
    shared_worktree = data.get("worktree")
    if shared_worktree is not None:
        raw_tasks = [
            {**raw_task, "worktree": raw_task.get("worktree", shared_worktree)}
            if isinstance(raw_task, dict)
            else raw_task
            for raw_task in raw_tasks
        ]
    parsed_tasks = [
        _parse_task(raw_task, index, project) for index, raw_task in enumerate(raw_tasks)
    ]
    task_ids = [task.id for task, _ in parsed_tasks]
    if len(set(task_ids)) != len(task_ids):
        raise ValueError("task id values must be unique")
    _check_path_overlaps(parsed_tasks)

    default_max_parallel = min(4, len(parsed_tasks))
    max_parallel = _as_integer(data.get("max_parallel", default_max_parallel), "max_parallel")
    if not 1 <= max_parallel <= 16:
        raise ValueError("max_parallel must be between 1 and 16")

    return Manifest(
        version=version,
        project=project,
        max_parallel=min(max_parallel, len(parsed_tasks)),
        tasks=tuple(task for task, _ in parsed_tasks),
    )
