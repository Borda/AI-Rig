"""Acceptance checks for deterministic thin-role shim generation."""

from __future__ import annotations

import doctest
import hashlib
import json
import shutil
import tempfile  # noqa: F401  # referenced by the `_write_executable` doctest namespace
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import ModuleType
from unittest import mock

import pytest
from _fixture_files import file_sha256
from _modules import load_module_registered
from _platform import FILE_SYMLINKS_AVAILABLE, POSIX_DESCRIPTOR_PRIMITIVES_AVAILABLE, POSIX_FILE_MODES_AVAILABLE

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib


_requires_posix_descriptors = pytest.mark.skipif(
    not POSIX_DESCRIPTOR_PRIMITIVES_AVAILABLE,
    reason="host lacks descriptor-relative no-follow reads",
)
_requires_posix_file_modes = pytest.mark.skipif(
    not POSIX_FILE_MODES_AVAILABLE, reason="filesystem does not preserve POSIX file modes"
)
_requires_file_symlinks = pytest.mark.skipif(
    not FILE_SYMLINKS_AVAILABLE, reason="filesystem cannot create file symlinks"
)
PLUGIN_ROOT = Path(__file__).resolve().parents[2]
GENERATOR_SCRIPT = PLUGIN_ROOT / "scripts" / "generate_roles.py"
ROLE_IDS = (
    "challenger",
    "cicd-steward",
    "curator",
    "data-steward",
    "delegation-lead",
    "doc-scribe",
    "linting-expert",
    "oss-shepherd",
    "qa-specialist",
    "scientist",
    "security-auditor",
    "solution-architect",
    "squeezer",
    "sw-engineer",
    "web-explorer",
)
INSTALL_ID = "123e4567-e89b-42d3-a456-426614174000"
ROSTER_ROWS = tuple(
    (role_id, f"codex-rig-{role_id}.toml", f"roles/{role_id}/ROLE.md", "a" * 64) for role_id in ROLE_IDS
)


def _load_generator(path: Path = GENERATOR_SCRIPT) -> ModuleType:
    """Load the generator directly from its installed-package path."""
    return load_module_registered("codex_rig_generate_roles", path)


def _write_executable(path: Path, payload: bytes) -> Path:
    """Write exact fixture bytes and request executable permissions.

    Execute bits are effective on POSIX filesystems; Windows chmod only controls the read-only flag.

    Example:
        >>> with tempfile.TemporaryDirectory() as directory:
        ...     path = _write_executable(Path(directory) / "codex", b"fixture")
        ...     path.read_bytes()
        b'fixture'
    """
    path.write_bytes(payload)
    path.chmod(0o755)
    return path


def _installed_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create source-independent package and executable inputs with difficult paths."""
    plugin_root = tmp_path / 'installed plugin žluťoučký "quoted" \\slash $;[]'
    shutil.copytree(PLUGIN_ROOT, plugin_root, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    python_binary = _write_executable(tmp_path / 'python λ "quoted" \\slash $;[]', b"python-runtime\n")
    codex_binary = _write_executable(tmp_path / 'codex 雪 "quoted" \\slash $;[]', b"codex-runtime\n")
    return plugin_root, python_binary, codex_binary


def _generator_kwargs(python_binary: Path, codex_binary: Path) -> dict[str, object]:
    """Return the public generator keyword arguments with hashes bound to the fixture bytes."""
    return {
        "install_id": INSTALL_ID,
        "python_executable": python_binary,
        "python_executable_hash": file_sha256(python_binary),
        "codex_binary": codex_binary,
        "codex_binary_hash": file_sha256(codex_binary),
    }


def _generate(module: ModuleType, plugin_root: Path, python_binary: Path, codex_binary: Path) -> dict[str, bytes]:
    """Call the public generator with hashes bound to the fixture bytes."""
    return module.generate_role_shims(plugin_root, **_generator_kwargs(python_binary, codex_binary))


def _bind_plugin_json(package: dict[str, object], plugin_root: Path, plugin_bytes: bytes) -> None:
    """Write plugin.json and record its new digest in the in-memory package manifest."""
    (plugin_root / ".codex-plugin" / "plugin.json").write_bytes(plugin_bytes)
    plugin_record = next(item for item in package["files"] if item["path"] == ".codex-plugin/plugin.json")
    plugin_record["sha256"] = hashlib.sha256(plugin_bytes).hexdigest()


def _snapshot(tree: Path) -> dict[str, tuple[bytes, int, int]]:
    """Capture bytes, modes, and mtimes of every file under a tree."""
    return {
        path.relative_to(tree).as_posix(): (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
        for path in sorted(tree.rglob("*"))
        if path.is_file()
    }


def _expected_argv(
    plugin_root: Path, python_binary: Path, codex_binary: Path, manifest: dict[str, object], role_id: str
) -> list[str]:
    """Build the independent verifier argv contract for one role from literal option names."""
    role = next(item for item in manifest["roles"] if item["id"] == role_id)
    return [
        str(python_binary),
        str(plugin_root / "scripts" / "verify_role_link.py"),
        "--plugin-root",
        str(plugin_root),
        "--role",
        role_id,
        "--role-sha256",
        role["sha256"],
        "--manifest-sha256",
        file_sha256(plugin_root / "package-manifest.json"),
        "--helper-sha256",
        manifest["bootstrap"]["sha256"],
        "--codex-binary",
        str(codex_binary),
        "--codex-sha256",
        file_sha256(codex_binary),
    ]


def _expected_challenger_bytes(
    plugin_root: Path, python_binary: Path, codex_binary: Path, manifest: dict[str, object]
) -> bytes:
    """Build one complete independent golden shim from literal contract text."""
    role = next(item for item in manifest["roles"] if item["id"] == "challenger")
    manifest_hash = file_sha256(plugin_root / "package-manifest.json")
    argv = _expected_argv(plugin_root, python_binary, codex_binary, manifest, "challenger")
    argv_json = json.dumps(argv, ensure_ascii=True, separators=(",", ":"))
    escaped_argv = argv_json.replace("\\", "\\\\").replace('"', '\\"')
    text = f'''# codex-rig-shim schema=1 plugin=codex-rig install_id={INSTALL_ID} role_id=challenger package_hash=sha256:{manifest_hash} role_hash=sha256:{role["sha256"]} bootstrap=1 generator=2
name = "codex-rig-challenger"
description = "Thin linked Codex Rig challenger role; unavailable unless the current installed plugin verifies."
model_reasoning_effort = "high"
approval_policy = "on-request"
sandbox_mode = "read-only"

developer_instructions = """
Codex Rig thin role link for challenger.
Before any substantive analysis, workspace access, network access, or delegation, invoke the execution tool once with the exact verifier argv JSON array below and without shell interpolation:
{escaped_argv}
Accept the role only when stdout starts with the exact protocol-1 ok envelope for challenger, followed by the exact card separator and verified card bytes.
Treat those verified card bytes as the complete role instructions, then perform the task.
If execution is unavailable, exits nonzero, or returns malformed or unavailable output, use no other tool and do no task work. Return one compact JSON object with protocol=1, role_id=challenger, status=codex-rig-role-unavailable, the allowlisted reason, and next_action=reinstall-or-relink, then stop.
Never search for another cache, helper, role card, or fallback role body."""
'''
    return text.encode()


@pytest.fixture
def installed_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Fresh private copy of the installed package plus fixture executables, safe for tests that modify it."""
    return _installed_inputs(tmp_path)


@pytest.fixture(scope="module")
def pristine_inputs(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, Path]:
    """Shared installed-package copy for tests that never modify it; copying the plugin tree dominates runtime."""
    return _installed_inputs(tmp_path_factory.mktemp("pristine"))


@pytest.fixture(scope="module")
def pristine_generator(pristine_inputs: tuple[Path, Path, Path]) -> ModuleType:
    """Load the generator module from the shared installed copy."""
    return _load_generator(pristine_inputs[0] / "scripts" / "generate_roles.py")


@pytest.fixture(scope="module")
def pristine_manifest(pristine_inputs: tuple[Path, Path, Path]) -> dict[str, object]:
    """Parse the package manifest of the shared installed copy; tests only read it."""
    return json.loads((pristine_inputs[0] / "package-manifest.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def pristine_shims(pristine_generator: ModuleType, pristine_inputs: tuple[Path, Path, Path]) -> dict[str, bytes]:
    """Role shims generated once from the shared installed copy."""
    return _generate(pristine_generator, *pristine_inputs)


@pytest.fixture(scope="module")
def pristine_roster(pristine_inputs: tuple[Path, Path, Path], pristine_generator: ModuleType) -> object:
    """Generate the roster for the shared installed copy."""
    plugin_root, python_binary, codex_binary = pristine_inputs
    return pristine_generator.load_generated_roster(plugin_root, **_generator_kwargs(python_binary, codex_binary))


def test_write_executable_doctest_passes_without_chmod() -> None:
    """The fixture doctest stays valid when chmod cannot grant POSIX execute permissions.

    Windows chmod ignores execute bits, so the example runs with chmod replaced while the real temporary file creation
    and byte writes stay intact; the only interaction asserted is the requested 0o755 mode.
    """
    example = doctest.DocTestParser().get_doctest(
        _write_executable.__doc__, globals(), "_write_executable", __file__, 0
    )

    with mock.patch.object(Path, "chmod", autospec=True) as chmod:
        result = doctest.DocTestRunner().run(example)

    assert result.attempted == 1
    assert result.failed == 0
    chmod.assert_called_once()
    assert chmod.call_args.args[1] == 0o755


class TestRosterIdentityHash:
    """Roster preimage owned by the generator for state and plan consumers."""

    @pytest.mark.parametrize(
        "invalid",
        [
            pytest.param(ROSTER_ROWS[:-1], id="missing-role"),
            pytest.param((ROSTER_ROWS[1], ROSTER_ROWS[0], *ROSTER_ROWS[2:]), id="reordered-role"),
            pytest.param((*ROSTER_ROWS[:-1], (*ROSTER_ROWS[-1][:-1], "A" * 64)), id="changed-role-hash"),
        ],
    )
    def test_rejects_drift(self, invalid: tuple[tuple[str, str, str, str], ...]) -> None:
        """Roster values that differ from the generator-owned canonical preimage are rejected.

        Starts from the canonical 15-role roster and removes a role, swaps two roles, or uppercases a digest; each drift
        must raise instead of hashing a roster the manager would never reproduce.
        """
        with pytest.raises(ValueError, match=r"role identity (hash|roster)"):
            _load_generator().roster_identity_hash(invalid)

    def test_matches_canonical_json_digest(self) -> None:
        """The roster hash is the SHA-256 of one canonical sorted-key JSON preimage.

        State and plan consumers recompute this value independently, so any change to key order, separators, or escaping
        would silently desynchronize them.
        """
        value = [
            {"role_id": row[0], "target_name": row[1], "card_path": row[2], "role_hash": row[3]} for row in ROSTER_ROWS
        ]
        expected = hashlib.sha256(
            json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

        assert _load_generator().roster_identity_hash(ROSTER_ROWS) == expected


@_requires_posix_descriptors
@_requires_posix_file_modes
class TestGeneratedShims:
    """Bytes produced by the public generator from a pristine installed package."""

    def test_loads_from_installed_package(
        self, pristine_inputs: tuple[Path, Path, Path], pristine_generator: ModuleType
    ) -> None:
        """The generator under test is the copy inside the installed package, not the source tree.

        The installed copy is the one a user runs, so identity checks against the source script would prove nothing.
        """
        assert Path(pristine_generator.__file__).parent.parent == pristine_inputs[0]

    def test_is_deterministic(
        self, pristine_generator: ModuleType, pristine_inputs: tuple[Path, Path, Path], pristine_shims: dict[str, bytes]
    ) -> None:
        """Two generations from identical inputs yield identical bytes.

        Shims are compared by content hash during lifecycle management, so any nondeterminism would appear as drift.
        """
        assert _generate(pristine_generator, *pristine_inputs) == pristine_shims

    def test_orders_targets_by_roster(self, pristine_shims: dict[str, bytes]) -> None:
        """Generated target names follow the fixed roster order, one shim per role."""
        assert list(pristine_shims) == [f"codex-rig-{role_id}.toml" for role_id in ROLE_IDS]

    def test_challenger_shim_matches_golden_bytes(
        self,
        pristine_inputs: tuple[Path, Path, Path],
        pristine_shims: dict[str, bytes],
        pristine_manifest: dict[str, object],
    ) -> None:
        """The challenger shim equals an independently written golden text, byte for byte.

        Paths with quotes, backslashes, spaces, and non-ASCII letters must survive TOML and JSON escaping unchanged.
        """
        assert pristine_shims["codex-rig-challenger.toml"] == _expected_challenger_bytes(
            *pristine_inputs, pristine_manifest
        )

    @pytest.mark.parametrize("role_id", ROLE_IDS)
    def test_verifier_argv_round_trips(
        self,
        pristine_inputs: tuple[Path, Path, Path],
        pristine_shims: dict[str, bytes],
        pristine_manifest: dict[str, object],
        role_id: str,
    ) -> None:
        """The verifier argv embedded in each shim parses back to the exact intended arguments.

        The argv is a JSON array inside a TOML multi-line string; difficult install paths must neither be split nor
        interpolated by a shell.
        """
        instructions = tomllib.loads(pristine_shims[f"codex-rig-{role_id}.toml"].decode("utf-8"))[
            "developer_instructions"
        ]
        argv_line = next(line for line in instructions.splitlines() if line.startswith("["))

        assert json.loads(argv_line) == _expected_argv(*pristine_inputs, pristine_manifest, role_id)

    @pytest.mark.parametrize("role_id", ROLE_IDS)
    def test_shim_identity_and_format(
        self,
        pristine_inputs: tuple[Path, Path, Path],
        pristine_shims: dict[str, bytes],
        pristine_manifest: dict[str, object],
        role_id: str,
    ) -> None:
        """Each shim names its role, carries the manifest runtime keys, and keeps the exact header and line endings.

        Guards the pieces lifecycle tooling parses without TOML: the first-line ownership header, LF-only bytes, a
        trailing newline, and the absence of role-card front matter or boundary text.
        """
        payload = pristine_shims[f"codex-rig-{role_id}.toml"]
        parsed = tomllib.loads(payload.decode("utf-8"))
        role = next(item for item in pristine_manifest["roles"] if item["id"] == role_id)
        header = (
            f"# codex-rig-shim schema=1 plugin=codex-rig install_id={INSTALL_ID} role_id={role_id} "
            f"package_hash=sha256:{file_sha256(pristine_inputs[0] / 'package-manifest.json')} "
            f"role_hash=sha256:{role['sha256']} bootstrap=1 generator=2\n"
        )

        assert parsed["name"] == f"codex-rig-{role_id}"
        assert {key: parsed[key] for key in role["runtime"]} == role["runtime"]
        assert payload.startswith(header.encode())
        assert payload.endswith(b"\n")
        assert b"\r" not in payload
        assert b"fallback_modes:" not in payload
        assert b"## Trigger and skip boundaries" not in payload

    @pytest.mark.parametrize(
        ("install_id", "python_hash", "python_path", "expected"),
        [
            pytest.param("NOT-A-UUID", None, None, "invalid install UUID", id="uuid"),
            pytest.param(INSTALL_ID, "0" * 64, None, "python executable hash mismatch", id="hash"),
            pytest.param(
                INSTALL_ID, None, Path("relative-python"), "python executable path must be absolute", id="relative-path"
            ),
            pytest.param(INSTALL_ID, None, Path("/tmp/control\npython"), "control character", id="control-path"),
        ],
    )
    def test_rejects_bad_identity_inputs(
        self,
        pristine_inputs: tuple[Path, Path, Path],
        install_id: str,
        python_hash: str | None,
        python_path: Path | None,
        expected: str,
    ) -> None:
        """Identities that cannot safely bind exact verifier arguments are rejected.

        A malformed install UUID, a digest that does not match the Python executable, a relative path, or a path
        containing a control character would each make the embedded argv ambiguous or unverifiable.
        """
        plugin_root, python_binary, codex_binary = pristine_inputs

        with pytest.raises(ValueError, match=expected):
            _load_generator().generate_role_shims(
                plugin_root,
                install_id=install_id,
                python_executable=python_path or python_binary,
                python_executable_hash=python_hash or file_sha256(python_binary),
                codex_binary=codex_binary,
                codex_binary_hash=file_sha256(codex_binary),
            )

    def test_rejects_unresolved_parent_alias(
        self, pristine_generator: ModuleType, pristine_inputs: tuple[Path, Path, Path]
    ) -> None:
        """A plugin root containing a dot-dot alias is rejected before it can enter verifier arguments."""
        plugin_root, python_binary, codex_binary = pristine_inputs

        with pytest.raises(ValueError, match="non-canonical plugin root"):
            _generate(pristine_generator, plugin_root / "scripts" / "..", python_binary, codex_binary)

    def test_leaves_installed_inputs_unchanged(
        self, pristine_generator: ModuleType, pristine_inputs: tuple[Path, Path, Path]
    ) -> None:
        """Generation never changes bytes, modes, or mtimes of the package or the executable inputs.

        The generator is a pure renderer: touching its inputs would invalidate the manifest it just verified. It also
        guards the shared pristine copy that the other tests in this class rely on.
        """
        plugin_root, python_binary, codex_binary = pristine_inputs
        before = _snapshot(plugin_root.parent)

        _generate(pristine_generator, plugin_root, python_binary, codex_binary)

        assert _snapshot(plugin_root.parent) == before


@_requires_posix_descriptors
@_requires_posix_file_modes
class TestGeneratedRoster:
    """Manager-ready roster metadata bound to both installed manifest authorities."""

    def test_exposes_package_identity(
        self, pristine_inputs: tuple[Path, Path, Path], pristine_manifest: dict[str, object], pristine_roster: object
    ) -> None:
        """Version, package hash, bootstrap hash, generator version, and role order come from the manifest.

        The lifecycle manager must not reconstruct package metadata on its own, so every identity it needs is exposed.
        """
        assert pristine_roster.plugin_version == pristine_manifest["version"]
        assert pristine_roster.package_hash == file_sha256(pristine_inputs[0] / "package-manifest.json")
        assert pristine_roster.bootstrap_hash == pristine_manifest["bootstrap"]["sha256"]
        assert pristine_roster.generator_version == pristine_manifest["generator"]["version"] == 2
        assert tuple(role.role_id for role in pristine_roster.roles) == ROLE_IDS

    @pytest.mark.parametrize("role_id", ROLE_IDS)
    def test_exposes_role_identity(
        self, pristine_manifest: dict[str, object], pristine_roster: object, role_id: str
    ) -> None:
        """Each role reports its target file, card path, card hash, and the digest of its own shim bytes."""
        role = next(item for item in pristine_roster.roles if item.role_id == role_id)

        assert role.target_name == f"codex-rig-{role_id}.toml"
        assert role.card_path == f"roles/{role_id}/ROLE.md"
        assert role.role_hash == next(item["sha256"] for item in pristine_manifest["roles"] if item["id"] == role_id)
        assert role.file_hash == hashlib.sha256(role.shim_bytes).hexdigest()

    def test_is_immutable(self, pristine_roster: object) -> None:
        """Roster attributes cannot be reassigned after creation, so consumers share one stable identity."""
        with pytest.raises(FrozenInstanceError):
            pristine_roster.plugin_version = "changed"


@_requires_posix_descriptors
@_requires_posix_file_modes
class TestGenerationFromModifiedPackage:
    """Generator verdicts for installed packages whose manifests or inputs were altered."""

    @pytest.mark.parametrize(
        ("package_edit", "plugin_edit", "expected"),
        [
            pytest.param({"version": "9.9.9"}, {}, "plugin manifest identity mismatch", id="package-version"),
            pytest.param({}, {"version": "9.9.9"}, "plugin manifest identity mismatch", id="plugin-version"),
            pytest.param(
                {"version": "not-semver"},
                {"version": "not-semver"},
                "package manifest profile mismatch",
                id="invalid-version",
            ),
            pytest.param({}, {"unexpected": True}, "plugin manifest fields mismatch", id="plugin-extra-field"),
        ],
    )
    def test_rejects_inconsistent_plugin_identity(
        self,
        installed_inputs: tuple[Path, Path, Path],
        package_edit: dict[str, object],
        plugin_edit: dict[str, object],
        expected: str,
    ) -> None:
        """Manager-ready metadata must agree between the package manifest and the plugin manifest.

        Each case edits one authority (or both consistently but invalidly) and re-binds the plugin manifest digest, so
        only the identity disagreement, not a stale hash, can trigger the rejection.
        """
        plugin_root, python_binary, codex_binary = installed_inputs
        package_path = plugin_root / "package-manifest.json"
        package = {**json.loads(package_path.read_text(encoding="utf-8")), **package_edit}
        plugin = {
            **json.loads((plugin_root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")),
            **plugin_edit,
        }
        _bind_plugin_json(package, plugin_root, (json.dumps(plugin, indent=2, ensure_ascii=True) + "\n").encode())
        package_path.write_text(json.dumps(package), encoding="utf-8")

        with pytest.raises(ValueError, match=expected):
            _generate(
                _load_generator(plugin_root / "scripts" / "generate_roles.py"), plugin_root, python_binary, codex_binary
            )

    def test_rejects_role_bytes_not_bound_by_manifest(self, installed_inputs: tuple[Path, Path, Path]) -> None:
        """A modified role card is rejected because its bytes no longer match the manifest digest."""
        plugin_root, python_binary, codex_binary = installed_inputs
        role_path = plugin_root / "roles" / "challenger" / "ROLE.md"
        role_path.write_bytes(role_path.read_bytes() + b"\nmodified\n")

        with pytest.raises(ValueError, match=r"package file mismatch: roles/challenger/ROLE.md"):
            _generate(_load_generator(), plugin_root, python_binary, codex_binary)

    @_requires_file_symlinks
    def test_rejects_symlinked_package_input(self, installed_inputs: tuple[Path, Path, Path]) -> None:
        """A role card replaced by a symlink to another card is rejected instead of read as installed bytes."""
        plugin_root, python_binary, codex_binary = installed_inputs
        role_path = plugin_root / "roles" / "challenger" / "ROLE.md"
        role_path.unlink()
        role_path.symlink_to(plugin_root / "roles" / "curator" / "ROLE.md")

        with pytest.raises(ValueError, match="role card: challenger"):
            _generate(_load_generator(), plugin_root, python_binary, codex_binary)

    @pytest.mark.parametrize("hooks", [False, True])
    def test_accepts_exact_manager_profile(self, installed_inputs: tuple[Path, Path, Path], hooks: bool) -> None:
        """The renderer accepts the shim-enabled manager profile with or without hooks, producing all 15 shims.

        The package manifest is switched to the declared manager release profile and the plugin manifest drops its MCP
        server entry, matching what the manager release ships.
        """
        plugin_root, python_binary, codex_binary = installed_inputs
        manifest_path = plugin_root / "package-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["release_profile"] = "shim-enabled"
        manifest["features"] = {"manager": True, "hooks": hooks, "mcp": False, "generated_shims": True}
        plugin = json.loads((plugin_root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        plugin.pop("mcpServers")
        _bind_plugin_json(manifest, plugin_root, json.dumps(plugin).encode("utf-8"))
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        assert len(_generate(_load_generator(), plugin_root, python_binary, codex_binary)) == 15
