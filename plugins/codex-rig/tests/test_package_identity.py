"""Acceptance checks for portable installed-package identity verification."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path
from types import ModuleType

import pytest

from _platform import FILE_SYMLINKS_AVAILABLE


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
IDENTITY_PATH = PLUGIN_ROOT / "scripts" / "_package_identity.py"


def _load_identity() -> ModuleType:
    """Load the package verifier directly from its installed script path."""
    specification = importlib.util.spec_from_file_location("codex_rig_package_identity", IDENTITY_PATH)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def _write_fixture(root: Path, *, recorded_mode: int | None = None) -> None:
    """Write one minimal complete schema-1 package fixture."""
    plugin = root / ".codex-plugin" / "plugin.json"
    plugin.parent.mkdir(parents=True)
    plugin.write_text(json.dumps({"name": "codex-rig", "version": "1.2.3"}), encoding="utf-8")
    payload = root / "payload.txt"
    payload.write_text("verified payload\n", encoding="utf-8")
    mode = stat.S_IMODE(payload.stat().st_mode) if recorded_mode is None else recorded_mode
    files = []
    for path in (plugin, payload):
        relative = path.relative_to(root).as_posix()
        files.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "mode": f"{mode if path == payload else stat.S_IMODE(path.stat().st_mode):04o}",
            }
        )
    manifest = {
        "schema": 1,
        "plugin": "codex-rig",
        "version": "1.2.3",
        "release_profile": "role-card-injected",
        "features": {"manager": True, "hooks": True, "mcp": False, "generated_shims": False},
        "skills": [],
        "roles": [],
        "bootstrap": {"protocol": 1, "helper": "payload.txt", "sha256": files[1]["sha256"]},
        "generator": {"version": 1, "path": "payload.txt", "sha256": files[1]["sha256"]},
        "files": files,
        "excluded": [],
    }
    (root / "package-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


@pytest.mark.packaging
def test_verify_package_checks_hashes_without_path_read_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Read verified inputs only through the bounded safe-open backend."""
    identity = _load_identity()
    _write_fixture(tmp_path)

    def _forbidden_read_bytes(path: Path) -> bytes:
        """Fail if package verification bypasses the bounded safe-read backend."""
        pytest.fail(f"unverified Path.read_bytes fallback: {path}")

    monkeypatch.setattr(Path, "read_bytes", _forbidden_read_bytes)
    result = identity.verify_package(tmp_path, enforce_modes=True)

    assert result.version == "1.2.3"
    assert result.files_verified == 2
    assert result.mode_status == "pass"


@pytest.mark.packaging
def test_verify_package_reports_simulated_windows_mode_check_not_applicable(tmp_path: Path) -> None:
    """Ignore only POSIX mode comparison when native Windows cannot retain it."""
    identity = _load_identity()
    _write_fixture(tmp_path, recorded_mode=0)
    report = tmp_path / ".reports" / "codex" / "calibration" / "run" / "debris.txt"
    report.parent.mkdir(parents=True)
    report.write_text("machine-local runtime artifact\n", encoding="utf-8")

    result = identity.verify_package(tmp_path, enforce_modes=False)

    assert result.files_verified == 2
    assert result.mode_status == "not-applicable"


@pytest.mark.packaging
def test_verify_package_bounds_every_recorded_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject a recorded file before reading beyond the configured safety limit."""
    identity = _load_identity()
    _write_fixture(tmp_path)
    monkeypatch.setattr(identity, "MAX_PACKAGE_FILE_BYTES", 4)

    with pytest.raises(identity.PackageIdentityError, match="oversized package file"):
        identity.verify_package(tmp_path, enforce_modes=os.name != "nt")


@pytest.mark.packaging
def test_verify_package_rejects_mode_drift_on_posix(tmp_path: Path) -> None:
    """Preserve exact mode enforcement on supported POSIX filesystems."""
    identity = _load_identity()
    _write_fixture(tmp_path, recorded_mode=0)

    with pytest.raises(identity.PackageIdentityError, match="mode mismatch: payload.txt"):
        identity.verify_package(tmp_path, enforce_modes=True)


@pytest.mark.parametrize("mutation", ("tamper", "extra"))
@pytest.mark.packaging
def test_verify_package_rejects_payload_drift(tmp_path: Path, mutation: str) -> None:
    """Reject changed bytes and unrecorded package payloads."""
    identity = _load_identity()
    _write_fixture(tmp_path)
    if mutation == "tamper":
        (tmp_path / "payload.txt").write_text("tampered\n", encoding="utf-8")
        expected = "hash mismatch: payload.txt"
    else:
        (tmp_path / "extra.txt").write_text("unrecorded\n", encoding="utf-8")
        expected = "package file closure mismatch"

    with pytest.raises(identity.PackageIdentityError, match=expected):
        identity.verify_package(tmp_path, enforce_modes=os.name != "nt")


@pytest.mark.parametrize("unsafe_path", [r"folder\payload.txt", "C:payload.txt"])
@pytest.mark.packaging
def test_verify_package_rejects_nonportable_record_paths(tmp_path: Path, unsafe_path: str) -> None:
    """Prevent manifest paths from changing containment meaning on Windows."""
    identity = _load_identity()
    _write_fixture(tmp_path)
    manifest_path = tmp_path / "package-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][-1]["path"] = unsafe_path
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(identity.PackageIdentityError, match="invalid package file path"):
        identity.verify_package(tmp_path)


@pytest.mark.skipif(not FILE_SYMLINKS_AVAILABLE, reason="host cannot create file symlinks")
@pytest.mark.packaging
def test_verify_package_rejects_symlink_payload(tmp_path: Path) -> None:
    """Reject links before any verified payload bytes are consumed."""
    identity = _load_identity()
    _write_fixture(tmp_path)
    payload = tmp_path / "payload.txt"
    payload.unlink()
    payload.symlink_to(tmp_path / ".codex-plugin" / "plugin.json")

    with pytest.raises(identity.PackageIdentityError, match="unsafe package node: payload.txt"):
        identity.verify_package(tmp_path, enforce_modes=True)
