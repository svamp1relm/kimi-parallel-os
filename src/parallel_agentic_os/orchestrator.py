from __future__ import annotations

import json
import os
import signal
import socket
import stat as stat_module
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .manifest import Manifest, Task, load_manifest
from .process import (
    ProcessIdentity,
    SessionIdError,
    SupervisorUnavailable,
    parse_session_id,
    resume_payload,
    spawn_kimi,
)
from .state import Attempt, StateConflictError, StateStore, SupervisorLock
from .worktree import (
    DirtyBaseError,
    WorktreeManager,
    WorktreeSetupError,
    validate_execution_roots,
)

TERMINAL = {"completed", "failed", "stopped", "setup_failed"}
ACTIVE = {"pending", "spawning", "running", "stopping", "unknown"}


class OrchestratorError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        hint: str,
        exit_code: int,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.hint = hint
        self.exit_code = exit_code
        self.context = context or {}
        super().__init__(message)


@dataclass(frozen=True)
class RunPaths:
    home: Path
    run_id: str

    @property
    def root(self) -> Path:
        return self.home / "runs" / self.run_id

    @property
    def metadata(self) -> Path:
        return self.root / "run.json"

    @property
    def socket(self) -> Path:
        return self.home / f"{self.run_id}.sock"

    @property
    def ready(self) -> Path:
        return self.root / "ready.json"

    @property
    def lock(self) -> Path:
        return self.root / "supervisor.lock"

    def task_dir(self, task_id: str, attempt_no: int) -> Path:
        return self.root / "tasks" / task_id / f"attempt-{attempt_no}"


def now() -> str:
    return datetime.now(UTC).isoformat()


def secure_home(path: Path) -> Path:
    if not sys.platform.startswith("linux"):
        raise OrchestratorError(
            "unsupported_platform",
            "Parallel agents currently requires Linux",
            "Run it on Linux with /proc and flock support",
            2,
            {"platform": sys.platform},
        )
    os.umask(0o077)
    home = path.expanduser()
    if home.is_symlink():
        raise OrchestratorError(
            "unsafe_state_home",
            "State home cannot be a symlink",
            "Choose a real directory",
            2,
            {"path": str(home)},
        )
    if home.exists():
        if not home.is_dir() or home.stat().st_mode & 0o077:
            raise OrchestratorError(
                "unsafe_state_home",
                "State home must be private",
                "Set permissions to 0700",
                2,
                {"path": str(home)},
            )
    else:
        home.mkdir(parents=True, mode=0o700)
    home.chmod(0o700)
    return home


def default_home() -> Path:
    return Path(os.environ.get("PAO_HOME", "~/.parallel_agentic_os"))


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        temporary.chmod(0o600)
        temporary.replace(path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def open_log(path: Path, *, exclusive: bool) -> int:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT
    flags |= os.O_EXCL if exclusive else os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    if not stat_module.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError(f"Log path is not a regular file: {path}")
    os.fchmod(fd, 0o600)
    return fd


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


class FileJournal:
    def __init__(self, root: Path) -> None:
        self.root = root

    def record_intent(self, intent: Any) -> None:
        write_json(
            self.root / f"intent-{time.time_ns()}.json",
            {
                "run_id": intent.run_id,
                "task_id": intent.task_id,
                "operation": intent.operation,
                "base_commit": intent.base_commit,
                "branch": intent.branch,
                "path": str(intent.path),
            },
        )

    def record_result(self, result: Any) -> None:
        write_json(
            self.root / f"result-{time.time_ns()}.json",
            {
                "status": result.status,
                "run_id": result.intent.run_id,
                "task_id": result.intent.task_id,
                "branch": result.intent.branch,
                "path": str(result.intent.path),
                "resources": [
                    {"kind": item.kind, "path": str(item.path), "branch": item.branch}
                    for item in result.resources
                ],
            },
        )


def run_paths(run_id: str, home: Path) -> RunPaths:
    return RunPaths(home=home, run_id=run_id)


def load_run(run_id: str, home: Path) -> dict[str, Any]:
    paths = run_paths(run_id, home)
    if not paths.metadata.is_file():
        raise OrchestratorError(
            "run_not_found",
            f"Run {run_id} was not found",
            "Use parallel-agents list",
            3,
            {"run_id": run_id},
        )
    return read_json(paths.metadata)


def load_manifest_for_run(run_id: str, home: Path) -> Manifest:
    metadata = load_run(run_id, home)
    return load_manifest(metadata["manifest_path"])


def task_metadata_path(paths: RunPaths, task_id: str, attempt_no: int) -> Path:
    return paths.task_dir(task_id, attempt_no) / "attempt.json"


def worktree_metadata_path(paths: RunPaths, task_id: str) -> Path:
    return paths.root / "tasks" / task_id / "worktree.json"


def create_run(manifest: Manifest, manifest_path: Path, home: Path) -> dict[str, Any]:
    run_id = f"pao-{uuid.uuid4().hex[:12]}"
    paths = run_paths(run_id, home)
    paths.root.mkdir(parents=True, mode=0o700)
    snapshot = paths.root / "manifest.json"
    snapshot.write_bytes(manifest_path.read_bytes())
    snapshot.chmod(0o600)
    store = StateStore(home / "state.db")
    store.create_run(run_id=run_id, task_ids=tuple(task.id for task in manifest.tasks))
    created_at = now()
    metadata = {
        "run_id": run_id,
        "project": str(manifest.project),
        "manifest_path": str(snapshot),
        "max_parallel": manifest.max_parallel,
        "created_at": created_at,
        "setup_failed": None,
        "supervisor": {"alive": False, "pid": None, "generation": 0},
    }
    write_json(paths.metadata, metadata)
    for task in manifest.tasks:
        store.create_attempt(run_id=run_id, task_id=task.id, attempt_no=1)
        attempt_id = uuid.uuid4().hex
        log_path = paths.task_dir(task.id, 1) / "output.jsonl"
        write_json(
            task_metadata_path(paths, task.id, 1),
            {
                "attempt_id": attempt_id,
                "attempt_no": 1,
                "log_path": str(log_path),
                "cwd": str(manifest.project),
                "hint": "",
                "spawn_intent": False,
            },
        )
    return metadata


def get_store(home: Path) -> StateStore:
    return StateStore(home / "state.db")


def latest_attempt(store: StateStore, run_id: str, task_id: str) -> Attempt:
    attempts = store.list_attempts(run_id=run_id, task_id=task_id)
    if not attempts:
        raise OrchestratorError(
            "attempt_not_found",
            f"Attempt for {task_id} was not found",
            "Check the task ID",
            3,
            {"run_id": run_id, "task_id": task_id},
        )
    return attempts[-1]


def supervisor_alive(metadata: dict[str, Any]) -> bool:
    supervisor = metadata.get("supervisor", {})
    if not supervisor.get("alive"):
        return False
    pid = supervisor.get("pid")
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def mark_supervisor_lost(run_id: str, home: Path, metadata: dict[str, Any]) -> None:
    if supervisor_alive(metadata):
        return
    store = get_store(home)
    run = load_run(run_id, home)
    for task in run_tasks(run, home):
        attempt = latest_attempt(store, run_id, task.id)
        if attempt.status in ACTIVE - {"unknown"}:
            store.transition_attempt(
                run_id=run_id,
                task_id=task.id,
                attempt_no=attempt.attempt_no,
                expected_status=attempt.status,
                status="unknown",
                failure_reason="supervisor_lost",
            )


def run_tasks(metadata: dict[str, Any], home: Path) -> tuple[Task, ...]:
    return load_manifest(metadata["manifest_path"]).tasks


def task_worktree(paths: RunPaths, task_id: str) -> dict[str, Any] | None:
    path = worktree_metadata_path(paths, task_id)
    if not path.is_file():
        return None
    value = read_json(path)
    return {"branch": value["branch"], "path": value["path"], "base_commit": value["base_commit"]}


def attempt_view(run_id: str, paths: RunPaths, attempt: Attempt) -> dict[str, Any]:
    metadata = read_json(task_metadata_path(paths, attempt.task_id, attempt.attempt_no))
    effective_root = Path(metadata["cwd"])
    resume = None
    if attempt.session_id and attempt.status in {"completed", "failed"}:
        resume = resume_payload(effective_root, attempt.session_id)
    resolve = None
    if attempt.status == "unknown":
        resolve = {
            "cwd": str(Path.cwd()),
            "argv": ["parallel-agents", "resolve", run_id, "--task", attempt.task_id],
        }
    return {
        "attempt_no": attempt.attempt_no,
        "status": attempt.status,
        "failure_reason": attempt.failure_reason,
        "exit_code": attempt.exit_code,
        "session_id": attempt.session_id,
        "resume": resume,
        "resolve": resolve,
        "created_at": attempt.created_at,
        "started_at": attempt.started_at,
        "finished_at": attempt.finished_at,
        "log_path": metadata["log_path"],
        "hint": metadata.get("hint", ""),
        "pid": attempt.pid,
        "pgid": attempt.pgid,
        "attempt_id": metadata["attempt_id"],
    }


def project_run(run_id: str, home: Path) -> dict[str, Any]:
    metadata = load_run(run_id, home)
    mark_supervisor_lost(run_id, home, metadata)
    metadata = load_run(run_id, home)
    store = get_store(home)
    paths = run_paths(run_id, home)
    task_values = []
    statuses = []
    finished_values = []
    for task in run_tasks(metadata, home):
        attempts = store.list_attempts(run_id=run_id, task_id=task.id)
        attempts.reverse()
        current = attempts[0]
        statuses.append(current.status)
        finished_values.extend(a.finished_at for a in attempts if a.finished_at)
        task_values.append(
            {
                "task_id": task.id,
                "status": current.status,
                "allowed_paths": list(task.allowed_paths),
                "worktree": task_worktree(paths, task.id),
                "attempts": [attempt_view(run_id, paths, attempt) for attempt in attempts],
            }
        )
    if metadata.get("setup_failed"):
        status = "setup_failed"
    elif any(value == "unknown" for value in statuses):
        status = "unknown"
    elif any(value in ACTIVE - {"unknown"} for value in statuses):
        status = "running"
    elif len(set(statuses)) == 1:
        status = statuses[0]
    else:
        status = "partial"
    return {
        "schema_version": 1,
        "run_id": run_id,
        "project": metadata["project"],
        "status": status,
        "failure_reason": metadata.get("setup_failed"),
        "created_at": metadata["created_at"],
        "finished_at": max(finished_values)
        if finished_values and all(value in TERMINAL for value in statuses)
        else None,
        "tasks": task_values,
    }


def export_run(run_id: str, home: Path) -> dict[str, Any]:
    projected = project_run(run_id, home)
    tasks = []
    for task in projected["tasks"]:
        attempts = []
        for attempt in task["attempts"]:
            attempts.append(
                {
                    key: attempt[key]
                    for key in (
                        "attempt_no",
                        "status",
                        "failure_reason",
                        "exit_code",
                        "session_id",
                        "resume",
                        "created_at",
                        "started_at",
                        "finished_at",
                    )
                }
            )
        tasks.append(
            {key: task[key] for key in ("task_id", "status", "allowed_paths", "worktree")}
            | {"attempts": attempts}
        )
    return {
        "schema_version": 1,
        "run": {
            key: projected[key]
            for key in ("run_id", "project", "status", "created_at", "finished_at", "tasks")
        }
        | {"tasks": tasks},
    }


def markdown_export(run_id: str, home: Path) -> str:
    value = export_run(run_id, home)["run"]
    lines = [
        f"# Parallel agents run {run_id}",
        "",
        f"- Project: `{value['project']}`",
        f"- Status: `{value['status']}`",
        "",
    ]
    for task in value["tasks"]:
        lines.append(f"## {task['task_id']}")
        lines.append("")
        for attempt in task["attempts"]:
            lines.append(
                f"- Attempt {attempt['attempt_no']}: `{attempt['status']}`; "
                f"session `{attempt['session_id']}`"
            )
        lines.append("")
    return "\n".join(lines)


class ProcInventory:
    @staticmethod
    def _identity(pid: int) -> ProcessIdentity | None:
        try:
            text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            end = text.rfind(")")
            fields = text[end + 2 :].split()
            return ProcessIdentity(pid=pid, pgid=int(fields[2]), start_time=int(fields[19]))
        except (OSError, ValueError, IndexError):
            return None

    def marker_processes(self, attempt_id: str) -> tuple[ProcessIdentity, ...]:
        found = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                environment = (entry / "environ").read_bytes().split(b"\0")
            except OSError:
                continue
            if f"PAO_ATTEMPT_ID={attempt_id}".encode() not in environment:
                continue
            identity = self._identity(int(entry.name))
            if identity:
                found.append(identity)
        return tuple(found)

    def pgid_processes(self, pgid: int) -> tuple[ProcessIdentity, ...]:
        found = []
        for entry in Path("/proc").iterdir():
            if entry.name.isdigit():
                identity = self._identity(int(entry.name))
                if identity and identity.pgid == pgid:
                    found.append(identity)
        return tuple(found)

    def is_alive(self, identity: ProcessIdentity) -> bool:
        current = self._identity(identity.pid)
        return current == identity


def bounded_output_lines(stream: Any, limit: int = 16 * 1024 * 1024):
    buffer = ""
    total = 0
    while chunk := stream.read(65536):
        total += len(chunk)
        if total > limit:
            raise OrchestratorError(
                "log_limit_exceeded",
                "Kimi output exceeded the attempt log limit",
                "Reduce the task output and retry manually",
                4,
            )
        buffer += chunk
        lines = buffer.splitlines(keepends=True)
        buffer = lines.pop() if lines else buffer
        yield from lines
    if buffer:
        yield buffer


class Supervisor:
    def __init__(self, run_id: str, home: Path) -> None:
        self.run_id = run_id
        self.home = home
        self.paths = run_paths(run_id, home)
        self.store = get_store(home)
        self.manifest = load_manifest_for_run(run_id, home)
        self.live: dict[tuple[str, int], Any] = {}
        self.live_lock = threading.Lock()
        self.stop_requested: set[tuple[str, int]] = set()
        self.server: socket.socket | None = None
        self.generation = int(time.time_ns())

    def _set_supervisor(self, alive: bool) -> None:
        metadata = load_run(self.run_id, self.home)
        metadata["supervisor"] = {"alive": alive, "pid": os.getpid(), "generation": self.generation}
        write_json(self.paths.metadata, metadata)

    def _start_socket(self) -> None:
        try:
            self.paths.socket.unlink(missing_ok=True)
            self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.server.bind(str(self.paths.socket))
            self.server.listen()
            self.server.settimeout(0.2)
        except OSError as error:
            write_json(self.paths.root / "startup-error.json", {"message": str(error)})
            raise SupervisorUnavailable(self.run_id) from error
        threading.Thread(target=self._accept_commands, daemon=True).start()
        write_json(
            self.paths.ready,
            {"supervisor_pid": os.getpid(), "control_socket": str(self.paths.socket)},
        )

    def _accept_commands(self) -> None:
        assert self.server is not None
        while True:
            try:
                connection, _ = self.server.accept()
            except TimeoutError:
                if not load_run(self.run_id, self.home)["supervisor"].get("alive"):
                    return
                continue
            except OSError:
                return
            try:
                connection.settimeout(1)
                request = json.loads(connection.makefile("r", encoding="utf-8").readline())
                response = self._handle_command(request)
            except Exception as error:
                response = {"accepted": False, "message": str(error)}
            try:
                connection.sendall((json.dumps(response) + "\n").encode())
            except OSError:
                pass
            finally:
                connection.close()

    def _escalate(self, key: tuple[str, int], process: Any) -> None:
        time.sleep(2)
        with self.live_lock:
            if self.live.get(key) is not process or process.poll() is not None:
                return
            attempt = latest_attempt(self.store, self.run_id, key[0])
            identity = ProcInventory._identity(process.pid)
            if identity is None or str(identity.start_time) != attempt.process_start_time:
                return
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _terminate(self, key: tuple[str, int], process: Any, attempt: Attempt) -> None:
        identity = ProcInventory._identity(process.pid)
        if identity is None or str(identity.start_time) != attempt.process_start_time:
            self.store.transition_attempt(
                run_id=self.run_id,
                task_id=key[0],
                attempt_no=key[1],
                expected_status=attempt.status,
                status="unknown",
                failure_reason="identity_mismatch",
            )
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        threading.Thread(target=self._escalate, args=(key, process), daemon=True).start()

    def _handle_command(self, request: dict[str, Any]) -> dict[str, Any]:
        if request.get("action") != "stop":
            return {"accepted": False, "message": "unknown command"}
        task_id = request.get("task_id")
        tasks = [task.id for task in self.manifest.tasks if task_id is None or task.id == task_id]
        if not tasks:
            raise OrchestratorError(
                "task_not_found",
                f"Task {task_id} was not found",
                "Check status for available task IDs",
                3,
                {"run_id": self.run_id, "task_id": task_id},
            )
        idempotent = True
        for current_task in tasks:
            attempt = latest_attempt(self.store, self.run_id, current_task)
            key = (current_task, attempt.attempt_no)
            if attempt.status in TERMINAL:
                continue
            idempotent = False
            self.stop_requested.add(key)
            with self.live_lock:
                process = self.live.get(key)
                if process is not None:
                    if attempt.status == "running":
                        self.store.transition_attempt(
                            run_id=self.run_id,
                            task_id=current_task,
                            attempt_no=attempt.attempt_no,
                            expected_status="running",
                            status="stopping",
                        )
                    self._terminate(
                        key, process, latest_attempt(self.store, self.run_id, current_task)
                    )
                elif attempt.status == "pending":
                    self.store.transition_attempt(
                        run_id=self.run_id,
                        task_id=current_task,
                        attempt_no=attempt.attempt_no,
                        expected_status=attempt.status,
                        status="stopped",
                        failure_reason="stop_requested",
                    )
        return {"accepted": True, "run_id": self.run_id, "idempotent": idempotent}

    def send_stop(self, task_id: str | None) -> dict[str, Any]:
        return self._handle_command({"action": "stop", "task_id": task_id})

    def prepare_worktrees(self) -> None:
        metadata = load_run(self.run_id, self.home)
        if metadata.get("setup_failed"):
            return
        for task in self.manifest.tasks:
            if not task.worktree.enabled or worktree_metadata_path(self.paths, task.id).is_file():
                continue
            try:
                prepared = WorktreeManager(
                    self.manifest.project,
                    journal=FileJournal(self.paths.root / "setup-journal"),
                ).prepare(
                    run_id=self.run_id,
                    task_id=task.id,
                    base_ref=task.worktree.base_ref or "HEAD",
                    allow_dirty_base=task.worktree.allow_dirty_base,
                )
            except DirtyBaseError:
                raise
            except WorktreeSetupError as error:
                metadata["setup_failed"] = error.failure_reason
                metadata["setup_resources"] = [
                    {"kind": item.kind, "path": str(item.path), "branch": item.branch}
                    for item in error.resources
                ]
                write_json(self.paths.metadata, metadata)
                attempt = latest_attempt(self.store, self.run_id, task.id)
                self.store.transition_attempt(
                    run_id=self.run_id,
                    task_id=task.id,
                    attempt_no=attempt.attempt_no,
                    expected_status=attempt.status,
                    status="setup_failed",
                    failure_reason=error.failure_reason,
                )
                continue
            write_json(
                worktree_metadata_path(self.paths, task.id),
                {
                    "branch": prepared.branch,
                    "path": str(prepared.path),
                    "base_commit": prepared.base_commit,
                },
            )
            attempt_meta_path = task_metadata_path(self.paths, task.id, 1)
            attempt_meta = read_json(attempt_meta_path)
            attempt_meta["cwd"] = str(prepared.effective_root)
            write_json(attempt_meta_path, attempt_meta)
        roots = {}
        for task in self.manifest.tasks:
            attempt_meta = read_json(task_metadata_path(self.paths, task.id, 1))
            roots[task.id] = (Path(attempt_meta["cwd"]), task.allowed_paths)
        validate_execution_roots(roots)

    def _failure_reason(self, exit_code: int, lines: list[str]) -> str | None:
        if exit_code == 0:
            return None
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "error":
                kind = event.get("error", {}).get("type")
                if kind in {"quota_exceeded", "authentication_failed"}:
                    return kind
        return "child_exit"

    def execute(self, attempt: Attempt) -> None:
        task = next(task for task in self.manifest.tasks if task.id == attempt.task_id)
        paths = self.paths
        key = (task.id, attempt.attempt_no)
        metadata_path = task_metadata_path(paths, task.id, attempt.attempt_no)
        metadata = read_json(metadata_path)
        log_path = Path(metadata["log_path"])
        process = None
        try:
            root = Path(metadata["cwd"]).resolve()
            canonical_paths = []
            for allowed in task.allowed_paths:
                resolved = (root / allowed).resolve()
                resolved.relative_to(root)
                canonical_paths.append(resolved.relative_to(root).as_posix())
            for allowed in canonical_paths:
                self.store.acquire_workspace_lease(
                    run_id=self.run_id,
                    task_id=task.id,
                    canonical_root=root,
                    allowed_path=allowed,
                )
            if key in self.stop_requested:
                self.store.transition_attempt(
                    run_id=self.run_id,
                    task_id=task.id,
                    attempt_no=attempt.attempt_no,
                    expected_status="pending",
                    status="stopped",
                    failure_reason="stop_requested",
                )
                return
            self.store.transition_attempt(
                run_id=self.run_id,
                task_id=task.id,
                attempt_no=attempt.attempt_no,
                expected_status="pending",
                status="spawning",
                supervisor_generation=self.generation,
            )
            metadata["spawn_intent"] = True
            metadata["canonical_allowed_paths"] = canonical_paths
            write_json(metadata_path, metadata)
            fd = open_log(log_path, exclusive=True)
            allowed = ", ".join(task.allowed_paths)
            inspection = f" {metadata['hint']}" if metadata.get("hint") else ""
            prompt = (
                f"{task.prompt}\n\n[Parallel agent constraints]\n"
                f"Write only within: {allowed}. Do not commit or push.{inspection} "
                "Return changed files and unresolved issues."
            )
            process = spawn_kimi(
                executable=os.environ.get("PAO_KIMI_EXECUTABLE", "kimi"),
                prompt=prompt,
                cwd=root,
                attempt_id=metadata["attempt_id"],
                environment=os.environ,
            )
            with self.live_lock:
                self.live[key] = process
            identity = ProcInventory._identity(process.pid)
            requested_status = "stopping" if key in self.stop_requested else "running"
            self.store.transition_attempt(
                run_id=self.run_id,
                task_id=task.id,
                attempt_no=attempt.attempt_no,
                expected_status="spawning",
                status=requested_status,
                pid=process.pid,
                pgid=process.pid,
                process_start_time=str(identity.start_time) if identity else None,
            )
            if requested_status == "stopping":
                self._terminate(key, process, latest_attempt(self.store, self.run_id, task.id))
            tail = deque(maxlen=100)
            last_direct_hint = None
            with os.fdopen(fd, "w", encoding="utf-8") as log:
                assert process.stdout is not None
                for line in bounded_output_lines(process.stdout):
                    tail.append(line)
                    log.write(line)
                    log.flush()
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") == "session.resume_hint":
                        last_direct_hint = line
            exit_code = process.wait()
            session_id = parse_session_id([last_direct_hint] if last_direct_hint else tail)
            with self.live_lock:
                self.live.pop(key, None)
            remaining = ProcInventory().marker_processes(metadata["attempt_id"])
            remaining += ProcInventory().pgid_processes(process.pid)
            current = latest_attempt(self.store, self.run_id, task.id)
            if remaining:
                status, reason = "unknown", "escaped_descendants"
            elif key in self.stop_requested or current.status == "stopping":
                status, reason = "stopped", "stop_requested"
            elif exit_code == 0 and session_id:
                status, reason = "completed", None
            else:
                status = "failed"
                reason = (
                    self._failure_reason(exit_code, list(tail))
                    if exit_code
                    else "session_id_missing"
                )
            self.store.transition_attempt(
                run_id=self.run_id,
                task_id=task.id,
                attempt_no=attempt.attempt_no,
                expected_status=current.status,
                status=status,
                exit_code=exit_code,
                session_id=session_id,
                failure_reason=reason,
            )
        except FileNotFoundError as error:
            metadata["hint"] = f"Install Kimi or set PAO_KIMI_EXECUTABLE: {error.filename}"
            write_json(metadata_path, metadata)
            current = latest_attempt(self.store, self.run_id, task.id)
            self.store.transition_attempt(
                run_id=self.run_id,
                task_id=task.id,
                attempt_no=attempt.attempt_no,
                expected_status=current.status,
                status="failed",
                failure_reason="executable_not_found",
            )
        except Exception as error:
            if process is not None and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            current = latest_attempt(self.store, self.run_id, task.id)
            if current.status in TERMINAL:
                return
            reason = (
                "workspace_conflict" if isinstance(error, StateConflictError) else "spawn_error"
            )
            metadata["hint"] = str(error)
            write_json(metadata_path, metadata)
            self.store.transition_attempt(
                run_id=self.run_id,
                task_id=task.id,
                attempt_no=attempt.attempt_no,
                expected_status=current.status,
                status="failed",
                failure_reason=reason,
            )
        finally:
            with self.live_lock:
                self.live.pop(key, None)

    def run(self) -> None:
        with SupervisorLock(self.paths.lock):
            self._set_supervisor(True)
            try:
                self._start_socket()
                self.prepare_worktrees()
                attempts = []
                if load_run(self.run_id, self.home).get("setup_failed"):
                    for task in self.manifest.tasks:
                        current = latest_attempt(self.store, self.run_id, task.id)
                        if current.status == "pending":
                            self.store.transition_attempt(
                                run_id=self.run_id,
                                task_id=task.id,
                                attempt_no=current.attempt_no,
                                expected_status="pending",
                                status="setup_failed",
                                failure_reason="setup_failed",
                            )
                for task in self.manifest.tasks:
                    attempt = latest_attempt(self.store, self.run_id, task.id)
                    if attempt.status == "pending":
                        attempts.append(attempt)
                with ThreadPoolExecutor(max_workers=self.manifest.max_parallel) as executor:
                    list(executor.map(self.execute, attempts))
            finally:
                self._set_supervisor(False)
                if self.server is not None:
                    self.server.close()
                self.paths.socket.unlink(missing_ok=True)


def request_stop(run_id: str, home: Path, task_id: str | None) -> dict[str, Any]:
    metadata = load_run(run_id, home)
    if not supervisor_alive(metadata):
        raise SupervisorUnavailable(run_id)
    paths = run_paths(run_id, home)
    try:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(5)
        connection.connect(str(paths.socket))
        connection.sendall((json.dumps({"action": "stop", "task_id": task_id}) + "\n").encode())
        response = json.loads(connection.makefile("r", encoding="utf-8").readline())
        connection.close()
        return response
    except OSError as error:
        raise SupervisorUnavailable(run_id) from error


def prepare_retry(run_id: str, task_id: str, home: Path, acknowledge: bool) -> Attempt:
    if not acknowledge:
        raise OrchestratorError(
            "dirty_workspace_not_acknowledged",
            "Retry needs dirty-workspace acknowledgement",
            "Add --acknowledge-dirty-workspace after inspecting changes",
            3,
            {"run_id": run_id, "task_id": task_id},
        )
    store = get_store(home)
    current = latest_attempt(store, run_id, task_id)
    if current.status not in {"failed", "stopped"}:
        raise OrchestratorError(
            "attempt_not_retryable",
            f"Attempt in state {current.status} cannot be retried",
            "Retry is allowed only for the latest failed/stopped attempt",
            3,
            {"run_id": run_id, "task_id": task_id, "state": current.status},
        )
    if supervisor_alive(load_run(run_id, home)):
        raise OrchestratorError(
            "attempt_not_retryable",
            "The run supervisor is still active",
            "Wait for the run to finish before retry",
            3,
            {"run_id": run_id, "task_id": task_id, "state": "running"},
        )
    paths = run_paths(run_id, home)
    with SupervisorLock(paths.root / "retry.lock"):
        current = latest_attempt(store, run_id, task_id)
        if current.status not in {"failed", "stopped"}:
            raise OrchestratorError(
                "attempt_not_retryable",
                f"Attempt in state {current.status} cannot be retried",
                "Retry is allowed only for the latest failed/stopped attempt",
                3,
                {"run_id": run_id, "task_id": task_id, "state": current.status},
            )
        previous = read_json(task_metadata_path(paths, task_id, current.attempt_no))
        next_no = current.attempt_no + 1
        write_json(
            task_metadata_path(paths, task_id, next_no),
            {
                "attempt_id": uuid.uuid4().hex,
                "attempt_no": next_no,
                "log_path": str(paths.task_dir(task_id, next_no) / "output.jsonl"),
                "cwd": previous["cwd"],
                "hint": "Inspect git status/diff and preserve the previous attempt changes",
                "spawn_intent": False,
            },
        )
        return store.retry_attempt(
            run_id=run_id,
            task_id=task_id,
            expected_attempt_no=current.attempt_no,
        )


def resolve(run_id: str, home: Path, task_id: str | None) -> dict[str, Any]:
    store = get_store(home)
    paths = run_paths(run_id, home)
    metadata = load_run(run_id, home)
    selected = [task for task in run_tasks(metadata, home) if task_id is None or task.id == task_id]
    if not selected:
        raise OrchestratorError(
            "task_not_found",
            f"Task {task_id} was not found",
            "Check status for available task IDs",
            3,
            {"run_id": run_id, "task_id": task_id},
        )
    if metadata.get("setup_failed"):
        return {
            "schema_version": 1,
            "run_id": run_id,
            "status": "manual_review",
            "resources": metadata.get("setup_resources", []),
            "journal": str(paths.root / "setup-journal"),
        }
    results = []
    for task in selected:
        attempt = latest_attempt(store, run_id, task.id)
        if attempt.status != "unknown":
            continue
        attempt_meta = read_json(task_metadata_path(paths, task.id, attempt.attempt_no))
        log_path = Path(attempt_meta["log_path"])
        lines = deque(maxlen=100)
        if log_path.exists():
            with log_path.open(encoding="utf-8") as stream:
                for line in stream:
                    lines.append(line)
        from .process import resolve_attempt

        identity = None
        if attempt.pid is not None and attempt.pgid is not None and attempt.process_start_time:
            identity = ProcessIdentity(
                attempt.pid,
                attempt.pgid,
                int(attempt.process_start_time),
            )
        try:
            resolution = resolve_attempt(
                {
                    "attempt_id": attempt_meta["attempt_id"],
                    "status": attempt.status,
                    "spawn_intent": attempt_meta["spawn_intent"],
                    "identity": identity,
                    "pgid": attempt.pgid,
                    "log_lines": lines,
                },
                inventory=ProcInventory(),
            )
        except SessionIdError:
            store.resolve_attempt(
                run_id=run_id,
                task_id=task.id,
                attempt_no=attempt.attempt_no,
                expected_status="unknown",
                verified_no_process=True,
            )
            results.append({"task_id": task.id, "status": "failed", "reason": "invalid_session_id"})
            continue
        if resolution.status == "unknown":
            processes = [
                {"pid": item.pid, "pgid": item.pgid, "start_time": item.start_time}
                for item in resolution.processes
            ]
            raise OrchestratorError(
                "supervisor_unavailable",
                "Live orphan processes require manual resolution",
                "Inspect and manually terminate only the verified processes, "
                "then run resolve again",
                5,
                {"run_id": run_id, "task_id": task.id, "processes": processes},
            )
        store.resolve_attempt(
            run_id=run_id,
            task_id=task.id,
            attempt_no=attempt.attempt_no,
            expected_status="unknown",
            verified_no_process=True,
            session_id=resolution.session_id,
            final_status=resolution.status,
        )
        results.append({"task_id": task.id, "status": resolution.status})
    return {"schema_version": 1, "run_id": run_id, "resolved": results}


def launch_detached(run_id: str, home: Path, project: Path) -> dict[str, Any]:
    paths = run_paths(run_id, home)
    paths.ready.unlink(missing_ok=True)
    (paths.root / "startup-error.json").unlink(missing_ok=True)
    log_path = paths.root / "supervisor.log"
    fd = open_log(log_path, exclusive=False)
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "parallel_agentic_os.worker", run_id],
            cwd=project,
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=fd,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        os.close(fd)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if paths.ready.is_file():
            ready = read_json(paths.ready)
            return {
                "accepted": True,
                "supervisor_pid": ready["supervisor_pid"],
                "control_socket": ready["control_socket"],
            }
        if (paths.root / "startup-error.json").is_file() or process.poll() is not None:
            raise SupervisorUnavailable(run_id)
        time.sleep(0.05)
    process.terminate()
    raise SupervisorUnavailable(run_id)
