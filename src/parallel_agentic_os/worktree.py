from __future__ import annotations

import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class CreatedResource:
    kind: str
    path: Path | None = None
    branch: str | None = None


@dataclass(frozen=True)
class PreparedWorktree:
    run_id: str
    task_id: str
    base_ref: str
    base_commit: str
    branch: str
    path: Path
    effective_root: Path


@dataclass(frozen=True)
class SetupIntent:
    run_id: str
    task_id: str
    operation: str
    base_commit: str
    branch: str
    path: Path


@dataclass(frozen=True)
class SetupResult:
    status: str
    intent: SetupIntent
    resources: tuple[CreatedResource, ...] = ()


@dataclass(frozen=True)
class SetupReconciliation:
    requires_manual_review: bool
    resources: tuple[CreatedResource, ...]


class WorktreeSetupError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        failure_reason: str = "worktree_setup_failed",
        resources: Iterable[CreatedResource] = (),
    ) -> None:
        self.status = "setup_failed"
        self.failure_reason = failure_reason
        self.resources = tuple(resources)
        super().__init__(message)


class DirtyBaseError(WorktreeSetupError):
    def __init__(self) -> None:
        super().__init__(
            "base project has uncommitted changes; explicit consent is required",
            failure_reason="dirty_base",
        )


class UnsafeExecutionRoot(ValueError):
    def __init__(self, task_id: str, effective_root: Path, allowed_path: str) -> None:
        self.task_id = task_id
        self.effective_root = effective_root
        self.allowed_path = allowed_path
        super().__init__(
            f"allowed path {allowed_path!r} for task {task_id!r} escapes its effective root"
        )


class WorkspaceConflict(ValueError):
    def __init__(self, task_ids: tuple[str, str]) -> None:
        self.task_ids = task_ids
        super().__init__(f"allowed paths overlap for tasks {task_ids[0]!r} and {task_ids[1]!r}")


class Journal(Protocol):
    def record_intent(self, intent: SetupIntent) -> None: ...

    def record_result(self, result: SetupResult) -> None: ...


class _MemoryJournal:
    def __init__(self) -> None:
        self.entries: list[tuple[str, object]] = []

    def record_intent(self, intent: SetupIntent) -> None:
        self.entries.append(("intent", intent))

    def record_result(self, result: SetupResult) -> None:
        self.entries.append(("result", result))


def _run_git(argv: Sequence[str], *, cwd: Path, shell: bool = False, **kwargs: Any):
    return subprocess.run(
        list(argv),
        cwd=cwd,
        shell=shell,
        capture_output=True,
        text=True,
        check=False,
        **kwargs,
    )


def _resources(intent: SetupIntent) -> tuple[CreatedResource, ...]:
    return (
        CreatedResource(kind="worktree", path=intent.path),
        CreatedResource(kind="branch", branch=intent.branch),
    )


class WorktreeManager:
    def __init__(
        self,
        project: Path,
        *,
        runner: Any = _run_git,
        journal: Journal | None = None,
    ) -> None:
        self.project = project
        self.runner = runner
        self.journal = journal if journal is not None else _MemoryJournal()

    def _git(self, *args: str):
        return self.runner(["git", *args], cwd=self.project, shell=False)

    def prepare(
        self,
        *,
        run_id: str,
        task_id: str,
        base_ref: str,
        allow_dirty_base: bool = False,
    ) -> PreparedWorktree:
        top_level = self._git("rev-parse", "--show-toplevel")
        if top_level.returncode != 0:
            raise WorktreeSetupError(
                "project is not a Git repository", failure_reason="not_a_git_repository"
            )

        revision = self._git("rev-parse", base_ref)
        if revision.returncode != 0 or not revision.stdout.strip():
            raise WorktreeSetupError(
                f"base ref {base_ref!r} could not be resolved",
                failure_reason="base_ref_unresolved",
            )
        base_commit = revision.stdout.strip()

        status = self._git("status", "--porcelain")
        if status.returncode != 0:
            raise WorktreeSetupError(
                "Git status could not be read", failure_reason="git_status_failed"
            )
        if status.stdout and not allow_dirty_base:
            raise DirtyBaseError()

        branch = f"pao-{run_id}-{task_id}"
        path = self.project.parent / f"pao-{self.project.name}-{run_id}-{task_id}"
        branch_check = self._git("show-ref", "--verify", "--quiet", f"refs/heads/{branch}")
        if branch_check.returncode == 0:
            raise WorktreeSetupError(
                f"branch {branch!r} already exists", failure_reason="branch_exists"
            )
        if branch_check.returncode != 1:
            raise WorktreeSetupError(
                f"branch {branch!r} could not be checked", failure_reason="branch_check_failed"
            )
        if path.exists():
            raise WorktreeSetupError(
                f"worktree path {str(path)!r} already exists", failure_reason="worktree_exists"
            )

        listed = self._git("worktree", "list", "--porcelain")
        if listed.returncode != 0:
            raise WorktreeSetupError(
                "existing worktrees could not be read", failure_reason="worktree_list_failed"
            )
        if f"worktree {path}" in listed.stdout.splitlines():
            raise WorktreeSetupError(
                f"worktree path {str(path)!r} is already registered",
                failure_reason="worktree_exists",
            )

        intent = SetupIntent(
            run_id=run_id,
            task_id=task_id,
            operation="worktree_add",
            base_commit=base_commit,
            branch=branch,
            path=path,
        )
        self.journal.record_intent(intent)
        added = self._git("worktree", "add", "-b", branch, str(path), base_commit)
        resources = _resources(intent)
        if added.returncode != 0:
            raise WorktreeSetupError(
                f"git worktree add failed: {added.stderr.strip()}",
                failure_reason="worktree_add_failed",
                resources=resources,
            )

        prepared = PreparedWorktree(
            run_id=run_id,
            task_id=task_id,
            base_ref=base_ref,
            base_commit=base_commit,
            branch=branch,
            path=path,
            effective_root=path,
        )
        self.journal.record_result(
            SetupResult(status="prepared", intent=intent, resources=resources)
        )
        return prepared

    def retry(
        self, prepared: PreparedWorktree, *, acknowledge_dirty_workspace: bool
    ) -> PreparedWorktree:
        if not acknowledge_dirty_workspace:
            raise WorktreeSetupError(
                "retry requires acknowledge_dirty_workspace=True",
                failure_reason="dirty_workspace_not_acknowledged",
            )
        return prepared


def reconcile_setup(
    entries: Iterable[tuple[str, object]], *, project: Path, runner: Any
) -> SetupReconciliation:
    del project, runner
    resources: list[CreatedResource] = []
    has_intent = False
    for kind, entry in entries:
        if kind != "intent" or not isinstance(entry, SetupIntent):
            continue
        has_intent = True
        resources.extend(_resources(entry))
    return SetupReconciliation(
        requires_manual_review=has_intent,
        resources=tuple(resources),
    )


def validate_execution_roots(
    roots: Mapping[str, tuple[Path, Iterable[str]]],
) -> None:
    validated: list[tuple[str, Path, tuple[Path, ...]]] = []
    for task_id, (effective_root, allowed_paths) in roots.items():
        canonical_root = effective_root.resolve()
        canonical_paths: list[Path] = []
        for allowed_path in allowed_paths:
            canonical_path = (effective_root / allowed_path).resolve()
            if canonical_path != canonical_root and canonical_root not in canonical_path.parents:
                raise UnsafeExecutionRoot(task_id, effective_root, allowed_path)
            canonical_paths.append(canonical_path)
        validated.append((task_id, canonical_root, tuple(canonical_paths)))

    for index, (first_id, first_root, first_paths) in enumerate(validated):
        for second_id, second_root, second_paths in validated[index + 1 :]:
            if first_root != second_root:
                continue
            if any(
                first == second or first in second.parents or second in first.parents
                for first in first_paths
                for second in second_paths
            ):
                raise WorkspaceConflict((first_id, second_id))
