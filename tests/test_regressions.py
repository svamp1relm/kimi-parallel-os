from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from conftest import FakeKimi, PaoCLI, current_attempt, terminal
from parallel_agentic_os.manifest import load_manifest
from parallel_agentic_os.orchestrator import (
    Supervisor,
    create_run,
    open_log,
    project_run,
    secure_home,
)


def test_stop_during_spawn_is_tracked_and_reaped(
    cli: PaoCLI,
    fake_kimi: FakeKimi,
    write_manifest,
    project: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = write_manifest(project, ["alpha"])
    manifest = load_manifest(manifest_path)
    home = secure_home(cli.home)
    created = create_run(manifest, manifest_path, home)
    run_id = created["run_id"]
    supervisor = Supervisor(run_id, home)
    attempt = supervisor.store.list_attempts(run_id=run_id, task_id="alpha")[0]

    import parallel_agentic_os.orchestrator as orchestrator

    entered = threading.Event()
    release = threading.Event()
    real_spawn = orchestrator.spawn_kimi

    def gated_spawn(**kwargs):
        entered.set()
        release.wait(timeout=5)
        return real_spawn(**kwargs)

    monkeypatch.setattr(orchestrator, "spawn_kimi", gated_spawn)
    worker = threading.Thread(target=supervisor.execute, args=(attempt,))
    worker.start()
    assert entered.wait(timeout=5)
    response = supervisor._handle_command({"action": "stop", "task_id": "alpha"})
    assert response["accepted"] is True
    release.set()
    worker.join(timeout=10)

    assert worker.is_alive() is False
    assert current_attempt(project_run(run_id, home), "alpha")["status"] == "stopped"
    assert supervisor.live == {}


def test_stop_escalates_uncooperative_child(
    cli: PaoCLI, fake_kimi: FakeKimi, write_manifest, project: Path
) -> None:
    manifest = write_manifest(project, ["alpha"])
    _, launched = cli.json_result(["run", str(manifest), "--detach", "--json"], fake_kimi)
    run_id = launched["run_id"]
    started = fake_kimi.wait_started("alpha")
    fake_kimi.send(started, {"action": "ignore-term"})
    fake_kimi.wait_event("ignore-term", "alpha", attempt_id=started["attempt_id"])

    _, stopped = cli.json_result(["stop", run_id, "--task", "alpha", "--json"], fake_kimi)
    assert stopped["accepted"] is True
    fake_kimi.wait_event("disconnected", "alpha", attempt_id=started["attempt_id"], timeout=10)
    status = cli.wait_status(run_id, fake_kimi, terminal)

    assert current_attempt(status, "alpha")["status"] == "stopped"


def test_run_uses_immutable_manifest_after_original_is_deleted(
    cli: PaoCLI, write_manifest, project: Path
) -> None:
    manifest_path = write_manifest(project, ["alpha"])
    manifest = load_manifest(manifest_path)
    home = secure_home(cli.home)
    created = create_run(manifest, manifest_path, home)

    manifest_path.unlink()
    projected = project_run(created["run_id"], home)

    assert projected["run_id"] == created["run_id"]
    assert projected["tasks"][0]["task_id"] == "alpha"
    assert (home / "runs" / created["run_id"] / "manifest.json").is_file()


def test_setup_failure_persists_journal_and_spawns_nothing(
    cli: PaoCLI,
    fake_kimi: FakeKimi,
    write_manifest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_orchestrator import install_git_wrapper, make_git_repo

    project = tmp_path / "git-project"
    make_git_repo(project)
    bin_dir, git_log = install_git_wrapper(tmp_path, fail_worktree_add=True)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("PAO_GIT_LOG", str(git_log))
    manifest_path = write_manifest(
        project,
        ["alpha", "beta"],
        worktree={"enabled": True, "allow_dirty_base": True},
    )
    manifest = load_manifest(manifest_path)
    home = secure_home(cli.home)
    created = create_run(manifest, manifest_path, home)

    Supervisor(created["run_id"], home).run()
    projected = project_run(created["run_id"], home)

    assert projected["status"] == "setup_failed"
    assert fake_kimi.starts["alpha"] == fake_kimi.starts["beta"] == 0
    journal = home / "runs" / created["run_id"] / "setup-journal"
    assert journal.is_dir()
    assert list(journal.glob("intent-*.json"))


def test_exclusive_log_rejects_existing_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("keep", encoding="utf-8")
    log = tmp_path / "output.jsonl"
    log.symlink_to(target)

    with pytest.raises((FileExistsError, OSError)):
        open_log(log, exclusive=True)

    assert target.read_text(encoding="utf-8") == "keep"


def test_status_task_filter_and_unknown_stop_target(
    cli: PaoCLI, fake_kimi: FakeKimi, write_manifest, project: Path
) -> None:
    manifest_path = write_manifest(project, ["alpha", "beta"])
    manifest = load_manifest(manifest_path)
    home = secure_home(cli.home)
    created = create_run(manifest, manifest_path, home)
    run_id = created["run_id"]

    filtered = cli.status(run_id, fake_kimi)
    assert len(filtered["tasks"]) == 2
    _, task = cli.json_result(["status", run_id, "--task", "alpha", "--json"], fake_kimi)
    assert [item["task_id"] for item in task["tasks"]] == ["alpha"]
    stopped = cli.run(["stop", run_id, "--task", "missing", "--json"], fake_kimi)
    assert stopped.returncode == 3
    assert json.loads(stopped.stdout)["error"]["code"] == "task_not_found"
