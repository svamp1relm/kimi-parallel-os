from __future__ import annotations

import ctypes
import json
import os
import queue
import select
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

_FAKE_KIMI = r"""#!{python}
import json
import os
import re
import signal
import socket
import sys

connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
connection.connect(os.environ["PAO_FAKE_SOCKET"])
reader = connection.makefile("r", encoding="utf-8")
writer = connection.makefile("w", encoding="utf-8")
write_lock = __import__("threading").Lock()


def send(event):
    with write_lock:
        writer.write(json.dumps(event) + "\n")
        writer.flush()


def exit_process(code, reason="command"):
    send({"type": "exiting", "exit_code": code, "reason": reason})
    raise SystemExit(code)


def handle_signal(signum, _frame):
    send({"type": "signal", "signal": signum})
    exit_process(128 + signum, "signal")


signal.signal(signal.SIGTERM, handle_signal)
argv = sys.argv[1:]
session_id = None
if "--session" in argv:
    session_id = argv[argv.index("--session") + 1]
    task = "resume"
    prompt = None
else:
    prompt = argv[1] if len(argv) > 1 else ""
    match = re.search(r"(?:^|\n)FAKE_TASK=([a-z][a-z0-9-]*)", prompt)
    task = match.group(1) if match else "unknown"

send({
    "type": "started",
    "task": task,
    "attempt_id": os.environ.get("PAO_ATTEMPT_ID"),
    "pid": os.getpid(),
    "pgid": os.getpgid(0),
    "cwd": os.getcwd(),
    "argv": argv,
    "session_id": session_id,
})

for line in reader:
    command = json.loads(line)
    action = command["action"]
    if action == "output":
        if command.get("raw") is not None:
            print(command["raw"], flush=True)
        if command.get("session_id") is not None:
            print(json.dumps({
                "type": "session.resume_hint",
                "session_id": command["session_id"],
            }), flush=True)
    elif action == "exit":
        exit_process(command["exit_code"])
    elif action == "complete":
        if command.get("raw") is not None:
            print(command["raw"], flush=True)
        if command.get("session_id") is not None:
            print(json.dumps({
                "type": "session.resume_hint",
                "session_id": command["session_id"],
            }), flush=True)
        exit_process(command["exit_code"])
    elif action == "ignore-term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        send({"type": "ignore-term"})
"""


@dataclass(eq=False)
class FakeClient:
    connection: socket.socket
    writer: Any
    task: str
    attempt_id: str | None
    pid: int
    event: dict[str, Any]
    connected: bool = True
    lock: threading.Lock = field(default_factory=threading.Lock)

    def send(self, command: dict[str, Any]) -> None:
        with self.lock:
            self.writer.write(json.dumps(command) + "\n")
            self.writer.flush()


class FakeKimi:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.socket_path = root / "fake-kimi.sock"
        self.executable = root / "fake-kimi"
        script = _FAKE_KIMI.replace("{python}", sys.executable)
        self.executable.write_text(script, encoding="utf-8")
        self.executable.chmod(0o700)
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(self.socket_path))
        self._server.listen()
        self._events: queue.Queue[dict[str, Any]] = queue.Queue()
        self._clients: list[FakeClient] = []
        self._lock = threading.Lock()
        self._closing = False
        self.starts = Counter()
        self.active = 0
        self.max_active = 0
        self._accept_thread = threading.Thread(target=self._accept, daemon=True)
        self._accept_thread.start()

    def _accept(self) -> None:
        while not self._closing:
            try:
                connection, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._read_client, args=(connection,), daemon=True).start()

    def _read_client(self, connection: socket.socket) -> None:
        reader = connection.makefile("r", encoding="utf-8")
        writer = connection.makefile("w", encoding="utf-8")
        client = None
        try:
            first = reader.readline()
            if not first:
                return
            event = json.loads(first)
            client = FakeClient(
                connection=connection,
                writer=writer,
                task=event["task"],
                attempt_id=event["attempt_id"],
                pid=event["pid"],
                event=event,
            )
            event["client"] = client
            with self._lock:
                self._clients.append(client)
                self.starts[client.task] += 1
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            self._events.put(event)
            for line in reader:
                event = json.loads(line)
                event.update(task=client.task, attempt_id=client.attempt_id, client=client)
                self._events.put(event)
        finally:
            if client is not None:
                with self._lock:
                    if client.connected:
                        client.connected = False
                        self.active -= 1
                self._events.put(
                    {
                        "type": "disconnected",
                        "task": client.task,
                        "attempt_id": client.attempt_id,
                        "client": client,
                    }
                )
            connection.close()

    @staticmethod
    def _matches(
        event: dict[str, Any],
        kind: str,
        task: str | None,
        attempt_id: str | None,
    ) -> bool:
        return (
            event["type"] == kind
            and (task is None or event["task"] == task)
            and (attempt_id is None or event.get("attempt_id") == attempt_id)
        )

    def wait_event(
        self,
        kind: str,
        task: str | None = None,
        *,
        attempt_id: str | None = None,
        timeout: float = 10,
    ) -> dict[str, Any]:
        held = []
        deadline = time.monotonic() + timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"timed out waiting for fake Kimi event {kind}/{task}")
                event = self._events.get(timeout=remaining)
                if self._matches(event, kind, task, attempt_id):
                    return event
                held.append(event)
        finally:
            for event in held:
                self._events.put(event)

    def wait_started(self, task: str, *, timeout: float = 10) -> dict[str, Any]:
        return self.wait_event("started", task, timeout=timeout)

    def complete(
        self,
        event: dict[str, Any],
        *,
        exit_code: int = 0,
        session_id: str | None = None,
        raw: str | None = None,
    ) -> str | None:
        if session_id is None and exit_code == 0:
            session_id = f"session_{uuid.uuid4()}"
        event["client"].send(
            {
                "action": "complete",
                "exit_code": exit_code,
                "session_id": session_id,
                "raw": raw,
            }
        )
        return session_id

    def send(self, event: dict[str, Any], command: dict[str, Any]) -> None:
        event["client"].send(command)

    def is_connected(self, event: dict[str, Any]) -> bool:
        return event["client"].connected

    def close(self) -> None:
        self._closing = True
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            if client.connected:
                try:
                    client.send({"action": "exit", "exit_code": 0})
                except OSError:
                    pass
        try:
            wake = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            wake.connect(str(self.socket_path))
            wake.close()
        except OSError:
            pass
        self._server.close()
        self._accept_thread.join(timeout=2)


@pytest.fixture
def fake_kimi(tmp_path: Path) -> FakeKimi:
    root = tmp_path / "fake-kimi-home"
    root.mkdir()
    fake = FakeKimi(root)
    yield fake
    fake.close()


class PaoCLI:
    def __init__(self, project: Path, home: Path) -> None:
        self.project = project
        self.home = home

    def env(
        self,
        fake: FakeKimi | None,
        *,
        extra: dict[str, str] | None = None,
        path: str | None = None,
    ) -> dict[str, str]:
        env = os.environ.copy()
        env["PAO_HOME"] = str(self.home)
        env["PYTHONPATH"] = os.pathsep.join(
            [str(self.project / "src"), env.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
        if fake is not None:
            env["PAO_KIMI_EXECUTABLE"] = str(fake.executable)
            env["PAO_FAKE_SOCKET"] = str(fake.socket_path)
        else:
            env.pop("PAO_KIMI_EXECUTABLE", None)
            env.pop("PAO_FAKE_SOCKET", None)
        if path is not None:
            env["PATH"] = path
        if extra:
            env.update(extra)
        return env

    def argv(self, args: list[str]) -> list[str]:
        return [sys.executable, "-m", "parallel_agentic_os.cli", *args]

    def run(
        self,
        args: list[str],
        fake: FakeKimi | None,
        *,
        env: dict[str, str] | None = None,
        timeout: float = 15,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self.argv(args),
            cwd=self.project,
            env=env or self.env(fake),
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )

    def start(
        self,
        args: list[str],
        fake: FakeKimi | None,
        *,
        env: dict[str, str] | None = None,
    ) -> subprocess.Popen[str]:
        return subprocess.Popen(
            self.argv(args),
            cwd=self.project,
            env=env or self.env(fake),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def finish(
        self, process: subprocess.Popen[str], *, timeout: float = 15
    ) -> subprocess.CompletedProcess[str]:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            raise AssertionError(
                f"CLI did not exit; stdout={stdout!r}; stderr={stderr!r}"
            ) from None
        return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)

    def json_result(
        self,
        args: list[str],
        fake: FakeKimi | None,
        *,
        returncode: int = 0,
        env: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
        result = self.run(args, fake, env=env)
        assert result.returncode == returncode, (result.stdout, result.stderr)
        assert result.stderr == "", result.stderr
        payload = json.loads(result.stdout)
        assert payload["schema_version"] == 1
        return result, payload

    def status(self, run_id: str, fake: FakeKimi | None) -> dict[str, Any]:
        _, payload = self.json_result(["status", run_id, "--json"], fake)
        return payload

    def wait_status(
        self,
        run_id: str,
        fake: FakeKimi,
        predicate: Callable[[dict[str, Any]], bool],
        *,
        timeout: float = 15,
    ) -> dict[str, Any]:
        self.home.mkdir(parents=True, exist_ok=True)
        with InotifyWatcher(self.home) as watcher:
            deadline = time.monotonic() + timeout
            while True:
                payload = self.status(run_id, fake)
                if predicate(payload):
                    return payload
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"timed out waiting for run state; last={payload!r}")
                watcher.wait(remaining)


class InotifyWatcher:
    IN_CREATE = 0x00000100
    IN_MODIFY = 0x00000002
    IN_CLOSE_WRITE = 0x00000008
    IN_MOVED_TO = 0x00000080
    IN_MOVED_FROM = 0x00000040
    IN_DELETE = 0x00000200

    def __init__(self, root: Path) -> None:
        self.root = root
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.fd = self.libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1")
        self._watch_descriptors: set[int] = set()
        self._refresh()

    def _refresh(self) -> None:
        mask = (
            self.IN_CREATE
            | self.IN_MODIFY
            | self.IN_CLOSE_WRITE
            | self.IN_MOVED_TO
            | self.IN_MOVED_FROM
            | self.IN_DELETE
        )
        for directory, _, _ in os.walk(self.root):
            descriptor = self.libc.inotify_add_watch(self.fd, os.fsencode(directory), mask)
            if descriptor >= 0:
                self._watch_descriptors.add(descriptor)

    def wait(self, timeout: float) -> None:
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            raise TimeoutError("state inotify timeout")
        try:
            os.read(self.fd, 65536)
        except BlockingIOError:
            pass
        self._refresh()

    def close(self) -> None:
        os.close(self.fd)

    def __enter__(self) -> InotifyWatcher:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@pytest.fixture
def cli(tmp_path: Path) -> PaoCLI:
    return PaoCLI(Path(__file__).parents[1], tmp_path / "pao-home")


@pytest.fixture
def write_manifest(tmp_path: Path) -> Callable[..., Path]:
    counter = 0

    def write(
        project: Path,
        tasks: list[dict[str, Any] | tuple[str, str] | str],
        *,
        max_parallel: int | None = None,
        worktree: dict[str, Any] | None = None,
    ) -> Path:
        nonlocal counter
        counter += 1
        normalized = []
        for _index, task in enumerate(tasks):
            if isinstance(task, str):
                task = {"id": task, "prompt": f"Implement {task}", "allowed_paths": [task]}
            elif isinstance(task, tuple):
                task_id, prompt = task
                task = {"id": task_id, "prompt": prompt, "allowed_paths": [task_id]}
            task = dict(task)
            task["prompt"] = f"FAKE_TASK={task['id']}\n{task['prompt']}"
            normalized.append(task)
        manifest: dict[str, Any] = {
            "version": 1,
            "project": str(project),
            "tasks": normalized,
        }
        if max_parallel is not None:
            manifest["max_parallel"] = max_parallel
        if worktree is not None:
            manifest["worktree"] = worktree
        path = tmp_path / f"manifest-{counter}.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return path

    return write


def task_view(payload: dict[str, Any], task_id: str) -> dict[str, Any]:
    return next(task for task in payload["tasks"] if task["task_id"] == task_id)


def current_attempt(payload: dict[str, Any], task_id: str) -> dict[str, Any]:
    attempts = task_view(payload, task_id)["attempts"]
    assert [attempt["attempt_no"] for attempt in attempts] == sorted(
        (attempt["attempt_no"] for attempt in attempts), reverse=True
    )
    return attempts[0]


def assert_owner_only(path: Path) -> None:
    for item in [path, *path.rglob("*")]:
        mode = stat.S_IMODE(item.lstat().st_mode)
        if item.is_dir() and not item.is_symlink():
            assert mode == 0o700, (item, oct(mode))
        else:
            assert mode == 0o600, (item, oct(mode))


def terminal(payload: dict[str, Any]) -> bool:
    return payload["status"] in {"completed", "failed", "partial", "stopped", "setup_failed"}


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = tmp_path / "project"
    path.mkdir()
    (path / "alpha").mkdir()
    (path / "beta").mkdir()
    (path / "gamma").mkdir()
    return path
