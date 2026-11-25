from __future__ import annotations

import fcntl
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

_TERMINAL_STATUSES = frozenset({"completed", "failed", "stopped", "setup_failed"})
_MUTABLE_FIELDS = frozenset(
    {
        "pid",
        "pgid",
        "process_start_time",
        "supervisor_generation",
        "exit_code",
        "session_id",
        "failure_reason",
    }
)


class StateConflictError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Attempt:
    run_id: str
    task_id: str
    attempt_no: int
    status: str
    pid: int | None
    pgid: int | None
    process_start_time: str | None
    supervisor_generation: int | None
    exit_code: int | None
    session_id: str | None
    failure_reason: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None


@dataclass(frozen=True, slots=True)
class WorkspaceLease:
    canonical_root: str
    allowed_path: str
    run_id: str
    task_id: str
    state: str
    created_at: str


class StateStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._prepare_private_file(self.db_path)
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    PRIMARY KEY (run_id, task_id),
                    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS attempts (
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    attempt_no INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    pid INTEGER,
                    pgid INTEGER,
                    process_start_time TEXT,
                    supervisor_generation INTEGER,
                    exit_code INTEGER,
                    session_id TEXT,
                    failure_reason TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    PRIMARY KEY (run_id, task_id, attempt_no),
                    FOREIGN KEY (run_id, task_id) REFERENCES tasks(run_id, task_id)
                        ON DELETE CASCADE
                );

                CREATE UNIQUE INDEX IF NOT EXISTS one_active_attempt
                ON attempts (run_id, task_id)
                WHERE status NOT IN ('completed', 'failed', 'stopped', 'setup_failed');

                CREATE TABLE IF NOT EXISTS workspace_leases (
                    canonical_root TEXT NOT NULL,
                    allowed_path TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, task_id, canonical_root, allowed_path),
                    FOREIGN KEY (run_id, task_id) REFERENCES tasks(run_id, task_id)
                        ON DELETE CASCADE
                );
                """
            )
        finally:
            connection.close()
        self._secure_database_files()

    @staticmethod
    def _prepare_private_file(path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.parent.is_symlink() or path.is_symlink():
            raise StateConflictError(f"Refusing symbolic-link state path: {path}")
        if path.exists() and not path.is_file():
            raise StateConflictError(f"State path is not a regular file: {path}")
        os.chmod(path.parent, 0o700)

    def _secure_database_files(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{self.db_path}{suffix}")
            if path.is_symlink():
                raise StateConflictError(f"Refusing symbolic-link state path: {path}")
            if path.exists():
                try:
                    os.chmod(path, 0o600)
                except FileNotFoundError:
                    continue

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path,
            timeout=10,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def _reading(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    @staticmethod
    def _attempt_from_row(row: sqlite3.Row) -> Attempt:
        return Attempt(
            run_id=row["run_id"],
            task_id=row["task_id"],
            attempt_no=row["attempt_no"],
            status=row["status"],
            pid=row["pid"],
            pgid=row["pgid"],
            process_start_time=row["process_start_time"],
            supervisor_generation=row["supervisor_generation"],
            exit_code=row["exit_code"],
            session_id=row["session_id"],
            failure_reason=row["failure_reason"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )

    @staticmethod
    def _lease_from_row(row: sqlite3.Row) -> WorkspaceLease:
        return WorkspaceLease(
            canonical_root=row["canonical_root"],
            allowed_path=row["allowed_path"],
            run_id=row["run_id"],
            task_id=row["task_id"],
            state=row["state"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _select_attempt(
        connection: sqlite3.Connection,
        run_id: str,
        task_id: str,
        attempt_no: int,
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM attempts WHERE run_id = ? AND task_id = ? AND attempt_no = ?",
            (run_id, task_id, attempt_no),
        ).fetchone()

    def create_run(self, *, run_id: str, task_ids: tuple[str, ...]) -> None:
        try:
            with self._transaction() as connection:
                connection.execute(
                    "INSERT INTO runs (run_id, created_at) VALUES (?, ?)",
                    (run_id, self._now()),
                )
                connection.executemany(
                    "INSERT INTO tasks (run_id, task_id, position) VALUES (?, ?, ?)",
                    [(run_id, task_id, position) for position, task_id in enumerate(task_ids)],
                )
        except sqlite3.IntegrityError as error:
            raise StateConflictError(f"Run already exists or is invalid: {run_id}") from error

    def create_attempt(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt_no: int,
    ) -> Attempt:
        try:
            with self._transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO attempts (
                        run_id, task_id, attempt_no, status, created_at
                    ) VALUES (?, ?, ?, 'pending', ?)
                    """,
                    (run_id, task_id, attempt_no, self._now()),
                )
                row = self._select_attempt(connection, run_id, task_id, attempt_no)
        except sqlite3.IntegrityError as error:
            raise StateConflictError(
                f"Attempt {attempt_no} conflicts with the active attempt or existing history"
            ) from error
        if row is None:
            raise StateConflictError("Created attempt could not be read")
        return self._attempt_from_row(row)

    def get_attempt(self, *, run_id: str, task_id: str, attempt_no: int) -> Attempt:
        with self._reading() as connection:
            row = self._select_attempt(connection, run_id, task_id, attempt_no)
        if row is None:
            raise StateConflictError(f"Attempt not found: {run_id}/{task_id} attempt {attempt_no}")
        return self._attempt_from_row(row)

    def list_attempts(self, *, run_id: str, task_id: str) -> list[Attempt]:
        with self._reading() as connection:
            rows = connection.execute(
                """
                SELECT * FROM attempts
                WHERE run_id = ? AND task_id = ?
                ORDER BY attempt_no
                """,
                (run_id, task_id),
            ).fetchall()
        return [self._attempt_from_row(row) for row in rows]

    def transition_attempt(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt_no: int,
        expected_status: str,
        status: str,
        **fields: Any,
    ) -> Attempt:
        unknown_fields = fields.keys() - _MUTABLE_FIELDS
        if unknown_fields:
            raise TypeError(f"Unsupported attempt fields: {sorted(unknown_fields)}")

        assignments = ["status = ?"]
        parameters: list[Any] = [status]
        for field in _MUTABLE_FIELDS:
            if field in fields:
                assignments.append(f"{field} = ?")
                parameters.append(fields[field])
        now = self._now()
        if status == "running":
            assignments.append("started_at = COALESCE(started_at, ?)")
            parameters.append(now)
        if status in _TERMINAL_STATUSES:
            assignments.append("finished_at = ?")
            parameters.append(now)

        with self._transaction() as connection:
            current = self._select_attempt(connection, run_id, task_id, attempt_no)
            if current is None or current["status"] != expected_status:
                raise StateConflictError(
                    f"Expected attempt status {expected_status!r}, "
                    f"found {None if current is None else current['status']!r}"
                )
            if current["status"] == "unknown":
                raise StateConflictError("Unknown attempts require verified resolution")
            parameters.extend([run_id, task_id, attempt_no, expected_status])
            cursor = connection.execute(
                f"""
                UPDATE attempts SET {", ".join(assignments)}
                WHERE run_id = ? AND task_id = ? AND attempt_no = ? AND status = ?
                """,
                parameters,
            )
            if cursor.rowcount != 1:
                raise StateConflictError("Attempt changed during compare-and-swap transition")
            if status in _TERMINAL_STATUSES:
                connection.execute(
                    "DELETE FROM workspace_leases WHERE run_id = ? AND task_id = ?",
                    (run_id, task_id),
                )
            row = self._select_attempt(connection, run_id, task_id, attempt_no)
        if row is None:
            raise StateConflictError("Transitioned attempt could not be read")
        return self._attempt_from_row(row)

    def retry_attempt(
        self,
        *,
        run_id: str,
        task_id: str,
        expected_attempt_no: int,
    ) -> Attempt:
        with self._transaction() as connection:
            latest = connection.execute(
                """
                SELECT * FROM attempts
                WHERE run_id = ? AND task_id = ?
                ORDER BY attempt_no DESC LIMIT 1
                """,
                (run_id, task_id),
            ).fetchone()
            if (
                latest is None
                or latest["attempt_no"] != expected_attempt_no
                or latest["status"] not in {"failed", "stopped"}
            ):
                raise StateConflictError("Only the latest failed or stopped attempt can be retried")
            next_attempt_no = expected_attempt_no + 1
            connection.execute(
                """
                INSERT INTO attempts (run_id, task_id, attempt_no, status, created_at)
                VALUES (?, ?, ?, 'pending', ?)
                """,
                (run_id, task_id, next_attempt_no, self._now()),
            )
            row = self._select_attempt(connection, run_id, task_id, next_attempt_no)
        if row is None:
            raise StateConflictError("Retry attempt could not be read")
        return self._attempt_from_row(row)

    def resolve_attempt(
        self,
        *,
        run_id: str,
        task_id: str,
        attempt_no: int,
        expected_status: str,
        verified_no_process: bool,
        session_id: str | None = None,
        final_status: str = "failed",
    ) -> Attempt:
        if final_status not in {"failed", "stopped"}:
            raise ValueError("final_status must be failed or stopped")
        with self._transaction() as connection:
            current = self._select_attempt(connection, run_id, task_id, attempt_no)
            if (
                current is None
                or current["status"] != expected_status
                or expected_status != "unknown"
                or not verified_no_process
            ):
                raise StateConflictError(
                    "Resolution requires an unknown attempt and verified process absence"
                )
            cursor = connection.execute(
                f"""
                UPDATE attempts
                SET status = '{final_status}',
                    failure_reason = COALESCE(failure_reason, 'supervisor_lost'),
                    session_id = COALESCE(?, session_id),
                    finished_at = ?
                WHERE run_id = ? AND task_id = ? AND attempt_no = ? AND status = 'unknown'
                """,
                (session_id, self._now(), run_id, task_id, attempt_no),
            )
            if cursor.rowcount != 1:
                raise StateConflictError("Attempt changed during resolution")
            connection.execute(
                "DELETE FROM workspace_leases WHERE run_id = ? AND task_id = ?",
                (run_id, task_id),
            )
            row = self._select_attempt(connection, run_id, task_id, attempt_no)
        if row is None:
            raise StateConflictError("Resolved attempt could not be read")
        return self._attempt_from_row(row)

    @staticmethod
    def _normalise_allowed_path(allowed_path: str) -> str:
        path = PurePosixPath(allowed_path)
        if not allowed_path or allowed_path == "." or path.is_absolute() or ".." in path.parts:
            raise ValueError("allowed_path must be a non-empty relative path without '..'")
        return path.as_posix()

    @staticmethod
    def _paths_overlap(first: str, second: str) -> bool:
        first_parts = PurePosixPath(first).parts
        second_parts = PurePosixPath(second).parts
        shortest = min(len(first_parts), len(second_parts))
        return first_parts[:shortest] == second_parts[:shortest]

    def acquire_workspace_lease(
        self,
        *,
        run_id: str,
        task_id: str,
        canonical_root: str | Path,
        allowed_path: str,
    ) -> WorkspaceLease:
        root = str(Path(canonical_root).expanduser().resolve(strict=False))
        normalised_path = self._normalise_allowed_path(allowed_path)
        with self._transaction() as connection:
            active = connection.execute(
                """
                SELECT 1 FROM attempts
                WHERE run_id = ? AND task_id = ?
                    AND status NOT IN ('completed', 'failed', 'stopped', 'setup_failed')
                LIMIT 1
                """,
                (run_id, task_id),
            ).fetchone()
            if active is None:
                raise StateConflictError("Workspace leases require an active attempt")
            leases = connection.execute(
                "SELECT canonical_root, allowed_path, run_id, task_id FROM workspace_leases"
            ).fetchall()
            for lease in leases:
                if lease["canonical_root"] == root and self._paths_overlap(
                    lease["allowed_path"], normalised_path
                ):
                    raise StateConflictError(
                        "Workspace path overlaps an active lease held by "
                        f"{lease['run_id']}/{lease['task_id']}"
                    )
            created_at = self._now()
            connection.execute(
                """
                INSERT INTO workspace_leases (
                    canonical_root, allowed_path, run_id, task_id, state, created_at
                ) VALUES (?, ?, ?, ?, 'active', ?)
                """,
                (root, normalised_path, run_id, task_id, created_at),
            )
            row = connection.execute(
                """
                SELECT * FROM workspace_leases
                WHERE run_id = ? AND task_id = ? AND canonical_root = ? AND allowed_path = ?
                """,
                (run_id, task_id, root, normalised_path),
            ).fetchone()
        if row is None:
            raise StateConflictError("Acquired workspace lease could not be read")
        return self._lease_from_row(row)

    def list_workspace_leases(self) -> list[WorkspaceLease]:
        with self._reading() as connection:
            rows = connection.execute(
                """
                SELECT * FROM workspace_leases
                ORDER BY canonical_root, allowed_path, run_id, task_id
                """
            ).fetchall()
        return [self._lease_from_row(row) for row in rows]


class SupervisorLock:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> None:
        if self._fd is not None:
            raise StateConflictError(f"Supervisor lock is already held: {self.path}")
        StateStore._prepare_private_file(self.path)
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self.path, flags, 0o600)
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as error:
            if "fd" in locals():
                os.close(fd)
            raise StateConflictError(f"Supervisor lock is unavailable: {self.path}") from error
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        fd = self._fd
        self._fd = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> SupervisorLock:
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()
