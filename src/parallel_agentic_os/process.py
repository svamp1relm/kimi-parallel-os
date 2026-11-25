from __future__ import annotations

import json
import os
import re
import shlex
import signal
import subprocess
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

_SESSION_HINT = re.compile(
    r"Run kimi --session "
    r"(session_[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}) to continue"
)
_SESSION_ID = re.compile(
    r"session_[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)


class SessionIdError(ValueError):
    pass


class SupervisorUnavailable(RuntimeError):
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(f"supervisor for run {run_id!r} did not become ready")


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    pgid: int
    start_time: int


@dataclass(frozen=True)
class AttemptResolution:
    status: str
    failure_reason: str | None = None
    session_id: str | None = None
    retry_allowed: bool = False
    processes: tuple[ProcessIdentity, ...] = ()
    exit_code: int | None = None


class ProcessInventory(Protocol):
    def marker_processes(self, attempt_id: str) -> tuple[ProcessIdentity, ...]: ...

    def pgid_processes(self, pgid: int) -> tuple[ProcessIdentity, ...]: ...

    def is_alive(self, identity: ProcessIdentity) -> bool: ...


def build_kimi_argv(executable: str, prompt: str) -> list[str]:
    return [executable, "-p", prompt, "--output-format", "stream-json"]


def spawn_kimi(
    *,
    executable: str,
    prompt: str,
    cwd: Path,
    attempt_id: str,
    environment: Mapping[str, str],
    popen_factory=subprocess.Popen,
):
    env = dict(environment)
    env["PAO_ATTEMPT_ID"] = attempt_id
    return popen_factory(
        build_kimi_argv(executable, prompt),
        cwd=cwd,
        shell=False,
        start_new_session=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )


def parse_session_id(lines: Iterable[str]) -> str | None:
    session_id: str | None = None
    for line in lines:
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        found: str | None = None
        if event.get("type") == "session.resume_hint":
            candidate = event.get("session_id")
            if not isinstance(candidate, str) or _SESSION_ID.fullmatch(candidate) is None:
                raise SessionIdError("malformed session resume hint")
            found = candidate
        else:
            session = event.get("session")
            if not isinstance(session, dict) or "resume_hint" not in session:
                continue
            hint = session["resume_hint"]
            if not isinstance(hint, str):
                raise SessionIdError("session resume hint is not a string")
            match = _SESSION_HINT.fullmatch(hint)
            if match is None:
                raise SessionIdError("malformed session resume hint")
            found = match.group(1)
        if session_id is not None:
            if found == session_id:
                raise SessionIdError("duplicate session resume hints")
            raise SessionIdError("ambiguous different session IDs")
        session_id = found
    return session_id


def finalize_attempt(exit_code: int, session_id: str | None) -> AttemptResolution:
    if exit_code == 0 and session_id is not None:
        return AttemptResolution(status="completed", session_id=session_id, exit_code=exit_code)
    reason = "session_id_missing" if exit_code == 0 else "process_failed"
    return AttemptResolution(
        status="failed",
        failure_reason=reason,
        session_id=session_id,
        retry_allowed=True,
        exit_code=exit_code,
    )


def resume_command(effective_root: Path, session_id: str) -> str:
    return f"cd {shlex.quote(str(effective_root))} && kimi --session {session_id}"


def resume_payload(effective_root: Path, session_id: str) -> dict[str, Any]:
    return {
        "cwd": str(effective_root),
        "argv": ["kimi", "--session", session_id],
    }


def complete_detached_handshake(run_id: str, *, ready: Any, timeout: float) -> str:
    if not ready.wait(timeout):
        raise SupervisorUnavailable(run_id)
    return run_id


class ProcessController:
    def __init__(
        self,
        *,
        attempt_id: str,
        process: Any,
        identity: ProcessIdentity,
        inventory: ProcessInventory,
        killpg=os.killpg,
        grace_period: float,
        grace: Any,
    ) -> None:
        self.attempt_id = attempt_id
        self.process = process
        self.identity = identity
        self.inventory = inventory
        self.killpg = killpg
        self.grace_period = grace_period
        self.grace = grace
        self.status = "running"
        self.failure_reason: str | None = None
        self._exit_code: int | None = None
        self._mutex = threading.Lock()
        self._wait_mutex = threading.Lock()

    def _has_exited(self) -> bool:
        return self._exit_code is not None or self.process.poll() is not None

    def _marked_processes(self) -> tuple[ProcessIdentity, ...]:
        return self.inventory.marker_processes(self.attempt_id)

    def _signal(self, sig: int) -> None:
        try:
            self.killpg(self.identity.pgid, sig)
        except ProcessLookupError:
            pass

    def reap(self) -> int:
        with self._wait_mutex:
            exit_code = self.process.wait()
        with self._mutex:
            self._exit_code = exit_code
            if self.status in {"stopping", "stopped"}:
                self.status = "stopped" if self._stopped_locked() else "unknown"
            elif self.status != "unknown":
                self.status = "completed" if exit_code == 0 else "failed"
                if self.status == "failed":
                    self.failure_reason = "process_failed"
        return exit_code

    def _stopped_locked(self) -> bool:
        if self._marked_processes():
            self.failure_reason = "escaped_descendants"
            return False
        if self.inventory.is_alive(self.identity):
            self.failure_reason = "process_still_running"
            return False
        if not self._has_exited():
            self.failure_reason = "identity_mismatch"
            return False
        self.failure_reason = None
        return True

    def stop(self) -> str:
        with self._mutex:
            if self.status in {"completed", "failed", "stopped"}:
                return self.status
            if not self.inventory.is_alive(self.identity):
                self.status = "stopped" if self._stopped_locked() else "unknown"
                return self.status
            self.status = "stopping"
            self._signal(signal.SIGTERM)

        self.grace(self.grace_period)

        with self._mutex:
            if self._stopped_locked():
                self.status = "stopped"
                return self.status
            if self.inventory.is_alive(self.identity) and not self._has_exited():
                self._signal(signal.SIGKILL)
            self.status = "stopped" if self._stopped_locked() else "unknown"
            return self.status


def _attempt_processes(
    attempt: Mapping[str, Any], inventory: ProcessInventory
) -> tuple[ProcessIdentity, ...]:
    attempt_id = str(attempt["attempt_id"])
    processes = list(inventory.marker_processes(attempt_id))
    pgid = attempt.get("pgid")
    if pgid is not None:
        processes.extend(inventory.pgid_processes(pgid))
    return tuple(dict.fromkeys(processes))


def _resolve_attempt(attempt: Mapping[str, Any], inventory: ProcessInventory) -> AttemptResolution:
    if attempt.get("status") == "pending" and not attempt.get("spawn_intent", False):
        return AttemptResolution(status="stopped", retry_allowed=True)

    identity = attempt.get("identity")
    processes = _attempt_processes(attempt, inventory)
    if processes:
        reason = "supervisor_lost"
        if identity is not None and identity not in processes:
            reason = "escaped_descendants"
        return AttemptResolution(
            status="unknown",
            failure_reason=reason,
            processes=processes,
            retry_allowed=False,
        )

    return AttemptResolution(
        status="failed",
        failure_reason="supervisor_lost",
        session_id=parse_session_id(attempt.get("log_lines", ())),
        retry_allowed=True,
    )


def reconcile_attempt(
    attempt: Mapping[str, Any], *, inventory: ProcessInventory, killpg: Any
) -> AttemptResolution:
    del killpg
    return _resolve_attempt(attempt, inventory)


def resolve_attempt(
    attempt: Mapping[str, Any], *, inventory: ProcessInventory
) -> AttemptResolution:
    return _resolve_attempt(attempt, inventory)
