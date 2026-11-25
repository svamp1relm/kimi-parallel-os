from __future__ import annotations

from pathlib import Path

import pytest

from conftest import PaoCLI
from parallel_agentic_os.manifest import load_manifest
from parallel_agentic_os.orchestrator import (
    OrchestratorError,
    Supervisor,
    create_run,
    resolve,
    secure_home,
)


def test_supervisor_command_validation_and_unknown_resolution_task(
    cli: PaoCLI, write_manifest, project: Path
) -> None:
    manifest_path = write_manifest(project, ["alpha"])
    manifest = load_manifest(manifest_path)
    home = secure_home(cli.home)
    created = create_run(manifest, manifest_path, home)
    supervisor = Supervisor(created["run_id"], home)

    assert supervisor._handle_command({"action": "unknown"})["accepted"] is False
    with pytest.raises(OrchestratorError) as error:
        supervisor._handle_command({"action": "stop", "task_id": "missing"})
    assert error.value.code == "task_not_found"

    with pytest.raises(OrchestratorError) as error:
        resolve(created["run_id"], home, "missing")
    assert error.value.code == "task_not_found"
