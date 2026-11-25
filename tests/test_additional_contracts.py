from __future__ import annotations

import io
import os
import stat
from pathlib import Path

import pytest

from conftest import FakeKimi, PaoCLI
from parallel_agentic_os.manifest import load_manifest
from parallel_agentic_os.orchestrator import (
    OrchestratorError,
    Supervisor,
    bounded_output_lines,
    create_run,
    open_log,
    prepare_retry,
    resolve,
    secure_home,
)


def test_bounded_output_splits_chunks_and_enforces_limit() -> None:
    stream = io.StringIO('{"type":"assistant"}\nlast')
    assert list(bounded_output_lines(stream)) == ['{"type":"assistant"}\n', "last"]

    class LargeStream:
        def __init__(self) -> None:
            self.calls = 0

        def read(self, _size: int) -> str:
            self.calls += 1
            return "x" * 100 if self.calls == 1 else ""

    with pytest.raises(OrchestratorError) as error:
        list(bounded_output_lines(LargeStream(), limit=99))
    assert error.value.code == "log_limit_exceeded"


def test_open_log_append_is_regular_owner_only_file(tmp_path: Path) -> None:
    path = tmp_path / "supervisor.log"
    descriptor = open_log(path, exclusive=False)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write("entry\n")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text(encoding="utf-8") == "entry\n"


def test_setup_failure_resolution_exposes_resources(
    cli: PaoCLI, fake_kimi: FakeKimi, write_manifest, tmp_path: Path
) -> None:
    from test_orchestrator import install_git_wrapper, make_git_repo

    project = tmp_path / "repo"
    make_git_repo(project)
    bin_dir, git_log = install_git_wrapper(tmp_path, fail_worktree_add=True)
    monkeypatch = pytest.MonkeyPatch()
    with monkeypatch.context() as patch:
        patch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        patch.setenv("PAO_GIT_LOG", str(git_log))
        manifest_path = write_manifest(project, ["alpha"], worktree={"enabled": True})
        manifest = load_manifest(manifest_path)
        home = secure_home(cli.home)
        created = create_run(manifest, manifest_path, home)
        Supervisor(created["run_id"], home).run()

    result = resolve(created["run_id"], home, "alpha")
    assert result["status"] == "manual_review"
    assert result["resources"][0]["kind"] == "worktree"
    assert result["resources"][1]["kind"] == "branch"


def test_retry_requires_acknowledgement_and_terminal_attempt(
    cli: PaoCLI, write_manifest, project: Path
) -> None:
    manifest_path = write_manifest(project, ["alpha"])
    manifest = load_manifest(manifest_path)
    home = secure_home(cli.home)
    created = create_run(manifest, manifest_path, home)
    with pytest.raises(OrchestratorError, match="acknowledge"):
        prepare_retry(created["run_id"], "alpha", home, False)
    with pytest.raises(OrchestratorError) as error:
        prepare_retry(created["run_id"], "alpha", home, True)
    assert error.value.code == "attempt_not_retryable"


def test_unsupported_platform_is_actionable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("parallel_agentic_os.orchestrator.sys.platform", "darwin")
    with pytest.raises(OrchestratorError) as error:
        secure_home(tmp_path / "home")
    assert error.value.code == "unsupported_platform"
    assert error.value.exit_code == 2
