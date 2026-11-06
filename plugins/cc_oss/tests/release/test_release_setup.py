"""Tests for ``bin/release_setup.py``.

Git subprocess calls are replaced by a sequential fake that returns pre-configured ``(returncode, stdout)`` pairs in
call order: the stable-branch path makes 3 git calls and the fallback path 8. Two scenarios stay real because the real
tool is their contract: a detached ``HEAD`` in a scratch repository, and the SKILL.md setup block run through Bash.

Output is written below ``${TMPDIR}/release-setup-<CSID>``; an autouse fixture redirects ``TMPDIR`` to ``tmp_path``. The
``conftest.py`` autouse fixture strips ``CLAUDE_CODE_SESSION_ID`` and ``CSID``, so ``<CSID>`` resolves to the literal
``"shared"`` fallback here.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path, PureWindowsPath
from typing import Any
from unittest import mock

import pytest
import release_setup as rs
import resolve_shared_path as rsp

_PLUGIN = Path(__file__).resolve().parents[2]
_SKILL = _PLUGIN / "skills/release/SKILL.md"
_SETUP_ANCHOR = "Extracted to `bin/release_setup.py`"
_STABLE_KEYS = ("SKILL_DIR", "REPO_ROOT", "BRANCH", "BRANCH_REF", "BRANCH_KEY", "DATE", "LAST_TAG")
_ALL_KEYS = (*_STABLE_KEYS, "CHERRY_PICK_SUBJECTS", "SOURCE_TAG_REF", "SHIPPED_ELSEWHERE_TAGS")
#: Git answers for the fallback path: no first-parent tag, so the last tag comes from the merge-base ancestor.
_FALLBACK_GIT = (
    (0, "/repo"),  # rev-parse --show-toplevel
    (0, "main"),  # branch --show-current
    (1, ""),  # describe --first-parent -> no branch tag
    (0, "v0.9.0"),  # describe -> source tag
    (0, "abc123"),  # rev-list -n1 refs/tags/v0.9.0
    (0, "def456"),  # merge-base
    (0, "v0.8.0"),  # describe def456 -> last tag
    (0, "cp1\ncp2"),  # git log v0.8.0..v0.9.0
)
_skip_without_symlinks = pytest.mark.skipif(
    sys.platform == "win32", reason="symlink and POSIX ownership semantics; Windows has no equivalent sentinel exposure"
)


@pytest.fixture(autouse=True)
def _isolated_tmpdir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point ``TMPDIR`` at ``tmp_path`` so no test writes sentinels into the machine's real temp directory."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))


@pytest.fixture(autouse=True)
def _stub_plugin_dir_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the install-registry subprocess out of the faked git call sequence.

    The real resolver shells out to the registry helper, which would consume entries of the sequential git fake and
    answer from the developer's real install. Resolver tests below swap the real function back in.
    """
    monkeypatch.setattr(rs, "resolve_plugin_dir", lambda *_args, **_kwargs: ("plugins/cc_oss/skills/release", 3))


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    """Return the session-scoped directory ``release_setup`` publishes its key files into."""
    return tmp_path / "release-setup-shared"


@pytest.fixture
def git_sequence(monkeypatch: pytest.MonkeyPatch) -> Callable[..., list[list[str]]]:
    """Return a function that installs a sequential git fake and yields the recorded argv lists.

    Calls beyond the configured outcomes answer ``(0, "")`` so an extra git call surfaces as a wrong value, not a crash.
    """

    def install(*outcomes: tuple[int, str]) -> list[list[str]]:
        """Replace ``subprocess.run`` and ``which`` inside ``release_setup`` with the sequential fake."""
        recorded: list[list[str]] = []

        def fake_run(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            """Return the next configured outcome for the recorded git command."""
            idx = len(recorded)
            recorded.append(list(cmd))
            returncode, stdout = outcomes[idx] if idx < len(outcomes) else (0, "")
            return subprocess.CompletedProcess(cmd, returncode, stdout=stdout)

        monkeypatch.setattr(rs.subprocess, "run", fake_run)
        monkeypatch.setattr(rs, "which", lambda _: "/fake/git")
        return recorded

    return install


@pytest.fixture
def run_stable(git_sequence: Callable[..., list[list[str]]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Return a function running ``main`` on the 3-call stable path and yielding the output directory.

    ``branch`` and ``tag`` choose the faked git answers; ``root`` overrides the ``TMPDIR`` the sentinels land under.
    """

    def run(branch: str = "main", tag: str = "v1.0.0", root: Path = tmp_path) -> Path:
        """Run ``release_setup.main`` once and return ``release-setup-shared`` below ``root``."""
        monkeypatch.setenv("TMPDIR", str(root))
        git_sequence((0, "/repo"), (0, branch), (0, tag))
        assert rs.main([]) == 0
        return root / "release-setup-shared"

    return run


class TestGitBashPaths:
    """Native Windows Python and Git Bash must agree on where sentinel paths point."""

    @mock.patch("release_setup.subprocess.run", autospec=True)
    def test_temp_dir_converts_posix_tmpdir(self, mock_run: mock.Mock, monkeypatch: pytest.MonkeyPatch) -> None:
        """A Git Bash ``TMPDIR`` is converted to a native drive path through ``cygpath -w``.

        On Windows the shell hands native Python a POSIX path such as ``/c/Users/...``; writing there would land the
        sentinels where Git Bash never reads them.
        """
        monkeypatch.setenv("TMPDIR", "/c/Users/test/AppData/Local/Temp")
        monkeypatch.setattr(sys, "platform", "win32")
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="C:\\Users\\test\\AppData\\Local\\Temp\n")

        resolved = rs._native_temp_dir()

        assert str(resolved) == "C:\\Users\\test\\AppData\\Local\\Temp"
        mock_run.assert_called_once_with(
            ["cygpath", "-w", "/c/Users/test/AppData/Local/Temp"], capture_output=True, text=True, check=True, timeout=5
        )

    @mock.patch("release_setup.subprocess.run", autospec=True)
    def test_shell_path_converts_drive_path(self, mock_run: mock.Mock, monkeypatch: pytest.MonkeyPatch) -> None:
        """A native drive path is emitted in Git Bash syntax through ``cygpath -u``.

        The setup output is read back by Bash, so a path written by native Python must be readable there.
        """
        monkeypatch.setattr(sys, "platform", "win32")
        mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="/c/Users/test/plugin\n")

        converted = rs._shell_path("C:\\Users\\test\\plugin")

        assert converted == "/c/Users/test/plugin"
        mock_run.assert_called_once_with(
            ["cygpath", "-u", "C:\\Users\\test\\plugin"], capture_output=True, text=True, check=True, timeout=5
        )


class TestStableBranch:
    """The stable path (first-parent tag found) publishes one file per key below the session directory."""

    @pytest.mark.parametrize("key", _ALL_KEYS)
    def test_writes_each_key_file_with_trailing_newline(self, run_stable: Callable[..., Path], key: str) -> None:
        """Every key file exists and ends with a newline.

        Consumers read these files with ``IFS= read -r VAR < file || VAR=""``; a file with no trailing newline makes
        ``read`` return non-zero on EOF, which fires the ``||`` fallback and silently clobbers the just-read value back
        to empty.
        """
        out_dir = run_stable()

        assert (out_dir / key).read_text(encoding="utf-8").endswith("\n")

    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            pytest.param("LAST_TAG", "v2.3.1\n", id="last-tag-from-first-parent-describe"),
            pytest.param("SOURCE_TAG_REF", "\n", id="source-tag-empty"),
            pytest.param("BRANCH", "main\n", id="branch-name"),
            pytest.param("REPO_ROOT", "/repo\n", id="repo-root-from-toplevel"),
        ],
    )
    def test_key_file_content(self, run_stable: Callable[..., Path], key: str, expected: str) -> None:
        """The stable path publishes the first-parent tag as ``LAST_TAG`` and leaves ``SOURCE_TAG_REF`` empty.

        Git answers ``/repo``, ``main`` and ``v2.3.1`` for the three calls; each value must land in its own key file.
        """
        out_dir = run_stable(tag="v2.3.1")

        assert (out_dir / key).read_text(encoding="utf-8") == expected


class TestShippedElsewhereTags:
    """Stable tags cut off HEAD's ancestry are published so Gather can subtract what they already shipped."""

    @pytest.mark.parametrize(
        ("all_tags", "unmerged", "ancestor_codes", "expected"),
        [
            pytest.param(
                "1.11.2\n1.11.1\n1.11.0\n1.10.0",
                "1.11.2\n1.11.1",
                (0, 0),
                "1.11.2 1.11.1\n",
                id="maintenance-branch-tags-newest-first",
            ),
            pytest.param("1.11.1\n1.11.0", "1.11.1", (1,), "\n", id="tag-not-descending-from-baseline-skipped"),
            pytest.param("1.12.0rc1\n1.11.0", "1.12.0rc1", (), "\n", id="prerelease-tag-skipped"),
            pytest.param("1.11.1\n1.11.0", "", (), "\n", id="tag-reachable-from-head-skipped"),
            pytest.param("2.0.0\n1.10.0", "2.0.0", (), "\n", id="baseline-not-a-tag-yields-nothing"),
        ],
    )
    def test_key_file_lists_qualifying_tags(
        self,
        git_sequence: Callable[..., list[list[str]]],
        out_dir: Path,
        all_tags: str,
        unmerged: str,
        ancestor_codes: tuple[int, ...],
        expected: str,
    ) -> None:
        """Only unmerged, stable, baseline-descending tags newer than the first-parent tag are listed.

        The first-parent tag is 1.11.0 here; 1.11.1 and 1.11.2 live on a release branch HEAD never merged.
        """
        git_sequence(
            (0, "/repo"),
            (0, "main"),
            (0, "1.11.0"),
            (0, all_tags),
            (0, unmerged),
            *((code, "") for code in ancestor_codes),
        )

        assert rs.main([]) == 0

        assert (out_dir / "SHIPPED_ELSEWHERE_TAGS").read_text(encoding="utf-8") == expected

    def test_stable_branch_fallback_leaves_key_empty(
        self, git_sequence: Callable[..., list[list[str]]], out_dir: Path
    ) -> None:
        """Without a first-parent tag the cherry-pick path owns the baseline and this key stays empty."""
        git_sequence(*_FALLBACK_GIT)

        assert rs.main([]) == 0

        assert (out_dir / "SHIPPED_ELSEWHERE_TAGS").read_text(encoding="utf-8") == "\n"


class TestBranchIdentity:
    """Branch names become artifact filenames, so they must stay distinct and portable."""

    @pytest.mark.parametrize(
        ("git_branch", "slug"),
        [
            pytest.param("feature/my-thing", "feature-my-thing", id="slash-to-hyphen"),
            pytest.param("feature+append", "feature+append", id="plus-preserved"),
            pytest.param("feature%append", "feature%25append", id="percent-encoded"),
            pytest.param("feature%25append", "feature%2525append", id="encoded-looking-name-distinct"),
            pytest.param("feature<append", "feature%3Cappend", id="windows-reserved-encoded"),
        ],
    )
    def test_slug_is_portable_and_raw_ref_is_kept(
        self, run_stable: Callable[..., Path], git_branch: str, slug: str
    ) -> None:
        """Git branch punctuation yields a distinct, Windows-safe slug while ``BRANCH_REF`` keeps the raw name.

        Slashes become hyphens and characters Windows forbids in filenames are percent-encoded, so a branch such as
        ``feature<append`` can still name a state file.
        """
        out_dir = run_stable(branch=git_branch)
        filename = f"release-last-processed-{slug}"

        assert (
            (out_dir / "BRANCH").read_text(encoding="utf-8"),
            (out_dir / "BRANCH_REF").read_text(encoding="utf-8"),
            PureWindowsPath(filename).name == filename,
            any(char in filename for char in '<>:"/\\|?*'),
        ) == (slug + "\n", git_branch + "\n", True, False)

    @pytest.mark.parametrize("raw_branch", ["feature/append", "feature-append", "Feature", "feature"])
    def test_branch_key_binds_raw_ref_hash(self, run_stable: Callable[..., Path], raw_branch: str) -> None:
        """``BRANCH_KEY`` ends with the SHA-256 of the raw ref and is short enough to be a portable filename.

        Binding the hash of the unmodified ref keeps persistent state files distinct even when the readable slug prefix
        collides.
        """
        key = (run_stable(branch=raw_branch) / "BRANCH_KEY").read_text(encoding="utf-8").strip()

        assert (key.endswith(hashlib.sha256(raw_branch.encode("utf-8")).hexdigest()), len(key) < 100) == (True, True)
        assert PureWindowsPath(key).name == key

    @pytest.mark.parametrize(
        ("first", "second"),
        [
            pytest.param("feature/append", "feature-append", id="slash-versus-hyphen"),
            pytest.param("Feature", "feature", id="windows-case-only"),
        ],
    )
    def test_branch_key_distinguishes_legacy_collisions(
        self, run_stable: Callable[..., Path], tmp_path: Path, first: str, second: str
    ) -> None:
        """Persistent filenames bind the raw Git ref even when the legacy slugs of two branches collide.

        ``feature/append`` and ``feature-append`` share a slug, and ``Feature``/``feature`` collide on case-folding
        filesystems; the keys must still differ so one branch never reads the other's state.
        """
        first_key = (run_stable(branch=first, root=tmp_path / "a") / "BRANCH_KEY").read_text(encoding="utf-8")
        second_key = (run_stable(branch=second, root=tmp_path / "b") / "BRANCH_KEY").read_text(encoding="utf-8")

        assert first_key != second_key


class TestFallbackPath:
    """Without a first-parent tag the script falls back to the stable-branch source tag and common ancestor."""

    def test_publishes_ancestor_tag_and_source_tag(
        self, git_sequence: Callable[..., list[list[str]]], out_dir: Path
    ) -> None:
        """``LAST_TAG`` comes from the common ancestor and ``SOURCE_TAG_REF`` names the tag it fell back to.

        The first-parent describe fails, so the run resolves the source tag ``v0.9.0``, its merge-base and the last tag
        before that commit.
        """
        git_sequence(*_FALLBACK_GIT)

        rc = rs.main([])

        assert rc == 0
        assert (
            (out_dir / "LAST_TAG").read_text(encoding="utf-8"),
            (out_dir / "SOURCE_TAG_REF").read_text(encoding="utf-8"),
        ) == ("v0.8.0\n", "v0.9.0\n")

    def test_prints_stable_branch_banner(
        self, git_sequence: Callable[..., list[list[str]]], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The fallback path announces ``Stable-branch mode`` on stderr.

        The banner tells the operator the range was derived from a source tag instead of the branch's own history.
        """
        git_sequence(*_FALLBACK_GIT)

        rs.main([])

        assert "Stable-branch mode" in capsys.readouterr().err

    def test_without_any_tag_reports_none_found_and_exits_zero(
        self, git_sequence: Callable[..., list[list[str]]], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A repository with no stable tags warns ``No stable tags found`` on stderr and still exits 0.

        Every describe fails, so the range falls back to the initial commit; a first release must remain possible.
        """
        git_sequence(
            (0, "/repo"),  # rev-parse
            (0, "main"),  # branch
            (1, ""),  # describe --first-parent -> empty
            (1, ""),  # describe -> no source tag
            (0, "abc0123"),  # rev-list --max-parents=0 -> first commit
            (1, ""),  # rev-list -n1 refs/tags/abc0123 -> fail
            (0, "def456"),  # merge-base
            (1, ""),  # describe def456 -> no tag
            (0, ""),  # git log
        )

        rc = rs.main([])

        assert rc == 0
        assert "No stable tags found" in capsys.readouterr().err


class TestEntryPoint:
    """Argument handling and the failure exits of ``main``."""

    @mock.patch("release_setup.which", autospec=True, return_value=None)
    def test_missing_git_raises(self, mock_which: mock.Mock) -> None:
        """A host without ``git`` on PATH raises ``FileNotFoundError`` naming git.

        The lookup is the first step, so nothing is written and no half-resolved range is left behind.
        """
        with pytest.raises(FileNotFoundError, match="git"):
            rs.main([])

        mock_which.assert_called_once_with("git")

    @mock.patch("release_setup.which", autospec=True)
    def test_help_exits_zero_before_resolving_git(
        self, mock_which: mock.Mock, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--help`` prints the usage line and exits 0 without looking up git.

        Usage must work on a machine that has no git at all, like every other ``bin/`` script.
        """
        with pytest.raises(SystemExit) as exc:
            rs.main(["--help"])

        assert exc.value.code == 0
        assert "usage: release_setup.py" in capsys.readouterr().out
        mock_which.assert_not_called()


def _git(repo: Path, *args: str) -> None:
    """Run one real git command in ``repo`` with a fixed identity."""
    identity = ["-c", "user.name=Test", "-c", "user.email=test@example.com"]
    subprocess.run(["git", "-C", str(repo), *identity, *args], check=True, capture_output=True)


@pytest.fixture
def detached_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a real scratch repository whose ``HEAD`` is detached and make it the current directory."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "start")
    _git(repo, "checkout", "-q", "--detach", "HEAD")
    monkeypatch.chdir(repo)
    return repo


def _shared_setup_block() -> str:
    """Return the bash block SKILL.md documents as the consumer of ``bin/release_setup.py``."""
    lines = _SKILL.read_text(encoding="utf-8").splitlines(keepends=True)
    anchor = next(i for i, line in enumerate(lines) if _SETUP_ANCHOR in line)
    start = next(i for i in range(anchor, len(lines)) if lines[i].strip() == "```bash") + 1
    end = next(i for i in range(start, len(lines)) if lines[i].strip() == "```")
    return "".join(lines[start:end])


class TestDetachedHead:
    """A detached checkout has no branch identity, so setup must stop instead of inventing one (real git)."""

    def test_refuses_without_publishing_state(self, detached_repo: Path, out_dir: Path) -> None:
        """A detached checkout exits 1 and leaves earlier sentinels untouched.

        Publishing a synthetic ``main`` identity would let a later step append release notes to the wrong branch.
        """
        out_dir.mkdir()
        (out_dir / "BRANCH_REF").write_text("main\n", encoding="utf-8", newline="\n")

        rc = rs.main([])

        assert (rc, (out_dir / "BRANCH_REF").read_text(encoding="utf-8"), (out_dir / "BRANCH_KEY").exists()) == (
            1,
            "main\n",
            False,
        )

    def test_skill_setup_block_propagates_failure(
        self, detached_repo: Path, out_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The documented SKILL.md consumer exits 1 even when a previous run left valid-looking branch files.

        Stale ``BRANCH_KEY`` and ``REPO_ROOT`` sentinels in the session directory must not let the shell block carry on
        as if setup had succeeded.
        """
        out_dir.mkdir()
        (out_dir / "BRANCH_KEY").write_text("stale-key\n", encoding="utf-8", newline="\n")
        (out_dir / "REPO_ROOT").write_text(f"{detached_repo}\n", encoding="utf-8", newline="\n")
        (tmp_path / "release-mode-shared").write_text("notes\n", encoding="utf-8", newline="\n")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "shared")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(_PLUGIN))

        completed = subprocess.run(
            [shutil.which("bash"), "-c", _shared_setup_block()], cwd=detached_repo, capture_output=True, text=True
        )

        assert completed.returncode == 1
        assert "requires an attached Git branch" in completed.stderr


@pytest.fixture
def real_resolver(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Run the real cache resolver against a throwaway home with the registry tier disabled."""
    monkeypatch.setattr(rs, "resolve_plugin_dir", rsp.resolve)
    monkeypatch.setattr(rsp, "_locate_helper", lambda *_args: None)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.setattr(rs.Path, "home", classmethod(lambda _cls: tmp_path))
    return tmp_path / ".claude" / "plugins" / "cache" / "borda-ai-rig" / "oss"


def _install_versions(cache_root: Path, *versions: str, orphaned: tuple[str, ...] = ()) -> None:
    """Create ``<version>/skills/release`` for each version and mark the ``orphaned`` ones with ``.orphaned_at``."""
    for version in versions:
        (cache_root / version / "skills" / "release").mkdir(parents=True)
    for version in orphaned:
        (cache_root / version / ".orphaned_at").write_text("1\n", encoding="utf-8", newline="\n")


class TestSkillDirResolution:
    """``_resolve_skill_dir`` finds the installed skill directory the host loads, else the source tree."""

    @pytest.mark.parametrize(
        ("versions", "orphaned", "expected"),
        [
            pytest.param(("0.38.4",), (), "0.38.4", id="versioned-cache-layout"),
            pytest.param(("0.50.0", "0.100.0", "0.200.0"), ("0.200.0",), "0.100.0", id="highest-numeric-skips-orphan"),
        ],
    )
    def test_picks_installed_version(
        self, real_resolver: Path, versions: tuple[str, ...], orphaned: tuple[str, ...], expected: str
    ) -> None:
        """The newest numeric, non-orphaned install wins, and the version dir sits between plugin id and ``skills``.

        Real layout is ``~/.claude/plugins/cache/<marketplace>/oss/<version>/skills/release``. ``0.100.0`` outranks
        ``0.50.0`` numerically and an orphaned newer install is ignored: the directory is later read and executed as
        instructions, so a lexically sorted or orphaned install must never win over the version Claude Code loads.
        """
        _install_versions(real_resolver, *versions, orphaned=orphaned)

        resolved = rs._resolve_skill_dir()

        assert resolved == str(real_resolver / expected / "skills" / "release")

    def test_falls_back_to_source_tree_when_not_installed(self, real_resolver: Path) -> None:
        """With no installed ``oss/<version>/skills/release`` anywhere the source-tree path is returned."""
        resolved = rs._resolve_skill_dir()

        assert resolved == "plugins/cc_oss/skills/release"


@_skip_without_symlinks
class TestSymlinkSafety:
    """A predictable sentinel name pre-planted as a symlink must never redirect writes."""

    def test_prepare_out_dir_refuses_symlinked_directory(self, tmp_path: Path) -> None:
        """A sentinel directory pre-planted as a symlink is refused, not written through.

        Another user who pre-creates the predictable ``release-setup-<token>`` name as a symlink could otherwise
        redirect the sentinels, including the ``SKILL_DIR`` later read and executed.
        """
        target = tmp_path / "elsewhere"
        target.mkdir()
        planted = tmp_path / "release-setup-shared"
        planted.symlink_to(target, target_is_directory=True)

        with pytest.raises(PermissionError):
            rs._prepare_out_dir(planted)

    def test_write_private_replaces_planted_symlink(self, tmp_path: Path) -> None:
        """A pre-planted sentinel symlink is replaced by a private file; its target stays untouched.

        Following the link would overwrite whatever file the attacker pointed it at.
        """
        victim = tmp_path / "victim.txt"
        victim.write_text("keep\n", encoding="utf-8", newline="\n")
        sentinel = tmp_path / "SKILL_DIR"
        sentinel.symlink_to(victim)

        rs._write_private(sentinel, "fresh\n")

        assert (victim.read_text(encoding="utf-8"), sentinel.is_symlink(), sentinel.read_text(encoding="utf-8")) == (
            "keep\n",
            False,
            "fresh\n",
        )

    def test_main_publishes_nothing_for_symlinked_directory(
        self, git_sequence: Callable[..., list[list[str]]], tmp_path: Path
    ) -> None:
        """``main`` exits 1 and publishes nothing when the sentinel directory is a planted symlink.

        The refusal must surface through the entry point, and the symlink target must stay empty.
        """
        target = tmp_path / "elsewhere"
        target.mkdir()
        (tmp_path / "release-setup-shared").symlink_to(target, target_is_directory=True)
        git_sequence((0, "/repo"), (0, "main"), (0, "v1.0.0"))

        rc = rs.main([])

        assert (rc, list(target.iterdir())) == (1, [])
