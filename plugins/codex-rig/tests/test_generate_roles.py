"""Acceptance checks for deterministic thin-role shim generation."""

from __future__ import annotations

import hashlib
import doctest
import importlib.util
import json
import shutil
import sys
import tempfile  # noqa: F401 - used by executable doctest examples
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib


WINDOWS_POSIX_SKIP_REASON = "requires POSIX filesystem modes, links, and executable semantics"
POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason=WINDOWS_POSIX_SKIP_REASON)
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
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


def _load_generator(path: Path = GENERATOR_SCRIPT) -> ModuleType:
    """Load the generator directly from its installed-package path."""
    specification = importlib.util.spec_from_file_location("codex_rig_generate_roles", path)
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def _digest(path: Path) -> str:
    """Return the lowercase SHA-256 digest for one fixture file.

    Example:
        >>> len(_digest(PLUGIN_ROOT / "package-manifest.json"))
        64
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


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


def test_executable_fixture_doctest_without_execute_permissions() -> None:
    """Keep the fixture example valid when chmod cannot grant POSIX execute permissions."""
    example = doctest.DocTestParser().get_doctest(
        _write_executable.__doc__, globals(), "_write_executable", __file__, 0
    )
    runner = doctest.DocTestRunner()
    # Windows chmod ignores execute bits; leave the real temporary file creation and byte writes intact.
    with patch.object(Path, "chmod", autospec=True) as chmod:
        result = runner.run(example)
    assert result.attempted == 1
    assert result.failed == 0
    chmod.assert_called_once()
    assert chmod.call_args.args[1] == 0o755


def _installed_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create source-independent package and executable inputs with difficult paths."""
    plugin_root = tmp_path / 'installed plugin žluťoučký "quoted" \\slash $;[]'
    shutil.copytree(PLUGIN_ROOT, plugin_root, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    python_binary = _write_executable(tmp_path / 'python λ "quoted" \\slash $;[]', b"python-runtime\n")
    codex_binary = _write_executable(tmp_path / 'codex 雪 "quoted" \\slash $;[]', b"codex-runtime\n")
    return plugin_root, python_binary, codex_binary


def _generate(module: ModuleType, plugin_root: Path, python_binary: Path, codex_binary: Path) -> dict[str, bytes]:
    """Call the public generator with hashes bound to the fixture bytes."""
    return module.generate_role_shims(
        plugin_root,
        install_id=INSTALL_ID,
        python_executable=python_binary,
        python_executable_hash=_digest(python_binary),
        codex_binary=codex_binary,
        codex_binary_hash=_digest(codex_binary),
    )


def test_roster_identity_hash_is_canonical_and_rejects_drift() -> None:
    """Keep one generator-owned roster preimage for state and plan consumers."""
    module = _load_generator()
    rows = tuple((role_id, f"codex-rig-{role_id}.toml", f"roles/{role_id}/ROLE.md", "a" * 64) for role_id in ROLE_IDS)
    value = [{"role_id": row[0], "target_name": row[1], "card_path": row[2], "role_hash": row[3]} for row in rows]
    expected = hashlib.sha256(
        json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    assert module.roster_identity_hash(rows) == expected
    for invalid in (rows[:-1], (rows[1], rows[0], *rows[2:]), (*rows[:-1], (*rows[-1][:-1], "A" * 64))):
        with pytest.raises(ValueError):
            module.roster_identity_hash(invalid)


def _expected_challenger_bytes(
    plugin_root: Path,
    python_binary: Path,
    codex_binary: Path,
    manifest: dict[str, object],
) -> bytes:
    """Build one complete independent golden shim from literal contract text."""
    role = next(item for item in manifest["roles"] if item["id"] == "challenger")
    manifest_hash = _digest(plugin_root / "package-manifest.json")
    argv = [
        str(python_binary),
        str(plugin_root / "scripts" / "verify_role_link.py"),
        "--plugin-root",
        str(plugin_root),
        "--role",
        "challenger",
        "--role-sha256",
        role["sha256"],
        "--manifest-sha256",
        manifest_hash,
        "--helper-sha256",
        manifest["bootstrap"]["sha256"],
        "--codex-binary",
        str(codex_binary),
        "--codex-sha256",
        _digest(codex_binary),
    ]
    argv_json = json.dumps(argv, ensure_ascii=True, separators=(",", ":"))
    escaped_argv = argv_json.replace("\\", "\\\\").replace('"', '\\"')
    text = f'''# codex-rig-shim schema=1 plugin=codex-rig install_id={INSTALL_ID} role_id=challenger package_hash=sha256:{manifest_hash} role_hash=sha256:{role["sha256"]} bootstrap=1 generator=1
name = "codex-rig-challenger"
description = "Thin linked Codex Rig challenger role; unavailable unless the current installed plugin verifies."
model = "gpt-5.6-terra"
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


@POSIX_ONLY
def test_generation_is_exact_deterministic_and_round_trips_argv(tmp_path: Path) -> None:
    """Freeze all role bytes and preserve difficult paths through TOML and JSON."""
    plugin_root, python_binary, codex_binary = _installed_inputs(tmp_path)
    module = _load_generator(plugin_root / "scripts" / "generate_roles.py")
    manifest_path = plugin_root / "package-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    role_records = {item["id"]: item for item in manifest["roles"]}

    first = _generate(module, plugin_root, python_binary, codex_binary)
    second = _generate(module, plugin_root, python_binary, codex_binary)

    assert first == second
    assert Path(module.__file__).parent.parent == plugin_root
    assert first["codex-rig-challenger.toml"] == _expected_challenger_bytes(
        plugin_root,
        python_binary,
        codex_binary,
        manifest,
    )
    assert list(first) == [f"codex-rig-{role_id}.toml" for role_id in ROLE_IDS]
    for role_id, payload in zip(ROLE_IDS, first.values(), strict=True):
        parsed = tomllib.loads(payload.decode("utf-8"))
        instructions = parsed["developer_instructions"]
        argv_line = next(line for line in instructions.splitlines() if line.startswith("["))
        expected_argv = [
            str(python_binary),
            str(plugin_root / "scripts" / "verify_role_link.py"),
            "--plugin-root",
            str(plugin_root),
            "--role",
            role_id,
            "--role-sha256",
            role_records[role_id]["sha256"],
            "--manifest-sha256",
            _digest(manifest_path),
            "--helper-sha256",
            manifest["bootstrap"]["sha256"],
            "--codex-binary",
            str(codex_binary),
            "--codex-sha256",
            _digest(codex_binary),
        ]

        assert json.loads(argv_line) == expected_argv
        assert parsed["name"] == f"codex-rig-{role_id}"
        assert {key: parsed[key] for key in role_records[role_id]["runtime"]} == role_records[role_id]["runtime"]
        assert payload.startswith(
            (
                "# codex-rig-shim schema=1 plugin=codex-rig "
                f"install_id={INSTALL_ID} role_id={role_id} package_hash=sha256:{_digest(manifest_path)} "
                f"role_hash=sha256:{role_records[role_id]['sha256']} bootstrap=1 generator=1\n"
            ).encode()
        )
        assert payload.endswith(b"\n")
        assert b"\r" not in payload
        assert b"fallback_modes:" not in payload
        assert b"## Trigger and skip boundaries" not in payload


@POSIX_ONLY
def test_generated_roster_exposes_immutable_manager_identities(tmp_path: Path) -> None:
    """Prevent the lifecycle manager from independently reconstructing package metadata."""
    plugin_root, python_binary, codex_binary = _installed_inputs(tmp_path)
    module = _load_generator(plugin_root / "scripts" / "generate_roles.py")
    manifest = json.loads((plugin_root / "package-manifest.json").read_text(encoding="utf-8"))

    roster = module.load_generated_roster(
        plugin_root,
        install_id=INSTALL_ID,
        python_executable=python_binary,
        python_executable_hash=_digest(python_binary),
        codex_binary=codex_binary,
        codex_binary_hash=_digest(codex_binary),
    )

    assert roster.plugin_version == manifest["version"]
    assert roster.package_hash == _digest(plugin_root / "package-manifest.json")
    assert roster.bootstrap_hash == manifest["bootstrap"]["sha256"]
    assert roster.generator_version == manifest["generator"]["version"] == 1
    assert tuple(role.role_id for role in roster.roles) == ROLE_IDS
    for role in roster.roles:
        assert role.target_name == f"codex-rig-{role.role_id}.toml"
        assert role.card_path == f"roles/{role.role_id}/ROLE.md"
        assert role.role_hash == next(item["sha256"] for item in manifest["roles"] if item["id"] == role.role_id)
        assert role.file_hash == hashlib.sha256(role.shim_bytes).hexdigest()
    with pytest.raises(FrozenInstanceError):
        roster.plugin_version = "changed"


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        pytest.param("package-version", "plugin manifest identity mismatch", id="package-version"),
        pytest.param("plugin-version", "plugin manifest identity mismatch", id="plugin-version"),
        pytest.param("invalid-version", "package manifest profile mismatch", id="invalid-version"),
        pytest.param("plugin-extra-field", "plugin manifest fields mismatch", id="plugin-extra-field"),
    ],
)
@POSIX_ONLY
def test_generated_roster_rejects_inconsistent_plugin_identity(
    tmp_path: Path,
    mutation: str,
    expected: str,
) -> None:
    """Bind manager-ready metadata to both installed manifest authorities."""
    plugin_root, python_binary, codex_binary = _installed_inputs(tmp_path)
    module = _load_generator(plugin_root / "scripts" / "generate_roles.py")
    package_path = plugin_root / "package-manifest.json"
    plugin_path = plugin_root / ".codex-plugin" / "plugin.json"
    package = json.loads(package_path.read_text(encoding="utf-8"))
    plugin = json.loads(plugin_path.read_text(encoding="utf-8"))
    if mutation == "package-version":
        package["version"] = "9.9.9"
    elif mutation == "plugin-version":
        plugin["version"] = "9.9.9"
    elif mutation == "invalid-version":
        package["version"] = "not-semver"
        plugin["version"] = "not-semver"
    else:
        plugin["unexpected"] = True
    if mutation != "package-version":
        plugin_bytes = (json.dumps(plugin, indent=2, ensure_ascii=True) + "\n").encode()
        plugin_path.write_bytes(plugin_bytes)
        plugin_record = next(item for item in package["files"] if item["path"] == ".codex-plugin/plugin.json")
        plugin_record["sha256"] = hashlib.sha256(plugin_bytes).hexdigest()
    package_path.write_text(json.dumps(package), encoding="utf-8")

    with pytest.raises(ValueError, match=expected):
        _generate(module, plugin_root, python_binary, codex_binary)


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
@POSIX_ONLY
def test_generation_rejects_bad_identity_inputs(
    tmp_path: Path,
    install_id: str,
    python_hash: str | None,
    python_path: Path | None,
    expected: str,
) -> None:
    """Reject identities that cannot safely bind exact verifier arguments."""
    module = _load_generator()
    plugin_root, python_binary, codex_binary = _installed_inputs(tmp_path)

    with pytest.raises(ValueError, match=expected):
        module.generate_role_shims(
            plugin_root,
            install_id=install_id,
            python_executable=python_path or python_binary,
            python_executable_hash=python_hash or _digest(python_binary),
            codex_binary=codex_binary,
            codex_binary_hash=_digest(codex_binary),
        )


@POSIX_ONLY
def test_generation_rejects_role_bytes_not_bound_by_manifest(tmp_path: Path) -> None:
    """Prevent modified or linked role cards from entering generated shims."""
    module = _load_generator()
    plugin_root, python_binary, codex_binary = _installed_inputs(tmp_path)
    role_path = plugin_root / "roles" / "challenger" / "ROLE.md"
    role_path.write_bytes(role_path.read_bytes() + b"\nmodified\n")

    with pytest.raises(ValueError, match="package file mismatch: roles/challenger/ROLE.md"):
        _generate(module, plugin_root, python_binary, codex_binary)


@POSIX_ONLY
def test_generation_rejects_symlinked_package_input(tmp_path: Path) -> None:
    """Prevent aliased package files from being treated as installed bytes."""
    module = _load_generator()
    plugin_root, python_binary, codex_binary = _installed_inputs(tmp_path)
    role_path = plugin_root / "roles" / "challenger" / "ROLE.md"
    role_path.unlink()
    role_path.symlink_to(plugin_root / "roles" / "curator" / "ROLE.md")

    with pytest.raises(ValueError, match="role card: challenger"):
        _generate(module, plugin_root, python_binary, codex_binary)


@POSIX_ONLY
def test_generation_rejects_unresolved_parent_alias(tmp_path: Path) -> None:
    """Prevent unresolved dot-dot aliases from entering verifier arguments."""
    module = _load_generator()
    plugin_root, python_binary, codex_binary = _installed_inputs(tmp_path)
    aliased_root = plugin_root / "scripts" / ".."

    with pytest.raises(ValueError, match="non-canonical plugin root"):
        _generate(module, aliased_root, python_binary, codex_binary)


@POSIX_ONLY
def test_generation_is_read_only_for_installed_inputs(tmp_path: Path) -> None:
    """Prevent the pure generator from changing its package or executable inputs."""
    plugin_root, python_binary, codex_binary = _installed_inputs(tmp_path)
    module = _load_generator(plugin_root / "scripts" / "generate_roles.py")

    def _snapshot() -> dict[str, tuple[bytes, int, int]]:
        """Capture bytes, modes, and mtimes for the read-only generation assertion."""
        return {
            path.relative_to(tmp_path).as_posix(): (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
            for path in sorted(tmp_path.rglob("*"))
            if path.is_file()
        }

    before = _snapshot()
    _generate(module, plugin_root, python_binary, codex_binary)

    assert _snapshot() == before


@pytest.mark.parametrize("hooks", [False, True])
@POSIX_ONLY
def test_generation_accepts_exact_manager_profile(tmp_path: Path, hooks: bool) -> None:
    """Keep the pure renderer usable by the declared manager release."""
    module = _load_generator()
    plugin_root, python_binary, codex_binary = _installed_inputs(tmp_path)
    manifest_path = plugin_root / "package-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["release_profile"] = "shim-enabled"
    manifest["features"] = {"manager": True, "hooks": hooks, "mcp": False, "generated_shims": True}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert len(_generate(module, plugin_root, python_binary, codex_binary)) == 15
