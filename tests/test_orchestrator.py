from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from conftest import (
    FakeKimi,
    PaoCLI,
    assert_owner_only,
    current_attempt,
    task_view,
    terminal,
)


def run_id(payload: dict) -> str:
    return payload["run_id"]


def assert_exact_spawn(event: dict, cwd: Path) -> None:
    assert event["cwd"] == str(cwd)
    assert event["argv"][0] == "-p"
    assert event["argv"][2:] == ["--output-format", "stream-json"]
    assert "FAKE_TASK=" in event["argv"][1]
    assert event["attempt_id"]
    assert event["pgid"] == event["pid"]


def wait_terminal(cli: PaoCLI, fake: FakeKimi, run: str) -> dict:
    return cli.wait_status(run, fake, terminal)


def git(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [shutil.which("git") or "git", *args],
        cwd=project,
        text=True,
        capture_output=True,
        check=True,
    )


def make_git_repo(project: Path) -> str:
    project.mkdir(parents=True, exist_ok=True)
    for name in ("alpha", "beta"):
        (project / name).mkdir(exist_ok=True)
        (project / name / "tracked.txt").write_text(f"{name}\n", encoding="utf-8")
    git(project, "init", "-b", "main")
    git(project, "config", "user.name", "PAO Tests")
    git(project, "config", "user.email", "pao@example.invalid")
    git(project, "add", ".")
    git(project, "commit", "-m", "initial")
    return git(project, "rev-parse", "HEAD").stdout.strip()


def install_git_wrapper(
    tmp_path: Path,
    *,
    fail_worktree_add: bool = False,
) -> tuple[Path, Path]:
    bin_dir = tmp_path / "git-bin"
    bin_dir.mkdir()
    log = tmp_path / "git-argv.jsonl"
    real_git = shutil.which("git")
    assert real_git
    wrapper = bin_dir / "git"
    wrapper.write_text(
        f"""#!{sys.executable}
import json
import os
import sys

with open(os.environ["PAO_GIT_LOG"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
if {fail_worktree_add!r} and sys.argv[1:3] == ["worktree", "add"]:
    print("fatal: branch or worktree already exists", file=sys.stderr)
    raise SystemExit(128)
os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])
""",
        encoding="utf-8",
    )
    wrapper.chmod(0o700)
    return bin_dir, log


def test_tc09_success_persists_session_and_exact_resume(
    cli: PaoCLI, fake_kimi: FakeKimi, write_manifest, project: Path
) -> None:
    manifest = write_manifest(project, ["alpha"])
    process = cli.start(["run", str(manifest), "--json"], fake_kimi)

    started = fake_kimi.wait_started("alpha")
    assert_exact_spawn(started, project)
    session = fake_kimi.complete(started)
    result = cli.finish(process)

    assert result.returncode == 0, (result.stdout, result.stderr)
    payload = json.loads(result.stdout)
    assert payload["status"] == "completed"
    status = cli.status(run_id(payload), fake_kimi)
    attempt = current_attempt(status, "alpha")
    assert attempt["status"] == "completed"
    assert attempt["exit_code"] == 0
    assert attempt["session_id"] == session
    assert attempt["resume"] == {
        "cwd": str(project),
        "argv": ["kimi", "--session", session],
    }
    human = cli.run(["status", run_id(payload)], fake_kimi)
    expected = f"cd {shlex.quote(str(project))} && kimi --session {session}"
    assert human.returncode == 0
    assert expected in human.stdout


def test_tc10_failed_task_does_not_cancel_independent_success(
    cli: PaoCLI, fake_kimi: FakeKimi, write_manifest, project: Path
) -> None:
    manifest = write_manifest(project, ["alpha", "beta"], max_parallel=2)
    process = cli.start(["run", str(manifest), "--json"], fake_kimi)
    alpha = fake_kimi.wait_started("alpha")
    beta = fake_kimi.wait_started("beta")

    failed_session = f"session_{uuid.uuid4()}"
    fake_kimi.complete(alpha, exit_code=23, session_id=failed_session)
    successful_session = fake_kimi.complete(beta)
    result = cli.finish(process)

    assert result.returncode == 4
    status = cli.status(run_id(json.loads(result.stdout)), fake_kimi)
    assert current_attempt(status, "alpha")["status"] == "failed"
    assert current_attempt(status, "alpha")["exit_code"] == 23
    assert current_attempt(status, "beta")["status"] == "completed"
    assert current_attempt(status, "beta")["session_id"] == successful_session
    assert fake_kimi.starts["alpha"] == fake_kimi.starts["beta"] == 1


def test_tc11_max_parallel_is_enforced_with_exact_start_events(
    cli: PaoCLI, fake_kimi: FakeKimi, write_manifest, project: Path
) -> None:
    manifest = write_manifest(project, ["alpha", "beta", "gamma"], max_parallel=2)
    process = cli.start(["run", str(manifest), "--json"], fake_kimi)
    alpha = fake_kimi.wait_started("alpha")
    beta = fake_kimi.wait_started("beta")
    assert set(fake_kimi.starts) <= {"alpha", "beta", "gamma"}
    assert fake_kimi.active == 2

    fake_kimi.complete(alpha)
    gamma = fake_kimi.wait_started("gamma")
    assert fake_kimi.max_active <= 2
    fake_kimi.complete(beta)
    fake_kimi.complete(gamma)
    result = cli.finish(process)

    assert result.returncode == 0
    assert fake_kimi.starts == {"alpha": 1, "beta": 1, "gamma": 1}
    assert fake_kimi.max_active == 2


def test_tc12_stop_targets_only_one_process_group(
    cli: PaoCLI, fake_kimi: FakeKimi, write_manifest, project: Path
) -> None:
    manifest = write_manifest(project, ["alpha", "beta"], max_parallel=2)
    _, launched = cli.json_result(["run", str(manifest), "--detach", "--json"], fake_kimi)
    run = run_id(launched)
    alpha = fake_kimi.wait_started("alpha")
    beta = fake_kimi.wait_started("beta")

    _, stopped = cli.json_result(["stop", run, "--task", "alpha", "--json"], fake_kimi)
    assert stopped["accepted"] is True
    fake_kimi.wait_event("signal", "alpha", attempt_id=alpha["attempt_id"])
    fake_kimi.wait_event("disconnected", "alpha", attempt_id=alpha["attempt_id"])
    assert not fake_kimi.is_connected(alpha)
    assert fake_kimi.is_connected(beta)

    fake_kimi.complete(beta)
    status = wait_terminal(cli, fake_kimi, run)
    assert current_attempt(status, "alpha")["status"] == "stopped"
    assert current_attempt(status, "beta")["status"] == "completed"
    assert fake_kimi.starts["alpha"] == fake_kimi.starts["beta"] == 1


@pytest.mark.parametrize("initial_state", ["failed", "stopped"])
def test_tc13_acknowledged_manual_retry_preserves_attempt_history_and_logs(
    cli: PaoCLI,
    fake_kimi: FakeKimi,
    write_manifest,
    project: Path,
    initial_state: str,
) -> None:
    manifest = write_manifest(project, ["alpha"])
    if initial_state == "failed":
        process = cli.start(["run", str(manifest), "--json"], fake_kimi)
        first = fake_kimi.wait_started("alpha")
        old_session = f"session_{uuid.uuid4()}"
        fake_kimi.complete(first, exit_code=9, session_id=old_session)
        initial = cli.finish(process)
        assert initial.returncode == 4
        run = run_id(json.loads(initial.stdout))
    else:
        _, launched = cli.json_result(["run", str(manifest), "--detach", "--json"], fake_kimi)
        run = run_id(launched)
        first = fake_kimi.wait_started("alpha")
        old_session = None
        cli.json_result(["stop", run, "--task", "alpha", "--json"], fake_kimi)
        fake_kimi.wait_event("disconnected", "alpha", attempt_id=first["attempt_id"])
        wait_terminal(cli, fake_kimi, run)

    before = cli.status(run, fake_kimi)
    old_attempt = current_attempt(before, "alpha")
    assert old_attempt["status"] == initial_state
    old_log = Path(old_attempt["log_path"])
    old_log_bytes = old_log.read_bytes()

    rejected = cli.run(["retry", run, "alpha", "--json"], fake_kimi)
    assert rejected.returncode == 3
    retry = cli.start(
        ["retry", run, "alpha", "--acknowledge-dirty-workspace", "--json"],
        fake_kimi,
    )
    second = fake_kimi.wait_started("alpha")
    assert second["attempt_id"] != first["attempt_id"]
    new_session = fake_kimi.complete(second)
    retried = cli.finish(retry)

    assert retried.returncode == 0, (retried.stdout, retried.stderr)
    after = cli.status(run, fake_kimi)
    attempts = task_view(after, "alpha")["attempts"]
    assert [attempt["attempt_no"] for attempt in attempts] == [2, 1]
    assert attempts[1]["status"] == initial_state
    assert attempts[1]["session_id"] == old_session
    assert attempts[0]["session_id"] == new_session
    assert new_session != old_session
    assert Path(attempts[0]["log_path"]) != old_log
    assert old_log.read_bytes() == old_log_bytes


def test_tc14_retry_rejects_completed_and_active_attempts(
    cli: PaoCLI, fake_kimi: FakeKimi, write_manifest, project: Path
) -> None:
    completed_manifest = write_manifest(project, ["alpha"])
    process = cli.start(["run", str(completed_manifest), "--json"], fake_kimi)
    started = fake_kimi.wait_started("alpha")
    fake_kimi.complete(started)
    completed = cli.finish(process)
    completed_run = run_id(json.loads(completed.stdout))

    rejected = cli.run(
        ["retry", completed_run, "alpha", "--acknowledge-dirty-workspace", "--json"],
        fake_kimi,
    )
    assert rejected.returncode == 3
    assert "completed" in rejected.stdout
    assert fake_kimi.starts["alpha"] == 1

    active_manifest = write_manifest(project, ["beta"])
    _, launched = cli.json_result(["run", str(active_manifest), "--detach", "--json"], fake_kimi)
    active_run = run_id(launched)
    active = fake_kimi.wait_started("beta")
    rejected = cli.run(
        ["retry", active_run, "beta", "--acknowledge-dirty-workspace", "--json"],
        fake_kimi,
    )
    assert rejected.returncode == 3
    assert "failed/stopped" in rejected.stdout
    assert fake_kimi.starts["beta"] == 1
    fake_kimi.complete(active)
    wait_terminal(cli, fake_kimi, active_run)


def test_tc15_external_failure_never_creates_automatic_retry(
    cli: PaoCLI, fake_kimi: FakeKimi, write_manifest, project: Path
) -> None:
    manifest = write_manifest(project, ["alpha"])
    process = cli.start(["run", str(manifest), "--json"], fake_kimi)
    started = fake_kimi.wait_started("alpha")
    fake_kimi.complete(started, exit_code=75)
    result = cli.finish(process)

    assert result.returncode == 4
    run = run_id(json.loads(result.stdout))
    assert len(task_view(cli.status(run, fake_kimi), "alpha")["attempts"]) == 1
    assert len(task_view(cli.status(run, fake_kimi), "alpha")["attempts"]) == 1
    assert fake_kimi.starts["alpha"] == 1


def test_tc16_worktree_uses_safe_argv_unique_resources_and_execution_cwd(
    cli: PaoCLI,
    fake_kimi: FakeKimi,
    write_manifest,
    tmp_path: Path,
) -> None:
    project = tmp_path / "git-project"
    base_commit = make_git_repo(project)
    bin_dir, git_log = install_git_wrapper(tmp_path)
    env = cli.env(
        fake_kimi,
        path=os.pathsep.join([str(bin_dir), os.environ["PATH"]]),
        extra={"PAO_GIT_LOG": str(git_log)},
    )
    manifest = write_manifest(
        project,
        ["alpha"],
        worktree={"enabled": True, "base_ref": "HEAD", "allow_dirty_base": False},
    )
    process = cli.start(["run", str(manifest), "--json"], fake_kimi, env=env)
    started = fake_kimi.wait_started("alpha")
    fake_kimi.complete(started)
    result = cli.finish(process)

    assert result.returncode == 0, (result.stdout, result.stderr)
    calls = [json.loads(line) for line in git_log.read_text(encoding="utf-8").splitlines()]
    worktree_add = next(call for call in calls if call[:2] == ["worktree", "add"])
    assert worktree_add[2] == "-b"
    branch = worktree_add[3]
    worktree = Path(worktree_add[4])
    assert branch.startswith("pao-")
    assert worktree.name.startswith("pao-")
    assert worktree != project
    assert started["cwd"] == str(worktree)
    status = cli.status(run_id(json.loads(result.stdout)), fake_kimi)
    task = task_view(status, "alpha")
    assert task["worktree"] == {
        "branch": branch,
        "path": str(worktree),
        "base_commit": base_commit,
    }
    assert (worktree / "alpha" / "tracked.txt").is_file()


@pytest.mark.parametrize("git_failure", ["not_a_repository", "existing_worktree_or_branch"])
def test_tc17_worktree_setup_failures_do_not_spawn_or_delete_resources(
    cli: PaoCLI,
    fake_kimi: FakeKimi,
    write_manifest,
    tmp_path: Path,
    git_failure: str,
) -> None:
    project = tmp_path / f"project-{git_failure}"
    project.mkdir()
    (project / "alpha").mkdir()
    sentinel = project / "alpha" / "keep.txt"
    sentinel.write_text("do not delete\n", encoding="utf-8")
    env = cli.env(fake_kimi)
    if git_failure == "existing_worktree_or_branch":
        make_git_repo(project)
        bin_dir, git_log = install_git_wrapper(tmp_path, fail_worktree_add=True)
        env = cli.env(
            fake_kimi,
            path=os.pathsep.join([str(bin_dir), os.environ["PATH"]]),
            extra={"PAO_GIT_LOG": str(git_log)},
        )
    manifest = write_manifest(project, ["alpha"], worktree={"enabled": True})

    result = cli.run(["run", str(manifest), "--json"], fake_kimi, env=env)

    assert result.returncode == 4
    payload = json.loads(result.stdout)
    assert payload["status"] == "setup_failed"
    assert payload["failure_reason"] in {"not_a_git_repository", "worktree_add_failed"}
    assert fake_kimi.starts["alpha"] == 0
    assert sentinel.read_text(encoding="utf-8") == "do not delete\n"


def test_tc18_detached_run_returns_only_after_socket_handshake(
    cli: PaoCLI, fake_kimi: FakeKimi, write_manifest, project: Path
) -> None:
    manifest = write_manifest(project, ["alpha"])
    _, launched = cli.json_result(["run", str(manifest), "--detach", "--json"], fake_kimi)

    assert launched["accepted"] is True
    assert launched["status"] == "running"
    assert launched["supervisor_pid"] > 0
    assert Path(launched["control_socket"]).is_socket()
    run = run_id(launched)
    started = fake_kimi.wait_started("alpha")
    session = fake_kimi.complete(started)
    status = wait_terminal(cli, fake_kimi, run)

    assert status["status"] == "completed"
    assert current_attempt(status, "alpha")["session_id"] == session


def test_tc21_missing_executable_is_actionable_and_other_tasks_are_still_attempted(
    cli: PaoCLI, fake_kimi: FakeKimi, write_manifest, project: Path, tmp_path: Path
) -> None:
    manifest = write_manifest(project, ["alpha", "beta"], max_parallel=2)
    missing = tmp_path / "missing" / "kimi"
    env = cli.env(fake_kimi, extra={"PAO_KIMI_EXECUTABLE": str(missing)})

    result = cli.run(["run", str(manifest), "--json"], fake_kimi, env=env)

    assert result.returncode == 4
    status = cli.status(run_id(json.loads(result.stdout)), fake_kimi)
    for task_id in ("alpha", "beta"):
        attempt = current_attempt(status, task_id)
        assert attempt["status"] == "failed"
        assert attempt["failure_reason"] == "executable_not_found"
        assert str(missing) in attempt["hint"]
        assert len(task_view(status, task_id)["attempts"]) == 1
    assert fake_kimi.starts["alpha"] == fake_kimi.starts["beta"] == 0


def test_tc22_nonzero_exit_retains_session_for_resume_and_retry(
    cli: PaoCLI, fake_kimi: FakeKimi, write_manifest, project: Path
) -> None:
    manifest = write_manifest(project, ["alpha"])
    process = cli.start(["run", str(manifest), "--json"], fake_kimi)
    started = fake_kimi.wait_started("alpha")
    session = f"session_{uuid.uuid4()}"
    fake_kimi.complete(started, exit_code=42, session_id=session)
    result = cli.finish(process)

    assert result.returncode == 4
    attempt = current_attempt(cli.status(run_id(json.loads(result.stdout)), fake_kimi), "alpha")
    assert attempt["status"] == "failed"
    assert attempt["exit_code"] == 42
    assert attempt["session_id"] == session
    assert attempt["resume"]["argv"] == ["kimi", "--session", session]


def test_tc23_state_logs_and_export_are_owner_only_and_exclude_raw_output(
    cli: PaoCLI,
    fake_kimi: FakeKimi,
    write_manifest,
    project: Path,
    tmp_path: Path,
) -> None:
    secret_prompt = "PROMPT-SECRET-09f4e2"
    secret_output = "RAW-LOG-SECRET-77a31c"
    manifest = write_manifest(
        project,
        [{"id": "alpha", "prompt": secret_prompt, "allowed_paths": ["alpha"]}],
    )
    process = cli.start(["run", str(manifest), "--json"], fake_kimi)
    started = fake_kimi.wait_started("alpha")
    fake_kimi.complete(started, raw=secret_output)
    result = cli.finish(process)
    assert result.returncode == 0
    run = run_id(json.loads(result.stdout))
    export = tmp_path / "owner-only.json"
    exported = cli.run(["export", run, "--format", "json", "--output", str(export)], fake_kimi)

    assert exported.returncode == 0
    assert_owner_only(cli.home)
    assert stat.S_IMODE(export.stat().st_mode) == 0o600
    assert secret_prompt not in exported.stdout
    assert secret_output not in exported.stdout
    exported_text = export.read_text(encoding="utf-8")
    assert secret_prompt not in exported_text
    assert secret_output not in exported_text


def test_tc26_completed_session_resumes_from_unrelated_cwd_while_peer_is_active(
    cli: PaoCLI,
    fake_kimi: FakeKimi,
    write_manifest,
    project: Path,
    tmp_path: Path,
) -> None:
    manifest = write_manifest(project, ["alpha", "beta"], max_parallel=2)
    _, launched = cli.json_result(["run", str(manifest), "--detach", "--json"], fake_kimi)
    run = run_id(launched)
    alpha = fake_kimi.wait_started("alpha")
    beta = fake_kimi.wait_started("beta")
    session = fake_kimi.complete(alpha)
    cli.wait_status(
        run,
        fake_kimi,
        lambda payload: current_attempt(payload, "alpha")["status"] == "completed",
    )

    status = cli.status(run, fake_kimi)
    resume = current_attempt(status, "alpha")["resume"]
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    resumed = subprocess.Popen(
        [str(fake_kimi.executable), *resume["argv"][1:]],
        cwd=unrelated,
        env=cli.env(fake_kimi),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    resumed_event = fake_kimi.wait_started("resume")
    assert resumed_event["session_id"] == session
    assert resumed_event["cwd"] == str(unrelated)
    assert fake_kimi.is_connected(beta)
    assert current_attempt(cli.status(run, fake_kimi), "beta")["status"] == "running"

    fake_kimi.complete(resumed_event, exit_code=0, session_id=None)
    assert resumed.wait(timeout=10) == 0
    fake_kimi.complete(beta)
    wait_terminal(cli, fake_kimi, run)


@pytest.mark.skipif(
    os.environ.get("PAO_REAL_KIMI_SMOKE") != "1",
    reason="real Kimi session consumes quota and is explicitly opt-in",
)
def test_tc26_real_kimi_smoke_is_never_part_of_default_suite(
    cli: PaoCLI, write_manifest, project: Path
) -> None:
    manifest = write_manifest(
        project,
        [{"id": "alpha", "prompt": "Reply with OK only.", "allowed_paths": ["alpha"]}],
    )
    result = cli.run(["run", str(manifest), "--json"], None, timeout=120)
    assert result.returncode == 0, (result.stdout, result.stderr)
    attempt = current_attempt(json.loads(result.stdout), "alpha")
    assert attempt["session_id"].startswith("session_")


def test_tc41_live_orphan_requires_operator_resolution_before_acknowledged_retry(
    cli: PaoCLI, fake_kimi: FakeKimi, write_manifest, project: Path
) -> None:
    manifest = write_manifest(project, ["alpha"])
    _, launched = cli.json_result(["run", str(manifest), "--detach", "--json"], fake_kimi)
    run = run_id(launched)
    orphan = fake_kimi.wait_started("alpha")
    os.kill(launched["supervisor_pid"], signal.SIGKILL)

    unknown = cli.wait_status(
        run,
        fake_kimi,
        lambda payload: current_attempt(payload, "alpha")["status"] == "unknown",
    )
    attempt = current_attempt(unknown, "alpha")
    assert attempt["failure_reason"] == "supervisor_lost"
    assert attempt["resume"] is None
    blocked = cli.run(["resolve", run, "--task", "alpha", "--json"], fake_kimi)
    assert blocked.returncode == 5
    assert str(orphan["pid"]) in blocked.stdout
    assert "manually" in blocked.stdout

    os.kill(orphan["pid"], signal.SIGTERM)
    fake_kimi.wait_event("disconnected", "alpha", attempt_id=orphan["attempt_id"])
    resolved = cli.run(["resolve", run, "--task", "alpha", "--json"], fake_kimi)
    assert resolved.returncode == 0, (resolved.stdout, resolved.stderr)
    attempt = current_attempt(cli.status(run, fake_kimi), "alpha")
    assert attempt["status"] == "failed"
    assert attempt["failure_reason"] == "supervisor_lost"

    retry = cli.start(
        ["retry", run, "alpha", "--acknowledge-dirty-workspace", "--json"],
        fake_kimi,
    )
    second = fake_kimi.wait_started("alpha")
    assert second["attempt_id"] != orphan["attempt_id"]
    fake_kimi.complete(second)
    result = cli.finish(retry)
    assert result.returncode == 0
    attempts = task_view(cli.status(run, fake_kimi), "alpha")["attempts"]
    assert [attempt["attempt_no"] for attempt in attempts] == [2, 1]


def test_tc45_dirty_base_opt_in_base_commit_and_failed_handshake_is_not_success(
    cli: PaoCLI,
    fake_kimi: FakeKimi,
    write_manifest,
    tmp_path: Path,
) -> None:
    project = tmp_path / "dirty-repo"
    base_commit = make_git_repo(project)
    (project / "alpha" / "dirty.txt").write_text("dirty\n", encoding="utf-8")
    rejected_manifest = write_manifest(project, ["alpha"], worktree={"enabled": True})
    rejected = cli.run(["run", str(rejected_manifest), "--json"], fake_kimi)
    assert rejected.returncode == 2
    assert fake_kimi.starts["alpha"] == 0

    accepted_manifest = write_manifest(
        project,
        ["alpha"],
        worktree={"enabled": True, "allow_dirty_base": True},
    )
    process = cli.start(["run", str(accepted_manifest), "--json"], fake_kimi)
    started = fake_kimi.wait_started("alpha")
    fake_kimi.complete(started)
    accepted = cli.finish(process)
    assert accepted.returncode == 0
    task = task_view(cli.status(run_id(json.loads(accepted.stdout)), fake_kimi), "alpha")
    assert task["worktree"]["base_commit"] == base_commit

    long_home = tmp_path / ("h" * 70) / ("h" * 70) / "pao-home"
    env = cli.env(fake_kimi, extra={"PAO_HOME": str(long_home)})
    handshake = cli.run(["run", str(accepted_manifest), "--detach", "--json"], fake_kimi, env=env)
    assert handshake.returncode == 5
    error = json.loads(handshake.stdout)
    assert "run_id" not in error
    assert error["error"]["code"] == "supervisor_unavailable"


@pytest.mark.parametrize(
    ("failure_kind", "exit_code"),
    [("quota_exceeded", 75), ("authentication_failed", 77)],
)
def test_tc48_persistent_global_failures_create_exactly_one_attempt_per_task(
    cli: PaoCLI,
    fake_kimi: FakeKimi,
    write_manifest,
    project: Path,
    failure_kind: str,
    exit_code: int,
) -> None:
    manifest = write_manifest(project, ["alpha", "beta"], max_parallel=2)
    process = cli.start(["run", str(manifest), "--json"], fake_kimi)
    started = [fake_kimi.wait_started(task) for task in ("alpha", "beta")]
    for event in started:
        raw = json.dumps({"type": "error", "error": {"type": failure_kind}})
        fake_kimi.complete(event, exit_code=exit_code, raw=raw)
    result = cli.finish(process)

    assert result.returncode == 4
    status = cli.status(run_id(json.loads(result.stdout)), fake_kimi)
    for task_id in ("alpha", "beta"):
        attempts = task_view(status, task_id)["attempts"]
        assert len(attempts) == 1
        assert attempts[0]["status"] == "failed"
        assert attempts[0]["failure_reason"] == failure_kind
        assert fake_kimi.starts[task_id] == 1
