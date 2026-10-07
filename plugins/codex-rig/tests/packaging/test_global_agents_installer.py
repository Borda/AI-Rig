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

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
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
    source.write_text("# Global Agent Instructions\nlegacy generic policy\n", encoding="utf-8")
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
    [
        "modified-body",
        "orphan-begin",
        "orphan-end",
        "duplicate",
    ],
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


@pytest.mark.parametrize("managed_state", ["absent", "current", "stale"])
def test_installer_refuses_overlapping_global_policy_without_writes(tmp_path: Path, managed_state: str) -> None:
    """Reject legacy collisions before mutation, including otherwise idempotent updates."""
    namespace = runpy.run_path(str(INSTALLER))
    source = tmp_path / "template.md"
    source.write_bytes(b"# Global Agent Instructions\n\nCurrent policy.\n")
    codex_home = tmp_path / "home"
    codex_home.mkdir()
    target = codex_home / "AGENTS.md"
    original = b"# Global Agent Instructions\n\nPRIVATE old policy.\n\n"
    if managed_state != "absent":
        body = source.read_bytes() if managed_state == "current" else b"# Global Agent Instructions\nOld managed.\n"
        original += namespace["managed_block"](body)
    target.write_bytes(original)

    result = _run_installer(source, codex_home)

    assert result.returncode == 4
    assert target.read_bytes() == original
    assert not (codex_home / "backups").exists()
    assert "overlap" in result.stderr
    assert "PRIVATE" not in result.stderr
    assert "[ok]" not in result.stdout


def _run_mode(source: Path, codex_home: Path, *options: str) -> subprocess.CompletedProcess[str]:
    """Exercise explicit diagnostic or migration options through the public CLI."""
    return subprocess.run(
        [sys.executable, str(INSTALLER), "--source", str(source), "--codex-home", str(codex_home), *options],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("heading_ending", [pytest.param(b"\n", id="lf"), pytest.param(b"\r\n", id="crlf")])
def test_prefix_migration_preserves_suffix_and_full_backup(tmp_path: Path, heading_ending: bytes) -> None:
    """Remove the exact reviewed LF or CRLF prefix and retain custom suffix bytes."""
    namespace = runpy.run_path(str(INSTALLER))
    source = tmp_path / "template.md"
    source.write_bytes(b"# Global Agent Instructions\nCurrent.\n")
    codex_home = tmp_path / "home"
    codex_home.mkdir()
    target = codex_home / "AGENTS.md"
    prefix = b"# Global Agent Instructions" + heading_ending + b"PRIVATE old policy.\r\n\r\n"
    suffix = b"\n# Personal policy\r\nKeep exact bytes.\r\n"
    block = namespace["managed_block"](source.read_bytes())
    original = prefix + block + suffix
    target.write_bytes(original)

    result = _run_mode(source, codex_home, "--migrate-legacy-prefix-sha256", hashlib.sha256(prefix).hexdigest())

    assert result.returncode == 0, result.stderr
    assert target.read_bytes() == block + suffix
    backups = list((codex_home / "backups" / "codex-rig").glob("*-AGENTS.md"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original


@pytest.mark.parametrize("damage", ["digest", "body", "ambiguous-prefix"])
def test_prefix_migration_refuses_unreviewed_bytes(tmp_path: Path, damage: str) -> None:
    """Fail without writes when migration consent or managed integrity is stale."""
    namespace = runpy.run_path(str(INSTALLER))
    source = tmp_path / "template.md"
    source.write_bytes(b"# Global Agent Instructions\nCurrent.\n")
    codex_home = tmp_path / "home"
    codex_home.mkdir()
    prefix = b"# Global Agent Instructions\nLegacy.\n\n"
    digest = hashlib.sha256(prefix).hexdigest()
    block = namespace["managed_block"](source.read_bytes())
    if damage == "digest":
        digest = "0" * 64
    elif damage == "body":
        block = block.replace(b"Current.", b"Modified.")
    else:
        prefix = b"# Personal policy\n" + prefix
    original = prefix + block
    target = codex_home / "AGENTS.md"
    target.write_bytes(original)

    result = _run_mode(source, codex_home, "--migrate-legacy-prefix-sha256", digest)

    assert result.returncode == 4
    assert target.read_bytes() == original
    assert not (codex_home / "backups").exists()


@pytest.mark.parametrize(
    "state", ["absent", "current", "stale", "duplicate", "modified", "orphan-marker", "legacy-skill"]
)
def test_check_is_read_only_and_rejects_degraded_state(tmp_path: Path, state: str) -> None:
    """Diagnose bounded instruction drift without making backups or modifying files."""
    namespace = runpy.run_path(str(INSTALLER))
    source = tmp_path / "template.md"
    source.write_bytes(b"# Global Agent Instructions\nCurrent.\n")
    codex_home = tmp_path / "home"
    block = namespace["managed_block"](source.read_bytes())
    if state != "absent":
        codex_home.mkdir()
        payload = block
        if state == "stale":
            payload = namespace["managed_block"](b"# Global Agent Instructions\nOld.\n")
        elif state == "duplicate":
            payload = b"# Global Agent Instructions\nPRIVATE.\n" + block
        elif state == "modified":
            payload = block.replace(b"Current.", b"Modified.")
        elif state == "orphan-marker":
            payload = block.replace(END_MARKER.encode("ascii"), b"")
        (codex_home / "AGENTS.md").write_bytes(payload)
        if state == "legacy-skill":
            skill = codex_home / "skills" / "develop" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            skill.write_bytes(
                b"---\nname: develop\ndescription: Minimal codex-native develop loop. Use for implementation tasks with linear plan-build-verify flow and measurable quality gates.\n---\n"
            )
    before = {p.relative_to(codex_home): p.read_bytes() for p in codex_home.rglob("*") if p.is_file()}

    result = _run_mode(source, codex_home, "--check")

    assert result.returncode == (0 if state in {"absent", "current"} else 4), result.stderr
    after = {p.relative_to(codex_home): p.read_bytes() for p in codex_home.rglob("*") if p.is_file()}
    assert after == before
    assert not (codex_home / "backups").exists()
    assert "PRIVATE" not in result.stderr
    if state == "absent":
        assert not codex_home.exists()
    if state == "legacy-skill":
        assert "legacy-skill-route" in result.stderr


def test_check_allows_unmanaged_optional_setup_and_custom_skill(tmp_path: Path) -> None:
    """Do not classify absent management or unrelated same-name custom skills as corruption."""
    source = tmp_path / "template.md"
    source.write_bytes(b"# Global Agent Instructions\nCurrent.\n")
    home = tmp_path / "home"
    home.mkdir()
    (home / "AGENTS.md").write_bytes(b"# Personal Policy\nCustom policy.\n")
    skill = home / "skills" / "develop" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_bytes(b"---\nname: develop\ndescription: Custom development workflow.\n---\n")

    result = _run_mode(source, home, "--check")

    assert result.returncode == 0, result.stderr
    assert "bounded instruction check" in result.stdout
    assert not (home / "backups").exists()


def test_check_matches_install_refusal_for_single_unmarked_global_policy(tmp_path: Path) -> None:
    """One unmanaged global heading must not pass preflight when ordinary install refuses overlap."""
    source = tmp_path / "template.md"
    source.write_bytes(b"# Global Agent Instructions\nCurrent managed policy.\n")
    home = tmp_path / "home"
    home.mkdir()
    target = home / "AGENTS.md"
    original = b"# Global Agent Instructions\nCustom unmanaged policy.\n"
    target.write_bytes(original)

    checked = _run_mode(source, home, "--check")
    installed = _run_mode(source, home)

    assert checked.returncode == installed.returncode == 4
    assert "global-agents-overlap" in checked.stderr
    assert "global-agents-overlap" in installed.stderr
    assert target.read_bytes() == original
    assert not (home / "backups").exists()


def test_check_preserves_exact_unmarked_template_adoption(tmp_path: Path) -> None:
    """An exact template copy is adoptable without creating a duplicated policy during preflight."""
    source = tmp_path / "template.md"
    original = b"# Global Agent Instructions\nCurrent managed policy.\n"
    source.write_bytes(original)
    home = tmp_path / "home"
    home.mkdir()
    target = home / "AGENTS.md"
    target.write_bytes(original)

    checked = _run_mode(source, home, "--check")
    assert checked.returncode == 0, checked.stderr
    assert target.read_bytes() == original
    assert not (home / "backups").exists()
    installed = _run_mode(source, home)
    assert installed.returncode == 0
    assert "adopted" in installed.stdout
    assert target.read_bytes().count(b"Current managed policy.") == 1


def test_migration_requires_existing_managed_block_without_creating_home(tmp_path: Path) -> None:
    """Reject removal consent that has no existing reviewed prefix to select."""
    source = tmp_path / "template.md"
    source.write_bytes(b"# Global Agent Instructions\nCurrent.\n")
    home = tmp_path / "absent-home"

    result = _run_mode(source, home, "--migrate-legacy-prefix-sha256", "0" * 64)

    assert result.returncode == 4
    assert not home.exists()


def test_atomic_write_refuses_same_byte_target_replacement(tmp_path: Path) -> None:
    """Reject replacement identity drift even when the target still has the same bytes."""
    namespace = runpy.run_path(str(INSTALLER))
    target = tmp_path / "AGENTS.md"
    payload = b"observed\n"
    target.write_bytes(payload)
    identity = target.stat()
    replacement = tmp_path / "replacement.md"
    replacement.write_bytes(payload)
    replacement.replace(target)

    with pytest.raises(namespace["UnsafeGlobalAgentsState"], match="replaced"):
        namespace["atomic_write"](target, b"desired\n", 0o600, payload, identity)

    assert target.read_bytes() == payload
    assert list(tmp_path.glob(".AGENTS.md.codex-rig-*")) == []


def test_migration_refuses_concurrent_replacement_before_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preserve a concurrently replaced target without creating migration backups."""
    namespace = runpy.run_path(str(INSTALLER))
    source = tmp_path / "template.md"
    source.write_bytes(b"# Global Agent Instructions\nCurrent.\n")
    home = tmp_path / "home"
    home.mkdir()
    prefix = b"# Global Agent Instructions\nLegacy.\n\n"
    original = prefix + namespace["managed_block"](source.read_bytes())
    target = home / "AGENTS.md"
    target.write_bytes(original)
    replacement = home / "concurrent.md"
    replacement.write_bytes(original)
    migrate = namespace["migrated_prefix_payload"]

    def replace_after_review(existing: bytes, block: bytes, digest: str) -> bytes:
        """Simulate replacement after migration validation but before backup creation."""
        desired = migrate(existing, block, digest)
        replacement.replace(target)
        return desired

    monkeypatch.setitem(namespace["install_global_agents"].__globals__, "migrated_prefix_payload", replace_after_review)

    with pytest.raises(namespace["UnsafeGlobalAgentsState"], match="changed before backup"):
        namespace["install_global_agents"](source, home, hashlib.sha256(prefix).hexdigest())

    assert target.read_bytes() == original
    assert not (home / "backups").exists()
