import json
import shlex
import signal
import subprocess
import threading
from pathlib import Path

import pytest

from parallel_agentic_os.process import (
    ProcessController,
    ProcessIdentity,
    SessionIdError,
    SupervisorUnavailable,
    build_kimi_argv,
    complete_detached_handshake,
    finalize_attempt,
    parse_session_id,
    reconcile_attempt,
    resolve_attempt,
    resume_command,
    resume_payload,
    spawn_kimi,
)

SESSION_ID = "session_123e4567-e89b-42d3-a456-426614174000"
OTHER_SESSION_ID = "session_123e4567-e89b-42d3-a456-426614174001"


def resume_line(session_id: str = SESSION_ID) -> str:
    return json.dumps({"session": {"resume_hint": f"Run kimi --session {session_id} to continue"}})


class FakeProcess:
    def __init__(self, pid: int = 100, pgid: int = 100, start_time: int = 10) -> None:
        self.pid = pid
        self.pgid = pgid
        self.start_time = start_time
        self.returncode: int | None = None
        self.wait_entered = threading.Event()
        self.exited = threading.Event()

    def wait(self, timeout: float | None = None) -> int:
        self.wait_entered.set()
        if not self.exited.wait(timeout):
            raise subprocess.TimeoutExpired("kimi", timeout)
        assert self.returncode is not None
        return self.returncode

    def poll(self) -> int | None:
        return self.returncode

    def exit(self, returncode: int) -> None:
        if self.returncode is None:
            self.returncode = returncode
            self.exited.set()


class FakeInventory:
    def __init__(self) -> None:
        self.live: list[ProcessIdentity] = []
        self.markers: dict[str, list[ProcessIdentity]] = {}

    def add(self, identity: ProcessIdentity, attempt_id: str | None = None) -> None:
        self.live.append(identity)
        if attempt_id is not None:
            self.markers.setdefault(attempt_id, []).append(identity)

    def remove(self, identity: ProcessIdentity) -> None:
        self.live = [item for item in self.live if item != identity]
        for attempt_id, processes in list(self.markers.items()):
            self.markers[attempt_id] = [item for item in processes if item != identity]

    def marker_processes(self, attempt_id: str) -> tuple[ProcessIdentity, ...]:
        return tuple(
            identity for identity in self.markers.get(attempt_id, []) if identity in self.live
        )

    def pgid_processes(self, pgid: int) -> tuple[ProcessIdentity, ...]:
        return tuple(identity for identity in self.live if identity.pgid == pgid)

    def is_alive(self, identity: ProcessIdentity) -> bool:
        return identity in self.live


class KillRecorder:
    def __init__(self, on_signal=None) -> None:
        self.calls: list[tuple[int, int]] = []
        self.on_signal = on_signal

    def __call__(self, pgid: int, sig: int) -> None:
        self.calls.append((pgid, sig))
        if self.on_signal is not None:
            self.on_signal(sig)


class ControlledGrace:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, seconds: float) -> None:
        self.entered.set()
        assert self.release.wait(2), "grace period was not released"


def identity_for(process: FakeProcess) -> ProcessIdentity:
    return ProcessIdentity(process.pid, process.pgid, process.start_time)


def make_controller(
    process: FakeProcess,
    inventory: FakeInventory,
    killpg,
    *,
    attempt_id: str = "attempt-1",
    grace=lambda seconds: None,
) -> ProcessController:
    identity = identity_for(process)
    inventory.add(identity, attempt_id)
    return ProcessController(
        attempt_id=attempt_id,
        process=process,
        identity=identity,
        inventory=inventory,
        killpg=killpg,
        grace_period=0.25,
        grace=grace,
    )


def start_daemon(target) -> threading.Thread:
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


def assert_stopped(thread: threading.Thread) -> None:
    thread.join(2)
    assert not thread.is_alive(), "event-controlled operation deadlocked"


def test_spawn_uses_exact_argv_cwd_and_attempt_marker_without_shell(tmp_path: Path) -> None:
    calls = []
    process = FakeProcess()
    prompt = "implement the tests\nDo not touch other paths"

    def popen_factory(argv, **kwargs):
        calls.append((argv, kwargs))
        return process

    root = tmp_path / "effective root"
    result = spawn_kimi(
        executable="kimi",
        prompt=prompt,
        cwd=root,
        attempt_id="attempt-42",
        environment={"PATH": "/definitely/no/real/kimi", "KEEP": "yes"},
        popen_factory=popen_factory,
    )

    assert result is process
    assert build_kimi_argv("kimi", prompt) == [
        "kimi",
        "-p",
        prompt,
        "--output-format",
        "stream-json",
    ]
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == ["kimi", "-p", prompt, "--output-format", "stream-json"]
    assert kwargs["cwd"] == root
    assert kwargs["shell"] is False
    assert kwargs["start_new_session"] is True
    assert kwargs["stdout"] == subprocess.PIPE
    assert kwargs["stderr"] == subprocess.STDOUT
    assert kwargs["text"] is True
    assert kwargs["env"] == {
        "PATH": "/definitely/no/real/kimi",
        "KEEP": "yes",
        "PAO_ATTEMPT_ID": "attempt-42",
    }


def test_parser_accepts_only_a_strict_uuid_from_session_resume_hint() -> None:
    lines = [
        json.dumps({"type": "assistant", "message": "working"}),
        json.dumps({"session_id": OTHER_SESSION_ID}),
        resume_line(),
    ]

    assert parse_session_id(lines) == SESSION_ID


@pytest.mark.parametrize(
    "hint",
    [
        "Run kimi --session session_not-a-uuid to continue",
        f"Run kimi --session \x1b[31m{SESSION_ID}\x1b[0m to continue",
        f"Run kimi --session {SESSION_ID[:-1]} to continue",
        f"Run kimi --session  {SESSION_ID} {OTHER_SESSION_ID} to continue",
    ],
)
def test_parser_rejects_malformed_ansi_or_ambiguous_ids(hint: str) -> None:
    line = json.dumps({"session": {"resume_hint": hint}})

    with pytest.raises(SessionIdError):
        parse_session_id([line])


def test_parser_rejects_duplicate_hints_even_when_ids_match() -> None:
    with pytest.raises(SessionIdError, match="duplicate|ambiguous"):
        parse_session_id([resume_line(), resume_line()])


def test_parser_rejects_different_session_ids() -> None:
    with pytest.raises(SessionIdError, match="ambiguous|different"):
        parse_session_id([resume_line(), resume_line(OTHER_SESSION_ID)])


def test_missing_id_fails_successful_exit_and_nonzero_exit_preserves_id() -> None:
    assert parse_session_id([json.dumps({"type": "result", "status": "ok"})]) is None

    missing = finalize_attempt(exit_code=0, session_id=None)
    assert missing.status == "failed"
    assert missing.failure_reason == "session_id_missing"
    assert missing.session_id is None

    failed = finalize_attempt(exit_code=9, session_id=SESSION_ID)
    assert failed.status == "failed"
    assert failed.session_id == SESSION_ID

    completed = finalize_attempt(exit_code=0, session_id=SESSION_ID)
    assert completed.status == "completed"
    assert completed.failure_reason is None


def test_stopping_one_attempt_does_not_signal_the_other_process_group() -> None:
    first = FakeProcess(pid=101, pgid=201)
    second = FakeProcess(pid=102, pgid=202)
    inventory = FakeInventory()

    def terminate_first(pgid: int, sig: int) -> None:
        assert (pgid, sig) == (201, signal.SIGTERM)
        first.exit(143)
        inventory.remove(identity_for(first))

    kills = KillRecorder(lambda sig: terminate_first(201, sig))
    first_controller = make_controller(first, inventory, kills, attempt_id="first")
    make_controller(second, inventory, kills, attempt_id="second")

    assert first_controller.stop() == "stopped"
    assert kills.calls == [(201, signal.SIGTERM)]
    assert second.poll() is None
    assert inventory.is_alive(identity_for(second))


def test_reaper_does_not_hold_lifecycle_mutex_across_blocking_wait() -> None:
    process = FakeProcess(pid=110, pgid=210)
    inventory = FakeInventory()

    def terminate(sig: int) -> None:
        assert sig == signal.SIGTERM
        process.exit(143)
        inventory.remove(identity_for(process))

    kills = KillRecorder(terminate)
    controller = make_controller(process, inventory, kills)
    reaper = start_daemon(controller.reap)
    assert process.wait_entered.wait(2), "reaper never entered wait"

    stopper = start_daemon(controller.stop)
    assert_stopped(stopper)
    assert_stopped(reaper)

    assert controller.status == "stopped"
    assert kills.calls == [(210, signal.SIGTERM)]


def test_ignored_sigterm_escalates_after_releasable_grace_without_mutex() -> None:
    process = FakeProcess(pid=120, pgid=220)
    inventory = FakeInventory()
    grace = ControlledGrace()

    def record_signal(sig: int) -> None:
        if sig == signal.SIGKILL:
            process.exit(137)
            inventory.remove(identity_for(process))

    kills = KillRecorder(record_signal)
    controller = make_controller(process, inventory, kills, grace=grace)
    stopper = start_daemon(controller.stop)

    assert grace.entered.wait(2), "stop did not enter its grace period"
    assert kills.calls == [(220, signal.SIGTERM)]
    grace.release.set()
    assert_stopped(stopper)

    assert kills.calls == [(220, signal.SIGTERM), (220, signal.SIGKILL)]
    assert controller.status == "stopped"


def test_stop_never_signals_a_reaped_or_reused_pgid() -> None:
    process = FakeProcess(pid=130, pgid=230)
    inventory = FakeInventory()
    kills = KillRecorder()
    controller = make_controller(process, inventory, kills)
    process.exit(0)
    inventory.remove(identity_for(process))
    assert controller.reap() == 0

    assert controller.stop() == "completed"
    assert kills.calls == []

    reused = ProcessIdentity(process.pid, process.pgid, process.start_time + 1)
    inventory.add(reused)
    assert controller.stop() == "completed"
    assert kills.calls == []


def test_confirmed_exit_during_grace_cancels_delayed_sigkill() -> None:
    process = FakeProcess(pid=140, pgid=240)
    inventory = FakeInventory()
    grace = ControlledGrace()

    def terminate(sig: int) -> None:
        assert sig == signal.SIGTERM
        process.exit(143)
        inventory.remove(identity_for(process))
        inventory.add(ProcessIdentity(process.pid, process.pgid, process.start_time + 1))

    kills = KillRecorder(terminate)
    controller = make_controller(process, inventory, kills, grace=grace)
    stopper = start_daemon(controller.stop)
    assert grace.entered.wait(2)
    grace.release.set()
    assert_stopped(stopper)

    assert kills.calls == [(240, signal.SIGTERM)]
    assert controller.status == "stopped"


def test_identity_mismatch_before_term_or_kill_fails_closed_without_signal() -> None:
    for mismatch_before_term in (True, False):
        process = FakeProcess(pid=150 + mismatch_before_term, pgid=250)
        inventory = FakeInventory()
        grace = ControlledGrace()
        kills = KillRecorder()
        controller = make_controller(process, inventory, kills, grace=grace)
        original = identity_for(process)

        if mismatch_before_term:
            inventory.remove(original)
            inventory.add(ProcessIdentity(original.pid, original.pgid, original.start_time + 1))
            assert controller.stop() == "unknown"
        else:

            def replace_identity(
                sig: int, *, inventory: FakeInventory = inventory, original=original
            ) -> None:
                assert sig == signal.SIGTERM
                inventory.remove(original)
                inventory.add(ProcessIdentity(original.pid, original.pgid, original.start_time + 1))

            kills.on_signal = replace_identity
            stopper = start_daemon(controller.stop)
            assert grace.entered.wait(2)
            grace.release.set()
            assert_stopped(stopper)
            assert controller.status == "unknown"

        assert (250, signal.SIGKILL) not in kills.calls
        if mismatch_before_term:
            assert kills.calls == []


def running_attempt(
    attempt_id: str,
    identity: ProcessIdentity | None,
    *,
    spawn_intent: bool = True,
) -> dict:
    return {
        "attempt_id": attempt_id,
        "status": "running",
        "spawn_intent": spawn_intent,
        "identity": identity,
        "pgid": identity.pgid if identity is not None else 999,
        "log_lines": [resume_line()],
    }


def test_reconciliation_after_supervisor_crash_never_signals_a_live_identity() -> None:
    process = FakeProcess(pid=160, pgid=260)
    identity = identity_for(process)
    inventory = FakeInventory()
    inventory.add(identity, "attempt-crash")
    kills = KillRecorder()

    resolution = reconcile_attempt(
        running_attempt("attempt-crash", identity), inventory=inventory, killpg=kills
    )

    assert resolution.status == "unknown"
    assert resolution.failure_reason == "supervisor_lost"
    assert resolution.retry_allowed is False
    assert kills.calls == []


def test_reconciliation_without_process_recovers_session_and_allows_retry() -> None:
    identity = ProcessIdentity(170, 270, 10)
    kills = KillRecorder()

    resolution = reconcile_attempt(
        running_attempt("attempt-gone", identity), inventory=FakeInventory(), killpg=kills
    )

    assert resolution.status == "failed"
    assert resolution.failure_reason == "supervisor_lost"
    assert resolution.session_id == SESSION_ID
    assert resolution.retry_allowed is True
    assert kills.calls == []


def test_spawn_before_pid_commit_is_found_by_marker_and_pending_without_intent_stops() -> None:
    orphan = ProcessIdentity(180, 280, 20)
    inventory = FakeInventory()
    inventory.add(orphan, "attempt-uncommitted")
    kills = KillRecorder()

    unresolved = reconcile_attempt(
        running_attempt("attempt-uncommitted", None), inventory=inventory, killpg=kills
    )
    assert unresolved.status == "unknown"
    assert unresolved.processes == (orphan,)
    assert unresolved.retry_allowed is False
    assert kills.calls == []

    pending = running_attempt("attempt-pending", None, spawn_intent=False)
    pending["status"] = "pending"
    resolved_pending = reconcile_attempt(pending, inventory=inventory, killpg=kills)
    assert resolved_pending.status == "stopped"
    assert resolved_pending.retry_allowed is True


def test_resolve_orphan_then_acknowledged_retry_becomes_available_only_after_exit() -> None:
    orphan = ProcessIdentity(190, 290, 30)
    attempt = running_attempt("attempt-orphan", orphan)
    attempt["status"] = "unknown"
    inventory = FakeInventory()
    inventory.add(orphan, "attempt-orphan")

    live = resolve_attempt(attempt, inventory=inventory)
    assert live.status == "unknown"
    assert live.processes == (orphan,)
    assert live.retry_allowed is False

    inventory.remove(orphan)
    resolved = resolve_attempt(attempt, inventory=inventory)
    assert resolved.status == "failed"
    assert resolved.failure_reason == "supervisor_lost"
    assert resolved.session_id == SESSION_ID
    assert resolved.retry_allowed is True


def test_exited_leader_with_marked_or_escaped_descendant_cannot_stop_or_retry() -> None:
    for escaped in (False, True):
        leader = ProcessIdentity(200, 300, 40)
        descendant = ProcessIdentity(201, 301 if escaped else 300, 41)
        attempt = running_attempt("attempt-descendant", leader)
        inventory = FakeInventory()
        inventory.add(descendant, "attempt-descendant")
        kills = KillRecorder()

        resolution = reconcile_attempt(attempt, inventory=inventory, killpg=kills)

        assert resolution.status == "unknown"
        assert resolution.failure_reason == "escaped_descendants"
        assert resolution.retry_allowed is False
        assert descendant in resolution.processes
        assert kills.calls == []


def test_new_retry_spawn_has_a_new_attempt_marker_and_session() -> None:
    processes = []
    calls = []

    def popen_factory(argv, **kwargs):
        process = FakeProcess(pid=300 + len(processes), pgid=400 + len(processes))
        processes.append(process)
        calls.append((argv, kwargs))
        return process

    for attempt_id in ("old-attempt", "new-attempt"):
        spawn_kimi(
            executable="kimi",
            prompt="inspect git status and diff before continuing",
            cwd=Path("/same/dirty/worktree"),
            attempt_id=attempt_id,
            environment={},
            popen_factory=popen_factory,
        )

    assert calls[0][1]["cwd"] == calls[1][1]["cwd"] == Path("/same/dirty/worktree")
    assert calls[0][1]["env"]["PAO_ATTEMPT_ID"] == "old-attempt"
    assert calls[1][1]["env"]["PAO_ATTEMPT_ID"] == "new-attempt"
    assert calls[0][0] == calls[1][0]
    first = finalize_attempt(exit_code=1, session_id=parse_session_id([resume_line()]))
    second = finalize_attempt(
        exit_code=0, session_id=parse_session_id([resume_line(OTHER_SESSION_ID)])
    )
    assert first.session_id != second.session_id


def test_resume_uses_effective_root_from_an_unrelated_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "worktree with spaces"
    unrelated = tmp_path / "unrelated"
    root.mkdir()
    unrelated.mkdir()
    monkeypatch.chdir(unrelated)

    assert resume_command(root, SESSION_ID) == (
        f"cd {shlex.quote(str(root))} && kimi --session {SESSION_ID}"
    )
    assert resume_payload(root, SESSION_ID) == {
        "cwd": str(root),
        "argv": ["kimi", "--session", SESSION_ID],
    }


def test_detached_handshake_failure_never_reports_successful_run_id() -> None:
    class NotReady:
        def __init__(self) -> None:
            self.timeouts = []

        def wait(self, timeout: float) -> bool:
            self.timeouts.append(timeout)
            return False

    ready = NotReady()

    with pytest.raises(SupervisorUnavailable) as error:
        complete_detached_handshake("run-1", ready=ready, timeout=2.5)

    assert error.value.run_id == "run-1"
    assert ready.timeouts == [2.5]
