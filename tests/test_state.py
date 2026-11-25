from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest

from parallel_agentic_os.state import StateConflictError, StateStore, SupervisorLock


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


@pytest.fixture
def store(db_path: Path) -> StateStore:
    return StateStore(db_path)


def create_run(store: StateStore, run_id: str = "run-a", *task_ids: str) -> None:
    store.create_run(run_id=run_id, task_ids=task_ids or ("task-a",))


def create_attempt(
    store: StateStore,
    run_id: str = "run-a",
    task_id: str = "task-a",
    attempt_no: int = 1,
) -> Any:
    return store.create_attempt(run_id=run_id, task_id=task_id, attempt_no=attempt_no)


def transition(
    store: StateStore,
    expected_status: str,
    status: str,
    *,
    run_id: str = "run-a",
    task_id: str = "task-a",
    attempt_no: int = 1,
    **fields: Any,
) -> Any:
    return store.transition_attempt(
        run_id=run_id,
        task_id=task_id,
        attempt_no=attempt_no,
        expected_status=expected_status,
        status=status,
        **fields,
    )


def start_attempt(store: StateStore, *, run_id: str = "run-a", task_id: str = "task-a") -> Any:
    create_attempt(store, run_id=run_id, task_id=task_id)
    transition(store, "pending", "spawning", run_id=run_id, task_id=task_id)
    return transition(
        store,
        "spawning",
        "running",
        run_id=run_id,
        task_id=task_id,
        pid=4100,
        pgid=4100,
        process_start_time="184467.00",
        supervisor_generation=1,
    )


def acquire_lease(
    store: StateStore,
    run_id: str,
    task_id: str,
    canonical_root: Path,
    allowed_path: str,
) -> Any:
    return store.acquire_workspace_lease(
        run_id=run_id,
        task_id=task_id,
        canonical_root=canonical_root,
        allowed_path=allowed_path,
    )


def concurrently(*calls: Callable[[], Any]) -> list[Any]:
    barrier = Barrier(len(calls))

    def invoke(call: Callable[[], Any]) -> Any:
        barrier.wait(timeout=10)
        try:
            return call()
        except StateConflictError as error:
            return error

    with ThreadPoolExecutor(max_workers=len(calls)) as executor:
        return [future.result() for future in [executor.submit(invoke, call) for call in calls]]


def assert_one_winner(results: list[Any]) -> Any:
    winners = [result for result in results if not isinstance(result, StateConflictError)]
    conflicts = [result for result in results if isinstance(result, StateConflictError)]
    assert len(winners) == 1
    assert len(conflicts) == len(results) - 1
    return winners[0]


def test_tc07_sqlite_persists_attempt_history_and_process_metadata(
    db_path: Path, store: StateStore
) -> None:
    create_run(store)
    start_attempt(store)
    first = transition(
        store,
        "running",
        "failed",
        exit_code=17,
        session_id="session_123e4567-e89b-42d3-a456-426614174000",
        failure_reason="child_exit",
    )

    second = store.retry_attempt(run_id="run-a", task_id="task-a", expected_attempt_no=1)

    assert second.attempt_no == 2
    assert second.status == "pending"
    assert second.pid is None
    assert second.exit_code is None
    assert second.session_id is None

    reopened = StateStore(db_path)
    history = reopened.list_attempts(run_id="run-a", task_id="task-a")
    assert [attempt.attempt_no for attempt in history] == [1, 2]

    persisted = history[0]
    assert persisted.status == first.status == "failed"
    assert persisted.pid == 4100
    assert persisted.pgid == 4100
    assert persisted.process_start_time == "184467.00"
    assert persisted.supervisor_generation == 1
    assert persisted.exit_code == 17
    assert persisted.failure_reason == "child_exit"
    assert persisted.session_id == "session_123e4567-e89b-42d3-a456-426614174000"

    created_at = datetime.fromisoformat(persisted.created_at)
    started_at = datetime.fromisoformat(persisted.started_at)
    finished_at = datetime.fromisoformat(persisted.finished_at)
    assert created_at.tzinfo is not None
    assert created_at <= started_at <= finished_at
    assert history[1].finished_at is None


def test_cas_transition_rejects_stale_expected_status_without_modifying_attempt(
    store: StateStore,
) -> None:
    create_run(store)
    start_attempt(store)

    with pytest.raises(StateConflictError):
        transition(store, "pending", "completed", exit_code=0)

    attempt = store.get_attempt(run_id="run-a", task_id="task-a", attempt_no=1)
    assert attempt.status == "running"
    assert attempt.exit_code is None
    assert attempt.finished_at is None


def test_concurrent_cas_transitions_have_exactly_one_winner(
    db_path: Path, store: StateStore
) -> None:
    create_run(store)
    start_attempt(store)

    def finish(status: str, exit_code: int) -> Callable[[], Any]:
        def call() -> Any:
            return transition(
                StateStore(db_path),
                "running",
                status,
                exit_code=exit_code,
                session_id="session_123e4567-e89b-42d3-a456-426614174000",
            )

        return call

    results = concurrently(finish("completed", 0), finish("failed", 9))
    winner = assert_one_winner(results)

    persisted = StateStore(db_path).get_attempt(run_id="run-a", task_id="task-a", attempt_no=1)
    assert persisted.status == winner.status
    assert persisted.exit_code == winner.exit_code
    assert persisted.finished_at is not None


def test_attempt_number_is_unique_even_after_prior_attempt_is_terminal(
    store: StateStore,
) -> None:
    create_run(store)
    start_attempt(store)
    transition(store, "running", "failed", exit_code=1)

    with pytest.raises(StateConflictError):
        create_attempt(store)

    second = create_attempt(store, attempt_no=2)
    assert second.attempt_no == 2
    assert second.status == "pending"


def test_only_one_active_attempt_is_allowed_per_run_and_task(store: StateStore) -> None:
    create_run(store, "run-a", "task-a", "task-b")
    start_attempt(store)

    with pytest.raises(StateConflictError):
        create_attempt(store, attempt_no=2)

    other_task = create_attempt(store, task_id="task-b")
    assert other_task.status == "pending"

    transition(store, "running", "stopped", exit_code=-15)
    retry = create_attempt(store, attempt_no=2)
    assert retry.status == "pending"


def test_tc29_duplicate_retry_has_one_cas_winner_and_preserves_history(
    db_path: Path, store: StateStore
) -> None:
    create_run(store)
    start_attempt(store)
    transition(store, "running", "failed", exit_code=1)

    def retry() -> Callable[[], Any]:
        return lambda: StateStore(db_path).retry_attempt(
            run_id="run-a", task_id="task-a", expected_attempt_no=1
        )

    results = concurrently(retry(), retry())
    winner = assert_one_winner(results)
    assert winner.attempt_no == 2
    assert winner.status == "pending"

    history = StateStore(db_path).list_attempts(run_id="run-a", task_id="task-a")
    assert [(attempt.attempt_no, attempt.status) for attempt in history] == [
        (1, "failed"),
        (2, "pending"),
    ]


def test_tc29_duplicate_supervisor_is_rejected_until_lock_is_released(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "run-a.supervisor.lock"
    first = SupervisorLock(lock_path)
    second = SupervisorLock(lock_path)
    first.acquire()
    try:
        with pytest.raises(StateConflictError):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()


@pytest.mark.parametrize(
    ("first_path", "second_path"),
    [
        ("src", "src"),
        ("src", "src/package"),
        ("src/package", "src"),
    ],
)
def test_tc39_cross_run_workspace_leases_reject_equal_and_nested_paths(
    store: StateStore,
    tmp_path: Path,
    first_path: str,
    second_path: str,
) -> None:
    create_run(store, "run-a", "task-a")
    create_run(store, "run-b", "task-b")
    create_attempt(store, run_id="run-a", task_id="task-a")
    create_attempt(store, run_id="run-b", task_id="task-b")
    root = tmp_path / "project"
    acquire_lease(store, "run-a", "task-a", root, first_path)

    with pytest.raises(StateConflictError):
        acquire_lease(store, "run-b", "task-b", root, second_path)


@pytest.mark.parametrize("other_path", ["src-other", "tests"])
def test_workspace_leases_use_path_boundaries_not_string_prefixes(
    store: StateStore, tmp_path: Path, other_path: str
) -> None:
    create_run(store, "run-a", "task-a")
    create_run(store, "run-b", "task-b")
    create_attempt(store, run_id="run-a", task_id="task-a")
    create_attempt(store, run_id="run-b", task_id="task-b")
    root = tmp_path / "project"
    acquire_lease(store, "run-a", "task-a", root, "src")

    acquire_lease(store, "run-b", "task-b", root, other_path)


def test_workspace_leases_with_same_relative_path_allow_different_roots(
    store: StateStore, tmp_path: Path
) -> None:
    create_run(store, "run-a", "task-a")
    create_run(store, "run-b", "task-b")
    create_attempt(store, run_id="run-a", task_id="task-a")
    create_attempt(store, run_id="run-b", task_id="task-b")

    acquire_lease(store, "run-a", "task-a", tmp_path / "project-a", "src")
    acquire_lease(store, "run-b", "task-b", tmp_path / "project-b", "src")


def test_tc39_concurrent_cross_run_lease_acquisition_has_one_winner(
    db_path: Path, store: StateStore, tmp_path: Path
) -> None:
    create_run(store, "run-a", "task-a")
    create_run(store, "run-b", "task-b")
    create_attempt(store, run_id="run-a", task_id="task-a")
    create_attempt(store, run_id="run-b", task_id="task-b")
    root = tmp_path / "project"

    def acquire(run_id: str, task_id: str) -> Callable[[], Any]:
        return lambda: acquire_lease(StateStore(db_path), run_id, task_id, root, "src")

    results = concurrently(acquire("run-a", "task-a"), acquire("run-b", "task-b"))
    assert_one_winner(results)

    leases = StateStore(db_path).list_workspace_leases()
    assert len(leases) == 1
    assert {(lease.run_id, lease.task_id) for lease in leases} in (
        {("run-a", "task-a")},
        {("run-b", "task-b")},
    )


@pytest.mark.parametrize(
    ("target_status", "transitions"),
    [
        ("pending", []),
        ("spawning", ["spawning"]),
        ("running", ["spawning", "running"]),
        ("stopping", ["spawning", "running", "stopping"]),
        ("unknown", ["spawning", "running", "unknown"]),
    ],
)
def test_workspace_lease_is_retained_for_every_nonterminal_attempt_state(
    store: StateStore,
    tmp_path: Path,
    target_status: str,
    transitions: list[str],
) -> None:
    create_run(store, "run-a", "task-a")
    create_run(store, "run-b", "task-b")
    create_attempt(store, run_id="run-a", task_id="task-a")
    create_attempt(store, run_id="run-b", task_id="task-b")
    root = tmp_path / "project"
    acquire_lease(store, "run-a", "task-a", root, "src")

    expected = "pending"
    for status in transitions:
        transition(store, expected, status, run_id="run-a", task_id="task-a")
        expected = status

    assert store.get_attempt(run_id="run-a", task_id="task-a", attempt_no=1).status == target_status
    with pytest.raises(StateConflictError):
        acquire_lease(store, "run-b", "task-b", root, "src/nested")


@pytest.mark.parametrize(
    ("terminal_status", "exit_code"),
    [("completed", 0), ("failed", 1), ("stopped", -15)],
)
def test_workspace_lease_is_released_after_verified_terminal_transition(
    store: StateStore, tmp_path: Path, terminal_status: str, exit_code: int
) -> None:
    create_run(store, "run-a", "task-a")
    create_run(store, "run-b", "task-b")
    start_attempt(store, run_id="run-a", task_id="task-a")
    create_attempt(store, run_id="run-b", task_id="task-b")
    root = tmp_path / "project"
    acquire_lease(store, "run-a", "task-a", root, "src")

    transition(
        store,
        "running",
        terminal_status,
        run_id="run-a",
        task_id="task-a",
        exit_code=exit_code,
    )

    acquire_lease(store, "run-b", "task-b", root, "src")
    leases = store.list_workspace_leases()
    assert [(lease.run_id, lease.task_id) for lease in leases] == [("run-b", "task-b")]


def test_tc39_unknown_lease_survives_unverified_resolution_and_releases_after_resolve(
    store: StateStore, tmp_path: Path
) -> None:
    create_run(store, "run-a", "task-a")
    create_run(store, "run-b", "task-b")
    start_attempt(store, run_id="run-a", task_id="task-a")
    create_attempt(store, run_id="run-b", task_id="task-b")
    root = tmp_path / "project"
    acquire_lease(store, "run-a", "task-a", root, "src")
    transition(
        store,
        "running",
        "unknown",
        run_id="run-a",
        task_id="task-a",
        failure_reason="supervisor_lost",
    )

    with pytest.raises(StateConflictError):
        transition(
            store,
            "unknown",
            "failed",
            run_id="run-a",
            task_id="task-a",
            failure_reason="supervisor_lost",
        )
    with pytest.raises(StateConflictError):
        store.resolve_attempt(
            run_id="run-a",
            task_id="task-a",
            attempt_no=1,
            expected_status="unknown",
            verified_no_process=False,
        )
    with pytest.raises(StateConflictError):
        acquire_lease(store, "run-b", "task-b", root, "src")

    resolved = store.resolve_attempt(
        run_id="run-a",
        task_id="task-a",
        attempt_no=1,
        expected_status="unknown",
        verified_no_process=True,
    )
    assert resolved.status == "failed"
    assert resolved.failure_reason == "supervisor_lost"
    assert resolved.finished_at is not None

    acquire_lease(store, "run-b", "task-b", root, "src")
