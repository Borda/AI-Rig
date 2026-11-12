"""Exercise package script boundaries against unsafe files and publication payloads."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from _modules import load_module, load_module_scoped
from _platform import FILE_SYMLINKS_AVAILABLE

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="host has no FIFO creation API")
def test_manifest_fifo_is_rejected_without_waiting(tmp_path: Path) -> None:
    """Reject a manifest FIFO before waiting for a writer to open it."""
    os.mkfifo(tmp_path / "package-manifest.json")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "_package_identity.py"),
            "--root",
            str(tmp_path),
            "--expected-package-hash",
            "0" * 64,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=2,
    )

    assert result.returncode == 2
    assert "Traceback" not in result.stderr


def _nested_tree(root: Path, depth: int) -> None:
    """Build a deep tree without relying on Python recursion."""
    directory = root
    for _ in range(depth):
        directory /= "d"
        directory.mkdir()
    (directory / "payload").write_bytes(b"data")


def test_inventory_depth_has_classified_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject excessive depth as an unsafe package rather than exhausting the stack."""
    safe = load_module_scoped("safe_inventory_regression", SCRIPTS / "_safe_package_io.py", monkeypatch)
    _nested_tree(tmp_path, 130)

    with pytest.raises(safe.SafePackageIOError, match="depth"):
        safe.inventory_package_files(tmp_path, excluded_parts=frozenset(), excluded_files=frozenset())


def test_inventory_recursion_error_is_classified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Normalize a stack exhaustion from a native inventory backend."""
    safe = load_module_scoped("safe_recursion_regression", SCRIPTS / "_safe_package_io.py", monkeypatch)

    def exhausted(*args: object) -> tuple[str, ...]:
        """Emulate exhausted recursion at the backend boundary."""
        raise RecursionError("stack exhausted")

    backend = "_inventory_windows" if os.name == "nt" else "_inventory_posix"
    monkeypatch.setattr(safe, backend, exhausted)

    with pytest.raises(safe.SafePackageIOError, match="depth"):
        safe.inventory_package_files(tmp_path, excluded_parts=frozenset(), excluded_files=frozenset())


@pytest.mark.parametrize("name", ["package-manifest.json", ".coverage"])
def test_inventory_excludes_only_root_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    """A root metadata exclusion cannot hide nested payload bytes with the same basename."""
    safe = load_module_scoped("safe_exclusion_regression", SCRIPTS / "_safe_package_io.py", monkeypatch)
    (tmp_path / name).write_bytes(b"root metadata")
    nested = tmp_path / "scripts" / name
    nested.parent.mkdir()
    nested.write_bytes(b"payload")

    assert safe.inventory_package_files(tmp_path, excluded_parts=frozenset(), excluded_files=frozenset({name})) == (
        f"scripts/{name}",
    )
    builder = load_module("builder_exclusion_regression", SCRIPTS / "build_package.py")
    monkeypatch.setattr(builder, "PACKAGE_ROOT", tmp_path)
    assert builder.iter_payload_files() == [nested]


def test_leftover_manifest_temporary_is_not_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep an interrupted manifest update out of the next inventory."""
    builder = load_module("builder_temporary_regression", SCRIPTS / "build_package.py")
    (tmp_path / "package-manifest.json.tmp").write_bytes(b"incomplete")
    (tmp_path / "payload").write_bytes(b"data")
    monkeypatch.setattr(builder, "PACKAGE_ROOT", tmp_path)

    assert builder.iter_payload_files() == [tmp_path / "payload"]


@pytest.mark.skipif(not FILE_SYMLINKS_AVAILABLE, reason="filesystem cannot create file symlinks")
def test_manifest_update_does_not_follow_temporary_symlink(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An existing temporary symlink cannot overwrite a file outside the manifest."""
    builder = load_module("builder_symlink_regression", SCRIPTS / "build_package.py")
    victim = tmp_path / "victim"
    victim.write_bytes(b"preserved")
    manifest = tmp_path / "package-manifest.json"
    manifest.with_suffix(".json.tmp").symlink_to(victim)
    monkeypatch.setattr(builder, "PACKAGE_ROOT", tmp_path)
    monkeypatch.setattr(builder, "MANIFEST_PATH", manifest)
    monkeypatch.setattr(builder, "parse_args", lambda: SimpleNamespace(update=True, check=False))
    monkeypatch.setattr(builder, "build_manifest", lambda: {"files": []})
    monkeypatch.setattr(builder, "enforces_posix_modes", lambda: True)

    with pytest.raises(FileExistsError):
        builder.main()

    assert victim.read_bytes() == b"preserved"
    assert not manifest.exists()


@pytest.mark.parametrize("relative", ["runtime.txt", "tests/fixture.txt"])
@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"/Us" + b"ers/private/project", id="home-path"),
        pytest.param(b"-----BEGIN RSA " + b"PRIVATE KEY-----", id="rsa-key"),
        pytest.param(b"ghp_" + b"A" * 36, id="github-token"),
        pytest.param(b"sk-" + b"A" * 48, id="api-key"),
    ],
)
def test_publication_scans_secrets_in_all_payloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str, payload: bytes
) -> None:
    """Test fixtures cannot bypass publication checks for private paths or credentials."""
    validator = load_module("publication_secret_regression", SCRIPTS / "validate_package.py")
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    (tmp_path / "package-manifest.json").write_text(json.dumps({"files": [{"path": relative}]}), encoding="utf-8")
    monkeypatch.setattr(validator, "PACKAGE_ROOT", tmp_path)

    with pytest.raises(ValueError, match="private material"):
        validator.validate_publication_payload()
