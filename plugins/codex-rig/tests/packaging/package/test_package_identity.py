"""Acceptance checks for portable installed-package identity verification."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
from _modules import load_module_scoped
from _platform import FILE_SYMLINKS_AVAILABLE

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
IDENTITY_PATH = PLUGIN_ROOT / "scripts" / "_package_identity.py"
#: POSIX mode comparison is meaningful everywhere except native Windows.
ENFORCE_MODES = os.name != "nt"


def _write_fixture(root: Path, *, recorded_mode: int | None = None) -> None:
    """Write one minimal complete schema-1 package fixture; ``recorded_mode`` overrides the payload's recorded mode."""
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
    (root / "package-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _tamper_payload(root: Path) -> None:
    """Change the payload's bytes after its hash was recorded."""
    (root / "payload.txt").write_text("tampered\n", encoding="utf-8")


def _add_unrecorded_file(root: Path) -> None:
    """Add a file the manifest does not record."""
    (root / "extra.txt").write_text("unrecorded\n", encoding="utf-8")


@pytest.fixture
def identity(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Load the package verifier directly from its installed script path, undoing its registration afterwards."""
    return load_module_scoped("codex_rig_package_identity", IDENTITY_PATH, monkeypatch)


@pytest.fixture
def make_package(tmp_path: Path) -> Callable[..., Path]:
    """Return a factory that writes the minimal package fixture into ``tmp_path`` and returns its root."""

    def make(*, recorded_mode: int | None = None) -> Path:
        """Write the fixture, optionally recording a mode different from the payload's real one."""
        _write_fixture(tmp_path, recorded_mode=recorded_mode)
        return tmp_path

    return make


@pytest.mark.packaging
class TestVerifyPackage:
    """``verify_package`` accepts only a package that matches its manifest exactly."""

    def test_checks_hashes_without_path_read_bytes(
        self, identity: ModuleType, make_package: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verified inputs are read only through the bounded safe-open backend, never ``Path.read_bytes``.

        ``Path.read_bytes`` is rigged to fail after the fixture is written; a clean verification of both recorded files
        with the mode check passing proves no unbounded fallback read happened.
        """
        root = make_package()

        def forbidden_read_bytes(path: Path) -> bytes:
            """Fail if package verification bypasses the bounded safe-read backend."""
            pytest.fail(f"unverified Path.read_bytes fallback: {path}")

        monkeypatch.setattr(Path, "read_bytes", forbidden_read_bytes)

        result = identity.verify_package(root, enforce_modes=True)

        assert result.version == "1.2.3"
        assert result.files_verified == 2
        assert result.mode_status == "pass"

    def test_reports_mode_check_not_applicable_when_modes_are_not_enforced(
        self, identity: ModuleType, make_package: Callable[..., Path]
    ) -> None:
        """Only the POSIX mode comparison is skipped when native Windows cannot retain modes.

        The recorded mode (0) disagrees with the file, yet verification passes with mode status "not-applicable", and
        machine-local ``.reports`` debris next to the package is ignored.
        """
        root = make_package(recorded_mode=0)
        debris = root / ".reports" / "codex" / "calibration" / "run" / "debris.txt"
        debris.parent.mkdir(parents=True)
        debris.write_text("machine-local runtime artifact\n", encoding="utf-8")

        result = identity.verify_package(root, enforce_modes=False)

        assert result.files_verified == 2
        assert result.mode_status == "not-applicable"

    def test_bounds_every_recorded_payload(
        self, identity: ModuleType, make_package: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A recorded file is rejected before any read beyond the configured safety limit.

        The limit is lowered to 4 bytes, below the 17-byte payload, so verification must raise an oversize error.
        """
        root = make_package()
        monkeypatch.setattr(identity, "MAX_PACKAGE_FILE_BYTES", 4)

        with pytest.raises(identity.PackageIdentityError, match="oversized package file"):
            identity.verify_package(root, enforce_modes=ENFORCE_MODES)

    def test_rejects_mode_drift_on_posix(self, identity: ModuleType, make_package: Callable[..., Path]) -> None:
        """Exact mode enforcement is preserved on supported POSIX filesystems.

        The manifest records mode 0 for a payload with a normal mode, and the error must name the file.
        """
        root = make_package(recorded_mode=0)

        with pytest.raises(identity.PackageIdentityError, match=r"mode mismatch: payload.txt"):
            identity.verify_package(root, enforce_modes=True)

    @pytest.mark.parametrize(
        ("mutate", "expected"),
        [
            pytest.param(_tamper_payload, "hash mismatch: payload.txt", id="tampered-bytes"),
            pytest.param(_add_unrecorded_file, "package file closure mismatch", id="unrecorded-file"),
        ],
    )
    def test_rejects_payload_drift(
        self,
        identity: ModuleType,
        make_package: Callable[..., Path],
        mutate: Callable[[Path], None],
        expected: str,
    ) -> None:
        """Changed bytes and unrecorded package payloads are both rejected with a specific reason.

        After a valid package is written, either the payload is edited or an extra file is added; neither may verify.
        """
        root = make_package()
        mutate(root)

        with pytest.raises(identity.PackageIdentityError, match=expected):
            identity.verify_package(root, enforce_modes=ENFORCE_MODES)

    @pytest.mark.parametrize("unsafe_path", [r"folder\payload.txt", "C:payload.txt"])
    def test_rejects_nonportable_record_paths(
        self, identity: ModuleType, make_package: Callable[..., Path], unsafe_path: str
    ) -> None:
        """Manifest paths cannot change containment meaning on Windows (backslash separators, drive-relative paths).

        The last recorded path is replaced by a Windows-ambiguous spelling; verification must refuse it as invalid.
        """
        root = make_package()
        manifest_path = root / "package-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"][-1]["path"] = unsafe_path
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        with pytest.raises(identity.PackageIdentityError, match="invalid package file path"):
            identity.verify_package(root)

    @pytest.mark.skipif(not FILE_SYMLINKS_AVAILABLE, reason="host cannot create file symlinks")
    def test_rejects_symlink_payload(self, identity: ModuleType, make_package: Callable[..., Path]) -> None:
        """A symlinked payload is rejected before any of its bytes are consumed.

        The recorded payload file is replaced by a link to another package file, which must be named as an unsafe node.
        """
        root = make_package()
        payload = root / "payload.txt"
        payload.unlink()
        payload.symlink_to(root / ".codex-plugin" / "plugin.json")

        with pytest.raises(identity.PackageIdentityError, match=r"unsafe package node: payload.txt"):
            identity.verify_package(root, enforce_modes=True)
