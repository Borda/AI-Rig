"""Acceptance checks for Codex Rig managed global-instruction installation."""

from __future__ import annotations

import hashlib
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

from _platform import FILE_SYMLINKS_AVAILABLE


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = PLUGIN_ROOT / "scripts" / "install_global_agents.py"
TEMPLATE = PLUGIN_ROOT / "assets" / "AGENTS.md"
BEGIN_PREFIX = "<!-- codex-rig:global-agents begin sha256="
END_MARKER = "<!-- codex-rig:global-agents end -->"


def _run_installer(
    source: Path, codex_home: Path, *, output_encoding: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the packaged installer against one isolated Codex home."""
    env = os.environ.copy()
    if output_encoding is not None:
        env["PYTHONIOENCODING"] = output_encoding
    return subprocess.run(
        [sys.executable, str(INSTALLER), "--source", str(source), "--codex-home", str(codex_home)],
        capture_output=True,
        env=env,
        text=True,
        check=False,
    )


def _run_remover(codex_home: Path) -> subprocess.CompletedProcess[str]:
    """Run the packaged installer in ``--remove`` mode against one isolated Codex home."""
    return subprocess.run(
        [sys.executable, str(INSTALLER), "--remove", "--codex-home", str(codex_home)],
        capture_output=True,
        text=True,
        check=False,
    )


def _managed_body(payload: bytes) -> tuple[str, bytes]:
    """Return the recorded digest and exact body bytes from one managed block.

    Example:
        >>> payload = (
        ...     b"<!-- codex-rig:global-agents begin sha256=digest -->\\nbody\\n"
        ...     b"<!-- codex-rig:global-agents end -->"
        ... )
        >>> _managed_body(payload)[1]
        b'body\\n'
    """
    begin_prefix = BEGIN_PREFIX.encode("ascii")
    end_marker = END_MARKER.encode("ascii")
    begin = payload.index(begin_prefix)
    marker_end = payload.index(b" -->", begin)
    digest = payload[begin + len(begin_prefix) : marker_end].decode("ascii")
    body_start = marker_end + len(b" -->\n")
    body_end = payload.index(end_marker, body_start)
    return digest, payload[body_start:body_end]


def test_global_agents_template_is_packaged_not_repository_policy() -> None:
    """Prevent generic global guidance from becoming repository-root policy."""
    assert TEMPLATE.is_file()
    assert TEMPLATE.read_text(encoding="utf-8").startswith("# Global Agent Instructions\n")
    # The repository ships its own root AGENTS.md (repository-scoped policy). What must never happen is the
    # generic packaged template being copied into that slot, so compare content rather than assert absence.
    repository_agents = PLUGIN_ROOT.parents[1] / "AGENTS.md"
    assert not repository_agents.exists() or repository_agents.read_bytes() != TEMPLATE.read_bytes()

    manifest = json.loads((PLUGIN_ROOT / "package-manifest.json").read_text(encoding="utf-8"))
    record = next(item for item in manifest["files"] if item["path"] == "assets/AGENTS.md")
    assert record == {
        "mode": "0644",
        "path": "assets/AGENTS.md",
        "sha256": hashlib.sha256(TEMPLATE.read_bytes()).hexdigest(),
    }


def test_global_agents_template_rejects_hypothetical_complexity() -> None:
    """Keep future-only scenarios from justifying current machinery."""
    policy = TEMPLATE.read_text(encoding="utf-8")

    assert "Hypothetical future states, risks, scale, reuse, or edge cases do not justify machinery" in policy
    assert "verified current evidence proves the simpler solution insufficient" in policy
    assert "Prefer maintained standard-library, native-platform, and already-installed package functionality" in policy
    assert "over custom code that duplicates it" in policy
    assert "Simplicity never removes trust-boundary validation" in policy
    assert "record the ceiling and observable trigger for revisiting it" in policy


def test_global_agents_installer_creates_managed_file_when_absent(tmp_path: Path) -> None:
    """Prove explicit installation creates one authenticated managed block."""
    source = tmp_path / "template.md"
    source.write_bytes(b"# Generic policy\r\n\r\nKeep this.\r\n")
    codex_home = tmp_path / "codex-home"

    result = _run_installer(source, codex_home, output_encoding="cp1252")

    assert result.returncode == 0, result.stderr
    target = codex_home / "AGENTS.md"
    payload = target.read_bytes()
    digest, body = _managed_body(payload)
    assert body == source.read_bytes()
    assert digest == hashlib.sha256(body).hexdigest()
    assert payload.count(BEGIN_PREFIX.encode("ascii")) == 1
    assert payload.count(END_MARKER.encode("ascii")) == 1
    assert "created" in result.stdout
    assert result.stdout.isascii()


def test_global_agents_installer_preserves_existing_content_and_is_idempotent(tmp_path: Path) -> None:
    """Prevent merge or repeated installation from damaging user-owned guidance."""
    source = tmp_path / "template.md"
    source.write_text("# Generic policy\n", encoding="utf-8")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    target = codex_home / "AGENTS.md"
    original = "# User policy\n\nKeep this exactly.\n"
    target.write_text(original, encoding="utf-8")

    first = _run_installer(source, codex_home)
    first_payload = target.read_bytes()
    backups = list((codex_home / "backups" / "codex-rig").glob("*-AGENTS.md"))
    second = _run_installer(source, codex_home)

    assert first.returncode == 0, first.stderr
    assert target.read_text(encoding="utf-8").startswith(original)
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original
    assert second.returncode == 0, second.stderr
    assert target.read_bytes() == first_payload
    assert list((codex_home / "backups" / "codex-rig").glob("*-AGENTS.md")) == backups
    assert "already current" in second.stdout


def test_global_agents_installer_updates_authenticated_block(tmp_path: Path) -> None:
    """Allow plugin upgrades to replace only an unmodified managed block."""
    source = tmp_path / "template.md"
    source.write_bytes(b"first policy\r\n")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    target = codex_home / "AGENTS.md"
    target.write_bytes(b"user prefix\r\n")
    user_prefix = target.read_bytes()
    assert _run_installer(source, codex_home).returncode == 0

    source.write_bytes(b"second policy\r\n")
    result = _run_installer(source, codex_home)

    assert result.returncode == 0, result.stderr
    payload = target.read_bytes()
    digest, body = _managed_body(payload)
    assert payload.startswith(user_prefix)
    assert body == source.read_bytes()
    assert digest == hashlib.sha256(body).hexdigest()
    assert payload.count(BEGIN_PREFIX.encode("ascii")) == 1
    assert "updated" in result.stdout


def test_global_agents_installer_adopts_exact_legacy_copy(tmp_path: Path) -> None:
    """Prevent an old unmarked full-template copy from being duplicated during migration."""
    source = tmp_path / "template.md"
    source.write_text("legacy generic policy\n", encoding="utf-8")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    target = codex_home / "AGENTS.md"
    target.write_bytes(source.read_bytes())

    result = _run_installer(source, codex_home)

    assert result.returncode == 0, result.stderr
    payload = target.read_bytes()
    _, body = _managed_body(payload)
    assert body == source.read_bytes()
    assert payload.count(b"legacy generic policy") == 1
    assert "adopted" in result.stdout
    backups = list((codex_home / "backups" / "codex-rig").glob("*-AGENTS.md"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == source.read_bytes()


@pytest.mark.parametrize(
    "damage",
    (
        "modified-body",
        "orphan-begin",
        "orphan-end",
        "duplicate",
    ),
)
def test_global_agents_installer_refuses_untrusted_managed_state(tmp_path: Path, damage: str) -> None:
    """Fail without writes when managed ownership evidence is ambiguous or changed."""
    source = tmp_path / "template.md"
    source.write_text("managed policy\n", encoding="utf-8")
    codex_home = tmp_path / "codex-home"
    assert _run_installer(source, codex_home).returncode == 0
    target = codex_home / "AGENTS.md"
    payload = target.read_text(encoding="utf-8")
    if damage == "modified-body":
        payload = payload.replace("managed policy", "manually changed policy")
    elif damage == "orphan-begin":
        payload = payload.replace(END_MARKER, "")
    elif damage == "orphan-end":
        payload = payload[payload.index(END_MARKER) :]
    else:
        payload += payload
    target.write_text(payload, encoding="utf-8")
    before = target.read_bytes()

    result = _run_installer(source, codex_home)

    assert result.returncode == 4
    assert target.read_bytes() == before
    assert "refusing" in result.stderr.lower()


@pytest.mark.skipif(not FILE_SYMLINKS_AVAILABLE, reason="host cannot create file symlinks")
def test_global_agents_installer_refuses_symlink_target(tmp_path: Path) -> None:
    """Prevent optional installation from following a target outside Codex home."""
    source = tmp_path / "template.md"
    source.write_text("managed policy\n", encoding="utf-8")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside\n", encoding="utf-8")
    (codex_home / "AGENTS.md").symlink_to(outside)

    result = _run_installer(source, codex_home)

    assert result.returncode == 4
    assert outside.read_text(encoding="utf-8") == "outside\n"
    assert "symlink" in result.stderr.lower()


def test_atomic_write_refuses_target_drift_before_replace(tmp_path: Path) -> None:
    """Prevent a concurrent target edit observed before replacement from being overwritten."""
    namespace = runpy.run_path(str(INSTALLER))
    target = tmp_path / "AGENTS.md"
    expected = b"observed\n"
    target.write_bytes(b"concurrent edit\n")

    with pytest.raises(namespace["UnsafeGlobalAgentsState"]):
        namespace["atomic_write"](target, b"replacement\n", 0o600, expected)

    assert target.read_bytes() == b"concurrent edit\n"


def test_remove_deletes_file_that_held_only_managed_block(tmp_path: Path) -> None:
    """Prove teardown deletes an AGENTS.md that Codex Rig alone created."""
    source = tmp_path / "template.md"
    source.write_text("managed policy\n", encoding="utf-8")
    codex_home = tmp_path / "codex-home"
    assert _run_installer(source, codex_home).returncode == 0
    target = codex_home / "AGENTS.md"
    original = target.read_bytes()

    result = _run_remover(codex_home)

    assert result.returncode == 0, result.stderr
    assert not target.exists()
    assert "removed-file" in result.stdout
    backups = list((codex_home / "backups" / "codex-rig").glob("*-AGENTS.md"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original


def test_remove_strips_block_and_preserves_user_content(tmp_path: Path) -> None:
    """Prove teardown removes only the managed block, keeping user-owned guidance."""
    source = tmp_path / "template.md"
    source.write_text("managed policy\n", encoding="utf-8")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    target = codex_home / "AGENTS.md"
    user_content = "# User policy\n\nKeep this exactly.\n"
    target.write_text(user_content, encoding="utf-8")
    assert _run_installer(source, codex_home).returncode == 0

    result = _run_remover(codex_home)

    assert result.returncode == 0, result.stderr
    assert target.read_text(encoding="utf-8") == user_content
    assert BEGIN_PREFIX not in target.read_text(encoding="utf-8")
    assert "removed-block" in result.stdout


def test_remove_is_noop_when_no_managed_block_present(tmp_path: Path) -> None:
    """Prove teardown leaves an unmanaged AGENTS.md untouched and reports absent."""
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    target = codex_home / "AGENTS.md"
    target.write_text("# only user content\n", encoding="utf-8")
    before = target.read_bytes()

    result = _run_remover(codex_home)

    assert result.returncode == 0, result.stderr
    assert target.read_bytes() == before
    assert "absent" in result.stdout


def test_remove_refuses_modified_managed_block(tmp_path: Path) -> None:
    """Fail without writes when the managed block was tampered with before teardown."""
    source = tmp_path / "template.md"
    source.write_text("managed policy\n", encoding="utf-8")
    codex_home = tmp_path / "codex-home"
    assert _run_installer(source, codex_home).returncode == 0
    target = codex_home / "AGENTS.md"
    target.write_text(
        target.read_text(encoding="utf-8").replace("managed policy", "manually changed"), encoding="utf-8"
    )
    before = target.read_bytes()

    result = _run_remover(codex_home)

    assert result.returncode == 4
    assert target.read_bytes() == before
    assert "refusing" in result.stderr.lower()


def test_remove_requires_no_source_argument(tmp_path: Path) -> None:
    """Prove ``--remove`` needs only ``--codex-home`` while install still requires ``--source``."""
    missing_source = subprocess.run(
        [sys.executable, str(INSTALLER), "--codex-home", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert missing_source.returncode == 2
    assert "--source is required unless --remove" in missing_source.stderr
