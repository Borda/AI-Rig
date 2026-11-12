"""Prove package closure, source imports, and bounded role settings at script boundaries."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from _modules import load_module_scoped
from _platform import POSIX_DESCRIPTOR_PRIMITIVES_AVAILABLE

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
_requires_posix = pytest.mark.skipif(
    not POSIX_DESCRIPTOR_PRIMITIVES_AVAILABLE, reason="host lacks descriptor-relative no-follow reads"
)


@pytest.mark.parametrize("relative", ["scripts/.reports/payload", "scripts/.pytest_cache/payload"])
def test_inventory_cannot_hide_nested_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: str) -> None:
    """Only root report and pytest metadata can be excluded from closure verification."""
    monkeypatch.syspath_prepend(str(SCRIPTS))
    identity = load_module_scoped("s10_inventory_identity", SCRIPTS / "_package_identity.py", monkeypatch)
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    target.write_bytes(b"unrecorded")

    assert identity.inventory_package_files(
        tmp_path, excluded_parts=identity.EXCLUDED_PARTS, excluded_files=identity.EXCLUDED_FILES
    ) == (relative,)


@_requires_posix
@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="host lacks FIFO creation")
def test_generator_fifo_fails_without_waiting(tmp_path: Path) -> None:
    """Role generation rejects FIFO inputs before an absent writer can block the read."""
    os.mkfifo(tmp_path / "package-manifest.json")
    code = (
        "import os,sys;sys.path.insert(0,sys.argv[1]);import generate_roles as g;"
        "fd=os.open(sys.argv[2],os.O_RDONLY|os.O_DIRECTORY);"
        "g._contained_file(fd,'package-manifest.json',1024,'package manifest')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(SCRIPTS), str(tmp_path)],
        capture_output=True,
        check=False,
        timeout=2,
    )
    assert result.returncode != 0
    assert b"unsafe package manifest" in result.stderr


def _role_fixture(root: Path, settings: dict[str, str]) -> tuple[int, dict, dict]:
    """Write independently hashed role/helper inputs for the real manifest-role reader."""
    helper = root / "scripts" / "verify_role_link.py"
    card = root / "roles" / "challenger" / "ROLE.md"
    helper.parent.mkdir()
    card.parent.mkdir(parents=True)
    helper.write_bytes(b"helper")
    frontmatter = {
        "role_id": "challenger",
        "name": "codex-rig-challenger",
        **settings,
        "fallback_modes": "[shim, built-in-injected, inline]",
    }
    card.write_text(
        "---\n" + "\n".join(f"{key}: {value}" for key, value in frontmatter.items()) + "\n---\nrole body\n",
        encoding="utf-8",
        newline="\n",
    )
    records = {
        path.relative_to(root).as_posix(): {
            "path": path.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "mode": f"{path.stat().st_mode & 0o777:04o}",
        }
        for path in (helper, card)
    }
    manifest = {
        "schema": 2,
        "bootstrap": {
            "protocol": 1,
            "helper": "scripts/verify_role_link.py",
            "sha256": records["scripts/verify_role_link.py"]["sha256"],
        },
        "roles": [
            {
                "id": "challenger",
                "path": "roles/challenger/ROLE.md",
                "sha256": records["roles/challenger/ROLE.md"]["sha256"],
                "runtime": settings,
            }
        ],
    }
    return os.open(root, os.O_RDONLY | os.O_DIRECTORY), manifest, records


@_requires_posix
@pytest.mark.parametrize(
    ("key", "value"),
    [
        pytest.param("sandbox_mode", "danger-full-access", id="unbounded-sandbox"),
        pytest.param("approval_policy", "never", id="disabled-approval"),
        pytest.param("model_reasoning_effort", "invented", id="unknown-effort"),
    ],
)
def test_role_runtime_rejects_unapproved_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    """Matching card and manifest text cannot authorize unsafe runtime settings."""
    generator = load_module_scoped("s10_runtime_generator", SCRIPTS / "generate_roles.py", monkeypatch)
    monkeypatch.setattr(generator, "ROLE_IDS", ("challenger",))
    settings = {
        "model_reasoning_effort": "high",
        "approval_policy": "on-request",
        "sandbox_mode": "read-only",
        key: value,
    }
    descriptor, manifest, records = _role_fixture(tmp_path, settings)
    try:
        with pytest.raises(ValueError, match=r"runtime.*value"):
            generator._validated_roles(descriptor, manifest, records)
    finally:
        os.close(descriptor)


@pytest.mark.parametrize(
    ("entrypoint", "dependency"),
    [
        pytest.param("build_package.py", "_package_identity", id="builder-identity"),
        pytest.param("build_package.py", "_safe_package_io", id="builder-safe-io"),
        pytest.param("validate_package.py", "_package_identity", id="validator-identity"),
        pytest.param("validate_package.py", "_safe_package_io", id="validator-safe-io"),
        pytest.param("manage_role_agents.py", "_agent_shim_lifecycle", id="manager-lifecycle"),
        pytest.param("sync_codex.py", "install_global_agents", id="sync-global-installer"),
    ],
)
def test_entrypoint_ignores_planted_dependency_bytecode(tmp_path: Path, entrypoint: str, dependency: str) -> None:
    """A valid unchecked-hash cache cannot execute before source package checks."""
    import py_compile
    import shutil

    copied = tmp_path / "scripts"
    shutil.copytree(SCRIPTS, copied, ignore=shutil.ignore_patterns("__pycache__"))
    marker = tmp_path / "executed"
    malicious = tmp_path / "malicious.py"
    malicious.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\nraise RuntimeError('cache executed')\n"
    )
    cache_name = Path(__import__("importlib.util").util.cache_from_source(str(copied / f"{dependency}.py"))).name
    cache = copied / "__pycache__" / cache_name
    cache.parent.mkdir(exist_ok=True)
    py_compile.compile(
        str(malicious), cfile=str(cache), invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH, doraise=True
    )
    result = subprocess.run(
        [sys.executable, str(copied / entrypoint), "--help"], capture_output=True, check=False, timeout=5
    )

    assert result.returncode == 0, result.stderr
    assert not marker.exists()

    cached = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            "import importlib.util,sys; s=importlib.util.spec_from_file_location('cached',sys.argv[1]);"
            "s.loader.exec_module(importlib.util.module_from_spec(s))",
            str(copied / f"{dependency}.py"),
        ],
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert cached.returncode != 0
    assert b"cache executed" in cached.stderr
    assert marker.read_text() == "executed"


def test_approval_preview_discloses_exact_shim_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Approval reveals the content and hash that bind the role's runtime settings."""
    from types import SimpleNamespace

    monkeypatch.syspath_prepend(str(SCRIPTS))
    manager = load_module_scoped("s10_approval_manager", SCRIPTS / "manage_role_agents.py", monkeypatch)
    shim = b'approval_policy = "on-request"\nsandbox_mode = "workspace-write"\n'
    digest = hashlib.sha256(shim).hexdigest()
    role = SimpleNamespace(role_id="challenger", file_hash=digest, shim_bytes=shim)
    operation = SimpleNamespace(role_id="challenger", intent="create")
    plan = SimpleNamespace(
        approval=SimpleNamespace(digest="a" * 64),
        candidate=SimpleNamespace(operations=(operation,)),
        roster=SimpleNamespace(roles=(role,)),
        codex_home=tmp_path,
        plugin_root=tmp_path,
    )
    monkeypatch.setattr(manager.sys, "platform", "darwin")
    monkeypatch.setattr(manager, "plan_recovery", lambda **kwargs: None)
    monkeypatch.setattr(manager, "plan_mutation", lambda **kwargs: plan)
    monkeypatch.setattr("builtins.input", lambda prompt: "")

    assert manager.main(["remove"]) == 3

    preview = json.loads(capsys.readouterr().out.splitlines()[0])
    assert preview["operations"][0]["shim_sha256"] == digest
    assert preview["operations"][0]["shim"] == shim.decode("utf-8")
