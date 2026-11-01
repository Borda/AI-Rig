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
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import pytest
from _fixture_files import tree_bytes
from _platform import DIRECTORY_SYMLINKS_AVAILABLE, FILE_SYMLINKS_AVAILABLE

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
INSTALLER = PLUGIN_ROOT / "scripts" / "install_global_agents.py"
TEMPLATE = PLUGIN_ROOT / "assets" / "AGENTS.md"
BEGIN_PREFIX = "<!-- codex-rig:global-agents begin sha256="
END_MARKER = "<!-- codex-rig:global-agents end -->"
HEADING = b"# Global Agent Instructions\n"
CURRENT = HEADING + b"Current.\n"
LEGACY_PREFIX = HEADING + b"Legacy.\n\n"
PLACEHOLDER = "{{CODEX_RIG_PLUGIN_ROOT}}"
#: Installed-root line the packaged template renders; group 1 is the absolute root written by the installer.
ROOT_LINE = re.compile(r"^`PLUGIN_ROOT` = `([^`]+)`", re.MULTILINE)
#: Every backticked Codex Rig shared-file pointer the global template names.
SHARED_POINTER = re.compile(r"`(shared/[\w.-]+\.(?:md|py))`")
LEGACY_DEVELOP_SKILL = (
    b"---\nname: develop\n"
    b"description: Minimal codex-native develop loop. Use for implementation tasks with linear plan-build-verify flow "
    b"and measurable quality gates.\n---\n"
)


def _load_installer() -> dict[str, Any]:
    """Execute the packaged installer script and return its public namespace."""
    return runpy.run_path(str(INSTALLER))


def _run_cli(*args: str | Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run the packaged installer script with exactly the given command-line arguments."""
    return subprocess.run(
        [sys.executable, str(INSTALLER), *map(str, args)], capture_output=True, env=env, text=True, check=False
    )


def _run_installer(
    source: Path, codex_home: Path, *options: str, output_encoding: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run the packaged installer against one isolated Codex home, optionally forcing the child's stdout encoding."""
    env = os.environ.copy()
    if output_encoding is not None:
        env["PYTHONIOENCODING"] = output_encoding
    return _run_cli("--source", source, "--codex-home", codex_home, *options, env=env)


def _run_remover(codex_home: Path) -> subprocess.CompletedProcess[str]:
    """Run the packaged installer in ``--remove`` mode against one isolated Codex home."""
    return _run_cli("--remove", "--codex-home", codex_home)


def _managed_block(body: bytes) -> bytes:
    """Return the authenticated managed block the installer would write for ``body``."""
    return _load_installer()["managed_block"](body)


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


def _backups(codex_home: Path) -> list[Path]:
    """Return the AGENTS.md backups the installer made, in stable order."""
    return sorted((codex_home / "backups" / "codex-rig").glob("*-AGENTS.md"))


def _write_tree(root: Path, files: Mapping[str, bytes]) -> None:
    """Write relative-path files under ``root``, creating parents (and ``root``) only when there is something to
    write."""
    for relative, data in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _installed_cache(codex_home: Path, version: str) -> Path:
    """Lay out the real template and shared docs where Codex installs Codex Rig under one isolated home."""
    cache = codex_home / "plugins" / "cache" / "borda-ai-rig" / "codex-rig" / version
    shutil.copytree(PLUGIN_ROOT / "shared", cache / "shared", ignore=shutil.ignore_patterns("__pycache__"))
    (cache / "assets").mkdir()
    shutil.copy2(TEMPLATE, cache / "assets" / "AGENTS.md")
    return cache


def _derived_route(cache: Path, marketplace: Path) -> tuple[Path, tuple[str, ...]]:
    """Install from the cached template and let the installer derive the plugin root from it."""
    return cache / "assets" / "AGENTS.md", ()


def _explicit_route(cache: Path, marketplace: Path) -> tuple[Path, tuple[str, ...]]:
    """Install from a marketplace-checkout template with sync's explicit ``--plugin-root``."""
    return marketplace, ("--plugin-root", str(cache))


def _block_edited_by_hand() -> bytes:
    """Return a block whose rendered root was changed after install, so its digest no longer matches."""
    block = _managed_block(b"`PLUGIN_ROOT` = `/opt/codex-rig/0.34.0`, the package\n")
    return block.replace(b"0.34.0", b"0.33.1")


def _duplicated_block() -> bytes:
    """Return two authenticated blocks, which installation refuses as malformed."""
    block = _managed_block(b"`PLUGIN_ROOT` = `/opt/codex-rig/0.34.0`, the package\n")
    return block + b"\n" + block


@pytest.fixture
def installer() -> dict[str, Any]:
    """Return a fresh namespace of the packaged installer for direct function calls."""
    return _load_installer()


@pytest.fixture
def codex_home(tmp_path: Path) -> Path:
    """Return an isolated Codex home path that does not exist yet."""
    return tmp_path / "codex-home"


@pytest.fixture
def target(codex_home: Path) -> Path:
    """Create the isolated Codex home and return its (not yet written) ``AGENTS.md`` path."""
    codex_home.mkdir()
    return codex_home / "AGENTS.md"


@pytest.fixture
def write_template(tmp_path: Path) -> Callable[[bytes], Path]:
    """Return a writer that stores the given bytes as the template to install and returns its path."""

    def write(content: bytes = b"managed policy\n") -> Path:
        """Write ``content`` as the installer's source template."""
        source = tmp_path / "template.md"
        source.write_bytes(content)
        return source

    return write


@pytest.fixture
def installed_cache(codex_home: Path) -> Callable[[str], Path]:
    """Return a factory that lays out one versioned installed cache under the isolated Codex home."""
    return lambda version: _installed_cache(codex_home, version)


@pytest.fixture
def marketplace_template(tmp_path: Path) -> Path:
    """Copy the real template to a marketplace-checkout location that is not the installed cache."""
    source = tmp_path / "marketplace" / "plugins" / "codex-rig" / "assets" / "AGENTS.md"
    source.parent.mkdir(parents=True)
    shutil.copy2(TEMPLATE, source)
    return source


class TestPackagedTemplate:
    """The generic global template ships inside the package, never as repository policy."""

    def test_is_packaged_not_repository_policy(self) -> None:
        """Generic global guidance stays in the package and is never copied into repository-root policy.

        The repository ships its own root AGENTS.md (repository-scoped policy). What must never happen is the generic
        packaged template landing in that slot, so content is compared rather than absence asserted.
        """
        manifest = json.loads((PLUGIN_ROOT / "package-manifest.json").read_text(encoding="utf-8"))
        record = next(item for item in manifest["files"] if item["path"] == "assets/AGENTS.md")
        repository_agents = PLUGIN_ROOT.parents[1] / "AGENTS.md"

        assert TEMPLATE.read_text(encoding="utf-8").startswith("# Global Agent Instructions\n")
        assert not repository_agents.exists() or repository_agents.read_bytes() != TEMPLATE.read_bytes()
        assert record == {
            "mode": "0644",
            "path": "assets/AGENTS.md",
            "sha256": hashlib.sha256(TEMPLATE.read_bytes()).hexdigest(),
        }

    def test_rejects_hypothetical_complexity(self) -> None:
        """The template tells agents that future-only scenarios never justify current machinery.

        Each phrase is one clause of the simplicity policy; losing any of them reopens speculative-complexity drift.
        """
        policy = TEMPLATE.read_text(encoding="utf-8")

        assert "Hypothetical future states, risks, scale, reuse, or edge cases do not justify machinery" in policy
        assert "verified current evidence proves the simpler solution insufficient" in policy
        assert (
            "Prefer maintained standard-library, native-platform, and already-installed package functionality" in policy
        )
        assert "over custom code that duplicates it" in policy
        assert "Simplicity never removes trust-boundary validation" in policy
        assert "record the ceiling and observable trigger for revisiting it" in policy


class TestInstall:
    """Explicit installation writes one authenticated managed block and refuses anything it cannot trust."""

    def test_creates_managed_file_when_absent(self, write_template: Callable[[bytes], Path], codex_home: Path) -> None:
        """Installing into a Codex home without AGENTS.md creates one authenticated managed block.

        The template uses CRLF and the child's stdout is forced to cp1252, so byte-exactness and ASCII-only output are
        both exercised.
        """
        source = write_template(b"# Generic policy\r\n\r\nKeep this.\r\n")

        result = _run_installer(source, codex_home, output_encoding="cp1252")

        assert result.returncode == 0, result.stderr
        payload = (codex_home / "AGENTS.md").read_bytes()
        digest, body = _managed_body(payload)
        assert body == source.read_bytes()
        assert digest == hashlib.sha256(body).hexdigest()
        assert payload.count(BEGIN_PREFIX.encode("ascii")) == 1
        assert payload.count(END_MARKER.encode("ascii")) == 1
        assert "created" in result.stdout
        assert result.stdout.isascii()

    def test_preserves_existing_content_and_is_idempotent(
        self, write_template: Callable[[bytes], Path], codex_home: Path, target: Path
    ) -> None:
        """Installing next to user-owned guidance keeps it byte-for-byte and a repeat install changes nothing.

        The first install appends the block and backs the original up once; the second finds the block current, so it
        must neither rewrite the file nor add a second backup.
        """
        source = write_template(b"# Generic policy\n")
        original = "# User policy\n\nKeep this exactly.\n"
        target.write_text(original, encoding="utf-8")

        first = _run_installer(source, codex_home)
        first_payload = target.read_bytes()
        backups = _backups(codex_home)
        second = _run_installer(source, codex_home)

        assert first.returncode == 0, first.stderr
        assert target.read_text(encoding="utf-8").startswith(original)
        assert len(backups) == 1
        assert backups[0].read_text(encoding="utf-8") == original
        assert second.returncode == 0, second.stderr
        assert target.read_bytes() == first_payload
        assert _backups(codex_home) == backups
        assert "already current" in second.stdout

    def test_updates_authenticated_block(
        self, write_template: Callable[[bytes], Path], codex_home: Path, target: Path
    ) -> None:
        """A plugin upgrade replaces only an unmodified managed block and keeps the user's text before it.

        The block is installed from the first template, then the template changes; the managed body must follow it while
        the user prefix is untouched and exactly one block remains.
        """
        source = write_template(b"first policy\r\n")
        user_prefix = b"user prefix\r\n"
        target.write_bytes(user_prefix)
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

    def test_adopts_exact_legacy_copy(
        self, write_template: Callable[[bytes], Path], codex_home: Path, target: Path
    ) -> None:
        """An old unmarked full-template copy is adopted into a managed block instead of being duplicated.

        Before managed blocks existed the template was copied verbatim; installing over such a file must migrate it in
        place (one copy of the policy, one backup of the original bytes).
        """
        source = write_template(HEADING + b"legacy generic policy\n")
        target.write_bytes(source.read_bytes())

        result = _run_installer(source, codex_home)

        assert result.returncode == 0, result.stderr
        payload = target.read_bytes()
        assert _managed_body(payload)[1] == source.read_bytes()
        assert payload.count(b"legacy generic policy") == 1
        assert "adopted" in result.stdout
        backups = _backups(codex_home)
        assert len(backups) == 1
        assert backups[0].read_bytes() == source.read_bytes()

    @pytest.mark.parametrize(
        "damage",
        [
            pytest.param(lambda text: text.replace("managed policy", "manually changed policy"), id="modified-body"),
            pytest.param(lambda text: text.replace(END_MARKER, ""), id="orphan-begin"),
            pytest.param(lambda text: text[text.index(END_MARKER) :], id="orphan-end"),
            pytest.param(lambda text: text + text, id="duplicate"),
        ],
    )
    def test_refuses_untrusted_managed_state(
        self, write_template: Callable[[bytes], Path], codex_home: Path, damage: Callable[[str], str]
    ) -> None:
        """Installation fails without writing when managed ownership evidence is ambiguous or changed.

        A previously installed block is damaged in four ways (edited body, missing end marker, missing begin marker,
        duplicated block); each must exit 4 with a refusal and leave the damaged file byte-identical.
        """
        source = write_template()
        assert _run_installer(source, codex_home).returncode == 0
        target = codex_home / "AGENTS.md"
        target.write_text(damage(target.read_text(encoding="utf-8")), encoding="utf-8")
        before = target.read_bytes()

        result = _run_installer(source, codex_home)

        assert result.returncode == 4
        assert target.read_bytes() == before
        assert "refusing" in result.stderr.lower()

    @pytest.mark.skipif(not FILE_SYMLINKS_AVAILABLE, reason="host cannot create file symlinks")
    def test_refuses_symlink_target(
        self, write_template: Callable[[bytes], Path], tmp_path: Path, codex_home: Path, target: Path
    ) -> None:
        """Optional installation never follows an AGENTS.md symlink out of the Codex home.

        The target is a symlink to a file outside the home; the installer must refuse and leave the outside file alone.
        """
        outside = tmp_path / "outside.md"
        outside.write_text("outside\n", encoding="utf-8")
        target.symlink_to(outside)

        result = _run_installer(write_template(), codex_home)

        assert result.returncode == 4
        assert outside.read_text(encoding="utf-8") == "outside\n"
        assert "symlink" in result.stderr.lower()

    @pytest.mark.parametrize(
        "managed_bodies",
        [
            pytest.param((), id="absent"),
            pytest.param((HEADING + b"\nCurrent policy.\n",), id="current"),
            pytest.param((HEADING + b"Old managed.\n",), id="stale"),
        ],
    )
    def test_refuses_overlapping_global_policy_without_writes(
        self, write_template: Callable[[bytes], Path], codex_home: Path, target: Path, managed_bodies: tuple[bytes, ...]
    ) -> None:
        """A user-owned copy of the global heading blocks installation before any mutation, managed block or not.

        The file already carries private text under the same heading, optionally followed by a current or stale managed
        block; the install must exit 4, write nothing, make no backup, and never echo the private text.
        """
        source = write_template(HEADING + b"\nCurrent policy.\n")
        original = HEADING + b"\nPRIVATE old policy.\n\n" + b"".join(_managed_block(body) for body in managed_bodies)
        target.write_bytes(original)

        result = _run_installer(source, codex_home)

        assert result.returncode == 4
        assert target.read_bytes() == original
        assert not (codex_home / "backups").exists()
        assert "overlap" in result.stderr
        assert "PRIVATE" not in result.stderr
        assert "[ok]" not in result.stdout


class TestAtomicWrite:
    """``atomic_write`` refuses to replace a target that changed after it was observed."""

    def test_refuses_target_drift_before_replace(self, installer: dict[str, Any], tmp_path: Path) -> None:
        """A concurrent edit seen before replacement is never overwritten.

        The target's bytes differ from the bytes the caller observed, so the write must raise and keep the edit.
        """
        target = tmp_path / "AGENTS.md"
        target.write_bytes(b"concurrent edit\n")

        with pytest.raises(installer["UnsafeGlobalAgentsState"]):
            installer["atomic_write"](target, b"replacement\n", 0o600, b"observed\n")

        assert target.read_bytes() == b"concurrent edit\n"

    def test_refuses_same_byte_target_replacement(self, installer: dict[str, Any], tmp_path: Path) -> None:
        """Replacement identity drift is rejected even when the new file has identical bytes.

        The target is swapped for a different file with the same content after its identity was recorded; the write must
        raise "replaced" and leave no temporary sibling behind.
        """
        target = tmp_path / "AGENTS.md"
        payload = b"observed\n"
        target.write_bytes(payload)
        identity = target.stat()
        replacement = tmp_path / "replacement.md"
        replacement.write_bytes(payload)
        replacement.replace(target)

        with pytest.raises(installer["UnsafeGlobalAgentsState"], match="replaced"):
            installer["atomic_write"](target, b"desired\n", 0o600, payload, identity)

        assert target.read_bytes() == payload
        assert list(tmp_path.glob(".AGENTS.md.codex-rig-*")) == []


class TestRemove:
    """``--remove`` strips only the authenticated managed block and refuses anything else."""

    def test_deletes_file_that_held_only_managed_block(
        self, write_template: Callable[[bytes], Path], codex_home: Path
    ) -> None:
        """Teardown deletes an AGENTS.md that Codex Rig alone created, keeping a backup of the removed bytes.

        The file holds nothing but the managed block, so removing the block leaves nothing worth keeping.
        """
        assert _run_installer(write_template(), codex_home).returncode == 0
        target = codex_home / "AGENTS.md"
        original = target.read_bytes()

        result = _run_remover(codex_home)

        assert result.returncode == 0, result.stderr
        assert not target.exists()
        assert "removed-file" in result.stdout
        backups = _backups(codex_home)
        assert len(backups) == 1
        assert backups[0].read_bytes() == original

    def test_strips_block_and_preserves_user_content(
        self, write_template: Callable[[bytes], Path], codex_home: Path, target: Path
    ) -> None:
        """Teardown removes only the managed block and keeps user-owned guidance exactly.

        The user text was there before installation, so after removal the file must equal the original text.
        """
        user_content = "# User policy\n\nKeep this exactly.\n"
        target.write_text(user_content, encoding="utf-8")
        assert _run_installer(write_template(), codex_home).returncode == 0

        result = _run_remover(codex_home)

        assert result.returncode == 0, result.stderr
        assert target.read_text(encoding="utf-8") == user_content
        assert "removed-block" in result.stdout

    def test_is_noop_when_no_managed_block_present(self, codex_home: Path, target: Path) -> None:
        """Teardown leaves an unmanaged AGENTS.md untouched and reports it absent.

        A file with only user content has nothing for Codex Rig to remove, so the bytes must not change.
        """
        target.write_text("# only user content\n", encoding="utf-8")
        before = target.read_bytes()

        result = _run_remover(codex_home)

        assert result.returncode == 0, result.stderr
        assert target.read_bytes() == before
        assert "absent" in result.stdout

    def test_refuses_modified_managed_block(self, write_template: Callable[[bytes], Path], codex_home: Path) -> None:
        """Teardown fails without writing when the managed block was tampered with.

        An edited body no longer matches its recorded digest, so the installer cannot prove it owns the block.
        """
        assert _run_installer(write_template(), codex_home).returncode == 0
        target = codex_home / "AGENTS.md"
        target.write_text(
            target.read_text(encoding="utf-8").replace("managed policy", "manually changed"), encoding="utf-8"
        )
        before = target.read_bytes()

        result = _run_remover(codex_home)

        assert result.returncode == 4
        assert target.read_bytes() == before
        assert "refusing" in result.stderr.lower()


class TestUsage:
    """Argument combinations the command line rejects as usage errors."""

    def test_install_requires_source_unless_remove(self, tmp_path: Path) -> None:
        """Installing without ``--source`` is a usage error, while ``--remove`` needs only ``--codex-home``.

        Only ``--codex-home`` is given, which is valid for removal but incomplete for installation.
        """
        result = _run_cli("--codex-home", tmp_path)

        assert result.returncode == 2
        assert "--source is required unless --remove" in result.stderr

    def test_remove_rejects_plugin_root(self, tmp_path: Path) -> None:
        """``--plugin-root`` only renders templates, so pairing it with ``--remove`` is a usage error, not a no-op."""
        result = _run_cli("--remove", "--codex-home", tmp_path, "--plugin-root", tmp_path)

        assert result.returncode == 2
        assert "does not apply to --remove" in result.stderr


class TestCheck:
    """``--check`` diagnoses instruction drift without ever modifying the Codex home."""

    @pytest.mark.parametrize(
        ("files", "returncode", "stderr_marker"),
        [
            pytest.param({}, 0, "", id="absent"),
            pytest.param({"AGENTS.md": _managed_block(CURRENT)}, 0, "", id="current"),
            pytest.param({"AGENTS.md": _managed_block(HEADING + b"Old.\n")}, 4, "", id="stale"),
            pytest.param({"AGENTS.md": HEADING + b"PRIVATE.\n" + _managed_block(CURRENT)}, 4, "", id="duplicate"),
            pytest.param(
                {"AGENTS.md": _managed_block(CURRENT).replace(b"Current.", b"Modified.")}, 4, "", id="modified"
            ),
            pytest.param(
                {"AGENTS.md": _managed_block(CURRENT).replace(END_MARKER.encode("ascii"), b"")},
                4,
                "",
                id="orphan-marker",
            ),
            pytest.param(
                {"AGENTS.md": _managed_block(CURRENT), "skills/develop/SKILL.md": LEGACY_DEVELOP_SKILL},
                4,
                "legacy-skill-route",
                id="legacy-skill",
            ),
        ],
    )
    def test_is_read_only_and_rejects_degraded_state(
        self,
        write_template: Callable[[bytes], Path],
        codex_home: Path,
        files: Mapping[str, bytes],
        returncode: int,
        stderr_marker: str,
    ) -> None:
        """Bounded instruction drift is diagnosed with exit 4 without backups, writes, or leaking private text.

        Seven Codex-home states (absent, current, stale, duplicated, modified, orphaned marker, legacy skill route) are
        checked; healthy or absent ones pass and every other one fails, none of them altering a single byte.
        """
        _write_tree(codex_home, files)
        before = tree_bytes(codex_home)

        result = _run_installer(write_template(CURRENT), codex_home, "--check")

        assert result.returncode == returncode, result.stderr
        assert tree_bytes(codex_home) == before
        assert codex_home.exists() == bool(files)
        assert not (codex_home / "backups").exists()
        assert "PRIVATE" not in result.stderr
        assert stderr_marker in result.stderr

    def test_allows_unmanaged_optional_setup_and_custom_skill(
        self, write_template: Callable[[bytes], Path], codex_home: Path
    ) -> None:
        """Absent management and an unrelated same-name custom skill are not classified as corruption.

        A user-authored AGENTS.md and a custom ``develop`` skill are legitimate optional setups the check must accept.
        """
        _write_tree(
            codex_home,
            {
                "AGENTS.md": b"# Personal Policy\nCustom policy.\n",
                "skills/develop/SKILL.md": b"---\nname: develop\ndescription: Custom development workflow.\n---\n",
            },
        )

        result = _run_installer(write_template(CURRENT), codex_home, "--check")

        assert result.returncode == 0, result.stderr
        assert "bounded instruction check" in result.stdout
        assert not (codex_home / "backups").exists()

    def test_matches_install_refusal_for_single_unmarked_global_policy(
        self, write_template: Callable[[bytes], Path], codex_home: Path, target: Path
    ) -> None:
        """One unmanaged global heading cannot pass preflight when ordinary install would refuse the overlap.

        Check and install must agree (both exit 4 with the same reason) and neither may touch the file.
        """
        source = write_template(HEADING + b"Current managed policy.\n")
        original = HEADING + b"Custom unmanaged policy.\n"
        target.write_bytes(original)

        checked = _run_installer(source, codex_home, "--check")
        installed = _run_installer(source, codex_home)

        assert checked.returncode == installed.returncode == 4
        assert "global-agents-overlap" in checked.stderr
        assert "global-agents-overlap" in installed.stderr
        assert target.read_bytes() == original
        assert not (codex_home / "backups").exists()

    def test_preserves_exact_unmarked_template_adoption(
        self, write_template: Callable[[bytes], Path], codex_home: Path, target: Path
    ) -> None:
        """An exact template copy stays adoptable: the check passes without writing, then install adopts it once.

        Preflight must not create a duplicated policy; only the later install may rewrite the file into one block.
        """
        original = HEADING + b"Current managed policy.\n"
        source = write_template(original)
        target.write_bytes(original)

        checked = _run_installer(source, codex_home, "--check")
        after_check = (target.read_bytes(), (codex_home / "backups").exists())
        installed = _run_installer(source, codex_home)

        assert checked.returncode == 0, checked.stderr
        assert after_check == (original, False)
        assert installed.returncode == 0
        assert "adopted" in installed.stdout
        assert target.read_bytes().count(b"Current managed policy.") == 1


class TestMigration:
    """Legacy-prefix migration removes only reviewed bytes and refuses stale consent."""

    @pytest.mark.parametrize("heading_ending", [pytest.param(b"\n", id="lf"), pytest.param(b"\r\n", id="crlf")])
    def test_preserves_suffix_and_full_backup(
        self, write_template: Callable[[bytes], Path], codex_home: Path, target: Path, heading_ending: bytes
    ) -> None:
        """Migration removes the exact reviewed LF or CRLF prefix and retains the custom suffix bytes.

        The full original file is backed up first, so the removed prefix remains recoverable.
        """
        source = write_template(CURRENT)
        prefix = b"# Global Agent Instructions" + heading_ending + b"PRIVATE old policy.\r\n\r\n"
        suffix = b"\n# Personal policy\r\nKeep exact bytes.\r\n"
        block = _managed_block(CURRENT)
        original = prefix + block + suffix
        target.write_bytes(original)

        result = _run_installer(
            source, codex_home, "--migrate-legacy-prefix-sha256", hashlib.sha256(prefix).hexdigest()
        )

        assert result.returncode == 0, result.stderr
        assert target.read_bytes() == block + suffix
        backups = _backups(codex_home)
        assert len(backups) == 1
        assert backups[0].read_bytes() == original

    @pytest.mark.parametrize(
        ("file_prefix", "consent_digest", "block_edit"),
        [
            pytest.param(LEGACY_PREFIX, "0" * 64, (b"Current.", b"Current."), id="stale-consent-digest"),
            pytest.param(
                LEGACY_PREFIX,
                hashlib.sha256(LEGACY_PREFIX).hexdigest(),
                (b"Current.", b"Modified."),
                id="modified-body",
            ),
            pytest.param(
                b"# Personal policy\n" + LEGACY_PREFIX,
                hashlib.sha256(LEGACY_PREFIX).hexdigest(),
                (b"Current.", b"Current."),
                id="ambiguous-prefix",
            ),
        ],
    )
    def test_refuses_unreviewed_bytes(
        self,
        write_template: Callable[[bytes], Path],
        codex_home: Path,
        target: Path,
        file_prefix: bytes,
        consent_digest: str,
        block_edit: tuple[bytes, bytes],
    ) -> None:
        """Migration fails without writes when consent is stale or managed integrity changed.

        The consent digest may not match the prefix, the managed body may be edited, or the prefix may carry extra
        personal text; each must exit 4 and leave both file and backups untouched.
        """
        original = file_prefix + _managed_block(CURRENT).replace(*block_edit)
        target.write_bytes(original)

        result = _run_installer(write_template(CURRENT), codex_home, "--migrate-legacy-prefix-sha256", consent_digest)

        assert result.returncode == 4
        assert target.read_bytes() == original
        assert not (codex_home / "backups").exists()

    def test_requires_existing_managed_block_without_creating_home(
        self, write_template: Callable[[bytes], Path], codex_home: Path
    ) -> None:
        """Removal consent with no existing reviewed prefix is rejected, and the absent home stays absent."""
        result = _run_installer(write_template(CURRENT), codex_home, "--migrate-legacy-prefix-sha256", "0" * 64)

        assert result.returncode == 4
        assert not codex_home.exists()

    def test_refuses_concurrent_replacement_before_backup(
        self,
        installer: dict[str, Any],
        write_template: Callable[[bytes], Path],
        codex_home: Path,
        target: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A target replaced between migration review and backup is preserved, with no backup written.

        The migration planner is wrapped so a different file takes the target's place right after validation; the
        installer must notice and raise "changed before backup" rather than back up or overwrite the newcomer.
        """
        source = write_template(CURRENT)
        original = LEGACY_PREFIX + installer["managed_block"](CURRENT)
        target.write_bytes(original)
        replacement = codex_home / "concurrent.md"
        replacement.write_bytes(original)
        migrate = installer["migrated_prefix_payload"]

        def replace_after_review(existing: bytes, block: bytes, digest: str) -> bytes:
            """Simulate replacement after migration validation but before backup creation."""
            desired = migrate(existing, block, digest)
            replacement.replace(target)
            return desired

        monkeypatch.setitem(
            installer["install_global_agents"].__globals__, "migrated_prefix_payload", replace_after_review
        )

        with pytest.raises(installer["UnsafeGlobalAgentsState"], match="changed before backup"):
            installer["install_global_agents"](source, codex_home, hashlib.sha256(LEGACY_PREFIX).hexdigest())

        assert target.read_bytes() == original
        assert not (codex_home / "backups").exists()


class TestPluginRootRendering:
    """The installer renders the absolute installed plugin root into the managed block."""

    @pytest.mark.packaging
    @pytest.mark.parametrize(
        "route",
        [
            pytest.param(_derived_route, id="derived-from-installed-source"),
            pytest.param(_explicit_route, id="explicit-plugin-root"),
        ],
    )
    def test_block_resolves_every_shared_pointer(
        self,
        installed_cache: Callable[[str], Path],
        marketplace_template: Path,
        codex_home: Path,
        route: Callable[[Path, Path], tuple[Path, tuple[str, ...]]],
    ) -> None:
        """Every shared-doc pointer in the installed global block resolves to a shipped file under the installed root.

        A fresh Codex session in an unrelated project reads only ``$CODEX_HOME/AGENTS.md``; it cannot find ``shared/
        global-baseline-details.md`` unless the block itself names the absolute plugin root. Both the default (root
        derived from the installed template) and sync's explicit ``--plugin-root`` with a marketplace-checkout template
        must write the installed cache root.
        """
        cache = installed_cache("0.34.0")
        source, options = route(cache, marketplace_template)

        result = _run_installer(source, codex_home, *options)

        assert result.returncode == 0, result.stderr
        body = _managed_body((codex_home / "AGENTS.md").read_bytes())[1].decode("utf-8")
        assert PLACEHOLDER not in body
        roots = ROOT_LINE.findall(body)
        assert len(roots) == 1
        assert Path(roots[0]).resolve() == cache.resolve()
        pointers = set(SHARED_POINTER.findall(body))
        assert "shared/global-baseline-details.md" in pointers
        assert set(re.findall(r"packaged\s+`(shared/[\w.-]+)`", body)) <= pointers
        missing = sorted(pointer for pointer in pointers if not (Path(roots[0]) / pointer).is_file())
        assert not missing, f"installed block points at files absent from the installed root: {missing}"

    @pytest.mark.parametrize(
        ("options", "expected_output"),
        [
            pytest.param(("--check",), "bounded instruction check", id="check-reports-current"),
            pytest.param((), "already current", id="reinstall-is-idempotent"),
        ],
    )
    def test_block_is_current_for_the_same_root(
        self,
        installed_cache: Callable[[str], Path],
        marketplace_template: Path,
        codex_home: Path,
        options: tuple[str, ...],
        expected_output: str,
    ) -> None:
        """A block rendered for one root is current for that root, so sync's post-install health check passes.

        Rendering happens before hashing in both install and check; rendering only on install would make every health
        check after sync report ``stale-managed-template`` and fail the sync.
        """
        cache = installed_cache("0.34.0")
        assert _run_installer(marketplace_template, codex_home, "--plugin-root", str(cache)).returncode == 0

        result = _run_installer(marketplace_template, codex_home, "--plugin-root", str(cache), *options)

        assert result.returncode == 0, result.stderr
        assert expected_output in result.stdout
        assert not result.stderr

    def test_upgrade_rewrites_block_to_new_installed_root(
        self, installed_cache: Callable[[str], Path], marketplace_template: Path, codex_home: Path
    ) -> None:
        """Installing a newer plugin version updates the authenticated block to the new versioned cache root.

        Codex installs each version into its own cache directory, so a block left naming the previous version would
        point at docs that may no longer exist after the upgrade.
        """
        old = installed_cache("0.33.1")
        new = installed_cache("0.34.0")
        assert _run_installer(marketplace_template, codex_home, "--plugin-root", str(old)).returncode == 0

        result = _run_installer(marketplace_template, codex_home, "--plugin-root", str(new))

        assert result.returncode == 0, result.stderr
        assert "updated" in result.stdout
        body = _managed_body((codex_home / "AGENTS.md").read_bytes())[1].decode("utf-8")
        assert ROOT_LINE.findall(body) == [str(new)]
        assert str(old) not in body

    def test_reads_back_the_installed_root(
        self,
        installer: dict[str, Any],
        installed_cache: Callable[[str], Path],
        marketplace_template: Path,
        codex_home: Path,
        target: Path,
    ) -> None:
        """The read-only parser returns exactly the root an install wrote, ignoring a look-alike line in user text.

        The SessionStart hook compares this value with its own plugin root to flag a block a direct plugin update left
        on the previous version, so it must parse the real packaged template's rendered line inside the block only.
        """
        cache = installed_cache("0.34.0")
        target.write_bytes(b"# My notes\n\n`PLUGIN_ROOT` = `/user/text/outside/the/block`\n")
        assert _run_installer(marketplace_template, codex_home, "--plugin-root", str(cache)).returncode == 0
        body = installer["authenticated_managed_body"](target.read_bytes())

        assert installer["rendered_plugin_root"](body) == os.path.realpath(cache)

    def test_reads_no_root_from_older_template(
        self, installer: dict[str, Any], tmp_path: Path, codex_home: Path
    ) -> None:
        """An authenticated block installed from a template before the ``PLUGIN_ROOT`` line names no root.

        This is the block every 0.33.1 install carries; the hook warns on it because the template shipped beside the
        hook always defines a root, so the block cannot resolve any packaged ``shared/<file>`` pointer.
        """
        source = tmp_path / "older" / "AGENTS.md"
        source.parent.mkdir()
        source.write_bytes(
            re.sub(rb"^.*\{\{CODEX_RIG_PLUGIN_ROOT\}\}.*\n", b"", TEMPLATE.read_bytes(), flags=re.MULTILINE)
        )
        assert _run_installer(source, codex_home).returncode == 0
        body = installer["authenticated_managed_body"]((codex_home / "AGENTS.md").read_bytes())

        assert body is not None
        assert installer["rendered_plugin_root"](body) is None

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param(b"# My notes\n\n`PLUGIN_ROOT` = `/opt/codex-rig/0.34.0`\n", id="no-managed-block"),
            pytest.param(_block_edited_by_hand(), id="hand-edited-block"),
            pytest.param(_duplicated_block(), id="duplicated-block"),
        ],
    )
    def test_authenticated_body_is_none_without_one_intact_block(
        self, installer: dict[str, Any], payload: bytes
    ) -> None:
        """No body is returned for a file without exactly one authenticated managed block.

        A root read from unauthenticated or user text would let the hook warn about content the installer does not own;
        ``--check`` is the diagnostic that reports edited and duplicated blocks.
        """
        assert installer["authenticated_managed_body"](payload) is None

    @pytest.mark.parametrize(
        ("root", "flavour"),
        [
            pytest.param("/codex-root/plugins/cache/borda-ai-rig/codex-rig/0.34.0", PurePosixPath, id="posix"),
            pytest.param(r"D:\codex-root\plugins\cache\borda-ai-rig\codex-rig\0.34.0", PureWindowsPath, id="windows"),
        ],
    )
    def test_keeps_absolute_root_of_either_flavour_on_any_host(
        self, installer: dict[str, Any], root: str, flavour: type
    ) -> None:
        """An absolute POSIX or Windows root renders unchanged on every host, so pointers resolve in its own flavour.

        The other host flavour's literal is kept verbatim; converting it through the host ``Path`` flavour would rewrite
        its separators and leave ``PLUGIN_ROOT/shared/<file>`` paths that exist on no host. The host flavour's root is
        canonicalized, which leaves a path without symlinked components (here a non-existent top-level directory) as is.
        """
        rendered = installer["rendered_template"](TEMPLATE.read_bytes(), TEMPLATE, root).decode("utf-8")

        assert PLACEHOLDER not in rendered
        assert ROOT_LINE.findall(rendered) == [root]
        installed_root = flavour(root)
        assert installed_root.is_absolute()
        resolved = [
            installed_root.joinpath(*PurePosixPath(pointer).parts) for pointer in SHARED_POINTER.findall(rendered)
        ]
        assert resolved
        assert {path.parent for path in resolved} == {installed_root / "shared"}
        assert all(str(path).startswith(root) for path in resolved)

    def test_anchors_relative_root_to_working_directory(
        self, installer: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A host-relative ``--plugin-root`` is made absolute, because a relative root resolves from no other
        project."""
        monkeypatch.chdir(tmp_path)

        rendered = installer["rendered_template"](TEMPLATE.read_bytes(), TEMPLATE, "relative-root").decode("utf-8")

        assert ROOT_LINE.findall(rendered) == [os.path.join(os.getcwd(), "relative-root")]

    @pytest.mark.skipif(not DIRECTORY_SYMLINKS_AVAILABLE, reason="host cannot create directory symlinks")
    def test_symlinked_home_renders_one_root_for_explicit_and_derived_routes(
        self, tmp_path: Path, marketplace_template: Path
    ) -> None:
        """Sync's explicit root and the skill check's derived root agree when the Codex home is reached via a symlink.

        Sync spells the root from ``CODEX_HOME`` as given, while the derived default resolves the template path; without
        shared canonicalization the two render different blocks and a healthy install is reported as a stale template.
        """
        real_home = tmp_path / "real-home"
        cache = _installed_cache(real_home, "0.34.0")
        linked_home = tmp_path / "linked-home"
        linked_home.symlink_to(real_home, target_is_directory=True)
        linked_cache = linked_home / cache.relative_to(real_home)
        assert _run_installer(marketplace_template, linked_home, "--plugin-root", str(linked_cache)).returncode == 0

        result = _run_installer(linked_cache / "assets" / "AGENTS.md", linked_home, "--check")

        assert result.returncode == 0, result.stderr
        assert ROOT_LINE.findall((real_home / "AGENTS.md").read_text(encoding="utf-8")) == [os.path.realpath(cache)]

    @pytest.mark.parametrize(
        ("template", "plugin_root", "message"),
        [
            pytest.param(
                b"# Policy\n", "/codex-rig", "no plugin-root placeholder", id="explicit-root-without-placeholder"
            ),
            pytest.param(
                f"{PLACEHOLDER}\n{PLACEHOLDER}\n".encode(), "/codex-rig", "repeats", id="repeated-placeholder"
            ),
            pytest.param(f"`{PLACEHOLDER}`\n".encode(), "/codex`rig", "backtick", id="backtick-in-root"),
            pytest.param(f"`{PLACEHOLDER}`\n".encode(), "/codex\nrig", "line break", id="line-break-in-root"),
        ],
    )
    def test_refuses_unrenderable_input(
        self, installer: dict[str, Any], template: bytes, plugin_root: str, message: str
    ) -> None:
        """Rendering refuses a root that would be silently dropped, ambiguous, or break out of its Markdown code span.

        Four inputs are covered: a root with no placeholder to fill, a repeated placeholder, and a root containing a
        backtick or a line break.
        """
        with pytest.raises(installer["UnsafeGlobalAgentsState"], match=message):
            installer["rendered_template"](template, TEMPLATE, plugin_root)
