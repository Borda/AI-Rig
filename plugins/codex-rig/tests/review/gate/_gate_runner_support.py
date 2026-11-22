"""Helpers shared by the gate runner acceptance modules.

They build throwaway committed repositories, compose portable gate commands, run the public ``run_gates.py`` CLI, load
the runner and the artifact validator by file path, and seal a retry's publication journal without installing it, so
recovery guards can be exercised against a real interrupted attempt.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from _modules import load_module

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
RUN_GATES = PLUGIN_ROOT / "shared" / "run_gates.py"
GATE_IDS = ("lint", "format", "types", "tests", "review")
CODEX_TRAILER = "Co-authored-by: Codex <codex@openai.com>"


def _git(repository: Path, *arguments: str) -> str:
    """Run one successful local Git command and return its standard output."""
    completed = subprocess.run(["git", *arguments], cwd=repository, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def _git_steps(repository: Path, steps: list[tuple[str, ...]]) -> None:
    """Run each listed Git command in order inside one repository."""
    for step in steps:
        _git(repository, *step)


def _initialize_repository(repository: Path) -> str:
    """Create a clean committed repository whose commits retain Codex attribution."""
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "Codex Rig Test")
    _git(repository, "config", "user.email", "codex-rig@example.invalid")
    (repository / "tracked.txt").write_text("base\n", encoding="utf-8", newline="\n")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "-m", f"fixture\n\n{CODEX_TRAILER}")
    return _git(repository, "rev-parse", "HEAD")


def _python_command(source: str) -> str:
    """Return a native-shell command that executes portable encoded Python source."""
    encoded = base64.b64encode(source.encode("utf-8")).decode("ascii")
    if sys.platform == "win32":
        executable = "& '" + sys.executable.replace("'", "''") + "'"
    else:
        executable = shlex.quote(sys.executable)
    return f"{executable} -c \"import base64;exec(compile(base64.b64decode('{encoded}'), '<gate>', 'exec'))\""


def _marker_command(marker: Path) -> str:
    """Return a gate command whose only effect is to create ``marker``, proving the command ran."""
    return _python_command(f"from pathlib import Path; Path({str(marker)!r}).write_text('ran', encoding='utf-8')")


def _skip_gates(keep: str) -> list[str]:
    """Return the options that declare every canonical gate except ``keep`` out of scope."""
    return [part for gate in GATE_IDS if gate != keep for part in (f"--skip-{gate}", "out of scope")]


def _run(
    arguments: list[str], cwd: Path | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one command to completion, capturing text output and never raising on a failing exit status."""
    return subprocess.run(arguments, cwd=cwd, env=env, capture_output=True, text=True, check=False)


def _run_lint_gate(
    repository: Path, output: Path, expected_head: str | None, command: str, worktree: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Run one executable lint check while declaring the other canonical gates unavailable."""
    arguments = [sys.executable, str(RUN_GATES), "--out", str(output), "--lint", command]
    if expected_head is not None:
        arguments.extend(("--expected-head", expected_head))
    if worktree is not None:
        arguments.extend(("--worktree", str(worktree)))
    return _run([*arguments, *_skip_gates("lint")], cwd=repository)


def _gate_check(output: Path, gate_id: str) -> dict[str, Any]:
    """Load one generated gate record by id from a ``run_gates.py`` output directory."""
    payload = json.loads((output / "gates.json").read_text(encoding="utf-8"))
    return next(check for check in payload["checks"] if check["id"] == gate_id)


def _receipt_text(output: Path, check: dict[str, Any], stream: str = "stderr") -> str:
    """Read the stdout or stderr receipt file that a gate record points at."""
    return (output / str(check[stream])).read_text(encoding="utf-8")


def _load_validator() -> Any:
    """Load the shared artifact validator without requiring a package import."""
    return load_module("gate_artifact_validator", PLUGIN_ROOT / "shared/validate-artifacts.py")


def _load_run_gates(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Import ``run_gates.py`` as a module whose registration in ``sys.modules`` is undone after the test."""
    monkeypatch.syspath_prepend(str(PLUGIN_ROOT / "shared"))
    specification = importlib.util.spec_from_file_location("sidecar_gate_runner", RUN_GATES)
    module = importlib.util.module_from_spec(specification)
    monkeypatch.setitem(sys.modules, specification.name, module)
    specification.loader.exec_module(module)
    return module


def _passing_proof(worktree: Path) -> bytes:
    """Return a structurally valid passing child proof for the single declared ``example`` module."""
    document = {
        "mode": "in-process-pytest",
        "status": "pass",
        "worktree": worktree.as_posix(),
        "invoked_interpreter": Path(sys.executable).as_posix(),
        "modules": {"example": {"status": "pass"}},
        "tests": {"test.py": {"status": "pass"}},
        "workers": {},
        "errors": [],
    }
    return json.dumps(document).encode("utf-8")


def _interrupt_before_install(gates: Any, monkeypatch: pytest.MonkeyPatch, output: Path, argv: list[str]) -> Path:
    """Run one in-process attempt that seals its publication journal, stop before installing, and return its stage."""
    finish = gates._finish_publication
    marker = output / ".gate-publication.json"

    def stop_before_install(root: Path) -> None:
        if marker.exists():
            raise OSError("simulated interruption")
        finish(root)

    with monkeypatch.context() as patch:
        patch.setattr(sys, "argv", argv)
        patch.setattr(gates, "_finish_publication", stop_before_install)
        assert gates.main() == 2
    return output / json.loads(marker.read_bytes())["stage"]


@dataclass(frozen=True)
class _PendingPublication:
    """A sealed retry candidate that stopped before installation, beside the complete receipt it will replace."""

    gates: Any
    output: Path
    stage: Path
    prior: bytes


def _listing(directory: Path) -> Callable[[], list[str]]:
    """Return an observer of the entry names currently inside ``directory``."""
    return lambda: sorted(entry.name for entry in directory.iterdir())


def _foreign_file(path: Path) -> Callable[[], bytes]:
    """Create another owner's file at ``path`` and return an observer of its bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"foreign owner")
    return path.read_bytes


def _swap_directory(directory: Path) -> None:
    """Replace ``directory`` by a new directory holding the same entries, so only the directory identity changes."""
    displaced = directory.with_name(f"{directory.name}-displaced")
    directory.rename(displaced)
    directory.mkdir()
    for entry in displaced.iterdir():
        entry.rename(directory / entry.name)


def _rewrite_after_manifest_install(output: Path, changed: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Append to ``changed`` immediately after installation replaces the live manifest, past every earlier check."""
    replace = os.replace

    def rewrite(source: str | Path, destination: str | Path) -> None:
        replace(source, destination)
        if Path(destination) == output / "gates.json":
            changed.write_bytes(changed.read_bytes() + b"\n")

    monkeypatch.setattr(os, "replace", rewrite)
