from dataclasses import dataclass
from pathlib import Path

import pytest

from parallel_agentic_os.worktree import (
    DirtyBaseError,
    UnsafeExecutionRoot,
    WorkspaceConflict,
    WorktreeManager,
    WorktreeSetupError,
    reconcile_setup,
    validate_execution_roots,
)

BASE_COMMIT = "0123456789abcdef0123456789abcdef01234567"


@dataclass
class GitResult:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


class InjectedCrash(Exception):
    pass


class FakeGit:
    def __init__(self, project: Path) -> None:
        self.project = project
        self.is_git = True
        self.dirty = False
        self.fail_add = False
        self.crash_after_add = False
        self.any_branch_exists = False
        self.existing_branches: set[str] = set()
        self.existing_worktrees: dict[str, str] = {}
        self.calls: list[tuple[list[str], Path, bool]] = []

    def __call__(self, argv, *, cwd: Path, shell: bool = False, **kwargs) -> GitResult:
        argv = list(argv)
        self.calls.append((argv, cwd, shell))
        assert argv[0] == "git"
        command = tuple(argv[1:])

        if command == ("rev-parse", "--show-toplevel"):
            if not self.is_git:
                return GitResult(128, stderr="not a git repository")
            return GitResult(stdout=f"{self.project}\n")
        if len(command) == 2 and command[0] == "rev-parse":
            return GitResult(stdout=f"{BASE_COMMIT}\n")
        if command == ("status", "--porcelain"):
            return GitResult(stdout=" M tracked.txt\n" if self.dirty else "")
        if len(command) == 4 and command[:3] == ("show-ref", "--verify", "--quiet"):
            branch = command[3].removeprefix("refs/heads/")
            exists = self.any_branch_exists or branch in self.existing_branches
            return GitResult(0 if exists else 1)
        if command == ("worktree", "list", "--porcelain"):
            lines = []
            for path, branch in self.existing_worktrees.items():
                lines.extend((f"worktree {path}", f"branch refs/heads/{branch}"))
            return GitResult(stdout="\n".join(lines) + ("\n" if lines else ""))
        if len(command) == 6 and command[:3] == ("worktree", "add", "-b"):
            branch, path, _base_ref = command[3:]
            worktree = Path(path)
            worktree.mkdir(parents=True, exist_ok=False)
            self.existing_branches.add(branch)
            self.existing_worktrees[path] = branch
            if self.crash_after_add:
                raise InjectedCrash("crash after Git mutation before result commit")
            if self.fail_add:
                return GitResult(128, stderr="simulated partial worktree add")
            return GitResult()
        if command[:2] in {
            ("worktree", "remove"),
            ("worktree", "prune"),
            ("branch", "-D"),
            ("reset", "--hard"),
            ("clean", "-fd"),
        }:
            return GitResult()
        raise AssertionError(f"unexpected fake Git invocation: {argv!r}")

    def mutations(self) -> list[list[str]]:
        return [
            argv
            for argv, _cwd, _shell in self.calls
            if tuple(argv[1:3]) in {("worktree", "add"), ("worktree", "remove")}
        ]

    def destructive_calls(self) -> list[list[str]]:
        destructive = {
            ("worktree", "remove"),
            ("worktree", "prune"),
            ("branch", "-D"),
            ("reset", "--hard"),
            ("clean", "-fd"),
        }
        return [argv for argv, _cwd, _shell in self.calls if tuple(argv[1:3]) in destructive]


class MemoryJournal:
    def __init__(self) -> None:
        self.entries: list[tuple[str, object]] = []
        self.crash_after_intent = False
        self.crash_after_result = False

    def record_intent(self, intent) -> None:
        self.entries.append(("intent", intent))
        if self.crash_after_intent:
            raise InjectedCrash("crash after setup intent commit")

    def record_result(self, result) -> None:
        self.entries.append(("result", result))
        if self.crash_after_result:
            raise InjectedCrash("crash after setup result commit")


def make_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    return project


def prepare(manager: WorktreeManager, task_id: str = "task-a", **kwargs):
    return manager.prepare(run_id="run-1", task_id=task_id, base_ref="main", **kwargs)


def test_prepare_uses_exact_no_shell_worktree_add_with_unique_branch_and_path(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    git = FakeGit(project)
    manager = WorktreeManager(project, runner=git, journal=MemoryJournal())

    first = prepare(manager, "task-a")
    second = prepare(manager, "task-b")

    assert first.branch != second.branch
    assert first.path != second.path
    assert first.effective_root == first.path
    assert first.base_commit == BASE_COMMIT
    for run_task, prepared in (("task-a", first), ("task-b", second)):
        assert "run-1" in prepared.branch
        assert run_task in prepared.branch
        assert "run-1" in prepared.path.name
        assert run_task in prepared.path.name

    add_calls = [call for call in git.calls if call[0][1:3] == ["worktree", "add"]]
    assert len(add_calls) == 2
    argv, cwd, shell = add_calls[0]
    assert argv == [
        "git",
        "worktree",
        "add",
        "-b",
        first.branch,
        str(first.path),
        BASE_COMMIT,
    ]
    assert cwd == project
    assert shell is False
    assert all(call[2] is False for call in git.calls)


def test_existing_branch_is_rejected_without_mutation(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    git = FakeGit(project)
    git.any_branch_exists = True
    manager = WorktreeManager(project, runner=git, journal=MemoryJournal())

    with pytest.raises(WorktreeSetupError, match="branch"):
        prepare(manager)

    assert git.mutations() == []
    assert git.destructive_calls() == []


def test_existing_task_worktree_is_not_overwritten_or_deleted(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    git = FakeGit(project)
    manager = WorktreeManager(project, runner=git, journal=MemoryJournal())
    prepared = prepare(manager)
    sentinel = prepared.path / "uncommitted.txt"
    sentinel.write_text("keep me", encoding="utf-8")
    add_count = len(git.mutations())

    with pytest.raises(WorktreeSetupError, match="worktree|path|branch"):
        prepare(manager)

    assert len(git.mutations()) == add_count
    assert sentinel.read_text(encoding="utf-8") == "keep me"
    assert git.destructive_calls() == []


def test_non_git_project_is_rejected_before_any_side_effect(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    git = FakeGit(project)
    git.is_git = False
    manager = WorktreeManager(project, runner=git, journal=MemoryJournal())

    with pytest.raises(WorktreeSetupError, match="not a Git|not a git"):
        prepare(manager)

    assert git.mutations() == []
    assert git.destructive_calls() == []


def test_dirty_base_requires_explicit_consent_and_records_exact_base_commit(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    git = FakeGit(project)
    git.dirty = True
    journal = MemoryJournal()
    manager = WorktreeManager(project, runner=git, journal=journal)

    with pytest.raises(DirtyBaseError):
        prepare(manager)
    assert git.mutations() == []

    prepared = prepare(manager, allow_dirty_base=True)
    assert prepared.base_commit == BASE_COMMIT
    intents = [entry for kind, entry in journal.entries if kind == "intent"]
    assert intents
    assert all(intent.base_commit == BASE_COMMIT for intent in intents)


def test_partial_worktree_add_reports_created_resources_without_cleanup_or_kimi(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    git = FakeGit(project)
    git.fail_add = True
    journal = MemoryJournal()
    manager = WorktreeManager(project, runner=git, journal=journal)

    with pytest.raises(WorktreeSetupError) as raised:
        prepare(manager)

    error = raised.value
    assert error.status == "setup_failed"
    assert error.failure_reason == "worktree_add_failed"
    assert error.resources
    worktrees = [resource for resource in error.resources if resource.kind == "worktree"]
    branches = [resource for resource in error.resources if resource.kind == "branch"]
    assert len(worktrees) == 1
    assert len(branches) == 1
    assert worktrees[0].path.exists()
    assert branches[0].branch in git.existing_branches
    assert git.destructive_calls() == []
    assert all(argv[0] == "git" and argv[0] != "kimi" for argv, _cwd, _shell in git.calls)
    assert any(kind == "intent" for kind, _entry in journal.entries)


@pytest.mark.parametrize("crash_point", ["after_intent", "after_mutation", "after_result"])
def test_crash_around_git_mutation_and_journal_commit_reconciles_without_deletion(
    tmp_path: Path, crash_point: str
) -> None:
    project = make_project(tmp_path)
    git = FakeGit(project)
    journal = MemoryJournal()
    journal.crash_after_intent = crash_point == "after_intent"
    journal.crash_after_result = crash_point == "after_result"
    git.crash_after_add = crash_point == "after_mutation"
    manager = WorktreeManager(project, runner=git, journal=journal)

    with pytest.raises(InjectedCrash):
        prepare(manager)

    add_calls = [call for call in git.calls if call[0][1:3] == ["worktree", "add"]]
    if crash_point == "after_intent":
        assert add_calls == []
    else:
        assert len(add_calls) == 1
        assert Path(add_calls[0][0][5]).exists()

    report = reconcile_setup(journal.entries, project=project, runner=git)
    assert report.requires_manual_review is True
    assert git.destructive_calls() == []
    if crash_point != "after_intent":
        assert Path(add_calls[0][0][5]).exists()


def test_acknowledged_retry_reuses_same_dirty_worktree_without_clean_or_reset(
    tmp_path: Path,
) -> None:
    project = make_project(tmp_path)
    git = FakeGit(project)
    manager = WorktreeManager(project, runner=git, journal=MemoryJournal())
    first = prepare(manager)
    dirty_file = first.path / "agent-change.txt"
    dirty_file.write_text("uncommitted work from attempt one", encoding="utf-8")
    calls_before_retry = list(git.calls)

    with pytest.raises(WorktreeSetupError, match="acknowledge"):
        manager.retry(first, acknowledge_dirty_workspace=False)

    retry = manager.retry(first, acknowledge_dirty_workspace=True)

    assert retry.path == first.path
    assert retry.effective_root == first.effective_root
    assert retry.branch == first.branch
    assert retry.base_commit == first.base_commit
    assert dirty_file.read_text(encoding="utf-8") == "uncommitted work from attempt one"
    assert git.calls == calls_before_retry
    assert git.destructive_calls() == []


def test_distinct_effective_roots_may_use_same_relative_allowed_paths(tmp_path: Path) -> None:
    first = tmp_path / "worktree-a"
    second = tmp_path / "worktree-b"
    first.mkdir()
    second.mkdir()

    validate_execution_roots(
        {
            "task-a": (first, ("src", "tests")),
            "task-b": (second, ("src", "tests")),
        }
    )


def test_execution_root_validation_rejects_overlapping_allowed_paths(tmp_path: Path) -> None:
    root = tmp_path / "worktree"
    root.mkdir()

    with pytest.raises(WorkspaceConflict) as raised:
        validate_execution_roots(
            {
                "task-a": (root, ("src",)),
                "task-b": (root, ("src/feature",)),
            }
        )

    assert raised.value.task_ids == ("task-a", "task-b")


def test_execution_root_validation_canonicalizes_symlinked_cwds(tmp_path: Path) -> None:
    real_root = tmp_path / "real-worktree"
    real_root.mkdir()
    alias = tmp_path / "alias-worktree"
    alias.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(WorkspaceConflict):
        validate_execution_roots(
            {
                "task-a": (real_root, ("src",)),
                "task-b": (alias, ("src",)),
            }
        )


def test_symlink_inside_effective_root_cannot_escape_execution_cwd(tmp_path: Path) -> None:
    root = tmp_path / "worktree"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "src").symlink_to(outside, target_is_directory=True)

    with pytest.raises(UnsafeExecutionRoot) as raised:
        validate_execution_roots({"task-a": (root, ("src",))})

    assert raised.value.task_id == "task-a"
    assert raised.value.effective_root == root
    assert raised.value.allowed_path == "src"


def test_failed_execution_root_validation_has_no_git_side_effects(tmp_path: Path) -> None:
    project = make_project(tmp_path)
    git = FakeGit(project)
    journal = MemoryJournal()
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / "allowed").symlink_to(outside, target_is_directory=True)

    with pytest.raises(UnsafeExecutionRoot):
        validate_execution_roots({"task-a": (project, ("allowed",))})

    assert git.calls == []
    assert journal.entries == []
