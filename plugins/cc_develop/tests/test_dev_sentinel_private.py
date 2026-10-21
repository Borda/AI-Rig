"""Tests for the ownership and private-directory guarantees of the develop ``bin/`` sentinel writers.

Sentinels sit at predictable names in a shared temp dir. A destination another user already owns must be refused rather
than replaced, a planted symlink must be replaced rather than followed, every directory a writer creates must be mode
0700, and the plan namespace read back from a sentinel must be the user's own directory under the temp dir before it is
used as a write target.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

import codemap_resolve
import dev_codemap_gate
import dev_run_dir
import pytest
from dev_parse_args import write_skill_files

_CSID = "sentinel-private-test"


def _probe_symlink() -> bool:
    """Return whether this process may create a symlink (capability probe, not a platform test)."""
    with tempfile.TemporaryDirectory() as tmp:
        try:
            Path(tmp, "link").symlink_to(Path(tmp, "target"))
        except (OSError, NotImplementedError):
            return False
        return True


_skip_no_symlink = pytest.mark.skipif(not _probe_symlink(), reason="symlinks unavailable to this process")
_skip_without_uids = pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX uids and mode bits unavailable")


@pytest.fixture(name="tmp_env")
def _tmp_env_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated ``$TMPDIR`` plus a fixed ``CSID`` so sentinel names are predictable."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CSID", _CSID)
    return tmp_path


def _report_foreign_owner(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    """Make ``path`` look owned by another user by reporting a different current uid.

    ``raising=False`` installs ``getuid`` on hosts without one, so the foreign-owner branch runs on every platform.
    """
    monkeypatch.setattr(os, "getuid", lambda: os.lstat(path).st_uid + 1, raising=False)


class TestWriteAtomicOwnership:
    """``write_atomic`` replaces only an entry the current user owns."""

    def test_own_existing_sentinel_is_replaced(self, tmp_path: Path) -> None:
        """A rerun overwrites the value this user wrote earlier, the normal path for every sentinel."""
        target = tmp_path / "value"
        target.write_text("old\n", encoding="utf-8")

        dev_run_dir.write_atomic(target, "new")

        assert target.read_bytes() == b"new\n"

    def test_foreign_owned_destination_is_refused_and_left_intact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A destination another user owns raises ``PermissionError`` and keeps its content.

        On a shared ``/tmp`` another user can create the predictable name first, easiest when ``CSID`` falls back to
        ``shared``. Refusing makes the failure visible instead of racing the sticky-bit rules.
        """
        target = tmp_path / "value"
        target.write_text("planted\n", encoding="utf-8")
        _report_foreign_owner(monkeypatch, target)

        with pytest.raises(PermissionError, match="owned by another user"):
            dev_run_dir.write_atomic(target, "payload")

        assert target.read_text(encoding="utf-8") == "planted\n"

    @_skip_no_symlink
    def test_foreign_owned_symlink_is_refused(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The owner check inspects the link itself, so another user's planted symlink is refused, not followed."""
        victim = tmp_path / "victim"
        victim.write_text("precious\n", encoding="utf-8")
        target = tmp_path / "value"
        target.symlink_to(victim)
        _report_foreign_owner(monkeypatch, target)

        with pytest.raises(PermissionError):
            dev_run_dir.write_atomic(target, "payload")

        assert victim.read_text(encoding="utf-8") == "precious\n"


class TestMakePrivateDirs:
    """``make_private_dirs`` is ``mkdir -p`` that makes every directory it creates private to the user."""

    @_skip_without_uids
    def test_created_directories_are_0700(self, tmp_path: Path) -> None:
        """Each newly created level gets mode 0700, parents included, unlike ``mkdir(parents=True)``."""
        leaf = tmp_path / "outer" / "inner"

        dev_run_dir.make_private_dirs(leaf)

        assert (stat.S_IMODE(leaf.parent.stat().st_mode), stat.S_IMODE(leaf.stat().st_mode)) == (0o700, 0o700)

    @_skip_without_uids
    def test_existing_directory_keeps_its_mode(self, tmp_path: Path) -> None:
        """A directory that already exists is not re-moded; the temp dir itself (``/tmp``) belongs to root."""
        shared = tmp_path / "shared"
        shared.mkdir()
        shared.chmod(0o755)

        dev_run_dir.make_private_dirs(shared)

        assert stat.S_IMODE(shared.stat().st_mode) == 0o755

    def test_file_in_the_way_raises(self, tmp_path: Path) -> None:
        """A regular file at the directory path raises instead of passing as a usable directory."""
        blocker = tmp_path / "blocker"
        blocker.write_text("", encoding="utf-8")

        with pytest.raises(NotADirectoryError):
            dev_run_dir.make_private_dirs(blocker)


class TestParseArgsSentinels:
    """``write_skill_files`` writes every flag sentinel through the link-safe, owner-checked writer."""

    @_skip_no_symlink
    def test_planted_symlink_is_replaced_not_followed(self, tmp_env: Path) -> None:
        """A symlink at a flag sentinel's name ends up a regular file, and the file it pointed at keeps its content."""
        victim = tmp_env / "victim"
        victim.write_text("precious\n", encoding="utf-8")
        sentinel = tmp_env / f"dev-feature-team-{_CSID}"
        sentinel.symlink_to(victim)

        write_skill_files("feature", "--team fix auth.py", tmp_dir=tmp_env)

        assert (victim.read_text(encoding="utf-8"), sentinel.is_symlink(), sentinel.read_text(encoding="utf-8")) == (
            "precious\n",
            False,
            "true\n",
        )

    @_skip_without_uids
    def test_missing_temp_dir_is_created_private(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A ``$TMPDIR`` that does not exist yet is created with mode 0700 before the sentinels go into it."""
        monkeypatch.setenv("CSID", _CSID)
        fresh = tmp_path / "fresh-tmp"

        write_skill_files("feature", "fix auth.py", tmp_dir=fresh)

        assert stat.S_IMODE(fresh.stat().st_mode) == 0o700


class TestPlanNamespaceOwnership:
    """The plan namespace becomes a write directory, so it must be this user's own real directory under the temp dir."""

    def _point_sentinel_at(self, tmp_env: Path, namespace: Path) -> None:
        """Record ``namespace`` in the ``dev-plan-ns-current`` sentinel, as plan/SKILL.md does."""
        (tmp_env / f"dev-plan-ns-current-{_CSID}").write_text(f"{namespace}\n", encoding="utf-8")

    def test_own_existing_namespace_is_accepted(self, tmp_env: Path) -> None:
        """The directory plan/SKILL.md created resolves to its codemap files, the normal path."""
        namespace = tmp_env / f"dev-plan-42-{_CSID}"
        namespace.mkdir()
        self._point_sentinel_at(tmp_env, namespace)

        assert dev_codemap_gate._resolve_paths("plan", tmp_env, _CSID) == (
            namespace / "codemap-raw",
            namespace / "codemap-enabled",
        )

    @_skip_no_symlink
    def test_symlinked_namespace_pointing_outside_is_rejected(
        self, tmp_env: Path, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        """A ``dev-plan-*`` name under the temp dir that is a symlink to a directory elsewhere aborts with exit 1.

        Checking only the parent directory would accept it, and the codemap files would land wherever the link points.
        """
        elsewhere = tmp_path_factory.mktemp("elsewhere")
        namespace = tmp_env / f"dev-plan-42-{_CSID}"
        namespace.symlink_to(elsewhere, target_is_directory=True)
        self._point_sentinel_at(tmp_env, namespace)

        with pytest.raises(SystemExit) as raised:
            dev_codemap_gate._resolve_paths("plan", tmp_env, _CSID)

        assert raised.value.code == 1

    def test_foreign_owned_namespace_is_rejected(self, tmp_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A namespace directory another user created aborts with exit 1 instead of receiving this user's files."""
        namespace = tmp_env / f"dev-plan-42-{_CSID}"
        namespace.mkdir()
        self._point_sentinel_at(tmp_env, namespace)
        _report_foreign_owner(monkeypatch, namespace)

        with pytest.raises(SystemExit) as raised:
            dev_codemap_gate._resolve_paths("plan", tmp_env, _CSID)

        assert raised.value.code == 1

    def test_regular_file_namespace_is_rejected(self, tmp_env: Path) -> None:
        """A regular file at the namespace path aborts with exit 1; only a directory can hold the codemap files."""
        namespace = tmp_env / f"dev-plan-42-{_CSID}"
        namespace.write_text("", encoding="utf-8")
        self._point_sentinel_at(tmp_env, namespace)

        with pytest.raises(SystemExit) as raised:
            dev_codemap_gate._resolve_paths("plan", tmp_env, _CSID)

        assert raised.value.code == 1


class TestCodemapCurrencyOwnership:
    """``codemap_resolve`` records currency only in a sentinel the current user owns; the status is advisory."""

    def test_own_currency_sentinel_is_replaced(self, tmp_env: Path) -> None:
        """A rerun overwrites the earlier status, the normal path."""
        sentinel = tmp_env / f"dev-codemap-currency-{_CSID}"
        sentinel.write_text("stale\n", encoding="utf-8")

        codemap_resolve._write_currency("dev-codemap-currency", "current")

        assert sentinel.read_text(encoding="utf-8") == "current\n"

    def test_foreign_owned_currency_sentinel_is_left_intact(
        self, tmp_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A currency sentinel another user owns is neither replaced nor turned into an error for the gate."""
        sentinel = tmp_env / f"dev-codemap-currency-{_CSID}"
        sentinel.write_text("planted\n", encoding="utf-8")
        _report_foreign_owner(monkeypatch, sentinel)

        codemap_resolve._write_currency("dev-codemap-currency", "current")

        assert sentinel.read_text(encoding="utf-8") == "planted\n"
