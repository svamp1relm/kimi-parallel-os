from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .manifest import load_manifest
from .orchestrator import (
    OrchestratorError,
    Supervisor,
    create_run,
    default_home,
    export_run,
    get_store,
    latest_attempt,
    launch_detached,
    load_run,
    markdown_export,
    prepare_retry,
    project_run,
    request_stop,
    resolve,
    secure_home,
)
from .process import SupervisorUnavailable
from .state import StateConflictError
from .worktree import DirtyBaseError


def emit(value: dict[str, Any]) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))


def error_payload(error: OrchestratorError) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "error": {
            "code": error.code,
            "message": error.message,
            "hint": error.hint,
            "context": error.context,
        },
    }


def structured_error(error: Exception, path: str | None = None) -> OrchestratorError:
    if isinstance(error, OrchestratorError):
        return error
    if isinstance(error, SupervisorUnavailable):
        return OrchestratorError(
            "supervisor_unavailable",
            "Supervisor is unavailable",
            "Inspect status and use resolve after any live processes are manually stopped",
            5,
            {"run_id": error.run_id},
        )
    if isinstance(error, DirtyBaseError):
        return OrchestratorError(
            "dirty_base",
            "The Git base has uncommitted changes",
            "Set allow_dirty_base=true only if HEAD is an intentional baseline",
            2,
        )
    if isinstance(error, StateConflictError):
        return OrchestratorError(
            "state_conflict", str(error), "Inspect current status before retrying", 3
        )
    if isinstance(error, FileNotFoundError):
        return OrchestratorError(
            "invalid_manifest",
            "Manifest was not found",
            "Check the manifest path",
            2,
            {"path": path or str(error.filename)},
        )
    if isinstance(error, ValueError):
        return OrchestratorError(
            "invalid_manifest",
            str(error),
            "Correct the manifest and try again",
            2,
            {"path": path} if path else {},
        )
    if isinstance(error, OSError):
        return OrchestratorError(
            "io_error",
            str(error),
            "Check the output path and permissions",
            4,
            {"path": path} if path else {},
        )
    return OrchestratorError(
        "internal_error", str(error), "Inspect the state and supervisor logs", 4
    )


def write_export(content: str, output: Path, force: bool) -> None:
    if output.exists() and not force:
        raise OrchestratorError(
            "output_exists",
            f"Output {output} already exists",
            "Use --force to replace it or choose another path",
            3,
            {"path": str(output)},
        )
    temporary = output.with_name(f"{output.name}.tmp-{os.getpid()}")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(content)
        temporary.chmod(0o600)
        temporary.replace(output)
        output.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise OrchestratorError(
            "invalid_arguments",
            message,
            "Use parallel-agents --help for the command contract",
            2,
        )


def parser() -> argparse.ArgumentParser:
    result = JsonArgumentParser(prog="parallel-agents")
    result.add_argument("--json", action="store_true")
    commands = result.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("manifest")
    run.add_argument("--detach", action="store_true")
    run.add_argument("--json", action="store_true")
    commands.add_parser("list").add_argument("--json", action="store_true")
    status = commands.add_parser("status")
    status.add_argument("run_id")
    status.add_argument("--task")
    status.add_argument("--attempt", type=int)
    status.add_argument("--json", action="store_true")
    stop = commands.add_parser("stop")
    stop.add_argument("run_id")
    stop.add_argument("--task")
    stop.add_argument("--json", action="store_true")
    resolve_parser = commands.add_parser("resolve")
    resolve_parser.add_argument("run_id")
    resolve_parser.add_argument("--task")
    resolve_parser.add_argument("--json", action="store_true")
    retry = commands.add_parser("retry")
    retry.add_argument("run_id")
    retry.add_argument("task_id")
    retry.add_argument("--detach", action="store_true")
    retry.add_argument("--acknowledge-dirty-workspace", action="store_true")
    retry.add_argument("--json", action="store_true")
    export = commands.add_parser("export")
    export.add_argument("run_id")
    export.add_argument("--format", choices=["json", "markdown"], default="json")
    export.add_argument("--output", default="-")
    export.add_argument("--force", action="store_true")
    export.add_argument("--json", action="store_true")
    return result


def human_status(payload: dict[str, Any]) -> None:
    print(f"{payload['run_id']} {payload['status']}")
    for task in payload["tasks"]:
        attempt = task["attempts"][0]
        print(f"  {task['task_id']}: {attempt['status']}")
        if attempt["session_id"]:
            print(f"    session: {attempt['session_id']}")
        if attempt["resume"]:
            from .process import resume_command

            print(f"    {resume_command(Path(attempt['resume']['cwd']), attempt['session_id'])}")
        elif attempt["status"] in {"running", "spawning"}:
            print("    session ID becomes available after completion")
        if attempt["resolve"]:
            print("    " + " ".join(attempt["resolve"]["argv"]))
        if attempt["failure_reason"]:
            print(f"    reason: {attempt['failure_reason']}; log: {attempt['log_path']}")


def list_runs(home: Path) -> dict[str, Any]:
    root = home / "runs"
    runs = []
    if root.is_dir():
        for metadata in sorted(
            root.glob("*/run.json"), key=lambda path: path.stat().st_mtime, reverse=True
        ):
            run_id = metadata.parent.name
            projected = project_run(run_id, home)
            runs.append(
                {
                    key: projected[key]
                    for key in ("run_id", "project", "status", "created_at", "finished_at")
                }
            )
    return {"schema_version": 1, "runs": runs}


def main(argv: list[str] | None = None) -> int:
    manifest_path = None
    try:
        args = parser().parse_args(argv)
        manifest_path = getattr(args, "manifest", None)
        home = secure_home(default_home())
        if args.command == "list":
            payload = list_runs(home)
            if args.json:
                emit(payload)
            elif not payload["runs"]:
                print("No runs.")
            else:
                for run in payload["runs"]:
                    print(f"{run['run_id']} {run['status']} {run['project']}")
            return 0
        if args.command == "status":
            payload = project_run(args.run_id, home)
            if args.task is not None:
                tasks = [task for task in payload["tasks"] if task["task_id"] == args.task]
                if not tasks:
                    raise OrchestratorError(
                        "task_not_found",
                        f"Task {args.task} was not found",
                        "Check status for available task IDs",
                        3,
                        {"run_id": args.run_id, "task_id": args.task},
                    )
                payload["tasks"] = tasks
            if args.attempt is not None:
                found = False
                for task in payload["tasks"]:
                    attempts = [
                        attempt
                        for attempt in task["attempts"]
                        if attempt["attempt_no"] == args.attempt
                    ]
                    found = found or bool(attempts)
                    task["attempts"] = attempts
                if not found:
                    raise OrchestratorError(
                        "attempt_not_found",
                        f"Attempt {args.attempt} was not found",
                        "Check the attempt number",
                        3,
                        {"run_id": args.run_id, "attempt": args.attempt},
                    )
            if args.json:
                emit(payload)
            else:
                human_status(payload)
            return 0
        if args.command == "export":
            if args.format == "markdown":
                content = markdown_export(args.run_id, home)
            else:
                content = (
                    json.dumps(
                        export_run(args.run_id, home), ensure_ascii=False, indent=2, sort_keys=True
                    )
                    + "\n"
                )
            if args.output == "-":
                print(content, end="")
            else:
                try:
                    write_export(content, Path(args.output), args.force)
                except OSError as error:
                    raise OrchestratorError(
                        "io_error",
                        str(error),
                        "Check the output path and permissions",
                        4,
                        {"path": args.output},
                    ) from error
                if args.json:
                    emit({"schema_version": 1, "run_id": args.run_id, "written": args.output})
            return 0
        if args.command == "resolve":
            payload = resolve(args.run_id, home, args.task)
            emit(payload)
            return 0
        if args.command == "stop":
            if args.task is not None:
                available = {task["task_id"] for task in project_run(args.run_id, home)["tasks"]}
                if args.task not in available:
                    raise OrchestratorError(
                        "task_not_found",
                        f"Task {args.task} was not found",
                        "Check status for available task IDs",
                        3,
                        {"run_id": args.run_id, "task_id": args.task},
                    )
            try:
                payload = request_stop(args.run_id, home, args.task)
            except SupervisorUnavailable:
                if args.task:
                    attempt = latest_attempt(get_store(home), args.run_id, args.task)
                    if attempt.status in {"stopped", "completed", "failed"}:
                        payload = {"accepted": True, "run_id": args.run_id, "idempotent": True}
                    else:
                        raise
                else:
                    raise
            emit({"schema_version": 1, **payload})
            return 0
        if args.command == "retry":
            attempt = prepare_retry(
                args.run_id, args.task_id, home, args.acknowledge_dirty_workspace
            )
            metadata = load_run(args.run_id, home)
            if args.detach:
                accepted = launch_detached(args.run_id, home, Path(metadata["project"]))
                payload = {
                    **accepted,
                    "schema_version": 1,
                    "run_id": args.run_id,
                    "status": "running",
                    "attempt_no": attempt.attempt_no,
                }
            else:
                Supervisor(args.run_id, home).run()
                payload = project_run(args.run_id, home)
            emit(payload)
            return 0 if args.detach or payload["status"] == "completed" else 4
        if args.command == "run":
            manifest_file = Path(manifest_path)
            manifest = load_manifest(manifest_file)
            created = create_run(manifest, manifest_file, home)
            run_id = created["run_id"]
            try:
                if args.detach:
                    accepted = launch_detached(run_id, home, manifest.project)
                    payload = {
                        **accepted,
                        "schema_version": 1,
                        "run_id": run_id,
                        "status": "running",
                    }
                else:
                    Supervisor(run_id, home).run()
                    payload = project_run(run_id, home)
            except DirtyBaseError:
                raise
            emit(payload)
            return 0 if args.detach or payload["status"] == "completed" else 4
        raise OrchestratorError("invalid_command", "Unknown command", "Use --help", 2)
    except Exception as raw_error:
        error = structured_error(raw_error, manifest_path)
        emit(error_payload(error))
        return error.exit_code


if __name__ == "__main__":
    sys.exit(main())
