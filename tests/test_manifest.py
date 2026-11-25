from __future__ import annotations

import json
from pathlib import Path

import pytest

from parallel_agentic_os.manifest import load_manifest


def make_task(task_id: str = "alpha", **changes: object) -> dict[str, object]:
    task: dict[str, object] = {
        "id": task_id,
        "prompt": f"Implement {task_id}",
        "allowed_paths": [f"src/{task_id}"],
        "worktree": {"enabled": False},
    }
    task.update(changes)
    return task


def make_manifest(
    project: Path,
    tasks: list[dict[str, object]] | None = None,
    **changes: object,
) -> dict[str, object]:
    manifest: dict[str, object] = {
        "version": 1,
        "project": str(project),
        "tasks": tasks if tasks is not None else [make_task()],
    }
    manifest.update(changes)
    return manifest


def write_manifest(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def assert_invalid(path: Path, field: str) -> None:
    with pytest.raises(ValueError) as exc_info:
        load_manifest(path)
    assert field.lower() in str(exc_info.value).lower()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    return project


@pytest.fixture
def manifest_path(tmp_path: Path) -> Path:
    return tmp_path / "manifest.json"


def test_valid_manifest_returns_ordered_immutable_tasks(project: Path, manifest_path: Path) -> None:
    path = write_manifest(
        manifest_path,
        make_manifest(
            project,
            [
                make_task("alpha", allowed_paths=["src/alpha", "tests/test_alpha.py"]),
                make_task("beta", allowed_paths=["src/beta"]),
            ],
        ),
    )

    manifest = load_manifest(path)

    assert manifest.version == 1
    assert manifest.project == project.resolve()
    assert manifest.max_parallel == 2
    assert isinstance(manifest.tasks, tuple)
    assert [task.id for task in manifest.tasks] == ["alpha", "beta"]
    assert manifest.tasks[0].prompt == "Implement alpha"
    assert manifest.tasks[0].allowed_paths == ("src/alpha", "tests/test_alpha.py")
    assert manifest.tasks[0].worktree.enabled is False
    with pytest.raises((AttributeError, TypeError)):
        manifest.tasks[0].prompt = "changed"
    with pytest.raises((AttributeError, TypeError)):
        manifest.tasks[0].allowed_paths = ("changed",)


def test_valid_worktree_configuration_is_preserved(project: Path, manifest_path: Path) -> None:
    path = write_manifest(
        manifest_path,
        make_manifest(
            project,
            [
                make_task(
                    worktree={
                        "enabled": True,
                        "base_ref": "main",
                        "allow_dirty_base": True,
                    }
                )
            ],
            max_parallel=1,
        ),
    )

    task = load_manifest(path).tasks[0]

    assert task.worktree.enabled is True
    assert task.worktree.base_ref == "main"
    assert task.worktree.allow_dirty_base is True


@pytest.mark.parametrize(
    ("task_count", "expected_max_parallel"),
    [(1, 1), (4, 4), (5, 4), (64, 4)],
)
def test_task_count_boundaries_and_default_max_parallel(
    project: Path,
    manifest_path: Path,
    task_count: int,
    expected_max_parallel: int,
) -> None:
    tasks = [make_task(f"task-{index}") for index in range(task_count)]
    path = write_manifest(manifest_path, make_manifest(project, tasks))

    manifest = load_manifest(path)

    assert len(manifest.tasks) == task_count
    assert manifest.max_parallel == expected_max_parallel


@pytest.mark.parametrize("task_id", ["a", "a0-", "a" * 64])
def test_valid_task_id_boundaries(project: Path, manifest_path: Path, task_id: str) -> None:
    path = write_manifest(manifest_path, make_manifest(project, [make_task(task_id)]))

    assert load_manifest(path).tasks[0].id == task_id


def test_malformed_json_is_rejected(manifest_path: Path) -> None:
    manifest_path.write_text('{"version": 1, "tasks": [', encoding="utf-8")

    with pytest.raises(ValueError, match="JSON|json"):
        load_manifest(manifest_path)


@pytest.mark.parametrize("location", ["manifest", "task", "worktree"])
def test_unknown_fields_are_rejected_at_every_level(
    project: Path, manifest_path: Path, location: str
) -> None:
    payload = make_manifest(project)
    target = payload
    if location in {"task", "worktree"}:
        target = payload["tasks"][0]
    if location == "worktree":
        target = target["worktree"]
    target["unexpected"] = True
    path = write_manifest(manifest_path, payload)

    assert_invalid(path, "unexpected")


@pytest.mark.parametrize("field", ["branch", "path"])
def test_worktree_branch_and_path_cannot_be_supplied(
    project: Path, manifest_path: Path, field: str
) -> None:
    task = make_task(worktree={"enabled": True, field: "caller-controlled"})
    path = write_manifest(manifest_path, make_manifest(project, [task]))

    assert_invalid(path, field)


@pytest.mark.parametrize(
    ("location", "field"),
    [
        ("manifest", "version"),
        ("manifest", "project"),
        ("manifest", "tasks"),
        ("task", "id"),
        ("task", "prompt"),
        ("task", "allowed_paths"),
        ("worktree", "enabled"),
    ],
)
def test_missing_required_fields_are_rejected(
    project: Path, manifest_path: Path, location: str, field: str
) -> None:
    payload = make_manifest(project)
    target = payload
    if location in {"task", "worktree"}:
        target = payload["tasks"][0]
    if location == "worktree":
        target = target["worktree"]
    del target[field]
    path = write_manifest(manifest_path, payload)

    assert_invalid(path, field)


@pytest.mark.parametrize(
    ("location", "field", "value"),
    [
        ("manifest", "version", "1"),
        ("manifest", "version", 1.0),
        ("manifest", "version", True),
        ("manifest", "project", None),
        ("manifest", "project", 123),
        ("manifest", "tasks", {}),
        ("manifest", "tasks", ["not-an-object"]),
        ("manifest", "max_parallel", "2"),
        ("manifest", "max_parallel", 2.0),
        ("manifest", "max_parallel", True),
        ("task", "id", 1),
        ("task", "id", None),
        ("task", "prompt", 1),
        ("task", "prompt", None),
        ("task", "allowed_paths", "src/alpha"),
        ("task", "allowed_paths", [1]),
        ("task", "worktree", []),
        ("worktree", "enabled", "yes"),
        ("worktree", "base_ref", 1),
        ("worktree", "allow_dirty_base", "yes"),
    ],
)
def test_wrong_field_types_are_rejected(
    project: Path,
    manifest_path: Path,
    location: str,
    field: str,
    value: object,
) -> None:
    payload = make_manifest(project)
    target = payload
    if location in {"task", "worktree"}:
        target = payload["tasks"][0]
    if location == "worktree":
        target = target["worktree"]
        target.update({"base_ref": "main", "allow_dirty_base": False})
    target[field] = value
    path = write_manifest(manifest_path, payload)

    assert_invalid(path, field)


@pytest.mark.parametrize(
    "task_id",
    [
        "",
        " ",
        " alpha",
        "alpha ",
        "Alpha",
        "1alpha",
        "-alpha",
        "alpha_beta",
        "alpha/beta",
        "a" * 65,
    ],
)
def test_bad_task_ids_are_rejected(project: Path, manifest_path: Path, task_id: str) -> None:
    path = write_manifest(manifest_path, make_manifest(project, [make_task(task_id)]))

    assert_invalid(path, "id")


def test_duplicate_task_ids_are_rejected(project: Path, manifest_path: Path) -> None:
    path = write_manifest(
        manifest_path,
        make_manifest(
            project,
            [
                make_task("alpha", allowed_paths=["src/alpha"]),
                make_task("alpha", allowed_paths=["src/other"]),
            ],
        ),
    )

    assert_invalid(path, "id")


@pytest.mark.parametrize("prompt", ["", " ", "\n\t"])
def test_empty_or_whitespace_prompt_is_rejected(
    project: Path, manifest_path: Path, prompt: str
) -> None:
    path = write_manifest(manifest_path, make_manifest(project, [make_task(prompt=prompt)]))

    assert_invalid(path, "prompt")


@pytest.mark.parametrize("allowed_paths", [[], [""], [" "]])
def test_empty_allowed_paths_are_rejected(
    project: Path, manifest_path: Path, allowed_paths: list[str]
) -> None:
    path = write_manifest(
        manifest_path,
        make_manifest(project, [make_task(allowed_paths=allowed_paths)]),
    )

    assert_invalid(path, "allowed_paths")


@pytest.mark.parametrize("project_value", ["", " ", "\t"])
def test_empty_or_whitespace_project_is_rejected(manifest_path: Path, project_value: str) -> None:
    path = write_manifest(
        manifest_path,
        {"version": 1, "project": project_value, "tasks": [make_task()]},
    )

    assert_invalid(path, "project")


@pytest.mark.parametrize("version", [0, 2, -1])
def test_unsupported_version_is_rejected(project: Path, manifest_path: Path, version: int) -> None:
    path = write_manifest(manifest_path, make_manifest(project, version=version))

    assert_invalid(path, "version")


def test_empty_and_excess_task_lists_are_rejected(project: Path, manifest_path: Path) -> None:
    empty_path = write_manifest(manifest_path, make_manifest(project, []))
    assert_invalid(empty_path, "tasks")

    excess_tasks = [make_task(f"task-{index}") for index in range(65)]
    excess_path = write_manifest(manifest_path, make_manifest(project, excess_tasks))
    assert_invalid(excess_path, "tasks")


@pytest.mark.parametrize(
    "allowed_path",
    ["/tmp", "..", "../outside", "src/../../outside", ".", "./"],
)
def test_unsafe_allowed_paths_are_rejected(
    project: Path, manifest_path: Path, allowed_path: str
) -> None:
    path = write_manifest(
        manifest_path,
        make_manifest(project, [make_task(allowed_paths=[allowed_path])]),
    )

    assert_invalid(path, "allowed_paths")


@pytest.mark.parametrize("allowed_path", ["escape", "escape/file.txt"])
def test_symlink_escape_is_rejected(
    tmp_path: Path,
    project: Path,
    manifest_path: Path,
    allowed_path: str,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / "escape").symlink_to(outside, target_is_directory=True)
    path = write_manifest(
        manifest_path,
        make_manifest(project, [make_task(allowed_paths=[allowed_path])]),
    )

    assert_invalid(path, "allowed_paths")


@pytest.mark.parametrize(
    ("first_path", "second_path"),
    [("src", "src"), ("src", "src/module"), ("src/module", "src")],
)
def test_overlapping_allowed_paths_are_rejected(
    project: Path,
    manifest_path: Path,
    first_path: str,
    second_path: str,
) -> None:
    path = write_manifest(
        manifest_path,
        make_manifest(
            project,
            [
                make_task("alpha", allowed_paths=[first_path]),
                make_task("beta", allowed_paths=[second_path]),
            ],
        ),
    )

    assert_invalid(path, "allowed_paths")


def test_same_relative_paths_in_separate_worktrees_are_allowed(
    project: Path, manifest_path: Path
) -> None:
    path = write_manifest(
        manifest_path,
        make_manifest(
            project,
            [
                make_task(
                    "alpha",
                    allowed_paths=["src"],
                    worktree={"enabled": True},
                ),
                make_task(
                    "beta",
                    allowed_paths=["src"],
                    worktree={"enabled": True},
                ),
            ],
        ),
    )

    manifest = load_manifest(path)

    assert [task.allowed_paths for task in manifest.tasks] == [("src",), ("src",)]


@pytest.mark.parametrize("max_parallel", [0, -1, 17, 100])
def test_invalid_max_parallel_is_rejected(
    project: Path, manifest_path: Path, max_parallel: int
) -> None:
    path = write_manifest(manifest_path, make_manifest(project, max_parallel=max_parallel))

    assert_invalid(path, "max_parallel")


@pytest.mark.parametrize(
    ("task_count", "max_parallel", "expected"),
    [(2, 16, 2), (16, 16, 16)],
)
def test_max_parallel_is_clamped_to_task_count(
    project: Path,
    manifest_path: Path,
    task_count: int,
    max_parallel: int,
    expected: int,
) -> None:
    tasks = [make_task(f"task-{index}") for index in range(task_count)]
    path = write_manifest(
        manifest_path,
        make_manifest(project, tasks, max_parallel=max_parallel),
    )

    assert load_manifest(path).max_parallel == expected
