from __future__ import annotations

from pathlib import Path

from conftest import PaoCLI
from parallel_agentic_os.manifest import load_manifest
from parallel_agentic_os.orchestrator import FileJournal, create_run, secure_home
from parallel_agentic_os.worktree import (
    CreatedResource,
    PreparedWorktree,
    SetupIntent,
    SetupResult,
    WorktreeManager,
)


def test_persistent_journal_result_and_retry_contract(
    cli: PaoCLI, write_manifest, project: Path
) -> None:
    manifest_path = write_manifest(project, ["alpha"])
    manifest = load_manifest(manifest_path)
    home = secure_home(cli.home)
    created = create_run(manifest, manifest_path, home)
    journal = FileJournal(home / "runs" / created["run_id"] / "setup-journal")
    intent = SetupIntent(
        run_id=created["run_id"],
        task_id="alpha",
        operation="worktree_add",
        base_commit="abc123",
        branch="pao-test",
        path=project / "worktree",
    )
    resource = CreatedResource(kind="worktree", path=intent.path)
    journal.record_result(SetupResult(status="prepared", intent=intent, resources=(resource,)))

    assert list(journal.root.glob("result-*.json"))
    manager = WorktreeManager(project)
    prepared_worktree = PreparedWorktree(
        run_id=intent.run_id,
        task_id=intent.task_id,
        base_ref="main",
        base_commit=intent.base_commit,
        branch=intent.branch,
        path=intent.path,
        effective_root=intent.path,
    )
    prepared = manager.retry(
        prepared_worktree,
        acknowledge_dirty_workspace=True,
    )
    assert prepared == prepared_worktree
