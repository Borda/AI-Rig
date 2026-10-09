"""Acceptance checks for Codex Rig managed global-instruction installation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest
from _platform import DIRECTORY_SYMLINKS_AVAILABLE, FILE_SYMLINKS_AVAILABLE

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


PLACEHOLDER = "{{CODEX_RIG_PLUGIN_ROOT}}"
#: Installed-root line the packaged template renders; group 1 is the absolute root written by the installer.
ROOT_LINE = re.compile(r"^`PLUGIN_ROOT` = `([^`]+)`", re.MULTILINE)
#: Every backticked Codex Rig shared-file pointer the global template names.
SHARED_POINTER = re.compile(r"`(shared/[\w.-]+\.(?:md|py))`")


def _installed_cache(codex_home: Path, version: str) -> Path:
    """Lay out the real template and shared docs where Codex installs Codex Rig under one isolated home."""
    cache = codex_home / "plugins" / "cache" / "borda-ai-rig" / "codex-rig" / version
    shutil.copytree(PLUGIN_ROOT / "shared", cache / "shared", ignore=shutil.ignore_patterns("__pycache__"))
    (cache / "assets").mkdir()
    shutil.copy2(TEMPLATE, cache / "assets" / "AGENTS.md")
    return cache


def _marketplace_template(tmp_path: Path) -> Path:
    """Copy the real template to a marketplace-checkout location that is not the installed cache."""
    source = tmp_path / "marketplace" / "plugins" / "codex-rig" / "assets" / "AGENTS.md"
    source.parent.mkdir(parents=True)
    shutil.copy2(TEMPLATE, source)
    return source


@pytest.mark.packaging
@pytest.mark.parametrize("root_selection", ["derived-from-installed-source", "explicit-plugin-root"])
def test_installed_block_resolves_every_shared_pointer(tmp_path: Path, root_selection: str) -> None:
    """Every shared-doc pointer in the installed global block resolves to a shipped file under the installed root.

    A fresh Codex session in an unrelated project reads only ``$CODEX_HOME/AGENTS.md``; it cannot find ``shared/global-
    baseline-details.md`` unless the block itself names the absolute plugin root. Both the default (root derived from
    the installed template) and sync's explicit ``--plugin-root`` with a marketplace-checkout template must write the
    installed cache root.
    """
    codex_home = tmp_path / "codex-home"
    cache = _installed_cache(codex_home, "0.34.0")
    source = cache / "assets" / "AGENTS.md"
    options: tuple[str, ...] = ()
    if root_selection == "explicit-plugin-root":
        source = _marketplace_template(tmp_path)
        options = ("--plugin-root", str(cache))

    result = _run_mode(source, codex_home, *options)

    assert result.returncode == 0, result.stderr
    body = _managed_body((codex_home / "AGENTS.md").read_bytes())[1].decode("utf-8")
    assert PLACEHOLDER not in body
    roots = ROOT_LINE.findall(body)
    assert len(roots) == 1
    assert Path(roots[0]).resolve() == cache.resolve()
    pointers = set(SHARED_POINTER.findall(body))
    assert "shared/global-baseline-details.md" in pointers
    assert set(re.findall(r"packaged `(shared/[\w.-]+)`", body)) <= pointers
    missing = sorted(pointer for pointer in pointers if not (Path(roots[0]) / pointer).is_file())
    assert not missing, f"installed block points at files absent from the installed root: {missing}"


@pytest.mark.parametrize(
    ("options", "expected_status", "expected_output"),
    [
        pytest.param(("--check",), 0, "bounded instruction check", id="check-reports-current"),
        pytest.param((), 0, "already current", id="reinstall-is-idempotent"),
    ],
)
def test_rendered_block_is_current_for_the_same_root(
    tmp_path: Path, options: tuple[str, ...], expected_status: int, expected_output: str
) -> None:
    """A block rendered for one root is current for that root, so sync's post-install health check passes.

    Rendering happens before hashing in both install and check; rendering only on install would make every health check
    after sync report ``stale-managed-template`` and fail the sync.
    """
    codex_home = tmp_path / "codex-home"
    cache = _installed_cache(codex_home, "0.34.0")
    source = _marketplace_template(tmp_path)
    assert _run_mode(source, codex_home, "--plugin-root", str(cache)).returncode == 0

    result = _run_mode(source, codex_home, "--plugin-root", str(cache), *options)

    assert result.returncode == expected_status, result.stderr
    assert expected_output in result.stdout
    assert not result.stderr


def test_plugin_upgrade_rewrites_block_to_new_installed_root(tmp_path: Path) -> None:
    """Installing a newer plugin version updates the authenticated block to the new versioned cache root.

    Codex installs each version into its own cache directory, so a block left naming the previous version would point at
    docs that may no longer exist after the upgrade.
    """
    codex_home = tmp_path / "codex-home"
    old = _installed_cache(codex_home, "0.33.1")
    new = _installed_cache(codex_home, "0.34.0")
    source = _marketplace_template(tmp_path)
    assert _run_mode(source, codex_home, "--plugin-root", str(old)).returncode == 0

    result = _run_mode(source, codex_home, "--plugin-root", str(new))

    assert result.returncode == 0, result.stderr
    assert "updated" in result.stdout
    body = _managed_body((codex_home / "AGENTS.md").read_bytes())[1].decode("utf-8")
    assert ROOT_LINE.findall(body) == [str(new)]
    assert str(old) not in body


def test_rendered_plugin_root_reads_back_the_installed_root(tmp_path: Path) -> None:
    """The read-only parser returns exactly the root an install wrote, ignoring a look-alike line in user text.

    The SessionStart hook compares this value with its own plugin root to flag a block a direct plugin update left on
    the previous version, so it must parse the real packaged template's rendered line inside the managed block only.
    """
    codex_home = tmp_path / "codex-home"
    cache = _installed_cache(codex_home, "0.34.0")
    (codex_home / "AGENTS.md").write_bytes(b"# My notes\n\n`PLUGIN_ROOT` = `/user/text/outside/the/block`\n")
    assert _run_mode(_marketplace_template(tmp_path), codex_home, "--plugin-root", str(cache)).returncode == 0
    namespace = runpy.run_path(str(INSTALLER))

    body = namespace["authenticated_managed_body"]((codex_home / "AGENTS.md").read_bytes())

    assert namespace["rendered_plugin_root"](body) == os.path.realpath(cache)


def test_rendered_plugin_root_is_none_for_a_block_from_an_older_template(tmp_path: Path) -> None:
    """An authenticated block installed from a template before the ``PLUGIN_ROOT`` line names no root.

    This is the block every 0.33.1 install carries; the hook warns on it because the template shipped beside the hook
    always defines a root, so the block cannot resolve any packaged ``shared/<file>`` pointer.
    """
    codex_home = tmp_path / "codex-home"
    source = tmp_path / "older" / "AGENTS.md"
    source.parent.mkdir()
    source.write_bytes(re.sub(rb"^.*\{\{CODEX_RIG_PLUGIN_ROOT\}\}.*\n", b"", TEMPLATE.read_bytes(), flags=re.MULTILINE))
    assert _run_mode(source, codex_home).returncode == 0
    namespace = runpy.run_path(str(INSTALLER))

    body = namespace["authenticated_managed_body"]((codex_home / "AGENTS.md").read_bytes())

    assert body is not None
    assert namespace["rendered_plugin_root"](body) is None


def _block_edited_by_hand() -> bytes:
    """Return a block whose rendered root was changed after install, so its digest no longer matches."""
    block = runpy.run_path(str(INSTALLER))["managed_block"](b"`PLUGIN_ROOT` = `/opt/codex-rig/0.34.0`, the package\n")
    return block.replace(b"0.34.0", b"0.33.1")


def _duplicated_block() -> bytes:
    """Return two authenticated blocks, which installation refuses as malformed."""
    block = runpy.run_path(str(INSTALLER))["managed_block"](b"`PLUGIN_ROOT` = `/opt/codex-rig/0.34.0`, the package\n")
    return block + b"\n" + block


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(b"# My notes\n\n`PLUGIN_ROOT` = `/opt/codex-rig/0.34.0`\n", id="no-managed-block"),
        pytest.param(_block_edited_by_hand(), id="hand-edited-block"),
        pytest.param(_duplicated_block(), id="duplicated-block"),
    ],
)
def test_authenticated_managed_body_is_none_without_one_intact_block(payload: bytes) -> None:
    """Return no body for a file without one single authenticated managed block.

    A root read from unauthenticated or user text would let the hook warn about content the installer does not own;
    ``--check`` is the diagnostic that reports edited and duplicated blocks.
    """
    namespace = runpy.run_path(str(INSTALLER))

    assert namespace["authenticated_managed_body"](payload) is None


@pytest.mark.parametrize(
    ("root", "flavour"),
    [
        pytest.param("/codex-root/plugins/cache/borda-ai-rig/codex-rig/0.34.0", PurePosixPath, id="posix"),
        pytest.param(r"D:\codex-root\plugins\cache\borda-ai-rig\codex-rig\0.34.0", PureWindowsPath, id="windows"),
    ],
)
def test_rendered_template_keeps_absolute_root_of_either_flavour_on_any_host(root: str, flavour: type) -> None:
    """An absolute POSIX or Windows root renders unchanged on every host, so pointers resolve in its own flavour.

    The other host flavour's literal is kept verbatim; converting it through the host ``Path`` flavour would rewrite its
    separators and leave ``PLUGIN_ROOT/shared/<file>`` paths that exist on no host. The host flavour's root is
    canonicalized, which leaves a path without symlinked components (here a non-existent top-level directory) as is.
    """
    namespace = runpy.run_path(str(INSTALLER))
    template = TEMPLATE.read_bytes()

    rendered = namespace["rendered_template"](template, TEMPLATE, root).decode("utf-8")

    assert PLACEHOLDER not in rendered
    assert ROOT_LINE.findall(rendered) == [root]
    installed_root = flavour(root)
    assert installed_root.is_absolute()
    resolved = [installed_root.joinpath(*PurePosixPath(pointer).parts) for pointer in SHARED_POINTER.findall(rendered)]
    assert resolved
    assert {path.parent for path in resolved} == {installed_root / "shared"}
    assert all(str(path).startswith(root) for path in resolved)


def test_rendered_template_anchors_relative_root_to_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host-relative ``--plugin-root`` is made absolute, because a relative root resolves from no other project."""
    namespace = runpy.run_path(str(INSTALLER))
    monkeypatch.chdir(tmp_path)

    rendered = namespace["rendered_template"](TEMPLATE.read_bytes(), TEMPLATE, "relative-root").decode("utf-8")

    assert ROOT_LINE.findall(rendered) == [os.path.join(os.getcwd(), "relative-root")]


@pytest.mark.skipif(not DIRECTORY_SYMLINKS_AVAILABLE, reason="host cannot create directory symlinks")
def test_symlinked_codex_home_renders_one_root_for_explicit_and_derived_routes(tmp_path: Path) -> None:
    """Sync's explicit root and the skill check's derived root agree when the Codex home is reached via a symlink.

    Sync spells the root from ``CODEX_HOME`` as given, while the derived default resolves the template path; without
    shared canonicalization the two render different blocks and a healthy install is reported as a stale template.
    """
    real_home = tmp_path / "real-home"
    cache = _installed_cache(real_home, "0.34.0")
    linked_home = tmp_path / "linked-home"
    linked_home.symlink_to(real_home, target_is_directory=True)
    linked_cache = linked_home / cache.relative_to(real_home)
    assert _run_mode(_marketplace_template(tmp_path), linked_home, "--plugin-root", str(linked_cache)).returncode == 0

    result = _run_mode(linked_cache / "assets" / "AGENTS.md", linked_home, "--check")

    assert result.returncode == 0, result.stderr
    assert ROOT_LINE.findall((real_home / "AGENTS.md").read_text(encoding="utf-8")) == [os.path.realpath(cache)]


@pytest.mark.parametrize(
    ("template", "plugin_root", "message"),
    [
        pytest.param(b"# Policy\n", "/codex-rig", "no plugin-root placeholder", id="explicit-root-without-placeholder"),
        pytest.param(f"{PLACEHOLDER}\n{PLACEHOLDER}\n".encode(), "/codex-rig", "repeats", id="repeated-placeholder"),
        pytest.param(f"`{PLACEHOLDER}`\n".encode(), "/codex`rig", "backtick", id="backtick-in-root"),
        pytest.param(f"`{PLACEHOLDER}`\n".encode(), "/codex\nrig", "line break", id="line-break-in-root"),
    ],
)
def test_rendered_template_refuses_unrenderable_input(template: bytes, plugin_root: str, message: str) -> None:
    """Refuse to write a root that would be silently dropped, ambiguous, or break out of its Markdown code span."""
    namespace = runpy.run_path(str(INSTALLER))

    with pytest.raises(namespace["UnsafeGlobalAgentsState"], match=message):
        namespace["rendered_template"](template, TEMPLATE, plugin_root)


def test_remove_rejects_plugin_root(tmp_path: Path) -> None:
    """``--plugin-root`` only renders templates, so pairing it with ``--remove`` is a usage error, not a no-op."""
    result = subprocess.run(
        [sys.executable, str(INSTALLER), "--remove", "--codex-home", str(tmp_path), "--plugin-root", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "does not apply to --remove" in result.stderr
