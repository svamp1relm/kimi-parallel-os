from __future__ import annotations

import json
from pathlib import Path

from conftest import PaoCLI
from parallel_agentic_os.manifest import load_manifest
from parallel_agentic_os.orchestrator import (
    ProcessIdentity,
    ProcInventory,
    Supervisor,
    create_run,
    secure_home,
)


def test_failure_classification_and_missing_process_identity(
    cli: PaoCLI, write_manifest, project: Path
) -> None:
    manifest_path = write_manifest(project, ["alpha"])
    manifest = load_manifest(manifest_path)
    home = secure_home(cli.home)
    created = create_run(manifest, manifest_path, home)
    supervisor = Supervisor(created["run_id"], home)

    assert supervisor._failure_reason(0, []) is None
    assert supervisor._failure_reason(1, []) == "child_exit"
    for kind in ("quota_exceeded", "authentication_failed"):
        line = json.dumps({"type": "error", "error": {"type": kind}})
        assert supervisor._failure_reason(1, [line]) == kind

    assert ProcInventory._identity(-1) is None
    assert ProcInventory().is_alive(ProcessIdentity(-1, -1, 0)) is False


def test_supervisor_metadata_lifecycle_flag(
    cli: PaoCLI, write_manifest, project: Path
) -> None:
    manifest_path = write_manifest(project, ["alpha"])
    manifest = load_manifest(manifest_path)
    home = secure_home(cli.home)
    created = create_run(manifest, manifest_path, home)
    supervisor = Supervisor(created["run_id"], home)

    supervisor._set_supervisor(True)
    supervisor._set_supervisor(False)
    assert supervisor.paths.metadata.is_file()
