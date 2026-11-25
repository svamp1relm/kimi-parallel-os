from __future__ import annotations

from pathlib import Path

from conftest import PaoCLI
from parallel_agentic_os.manifest import load_manifest
from parallel_agentic_os.orchestrator import Supervisor, create_run, secure_home


def test_stop_pending_attempt_is_terminal_without_spawning(
    cli: PaoCLI, write_manifest, project: Path
) -> None:
    manifest_path = write_manifest(project, ["alpha"])
    manifest = load_manifest(manifest_path)
    home = secure_home(cli.home)
    created = create_run(manifest, manifest_path, home)
    supervisor = Supervisor(created["run_id"], home)

    result = supervisor.send_stop("alpha")

    assert result["accepted"] is True
    attempt = supervisor.store.list_attempts(run_id=created["run_id"], task_id="alpha")[0]
    assert attempt.status == "stopped"
    assert supervisor.live == {}
