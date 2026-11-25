from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import subprocess
import uuid
from pathlib import Path

from conftest import FakeKimi, PaoCLI, current_attempt, task_view, terminal


def run_id(payload: dict) -> str:
    return payload["run_id"]


def wait_terminal(cli: PaoCLI, fake: FakeKimi, run: str) -> dict:
    return cli.wait_status(run, fake, terminal)


def complete_run(
    cli: PaoCLI,
    fake: FakeKimi,
    manifest: Path,
    *,
    failures: dict[str, int] | None = None,
    raw: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, str | None]]:
    failures = failures or {}
    process = cli.start(["run", str(manifest), "--json"], fake)
    task_ids = set(failures) if failures else None
    if task_ids is None:
        task_ids = {"alpha"}
    started = {task_id: fake.wait_started(task_id) for task_id in sorted(task_ids)}
    sessions: dict[str, str | None] = {}
    for task_id, event in started.items():
        exit_code = failures.get(task_id, 0)
        session = None if exit_code == 0 else f"session_{uuid.uuid4()}"
        sessions[task_id] = fake.complete(
            event,
            exit_code=exit_code,
            session_id=session,
            raw=raw,
        )
    return cli.finish(process), sessions


def assert_structured_error(
    result: subprocess.CompletedProcess[str],
    *,
    returncode: int,
    code: str,
) -> dict:
    assert result.returncode == returncode, (result.stdout, result.stderr)
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert payload["schema_version"] == 1
    assert set(payload) == {"schema_version", "error"}
    error = payload["error"]
    assert set(error) == {"code", "message", "hint", "context"}
    assert error["code"] == code
    assert isinstance(error["message"], str) and error["message"]
    assert isinstance(error["hint"], str) and error["hint"]
    assert isinstance(error["context"], dict)
    return error


def test_tc19_list_and_status_human_json_show_sessions_and_resume(
    cli: PaoCLI, fake_kimi: FakeKimi, write_manifest, project: Path
) -> None:
    first_process = cli.start(["run", str(write_manifest(project, ["alpha"])), "--json"], fake_kimi)
    first_started = fake_kimi.wait_started("alpha")
    fake_kimi.complete(first_started)
    first = cli.finish(first_process)
    assert first.returncode == 0
    first_run = run_id(json.loads(first.stdout))

    second_manifest = write_manifest(project, ["alpha", "beta"], max_parallel=2)
    second_process = cli.start(["run", str(second_manifest), "--json"], fake_kimi)
    alpha = fake_kimi.wait_started("alpha")
    beta = fake_kimi.wait_started("beta")
    failed_session = f"session_{uuid.uuid4()}"
    fake_kimi.complete(alpha, exit_code=12, session_id=failed_session)
    second_session = fake_kimi.complete(beta)
    second = cli.finish(second_process)
    assert second.returncode == 4
    second_run = run_id(json.loads(second.stdout))

    _, listing = cli.json_result(["list", "--json"], fake_kimi)
    assert [run["run_id"] for run in listing["runs"]] == [second_run, first_run]
    assert [run["status"] for run in listing["runs"]] == ["partial", "completed"]
    _, status = cli.json_result(["status", second_run, "--json"], fake_kimi)
    assert [task["task_id"] for task in status["tasks"]] == ["alpha", "beta"]
    assert current_attempt(status, "alpha")["session_id"] == failed_session
    assert current_attempt(status, "beta")["session_id"] == second_session
    assert current_attempt(status, "alpha")["resume"]["argv"] == [
        "kimi",
        "--session",
        failed_session,
    ]

    human_list = cli.run(["list"], fake_kimi)
    human_status = cli.run(["status", second_run], fake_kimi)
    assert human_list.returncode == human_status.returncode == 0
    assert first_run in human_list.stdout and second_run in human_list.stdout
    assert "partial" in human_list.stdout and "completed" in human_list.stdout
    assert failed_session in human_status.stdout and second_session in human_status.stdout
    assert f"kimi --session {second_session}" in human_status.stdout
    assert "alpha" in human_status.stdout and "beta" in human_status.stdout


def test_tc20_export_completed_json_and_markdown_excludes_sensitive_content(
    cli: PaoCLI,
    fake_kimi: FakeKimi,
    write_manifest,
    project: Path,
) -> None:
    prompt_secret = "PROMPT-SECRET-TC20"
    log_secret = "RAW-LOG-SECRET-TC20"
    file_secret = "PROJECT-FILE-SECRET-TC20"
    (project / "alpha" / "secret.txt").write_text(file_secret, encoding="utf-8")
    manifest = write_manifest(
        project,
        [{"id": "alpha", "prompt": prompt_secret, "allowed_paths": ["alpha"]}],
    )
    process = cli.start(["run", str(manifest), "--json"], fake_kimi)
    started = fake_kimi.wait_started("alpha")
    session = fake_kimi.complete(started, raw=log_secret)
    result = cli.finish(process)
    run = run_id(json.loads(result.stdout))

    json_export = cli.run(["export", run, "--format", "json"], fake_kimi)
    markdown_export = cli.run(["export", run, "--format", "markdown"], fake_kimi)

    assert json_export.returncode == markdown_export.returncode == 0
    for output in (json_export.stdout, markdown_export.stdout):
        assert run in output
        assert "alpha" in output
        assert session in output
        assert prompt_secret not in output
        assert log_secret not in output
        assert file_secret not in output
    exported = json.loads(json_export.stdout)
    assert exported["schema_version"] == 1
    assert exported["run"]["project"] == str(project)
    assert markdown_export.stdout.startswith("# Parallel agents run ")


def test_tc24_full_fake_cli_run_status_retry_export_without_real_kimi(
    cli: PaoCLI,
    fake_kimi: FakeKimi,
    write_manifest,
    project: Path,
    tmp_path: Path,
) -> None:
    manifest = write_manifest(project, ["alpha"])
    process = cli.start(["run", str(manifest), "--json"], fake_kimi)
    first = fake_kimi.wait_started("alpha")
    fake_kimi.complete(first, exit_code=31)
    failed = cli.finish(process)
    assert failed.returncode == 4
    run = run_id(json.loads(failed.stdout))
    assert current_attempt(cli.status(run, fake_kimi), "alpha")["status"] == "failed"

    retry = cli.start(
        ["retry", run, "alpha", "--acknowledge-dirty-workspace", "--json"],
        fake_kimi,
    )
    second = fake_kimi.wait_started("alpha")
    assert second["attempt_id"] != first["attempt_id"]
    session = fake_kimi.complete(second)
    assert cli.finish(retry).returncode == 0
    status = cli.status(run, fake_kimi)
    assert current_attempt(status, "alpha")["session_id"] == session
    assert len(task_view(status, "alpha")["attempts"]) == 2

    output = tmp_path / "full-e2e.md"
    exported = cli.run(["export", run, "--format", "markdown", "--output", str(output)], fake_kimi)
    assert exported.returncode == 0
    assert session in output.read_text(encoding="utf-8")
    assert shutil.which("kimi", path=str(fake_kimi.root)) is None
    assert fake_kimi.starts["alpha"] == 2


def test_tc33_golden_empty_running_mixed_failed_and_unknown_views(
    cli: PaoCLI, fake_kimi: FakeKimi, write_manifest, project: Path
) -> None:
    empty = cli.run(["list"], fake_kimi)
    assert empty.returncode == 0
    assert empty.stdout == "No runs.\n"
    _, empty_json = cli.json_result(["list", "--json"], fake_kimi)
    assert empty_json == {"schema_version": 1, "runs": []}

    manifest = write_manifest(project, ["alpha", "beta"], max_parallel=2)
    _, launched = cli.json_result(["run", str(manifest), "--detach", "--json"], fake_kimi)
    run = run_id(launched)
    alpha = fake_kimi.wait_started("alpha")
    beta = fake_kimi.wait_started("beta")
    premature = f"session_{uuid.uuid4()}"
    fake_kimi.send(alpha, {"action": "output", "session_id": premature})
    running = cli.status(run, fake_kimi)
    assert running["status"] == "running"
    for task_id in ("alpha", "beta"):
        attempt = current_attempt(running, task_id)
        assert attempt["status"] == "running"
        assert attempt["session_id"] is None
        assert attempt["resume"] is None
    running_human = cli.run(["status", run], fake_kimi)
    assert "after completion" in running_human.stdout

    completed_session = fake_kimi.complete(alpha)
    failed_session = f"session_{uuid.uuid4()}"
    fake_kimi.complete(beta, exit_code=19, session_id=failed_session)
    mixed = wait_terminal(cli, fake_kimi, run)
    assert mixed["status"] == "partial"
    assert [task["task_id"] for task in mixed["tasks"]] == ["alpha", "beta"]
    assert current_attempt(mixed, "alpha")["resume"]["argv"][-1] == completed_session
    assert current_attempt(mixed, "beta")["resume"]["argv"][-1] == failed_session
    assert cli.status(run, fake_kimi) == mixed
    assert (
        cli.run(["list", "--json"], fake_kimi).stdout
        == cli.run(["list", "--json"], fake_kimi).stdout
    )

    _, orphaned_launch = cli.json_result(
        ["run", str(write_manifest(project, ["alpha"])), "--detach", "--json"],
        fake_kimi,
    )
    orphaned_run = run_id(orphaned_launch)
    orphan = fake_kimi.wait_started("alpha")
    os.kill(orphaned_launch["supervisor_pid"], signal.SIGKILL)
    unknown = cli.wait_status(
        orphaned_run,
        fake_kimi,
        lambda payload: current_attempt(payload, "alpha")["status"] == "unknown",
    )
    unknown_attempt = current_attempt(unknown, "alpha")
    assert unknown_attempt["resume"] is None
    assert unknown_attempt["resolve"]["argv"] == [
        "parallel-agents",
        "resolve",
        orphaned_run,
        "--task",
        "alpha",
    ]
    unknown_human = cli.run(["status", orphaned_run], fake_kimi)
    assert "unknown" in unknown_human.stdout
    assert f"parallel-agents resolve {orphaned_run} --task alpha" in unknown_human.stdout
    os.kill(orphan["pid"], signal.SIGTERM)
    fake_kimi.wait_event("disconnected", "alpha", attempt_id=orphan["attempt_id"])
    cli.run(["resolve", orphaned_run, "--task", "alpha", "--json"], fake_kimi)


def test_tc34_structured_errors_cover_validation_not_found_conflict_supervisor_and_io(
    cli: PaoCLI,
    fake_kimi: FakeKimi,
    write_manifest,
    project: Path,
    tmp_path: Path,
) -> None:
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{not-json", encoding="utf-8")
    error = assert_structured_error(
        cli.run(["run", str(invalid), "--json"], fake_kimi),
        returncode=2,
        code="invalid_manifest",
    )
    assert error["context"]["path"] == str(invalid)
    assert_structured_error(
        cli.run(["status", "run-does-not-exist", "--json"], fake_kimi),
        returncode=3,
        code="run_not_found",
    )

    manifest = write_manifest(project, ["alpha"])
    _, launched = cli.json_result(["run", str(manifest), "--detach", "--json"], fake_kimi)
    run = run_id(launched)
    active = fake_kimi.wait_started("alpha")
    error = assert_structured_error(
        cli.run(
            ["retry", run, "alpha", "--acknowledge-dirty-workspace", "--json"],
            fake_kimi,
        ),
        returncode=3,
        code="attempt_not_retryable",
    )
    assert error["context"]["state"] == "running"
    os.kill(launched["supervisor_pid"], signal.SIGKILL)
    assert_structured_error(
        cli.run(["stop", run, "--task", "alpha", "--json"], fake_kimi),
        returncode=5,
        code="supervisor_unavailable",
    )
    os.kill(active["pid"], signal.SIGTERM)
    fake_kimi.wait_event("disconnected", "alpha", attempt_id=active["attempt_id"])
    cli.run(["resolve", run, "--task", "alpha", "--json"], fake_kimi)

    blocker = tmp_path / "not-a-directory"
    blocker.write_text("unchanged", encoding="utf-8")
    error = assert_structured_error(
        cli.run(
            ["export", run, "--output", str(blocker / "export.json"), "--json"],
            fake_kimi,
        ),
        returncode=4,
        code="io_error",
    )
    assert error["context"]["path"] == str(blocker / "export.json")
    assert blocker.read_text(encoding="utf-8") == "unchanged"


def test_tc35_export_states_allowlist_determinism_atomic_file_and_overwrite(
    cli: PaoCLI,
    fake_kimi: FakeKimi,
    write_manifest,
    project: Path,
    tmp_path: Path,
) -> None:
    prompt_secret = "PROMPT-SECRET-TC35"
    log_secret = "RAW-LOG-SECRET-TC35"
    manifest = write_manifest(
        project,
        [
            {"id": "alpha", "prompt": prompt_secret, "allowed_paths": ["alpha"]},
            {"id": "beta", "prompt": prompt_secret, "allowed_paths": ["beta"]},
        ],
        max_parallel=2,
    )
    _, launched = cli.json_result(["run", str(manifest), "--detach", "--json"], fake_kimi)
    run = run_id(launched)
    alpha = fake_kimi.wait_started("alpha")
    beta = fake_kimi.wait_started("beta")

    running = cli.run(["export", run, "--format", "json"], fake_kimi)
    assert running.returncode == 0
    running_payload = json.loads(running.stdout)
    assert running_payload["run"]["status"] == "running"
    assert prompt_secret not in running.stdout and log_secret not in running.stdout

    fake_kimi.complete(alpha)
    failed_session = f"session_{uuid.uuid4()}"
    fake_kimi.complete(beta, exit_code=8, session_id=failed_session, raw=log_secret)
    wait_terminal(cli, fake_kimi, run)
    first = cli.run(["export", run, "--format", "json"], fake_kimi)
    second = cli.run(["export", run, "--format", "json"], fake_kimi)
    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout
    assert prompt_secret not in first.stdout and log_secret not in first.stdout
    payload = json.loads(first.stdout)
    assert set(payload) == {"schema_version", "run"}
    exported_run = payload["run"]
    assert set(exported_run) == {
        "run_id",
        "project",
        "status",
        "created_at",
        "finished_at",
        "tasks",
    }
    for task in exported_run["tasks"]:
        assert set(task) == {"task_id", "status", "allowed_paths", "worktree", "attempts"}
        for attempt in task["attempts"]:
            assert set(attempt) == {
                "attempt_no",
                "status",
                "failure_reason",
                "exit_code",
                "session_id",
                "resume",
                "created_at",
                "started_at",
                "finished_at",
            }

    output = tmp_path / "export.json"
    written = cli.run(["export", run, "--output", str(output)], fake_kimi)
    assert written.returncode == 0
    assert output.read_text(encoding="utf-8") == first.stdout
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert list(tmp_path.glob("export.json.tmp*")) == []
    original = output.read_bytes()
    assert_structured_error(
        cli.run(["export", run, "--output", str(output), "--json"], fake_kimi),
        returncode=3,
        code="output_exists",
    )
    assert output.read_bytes() == original
    forced = cli.run(
        ["export", run, "--format", "markdown", "--output", str(output), "--force"],
        fake_kimi,
    )
    assert forced.returncode == 0
    assert output.read_text(encoding="utf-8").startswith("# Parallel agents run ")
    assert log_secret not in output.read_text(encoding="utf-8")


def test_tc36_state_symlink_permissions_and_sensitive_logs_are_fail_closed(
    cli: PaoCLI,
    fake_kimi: FakeKimi,
    write_manifest,
    project: Path,
    tmp_path: Path,
) -> None:
    actual_home = tmp_path / "actual-home"
    actual_home.mkdir()
    symlink_home = tmp_path / "symlink-home"
    symlink_home.symlink_to(actual_home, target_is_directory=True)
    env = cli.env(fake_kimi, extra={"PAO_HOME": str(symlink_home)})
    assert_structured_error(
        cli.run(["list", "--json"], fake_kimi, env=env),
        returncode=2,
        code="unsafe_state_home",
    )
    assert list(actual_home.iterdir()) == []

    permissive_home = tmp_path / "permissive-home"
    permissive_home.mkdir(mode=0o777)
    permissive_home.chmod(0o777)
    env = cli.env(fake_kimi, extra={"PAO_HOME": str(permissive_home)})
    assert_structured_error(
        cli.run(["list", "--json"], fake_kimi, env=env),
        returncode=2,
        code="unsafe_state_home",
    )

    prompt_secret = "PROMPT-SECRET-TC36"
    log_secret = "RAW-LOG-SECRET-TC36"
    manifest = write_manifest(
        project,
        [{"id": "alpha", "prompt": prompt_secret, "allowed_paths": ["alpha"]}],
    )
    process = cli.start(["run", str(manifest), "--json"], fake_kimi)
    started = fake_kimi.wait_started("alpha")
    fake_kimi.complete(started, exit_code=4, raw=log_secret)
    failed = cli.finish(process)
    run = run_id(json.loads(failed.stdout))
    blocker = tmp_path / "io-blocker"
    blocker.write_text("x", encoding="utf-8")
    io_error = cli.run(["export", run, "--output", str(blocker / "out.json"), "--json"], fake_kimi)
    assert io_error.returncode == 4
    assert prompt_secret not in io_error.stdout + io_error.stderr
    assert log_secret not in io_error.stdout + io_error.stderr
    exported = cli.run(["export", run], fake_kimi)
    assert prompt_secret not in exported.stdout
    assert log_secret not in exported.stdout

    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
    assert "argv" in readme
    assert "process list" in readme.lower()
    assert "secret" in readme.lower()


def test_tc37_fake_harness_uses_events_and_absolute_injection_without_path_kimi(
    cli: PaoCLI,
    fake_kimi: FakeKimi,
    write_manifest,
    project: Path,
    tmp_path: Path,
) -> None:
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    assert shutil.which("kimi", path=str(empty_path)) is None
    env = cli.env(fake_kimi, path=str(empty_path))
    manifest = write_manifest(project, ["alpha"])
    process = cli.start(["run", str(manifest), "--json"], fake_kimi, env=env)

    started = fake_kimi.wait_started("alpha")
    assert started["argv"][0] == "-p"
    assert started["argv"][2:] == ["--output-format", "stream-json"]
    assert started["cwd"] == str(project)
    fake_kimi.complete(started)
    fake_kimi.wait_event("exiting", "alpha", attempt_id=started["attempt_id"])
    fake_kimi.wait_event("disconnected", "alpha", attempt_id=started["attempt_id"])
    result = cli.finish(process)

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert fake_kimi.starts == {"alpha": 1}


def test_tc46_json_accepted_terminal_idempotent_stop_and_failed_details(
    cli: PaoCLI, fake_kimi: FakeKimi, write_manifest, project: Path
) -> None:
    manifest = write_manifest(project, ["alpha"])
    _, launched = cli.json_result(["run", str(manifest), "--detach", "--json"], fake_kimi)
    run = run_id(launched)
    assert launched["accepted"] is True
    assert launched["status"] == "running"
    first = fake_kimi.wait_started("alpha")

    _, stop_one = cli.json_result(["stop", run, "--task", "alpha", "--json"], fake_kimi)
    fake_kimi.wait_event("disconnected", "alpha", attempt_id=first["attempt_id"])
    _, stop_two = cli.json_result(["stop", run, "--task", "alpha", "--json"], fake_kimi)
    assert stop_one["accepted"] == stop_two["accepted"] is True
    assert stop_one["run_id"] == stop_two["run_id"] == run
    assert stop_two["idempotent"] is True
    stopped = wait_terminal(cli, fake_kimi, run)
    assert current_attempt(stopped, "alpha")["status"] == "stopped"

    _, retry = cli.json_result(
        [
            "retry",
            run,
            "alpha",
            "--detach",
            "--acknowledge-dirty-workspace",
            "--json",
        ],
        fake_kimi,
    )
    assert retry["accepted"] is True
    assert retry["attempt_no"] == 2
    second = fake_kimi.wait_started("alpha")
    cli.json_result(["stop", run, "--task", "alpha", "--json"], fake_kimi)
    fake_kimi.wait_event("disconnected", "alpha", attempt_id=second["attempt_id"])
    wait_terminal(cli, fake_kimi, run)

    failed_process = cli.start(
        ["run", str(write_manifest(project, ["alpha"])), "--json"], fake_kimi
    )
    failed_start = fake_kimi.wait_started("alpha")
    log_secret = "RAW-LOG-SECRET-TC46"
    fake_kimi.complete(failed_start, exit_code=64, raw=log_secret)
    failed_result = cli.finish(failed_process)
    assert failed_result.returncode == 4
    assert log_secret not in failed_result.stdout + failed_result.stderr
    failed_payload = json.loads(failed_result.stdout)
    assert failed_payload["schema_version"] == 1
    assert failed_payload["status"] == "failed"
    failed_run = run_id(failed_payload)
    attempt = current_attempt(cli.status(failed_run, fake_kimi), "alpha")
    assert attempt["status"] == "failed"
    assert attempt["failure_reason"] == "child_exit"
    assert attempt["exit_code"] == 64
    log_path = Path(attempt["log_path"])
    assert log_path.is_file()
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
    assert log_secret in log_path.read_text(encoding="utf-8")
